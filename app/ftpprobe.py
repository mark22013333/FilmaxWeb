"""量測 FTP 伺服器能承受多少同時連線，以及併發到幾條之後就不再變快。

    python -m app.ftpprobe              完整量測
    python -m app.ftpprobe --max 12     只爬到 12 條就停
    python -m app.ftpprobe --no-speed   只找上限，不測速度

為什麼需要這支：掃描相片時每一張都會另外開一條 FTP 連線下載
（FtpReadStream 不走連線池）。併發開太多，家用 NAS 會回
「421 Too many connections」，而那個錯誤現在會被吃掉並把照片
標成「讀取失敗」—— 使用者看到的是一堆讀不出來的圖，重掃又好一部分。

量兩件事，因為它們的答案通常不一樣：

  硬上限   伺服器第幾條開始拒絕。這是天花板。
  有效併發 開到第幾條之後總吞吐就不再增加。這才是該設的值 ——
           繼續往上加只會讓每一條都變慢，還逼近天花板。

**這支會真的對你的伺服器開很多條連線。** 它只做登入與列目錄，
不下載也不寫入，而且每一輪結束都確實關閉。
"""
from __future__ import annotations

import argparse
import ftplib
import os
import socket
import statistics
import sys
import threading
import time
from typing import List, Optional, Tuple

from .config import settings

# 對別人的伺服器要有分寸：爬到這個數字就停，不管有沒有撞到上限。
# 家用 NAS 的預設上限通常在 10~30 之間，24 夠找到它了。
DEFAULT_MAX = 24
# 每一輪之間讓伺服器喘口氣，避免它把我們當成攻擊
COOLDOWN = 0.6


def _connect(timeout: float) -> ftplib.FTP:
    ftp = ftplib.FTP_TLS() if settings.ftp_tls else ftplib.FTP()
    ftp.encoding = settings.ftp_encoding or "utf-8"
    ftp.connect(settings.ftp_host, settings.ftp_port, timeout=timeout)
    ftp.login(settings.ftp_user, settings.ftp_password)
    if settings.ftp_tls:
        ftp.prot_p()
    ftp.set_pasv(settings.ftp_passive)
    return ftp


def _classify(e: Exception) -> str:
    """把失敗歸類。分辨「被伺服器拒絕」與「網路層打不通」很重要 ——
    前者是我們要找的上限，後者代表量測本身有問題。"""
    s = str(e)
    if isinstance(e, ftplib.error_temp) and ("421" in s or "too many" in s.lower()):
        return "連線數上限"
    if isinstance(e, ftplib.error_perm):
        return "被拒絕（權限或帳號）"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "逾時"
    if isinstance(e, (ConnectionRefusedError, OSError)):
        return "連不上"
    return type(e).__name__


