#!/usr/bin/env python3
"""FilmaxWeb 端對端測試

對「正在執行中的服務」跑一遍完整鏈路，逐層確認問題出在哪一環：

    環境(ffmpeg/ffprobe) → 服務 → FTP → 掃描索引 → 格式分析
      → Range 直接串流 → HLS 分段轉碼 → 字幕

用法（在專案目錄下，服務要先啟動）：

    .venv\\Scripts\\python e2e_test.py                # 自動挑樣本檔測
    .venv\\Scripts\\python e2e_test.py --file-id 42   # 只測某個檔案
    .venv\\Scripts\\python e2e_test.py --samples 5    # 多測幾個
    .venv\\Scripts\\python e2e_test.py --skip-transcode  # 不測轉碼（很快）

只用標準函式庫，不需要額外套件。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 輸出工具
IS_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if IS_TTY else text


GREEN, RED, YELL, DIM, BOLD = "32", "31", "33", "90", "1"

results: List[Tuple[str, str, str]] = []   # (狀態, 名稱, 說明)
ENV: Dict[str, bool] = {"ffprobe": True, "ftp": True}


def step(name: str) -> None:
    print(f"\n{_c(BOLD, '▶ ' + name)}")


def ok(name: str, detail: str = "") -> None:
    results.append(("PASS", name, detail))
    print(f"  {_c(GREEN, 'PASS')}  {name}" + (f"  {_c(DIM, detail)}" if detail else ""))


def fail(name: str, detail: str = "", hint: str = "") -> None:
    results.append(("FAIL", name, detail))
    print(f"  {_c(RED, 'FAIL')}  {name}")
    if detail:
        for line in str(detail).splitlines()[:6]:
            print(f"        {line}")
    if hint:
        print(f"        {_c(YELL, '→ ' + hint)}")


def warn(name: str, detail: str = "") -> None:
    results.append(("WARN", name, detail))
    print(f"  {_c(YELL, 'WARN')}  {name}" + (f"  {_c(DIM, detail)}" if detail else ""))


def info(text: str) -> None:
    print(f"        {_c(DIM, text)}")


# ---------------------------------------------------------------- HTTP
BASE = "http://localhost:8080"
_jar = CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def cookie_header() -> str:
    """把登入後拿到的 cookie 組成一行，好餵給 ffprobe 的 -headers。"""
    return "; ".join(f"{c.name}={c.value}" for c in _jar)


def login(password: str) -> bool:
    """服務有開 AUTH_ENABLED 時，先登入拿 cookie。"""
    data = urllib.parse.urlencode({"password": password}).encode()
    r = urllib.request.Request(BASE + "/login", data=data, method="POST")
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status < 400
    except urllib.error.HTTPError:
        return False


def req(path: str, method: str = "GET", headers: Optional[Dict[str, str]] = None,
        timeout: int = 60, read: bool = True) -> Tuple[int, Dict[str, str], bytes]:
    url = path if path.startswith("http") else BASE + path
    r = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with _opener.open(r, timeout=timeout) as resp:
            body = resp.read() if read else b""
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()[:2000]


def jget(path: str, method: str = "GET", timeout: int = 120) -> Any:
    code, _, body = req(path, method=method, timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"HTTP {code}: {body.decode('utf-8', 'ignore')[:300]}")
    return json.loads(body.decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 測試步驟
FFPROBE = "ffprobe"


def probe_local(path_or_url: str, extra: Optional[List[str]] = None) -> Optional[dict]:
    head: List[str] = []
    # 服務有開登入驗證時，ffprobe 也要帶著 session cookie 才讀得到 /api/stream
    if path_or_url.startswith("http"):
        ck = cookie_header()
        if ck:
            head = ["-headers", f"Cookie: {ck}\r\n"]
    cmd = [FFPROBE, "-v", "error"] + head + ["-print_format", "json",
           "-show_format", "-show_streams"] + (extra or []) + [path_or_url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout.decode("utf-8", "ignore"))
    except Exception:
        return None


def t_service() -> bool:
    step("1. 服務是否活著")
    try:
        code, _, body = req("/healthz", timeout=10)
    except Exception as e:
        fail("連線到服務", str(e), f"服務沒啟動？先跑「啟動.bat」，或用 --base 指定正確位址（目前 {BASE}）")
        return False
    if code == 200:
        ok("連線到服務", BASE)
        return True
    fail("連線到服務", f"HTTP {code}")
    return False


def t_env() -> bool:
    global FFPROBE
    step("2. 外部相依 (ffmpeg / ffprobe / FTP / TMDB)")
    try:
        d = jget("/api/diagnostics")
    except Exception as e:
        fail("讀取診斷資訊", str(e))
        return False

    healthy = True
    for kind in ("ffmpeg", "ffprobe"):
        t = d["tools"].get(kind, {})
        if t.get("ok"):
            ok(kind, f"{t['resolved']}  ({t.get('version') or ''})")
            if kind == "ffprobe":
                FFPROBE = t["resolved"]
        elif t.get("fallback"):
            warn(kind, "找不到，改用解析 ffmpeg 輸出的備援方式（可正常播放，但建議裝完整的 ffmpeg）")
        else:
            healthy = False
            ENV[kind] = False
            fail(kind, t.get("error") or "無法執行", t.get("hint", ""))

    hw = d.get("hwaccel")
    (ok if hw and hw != "none" else warn)(
        "硬體轉碼", f"使用 {hw.upper()}" if hw and hw != "none" else "沒吃到硬體加速，會用 CPU 轉碼（比較慢）")

    if d["ftp"].get("ok"):
        ok("FTP 連線", d["ftp"].get("welcome", "")[:60])
    else:
        healthy = False
        ENV["ftp"] = False
        fail("FTP 連線", d["ftp"].get("error", ""), "檢查 .env 的 FTP_HOST / PORT / USER / PASSWORD")

    (ok if d.get("tmdb_enabled") else warn)(
        "TMDB", "已啟用" if d.get("tmdb_enabled") else "未設定金鑰，不會有海報與簡介（不影響播放）")

    if d.get("probe_failed"):
        warn("既有分析失敗的檔案", f"{d['probe_failed']} 個 · 範例錯誤：{(d.get('probe_error_sample') or '')[:120]}")
    return healthy


def t_library() -> List[dict]:
    step("3. 媒體庫索引")
    try:
        stats = jget("/api/stats")
        lib = jget("/api/library?page_size=200")
    except Exception as e:
        fail("讀取媒體庫", str(e))
        return []
    if not lib["total"]:
        fail("媒體庫有內容", "0 個條目", "還沒掃描過？在網頁按「掃描媒體庫」，或確認 .env 的 LIBRARY_ROOTS 路徑正確")
        return []
    ok("媒體庫有內容",
       f"{stats['movies']} 部電影 · {stats['shows']} 部影集 · {stats['files']} 個檔案 · "
       f"{stats['total_size'] / 1024 ** 4:.2f} TB")
    if stats.get("unprobed"):
        warn("尚未分析的檔案", f"{stats['unprobed']} / {stats['files']} 個（沒分析過就不能播）")
    return lib["items"]


def collect_files(items: List[dict], want: int) -> List[dict]:
    """從條目挑出實際的檔案，盡量涵蓋不同副檔名。"""
    out: List[dict] = []
    seen_ext = set()
    for it in items:
        try:
            det = jget(f"/api/items/{it['id']}")
        except Exception:
            continue
        files = list(det.get("files") or [])
        for season in det.get("seasons") or []:
            for ep in season.get("episodes") or []:
                files += ep.get("files") or []
        for f in files:
            ext = (f.get("ext") or "").lower()
            # 優先挑沒看過的副檔名，讓 direct / hls 兩條路都被覆蓋到
            if ext not in seen_ext:
                seen_ext.add(ext)
                out.append(f)
            elif len(out) < want:
                out.append(f)
            if len(out) >= want:
                return out
    return out


def t_probe(f: dict) -> Optional[dict]:
    fid = f["id"]
    try:
        r = jget(f"/api/probe/{fid}", method="POST", timeout=300)
    except Exception as e:
        fail("格式分析 (ffprobe)", str(e))
        return None
    if r.get("probe_state") != "ok":
        if not ENV["ftp"]:
            hint = "FTP 連不上（見上面），檔案根本讀不到 — 先修 FTP"
        elif not ENV["ffprobe"]:
            hint = "ffprobe 找不到（見上面）— 先把 ffmpeg 裝完整"
        else:
            hint = "FTP 上讀得到這個檔案嗎？看服務主控台的詳細錯誤"
        fail("格式分析 (ffprobe)", r.get("probe_error", ""), hint)
        return None
    try:
        play = jget(f"/api/play/{fid}")
    except Exception as e:
        fail("取得播放資訊", str(e))
        return None
    ok("格式分析 (ffprobe)",
       f"{play.get('video_codec')}/{play.get('audio_codec')} · "
       f"{play.get('width')}x{play.get('height')} · {(play.get('duration') or 0) / 60:.1f} 分 · 模式 {play['mode']}")
    return play


def t_range(f: dict, play: dict) -> bool:
    fid = f["id"]
    size = f.get("size") or play.get("size") or 0
    if not size:
        warn("Range 串流", "不知道檔案大小，跳過")
        return True
    good = True

    # 開頭 64KB
    n = 65536
    code, h, body = req(f"/api/stream/{fid}", headers={"Range": f"bytes=0-{n - 1}"}, timeout=120)
    if code != 206:
        fail("Range 串流 (開頭)", f"預期 206，得到 {code}", "FTP 讀取失敗，看服務主控台的錯誤訊息")
        good = False
    elif len(body) != n:
        fail("Range 串流 (開頭)", f"預期 {n} bytes，實際 {len(body)}")
        good = False
    elif h.get("content-range") != f"bytes 0-{n - 1}/{size}":
        fail("Range 串流 (開頭)", f"Content-Range 不對: {h.get('content-range')}")
        good = False
    else:
        ok("Range 串流 (開頭)", f"206 · {n} bytes · {h.get('content-range')}")

    # 檔案中段 —— 這一段才是拖曳進度條真正會用到的路徑
    mid = size // 2
    t0 = time.time()
    code, h, body = req(f"/api/stream/{fid}", headers={"Range": f"bytes={mid}-{mid + n - 1}"}, timeout=120)
    dt = time.time() - t0
    if code != 206 or len(body) != n:
        fail("Range 串流 (中段 seek)", f"HTTP {code}, {len(body)} bytes",
             "FTP 伺服器可能不支援 REST 指令（斷點續傳），這樣就無法拖曳進度")
        good = False
    else:
        ok("Range 串流 (中段 seek)", f"206 · offset {mid} · {dt:.2f}s")
    return good


def t_direct(f: dict) -> bool:
    """direct 模式：ffprobe 直接讀 HTTP 串流，能解析就代表瀏覽器也吃得下。"""
    url = f"{BASE}/api/stream/{f['id']}"
    d = probe_local(url)
    if not d:
        fail("直接串流可解析", "ffprobe 讀不到這個 HTTP 串流")
        return False
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    ok("直接串流可解析", f"{v.get('codec_name')} {v.get('width')}x{v.get('height')}")
    return True


def t_hls(f: dict, play: dict, deep: bool) -> bool:
    fid = f["id"]
    good = True
    code, _, body = req(f"/api/hls/{fid}/index.m3u8", timeout=60)
    if code != 200:
        fail("HLS 播放清單", f"HTTP {code}: {body.decode('utf-8', 'ignore')[:200]}")
        return False
    text = body.decode("utf-8", "ignore")
    segs = [l for l in text.splitlines() if l.startswith("seg-")]
    if not segs:
        fail("HLS 播放清單", "清單裡沒有分段")
        return False
    extinf = [float(l[8:].rstrip(",")) for l in text.splitlines()
              if l.startswith("#EXTINF:") and l[8:].rstrip(",").replace(".", "", 1).isdigit()]
    seg_len = max(extinf) if extinf else 0
    ok("HLS 播放清單", f"{len(segs)} 段 · 每段 {seg_len:.0f}s · 總長 {sum(extinf) / 60:.1f} 分")

    if not deep:
        return good

    prof = urllib.parse.parse_qs(urllib.parse.urlparse(segs[0]).query).get("p", [""])[0]

    def fetch_seg(idx: int, label: str) -> bool:
        nonlocal good
        t0 = time.time()
        code, _, data = req(f"/api/hls/{fid}/seg-{idx}.ts?p={prof}", timeout=600)
        dt = time.time() - t0
        if code != 200 or not data:
            fail(f"HLS 分段 {label}", f"HTTP {code}: {data.decode('utf-8', 'ignore')[:300]}",
                 "看服務主控台的 ffmpeg 錯誤訊息")
            good = False
            return False
        import tempfile, os as _os
        fd, tmp = tempfile.mkstemp(suffix=".ts")
        _os.close(fd)
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            d = probe_local(tmp)
            if not d:
                fail(f"HLS 分段 {label}", "產出的分段 ffprobe 解不開（轉碼參數有問題）")
                good = False
                return False
            v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
            a = next((s for s in d["streams"] if s["codec_type"] == "audio"), {})
            dur = float(d["format"].get("duration") or 0)
            start = float(d["format"].get("start_time") or 0)
            codec_ok = v.get("codec_name") == "h264" and a.get("codec_name") == "aac"
            speed = dur / dt if dt else 0
            detail = (f"{len(data) / 1024 / 1024:.1f} MB · {dur:.1f}s 影片 / {dt:.1f}s 轉碼 "
                      f"({speed:.1f}x 即時) · 起始時間 {start:.1f}s · {v.get('codec_name')}/{a.get('codec_name')}")
            if not codec_ok:
                fail(f"HLS 分段 {label}", "編碼不是 h264/aac，瀏覽器可能播不出來 — " + detail)
                good = False
                return False
            ok(f"HLS 分段 {label}", detail)
            if speed < 1.0:
                warn(f"HLS 分段 {label} 轉碼速度",
                     f"只有 {speed:.1f}x 即時速度，播放會卡。建議開硬體轉碼或調低 TRANSCODE_MAX_HEIGHT")
            return True
        finally:
            try:
                _os.unlink(tmp)
            except OSError:
                pass

    fetch_seg(0, "第 0 段 (開頭)")
    if len(segs) > 4:
        mid = len(segs) // 2
        # 直接跳中間，驗證「拖曳到任意位置」能不能單獨轉出來
        fetch_seg(mid, f"第 {mid} 段 (中段 seek)")
    return good


def t_subtitles(play: dict) -> None:
    subs = play.get("subtitles") or []
    if not subs:
        info("這個檔案沒有偵測到字幕軌")
        return
    s = subs[0]
    code, _, body = req(s["url"], timeout=300)
    if code != 200 or not body.startswith(b"WEBVTT"):
        fail("字幕轉 WebVTT", f"HTTP {code}: {body[:150]!r}")
    else:
        ok("字幕轉 WebVTT", f"{s['label']} · {len(body)} bytes")


# ---------------------------------------------------------------- main
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description="FilmaxWeb 端對端測試")
    ap.add_argument("--base", default=BASE, help="服務位址，預設 http://localhost:8080")
    ap.add_argument("--file-id", type=int, help="只測這一個檔案 id")
    ap.add_argument("--samples", type=int, default=2, help="自動挑幾個檔案來測（預設 2）")
    ap.add_argument("--skip-transcode", action="store_true", help="跳過 HLS 分段轉碼（最花時間的部分）")
    ap.add_argument("--password", default="", help="服務有開登入驗證時的密碼")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print(_c(BOLD, "FilmaxWeb 端對端測試") + _c(DIM, f"  → {BASE}"))

    if not t_service():
        return 2

    code, _, _ = req("/api/stats", timeout=20)
    if code == 401:
        pw = args.password
        if not pw:
            import getpass
            print(_c(YELL, "\n這個服務有開登入驗證。"))
            try:
                pw = getpass.getpass("請輸入密碼（或用 --password 帶入）：")
            except (EOFError, KeyboardInterrupt):
                pw = ""
        if not pw or not login(pw):
            fail("登入", "密碼不正確或登入失敗")
            return 2
        ok("登入", "已取得 session")

    env_ok = t_env()

    targets: List[dict] = []
    if args.file_id:
        try:
            play = jget(f"/api/play/{args.file_id}")
            targets = [{"id": args.file_id, "ext": play.get("ext"), "size": play.get("size"),
                        "filename": play.get("filename")}]
        except Exception as e:
            fail("取得指定檔案", str(e))
            return 2
    else:
        items = t_library()
        if not items:
            return 2
        targets = collect_files(items, args.samples)
        if not targets:
            fail("挑選測試檔案", "媒體庫裡找不到任何檔案")
            return 2

    for i, f in enumerate(targets, 1):
        step(f"4.{i} 檔案測試: {f.get('filename')}")
        info(f"id={f['id']} · {(f.get('size') or 0) / 1024 ** 3:.2f} GB · .{f.get('ext')}")
        play = t_probe(f)
        if not play:
            continue
        t_range(f, play)
        if play["mode"] == "direct":
            t_direct(f)
        else:
            t_hls(f, play, deep=not args.skip_transcode)
        t_subtitles(play)

    # ---- 總結 ----
    p = sum(1 for r in results if r[0] == "PASS")
    w = sum(1 for r in results if r[0] == "WARN")
    fl = sum(1 for r in results if r[0] == "FAIL")
    print("\n" + "─" * 68)
    print(f"{_c(BOLD,'結果')}  {_c(GREEN, str(p) + ' 通過')}   "
          f"{_c(YELL, str(w) + ' 警告')}   {_c(RED, str(fl) + ' 失敗')}")
    if fl:
        print("\n失敗項目：")
        for st, name, detail in results:
            if st == "FAIL":
                print(f"  • {name}" + (f" — {str(detail).splitlines()[0][:110]}" if detail else ""))
        if not env_ok:
            print(_c(YELL, "\n先把上面的環境問題修好（多半是 ffmpeg/ffprobe），其他失敗通常會跟著消失。"))
    print("─" * 68)
    return 1 if fl else 0


if __name__ == "__main__":
    sys.exit(main())