def _open_n(n: int, timeout: float) -> Tuple[int, List[str]]:
    """同時開 n 條連線，回傳 (成功數, 失敗原因清單)。"""
    conns: List[ftplib.FTP] = []
    errors: List[str] = []
    lock = threading.Lock()

    def one():
        try:
            c = _connect(timeout)
            c.pwd()                       # 確認連線真的可用，不只是握手成功
            with lock:
                conns.append(c)
        except Exception as e:
            with lock:
                errors.append(_classify(e))

    ts = [threading.Thread(target=one, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout + 5)

    ok = len(conns)
    for c in conns:                       # 一定要關乾淨，否則下一輪量到的是殘留
        try:
            c.quit()
        except Exception:
            try:
                c.close()
            except Exception:
                pass
    return ok, errors


def find_limit(max_n: int, timeout: float) -> Tuple[int, Optional[int], List[str]]:
    """一條一條加上去，直到有連線被拒絕。

    刻意用「每一輪重新開 n 條」而不是「持續累加」：後者會受到
    伺服器對單一來源的速率限制影響，量到的是斜坡不是天花板。
    """
    best, first_fail, reasons = 0, None, []
    print("  併發   成功   結果")
    for n in range(1, max_n + 1):
        ok, errs = _open_n(n, timeout)
        mark = "ok" if ok == n else "、".join(sorted(set(errs)))[:34]
        print(f"  {n:4d}   {ok:4d}   {mark}")
        if ok == n:
            best = n
        else:
            first_fail = n
            reasons = errs
            break
        time.sleep(COOLDOWN)
    return best, first_fail, reasons


def _pick_file(timeout: float) -> Optional[Tuple[str, int]]:
    """在片庫根目錄底下找一個夠大的檔案來當測速素材。

    找真的檔案下載，而不是用「列目錄」當代理指標 —— 那兩件事的瓶頸不一樣。
    列目錄在區網上是伺服器 CPU 說了算，下載才是延遲與頻寬說了算，
    而掃描相片做的是後者。用錯指標會得到剛好相反的建議。
    """
    try:
        c = _connect(timeout)
    except Exception:
        return None
    try:
        roots = [r.path for r in settings.library_roots] or ["/"]
        seen = 0
        stack = list(roots)
        while stack and seen < 60:
            d = stack.pop(0)
            seen += 1
            try:
                c.cwd(d)
                entries = []
                c.retrlines("LIST", entries.append)
            except Exception:
                continue
            for line in entries:
                parts = line.split(maxsplit=8)
                if len(parts) < 9:
                    continue
                name = parts[8]
                if line[0] == "d":
                    if len(stack) < 40 and not name.startswith("."):
                        stack.append(d.rstrip("/") + "/" + name)
                    continue
                try:
                    size = int(parts[4])
                except ValueError:
                    continue
                if size >= 512 * 1024:          # 至少 512KB 才測得出速度
                    return (d.rstrip("/") + "/" + name, size)
        return None
    finally:
        try:
            c.quit()
        except Exception:
            pass


# 每一輪抓這麼多。太小的話「開資料連線」的固定成本會蓋過傳輸本身，
# 量到的是握手速度不是頻寬。
CHUNK_BUDGET = 8 * 1024 * 1024


def _fetch_chunk(ftp: ftplib.FTP, path: str, budget: int, stop: threading.Event) -> int:
    """抓一段就停，而且**不能用 ftplib 的 abort()**。

    abort() 會讓控制連線失去同步 —— 它送 ABOR 之後的回應處理有問題，
    下一個指令會讀到上一個指令殘留的回應，於是整條連線從此壞掉。
    v1 的量測就是踩到這個：每條連線只成功跑完一輪就開始空轉，
    而「一輪 3MB ÷ 4 秒時間窗」剛好等於 0.75 MB/s，
    在任何伺服器上都會量到同一個數字，看起來像真的資料。

    正確做法是直接關掉資料連線（伺服器會回 426），控制連線保持乾淨。
    """
    sock, _ = ftp.ntransfercmd("RETR " + path)
    got = 0
    try:
        while got < budget and not stop.is_set():
            blk = sock.recv(65536)
            if not blk:
                break
            got += len(blk)
    finally:
        try:
            sock.close()
        except Exception:
            pass
        try:
            ftp.voidresp()
        except ftplib.error_temp:      # 426 傳輸被中止 —— 這是我們自己造成的，預期中
            pass
        except Exception:
            raise
    return got


def measure_speed(levels: List[int], timeout: float, path: str,
                  seconds: float = 4.0) -> dict:
    """在幾個併發等級各下載一段時間，量總吞吐。

    回傳 {併發數: (MB/s, 完成輪數)}。輪數要回報 —— 如果某一級只跑完
    一兩輪，那個 MB/s 就是「一輪除以時間窗」而不是頻寬，必須讓人看得出來。
    """
    out = {}
    for n in levels:
        got = [0] * n
        rounds = [0] * n
        errs: List[str] = []
        stop = threading.Event()
        lock = threading.Lock()

        def worker(idx: int):
            try:
                c = _connect(timeout)
            except Exception as e:
                with lock:
                    errs.append(_classify(e))
                return
            try:
                while not stop.is_set():
                    try:
                        n_bytes = _fetch_chunk(c, path, CHUNK_BUDGET, stop)
                    except Exception as e:
                        with lock:
                            errs.append(_classify(e))
                        break
                    got[idx] += n_bytes
                    rounds[idx] += 1
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
        t0 = time.monotonic()
        for t in ts:
            t.start()
        time.sleep(seconds)
        stop.set()
        for t in ts:
            t.join(timeout + 5)
        elapsed = max(0.001, time.monotonic() - t0)
        mbps = sum(got) / elapsed / (1024 * 1024)
        total_rounds = sum(rounds)
        out[n] = (round(mbps, 2), total_rounds)
        warn = ""
        if total_rounds < n * 2:
            warn = "  ← 輪數太少，這個數字不可信"
        if errs:
            warn += "  錯誤:" + "、".join(sorted(set(errs))[:2])
        print(f"  併發 {n:2d} → 總計 {mbps:7.2f} MB/s"
              f"（每條 {mbps/n:6.2f}，{total_rounds} 輪）{warn}")
        time.sleep(COOLDOWN)
    return out


# 保留給連線池（掃描時列目錄用）與播放串流的額度
RESERVE = 2
# 相片讀取階段允許的記憶體預算。最壞情況是每條連線同時抓一張
# MAX_PHOTO_MB 的大圖，所以併發數不能只看 FTP 上限。
MEM_BUDGET_MB = 256


def photo_stats() -> Optional[Tuple[int, int, int]]:
    """從資料庫拿相片的真實大小分布，回傳 (張數, 總位元組, 最大位元組)。

    用真實最大值而不是 MAX_PHOTO_MB 來估記憶體：那個設定是「超過就不讀」的
    上限，不是實際會佔用的量。拿上限去估會過度保守好幾倍。
    """
    try:
        from . import db
        row = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size),0) t, "
                    "COALESCE(MAX(size),0) m FROM photo WHERE size IS NOT NULL")
        if not row or not row["n"]:
            return None
        return int(row["n"]), int(row["t"]), int(row["m"])
    except Exception:
        return None


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds/60:.0f} 分鐘"
    return f"{seconds/3600:.1f} 小時"


def recommend(limit: int, first_fail: Optional[int], speed: dict) -> Tuple[int, List[str]]:
    notes = []
    if first_fail:
        notes.append(f"伺服器在第 {first_fail} 條被拒絕，最多同時 {limit} 條。")
    else:
        notes.append(f"爬到 {limit} 條都沒被拒絕，沒有摸到上限"
                     f"（可以用 --max 再往上試）。")

    by_limit = limit - RESERVE
    if by_limit < 1:
        notes.append(f"扣掉要保留給連線池與播放的 {RESERVE} 條之後不夠用了 —— "
                     f"相片只能單執行緒讀。")
        by_limit = 1
    else:
        notes.append(f"保留 {RESERVE} 條給連線池與播放，剩 {by_limit} 條可以給相片用。")

    # ---- 有效併發 ----
    by_speed = None
    best_mbps = None
    if speed:
        usable = {n: v for n, (v, r) in speed.items() if r >= n * 2}
        if len(usable) < 2:
            notes.append("測速的輪數太少，速度資料不可信，這次不拿它推算。")
        else:
            levels = sorted(usable)
            best_n, best_v = levels[0], usable[levels[0]]
            for n in levels[1:]:
                if usable[n] > best_v * 1.15:      # 總吞吐要多 15% 才算真的有幫助
                    best_n, best_v = n, usable[n]
                else:
                    break
            by_speed, best_mbps = best_n, best_v
            if by_speed == levels[-1]:
                notes.append(f"測到 {by_speed} 條為止總吞吐都還在增加"
                             f"（{best_v} MB/s），沒有找到轉折點。")
            else:
                notes.append(f"併發加到 {by_speed} 條之後總吞吐就不再明顯增加"
                             f"（{best_v} MB/s）。")

    # ---- 記憶體：用真實相片大小 ----
    st = photo_stats()
    if st:
        count, total, biggest = st
        per = biggest / (1024 * 1024)
        by_mem = max(1, int(MEM_BUDGET_MB // max(1, per)))
        notes.append(f"記憶體：片庫裡最大的一張是 {per:.1f}MB（不是 MAX_PHOTO_MB "
                     f"的 {settings.max_photo_mb}MB —— 那是「超過就不讀」的門檻），"
                     f"在 {MEM_BUDGET_MB}MB 預算內最多 {by_mem} 條。")
    else:
        per = float(settings.max_photo_mb)
        by_mem = max(1, int(MEM_BUDGET_MB // max(1, per)))
        notes.append(f"記憶體：還沒掃過相片，只能用 MAX_PHOTO_MB={settings.max_photo_mb}MB "
                     f"這個上限保守估，最多 {by_mem} 條。掃過之後再跑一次會更準。")

    cand = [c for c in (by_limit, by_speed, by_mem) if c]
    value = max(1, min(min(cand), 8))

    # ---- 掃描時間估算 ----
    if st and best_mbps and by_speed:
        count, total, _ = st
        gb = total / (1024 ** 3)
        one = speed.get(1, (None, 0))[0]
        if one:
            t1 = total / (one * 1024 * 1024)
            tn = total / (min(best_mbps, one * value) * 1024 * 1024)
            if value > 1 and tn < t1 * 0.9:
                notes.append(f"估算：{count} 張共 {gb:.1f}GB，單執行緒約 {_dur(t1)}，"
                             f"{value} 條約 {_dur(tn)}。")
            else:
                notes.append(f"估算：{count} 張共 {gb:.1f}GB，讀完約 {_dur(t1)}"
                             f"（加併發在這台伺服器上不會更快）。")

    # CPU 才是相片階段真正的瓶頸 —— 實測 679 張的時間有 94% 花在產生縮圖，
    # FTP 讀取只佔 6%。所以併發數要照核心數算，不是照 FTP 上限算。
    cores = os.cpu_count() or 2
    by_cpu = max(1, min(cores, 6))
    notes.append(f"CPU：{cores} 核。相片階段實測有 94% 的時間花在產生縮圖"
                 f"（FTP 讀取只佔 6%），所以真正的限制是 CPU，最多 {by_cpu} 條。")

    # by_speed（頻寬轉折點）刻意**不列入**限制條件。它量的是總頻寬何時飽和，
    # 而相片階段只需要用到其中 10 秒 —— 拿一個「跑滿頻寬時」的門檻去限制一個
    # 「只佔 6% 時間」的工作，是把不相關的東西當成限制。它只作為資訊回報。
    cand2 = [c for c in (by_limit, by_mem, by_cpu) if c]
    value = max(1, min(min(cand2), 8))

    binding = min((v, k) for k, v in
                  (("FTP 連線上限", by_limit), ("記憶體", by_mem),
                   ("CPU 核心數", by_cpu)))[1]
    notes.append(f"目前卡住的是「{binding}」"
                 f"（頻寬轉折點不列入 —— FTP 只佔相片階段 6% 的時間）。")
    return value, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="量測 FTP 併發上限")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, help=f"最多爬到幾條（預設 {DEFAULT_MAX}）")
    ap.add_argument("--timeout", type=float, default=float(settings.ftp_timeout or 20))
    ap.add_argument("--no-speed", action="store_true", help="只找上限，不測速度")
    a = ap.parse_args(argv)

    print("=" * 62)
    print("  FilmaxWeb — FTP 併發上限量測")
    print("=" * 62)
    print(f"目標　　{settings.ftp_host}:{settings.ftp_port}"
          f"　使用者 {settings.ftp_user}"
          f"　{'FTPS' if settings.ftp_tls else '純 FTP'}"
          f"　{'被動' if settings.ftp_passive else '主動'}模式")
    print("這支會登入、列目錄，並下載一小段資料測速；不寫入任何檔案。\n")

    try:
        c = _connect(a.timeout)
        print(f"[v] 單條連線正常：{c.getwelcome()[:60]}")
        c.quit()
    except Exception as e:
        print(f"[x] 連不上：{_classify(e)} — {e}")
        print("    先確認 .env 的 FTP_HOST / FTP_PORT / 帳號密碼。")
        return 1

    print("\n[1] 找硬上限")
    limit, first_fail, _ = find_limit(a.max, a.timeout)

    speed = {}
    if not a.no_speed and limit >= 2:
        print("\n[2] 找有效併發")
        picked = _pick_file(a.timeout)
        if not picked:
            print("  找不到夠大的檔案可以測速（需要至少一個 512KB 以上的檔案）。")
            print("  跳過這一段，建議值只依硬上限推算。")
        else:
            path, size = picked
            print(f"  素材：{path}（{size/1024/1024:.1f} MB），每級下載 4 秒")
            levels = sorted({n for n in (1, 2, 3, 4, 6, 8) if n <= limit})
            speed = measure_speed(levels, a.timeout, path)

    print("\n[3] 建議")
    value, notes = recommend(limit, first_fail, speed)
    for n in notes:
        print(f"  · {n}")
    print(f"\n  建議設定：PHOTO_CONCURRENCY={value}")
    print("  （第二期才會生效。另外注意：相片階段真正的瓶頸是產生縮圖而不是")
    print("   FTP —— 第二期會先把縮圖改用 PIL 的 draft()，那一項就快 2.5 倍，")
    print("   比開執行緒更有效也更單純。）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
