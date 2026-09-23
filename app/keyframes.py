"""分段邊界表：keyframe 掃描與背景佇列（規格：J 章第 0 層）。

兩階 HLS 的上階是 remux（`-c:v copy`），而 copy 只能從 keyframe 起頭 ——
所以上階的分段邊界只能由片源自己的 keyframe 決定，不能用「第 i 段 = [i*6, (i+1)*6)」
這種算出來的邊界。這個模組負責把那份 keyframe 表算出來、存起來。

**為什麼是背景佇列，而不是掃描的一個階段。**
實測 6.2 秒／GB（`-skip_frame nokey`，兩部代表性片源 28.8 與 33.5 秒）——
全庫 408 GB 約 42 分鐘。塞進掃描迴圈會讓「掃描」從幾分鐘變成 45 分鐘，
性質完全不同。探測那一階是每檔一秒級，這一階是每檔 30 秒，不該混在一起。

**為什麼並行度是 1。**
探測是短、CPU 為主，所以它用 `PROBE_CONCURRENCY` 開好幾條。這一階相反：
長、純 I/O，而且讀的是轉碼器同時在讀的那顆磁碟。開多條只會互相拖慢 ——
`hls.py` 的註解已經為這件事付過學費（「同一個檔案單獨跑 1.9 秒，
三個並行時變成 10.9 秒」）。

**存原始資料而不是算好的邊界。**
邊界規則還沒定案（「湊滿 >= 分段長度的最少 keyframe 數」讓段長變成 keyframe
間距的整數倍，實測 6 秒的設定變成 7.34 秒；另一種規則是取「離目標最近」的）。
存原始的時間與位元組偏移，換規則就不必重掃那 42 分鐘。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db, localfs, media
from .config import settings

log = logging.getLogger("filmax.keyframes")

# 只有這些視訊編碼有機會走上階（瀏覽器吃得下、可以 -c:v copy）。
# 其他的（hevc 那 13 部）沒有上階可給，直接標 skipped 並記原因 ——
# 標出來比留空白好，否則將來看到空的會以為是「還沒跑到」。
UPPER_RUNG_CODECS = {"h264", "avc1"}

# profile 問不到時寫這個字串，**不要留 NULL**。NULL 的意思是「還沒問過」，
# 而回填的條件就是 NULL —— 問不到卻留 NULL 的話，那個檔案會每次啟動都被
# 重新問一次，永遠問不出來。`hls.codecs_attr()` 認不得這個字串，
# 所以結果一樣是「省略 CODECS」，只是不會再被排進回填。
STREAM_UNKNOWN = "unknown"

_queue_lock = threading.Lock()
_queue_thread: Optional[threading.Thread] = None
_priority: List[int] = []          # 開播插隊用
_priority_lock = threading.Lock()

# 失敗之後多久才可以再試一次（秒）。
#
# **為什麼需要它（實際發生過）。**原本 `_next_row()` 把 `failed` 跟 `pending`
# 一視同仁：掃失敗 → 標 failed → 下一輪又挑到它（ORDER BY id，它還是最小的
# 那一個）→ 再失敗。沒有任何延遲，速度只受限於 ffprobe 開一次行程的時間：
# production 上一支已經搬走的檔案（FTP 回 550）被這樣重試了 4.5 萬次，
# 每分鐘約 475 條新的 FTP 連線，連續兩個多小時 —— 而且因為它永遠排在最前面，
# **它後面的檔案一個都輪不到**。開播時的 `request_soon()` 也會把 failed
# 再插回最前面，每一次 master 請求都是一次新的重試。
#
# 記在記憶體而不是 DB：重啟之後每個失敗的檔案各重試一次（原本就是這個語意），
# 不必為了一個時間戳加 migration。明確的重試途徑有兩條：重新掃描
# （`start_background(retry_failed=True)`）與後台的手動重試。
FAILED_RETRY_COOLDOWN = 1800.0
_failed_at: Dict[int, float] = {}  # file_id → 失敗時的 time.monotonic()
_failed_lock = threading.Lock()


def _note_failed(file_id: int) -> None:
    with _failed_lock:
        _failed_at[file_id] = time.monotonic()


def retry_due(file_id: int, now: Optional[float] = None) -> bool:
    """這個 failed 的檔案現在可以再試了嗎（冷卻期過了、或這個行程還沒試過）。"""
    with _failed_lock:
        t = _failed_at.get(file_id)
    if t is None:
        return True
    return ((time.monotonic() if now is None else now) - t) >= FAILED_RETRY_COOLDOWN


def clear_failed_cooldown() -> int:
    """讓所有 failed 的檔案下一輪就能再試一次。回傳清掉幾筆。

    給「使用者重新掃描」與「管理員手動重試」用 —— 那是明確的意圖，
    不該被冷卻期擋住。**每個檔案在一輪裡仍然最多試一次**（見 run_queue）。
    """
    with _failed_lock:
        n = len(_failed_at)
        _failed_at.clear()
    return n


def runnable(file_id: int, kf_state: Optional[str]) -> bool:
    """開播插隊要不要做：pending 一律要，failed 只在冷卻期過了才要。"""
    if kf_state == "pending":
        return True
    return kf_state == "failed" and retry_due(file_id)


# --------------------------------------------------------------------------
# 掃描一個檔案
# --------------------------------------------------------------------------
def _probe_cmd(source: str, is_local: bool) -> List[str]:
    """問 keyframe 的時間與位元組偏移。

    **兩個欄位都要。**時間決定邊界，偏移決定每段多大 —— 而每段多大就是
    `BANDWIDTH` 的來源，而 HLS 的 `BANDWIDTH` 是**單段峰值**不是平均
    （實測峰值是平均的 1.99 倍，填平均會讓 hls.js 選了上階再在每個峰值卡一次）。

    **欄位名是 `pkt_pos` 不是 `pos`。**實測 `frame=pts_time,pos` 只吐得出時間、
    偏移那一欄是空的；`pkt_pos` 兩個都有，而且值跟 `-show_packets` 完全一致
    （ffmpeg 6.1 與 9.0 都驗過）。

    用 `-skip_frame nokey` 而不是 `-show_packets`：兩種問法算出來的 keyframe 數
    完全一致，但前者便宜（實測 hevc 上 33.5 對 52.4 秒 —— `-show_packets`
    要把每一個封包都吐成文字）。
    """
    cmd = [media.resolve_tool("ffprobe"), "-v", "error"]
    if not is_local:
        # 走 HTTP 來源才需要內部憑證與逾時
        cmd += [*media.source_headers(), "-rw_timeout", "30000000"]
    cmd += ["-select_streams", "v:0", "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time,pkt_pos",
            "-of", "csv=p=0", source]
    return cmd


def _stream_cmd(source: str, is_local: bool) -> List[str]:
    """問視訊串流的 profile 與 level（給上階的 `CODECS` 用）。

    **刻意分成第二次呼叫。**`-show_entries frame=...:stream=...` 一次問得到，
    但那會把 frame 的 CSV 與 stream 的 CSV 混在同一份輸出裡，
    靠「這一行 float() 失敗所以它是 stream」來分辨 —— 那是隱含約定，
    將來加一個欄位就會壞。這一次呼叫**不解碼**（沒有 `-skip_frame`），
    只讀檔頭，實測毫秒級，不值得為它換掉一個已經驗過的解析函式。
    """
    cmd = [media.resolve_tool("ffprobe"), "-v", "error"]
    if not is_local:
        cmd += [*media.source_headers(), "-rw_timeout", "30000000"]
    cmd += ["-select_streams", "v:0",
            "-show_entries", "stream=profile,level",
            "-of", "csv=p=0", source]
    return cmd


def _parse_stream(out: bytes) -> Tuple[Optional[str], Optional[int]]:
    """回傳 (profile, level)。問不到就回 (None, None) —— 呼叫端要省略 CODECS。"""
    for line in out.decode("utf-8", "ignore").splitlines():
        if not line.strip():
            continue
        f = [x.strip() for x in line.split(",")]
        prof = f[0] or None
        lvl: Optional[int] = None
        if len(f) > 1:
            try:
                lvl = int(f[1])
            except ValueError:
                lvl = None
        # ffprobe 對問不到的欄位吐 "unknown"／"N/A"，那不是 profile 名稱
        if prof and prof.lower() in ("unknown", "n/a", "-99"):
            prof = None
        if lvl is not None and lvl < 0:
            lvl = None          # -99 = 沒有 level 這個概念
        return prof, lvl
    return None, None


def _parse(out: bytes) -> Tuple[List[float], List[int]]:
    times: List[float] = []
    poss: List[int] = []
    for line in out.decode("utf-8", "ignore").splitlines():
        f = line.split(",")
        if len(f) < 2:
            continue
        try:
            t, pos = float(f[0]), int(f[1])
        except ValueError:
            continue        # N/A 或空欄位，跳過
        times.append(round(t, 3))
        poss.append(pos)
    # 按時間排序：B-frame 讓封包順序不等於顯示順序
    pairs = sorted(zip(times, poss))
    return [t for t, _ in pairs], [p for _, p in pairs]


def source_for(row: Dict[str, Any]) -> Tuple[str, bool]:
    """回傳 (要餵給 ffprobe 的來源, 是不是本機路徑)。

    本機直讀（第 −1 層）拿得到就用它：全檔掃描要讀完整個檔案，走
    「HTTP → FastAPI → FTP → 磁碟」那四層等於把 42 分鐘再乘上一個係數，
    而且會佔住 FTP 連線池整整 30 秒以上。認不得就照舊走 HTTP。
    """
    local = localfs.resolve_local(row.get("ftp_path") or "")
    if local is not None:
        return str(local), True
    return media.source_url(int(row["id"])), False


def scan_one(row: Dict[str, Any], cancel=None) -> str:
    """掃一個檔案並寫進 DB。回傳新的 kf_state。"""
    fid = int(row["id"])
    codec = (row.get("video_codec") or "").lower()
    if codec not in UPPER_RUNG_CODECS:
        db.execute("UPDATE media_file SET kf_state='skipped', kf_error=? WHERE id=?",
                   (f"視訊是 {codec or '未知'}，沒有上階可給（只發單階轉碼）", fid))
        return "skipped"

    source, is_local = source_for(row)
    t0 = time.time()
    try:
        code, out, err = media.run_tool(_probe_cmd(source, is_local),
                                        timeout=1800, cancel=cancel)
    except media.Cancelled:
        # 使用者按了停止。這不是失敗 —— 留在 pending，下一輪自然會再排。
        return "pending"
    if code != 0:
        msg = media.scrub_bytes(err or b"").decode("utf-8", "ignore")[:400]
        db.execute("UPDATE media_file SET kf_state='failed', kf_error=? WHERE id=?",
                   (msg or f"ffprobe exit {code}", fid))
        log.warning("keyframe 掃描失敗 file=%s: %s", fid, msg[:160])
        return "failed"

    times, poss = _parse(out)
    if len(times) < 2:
        db.execute("UPDATE media_file SET kf_state='failed', kf_error=? WHERE id=?",
                   ("抓不到足夠的 keyframe", fid))
        return "failed"

    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    gaps.sort()
    db.execute(
        """INSERT INTO media_keyframe(file_id, times, positions, count,
                                      gap_min, gap_med, gap_max, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(file_id) DO UPDATE SET
               times=excluded.times, positions=excluded.positions,
               count=excluded.count, gap_min=excluded.gap_min,
               gap_med=excluded.gap_med, gap_max=excluded.gap_max,
               updated_at=excluded.updated_at""",
        (fid, json.dumps(times), json.dumps(poss), len(times),
         round(gaps[0], 3), round(gaps[len(gaps) // 2], 3), round(gaps[-1], 3),
         int(time.time())))
    # profile／level 跟邊界表一起更新：兩者都是上階的前置條件，
    # 分開更新會出現「有邊界表但沒有 CODECS」這種只差一半的狀態。
    prof = lvl = None
    try:
        code2, out2, _ = media.run_tool(_stream_cmd(source, is_local),
                                        timeout=120, cancel=cancel)
        if code2 == 0:
            prof, lvl = _parse_stream(out2)
    except media.Cancelled:
        return "pending"
    except Exception as e:                       # 問不到不是失敗：省略 CODECS 就好
        log.debug("profile/level 問不到 file=%s: %s", fid, e)
    # 退格行為跟邊界表一起量：上階要用的兩個前置條件，分開更新就會出現
    # 「有邊界表但不知道要不要補償」這種只差一半的狀態。
    try:
        backoff = measure_backoff(row, times, cancel=cancel)
    except media.Cancelled:
        return "pending"
    db.execute("""UPDATE media_file SET kf_state='ok', kf_error=NULL,
                         video_profile=?, video_level=?, seek_backoff=? WHERE id=?""",
               (prof or STREAM_UNKNOWN, lvl, backoff, fid))
    log.info("keyframe file=%s %s 個（%.1fs，%s）", fid, len(times),
             time.time() - t0, "本機直讀" if is_local else "走 FTP")
    return "ok"


# --------------------------------------------------------------------------
# 邊界推導（規則還沒定案，所以跟儲存分開）
# --------------------------------------------------------------------------
def derive_bounds(times: List[float], positions: List[int], seg_seconds: float,
                  duration: Optional[float] = None,
                  total_size: Optional[int] = None) -> List[Tuple[float, float, int]]:
    """從 keyframe 表算分段邊界。回傳 [(起, 迄, 位元組), ...]。

    規則：**第 i 段的結尾取離 `(i+1) * seg_seconds` 最近的 keyframe。**
    也就是說邊界是「對齊到固定格線」，不是「從上一個邊界往後累加」。

    **為什麼不是「湊滿 >= seg_seconds 的最少 keyframe 數」（原本的規則）。**
    那條規則是相對的：每一段都從前一段的結尾往後量，於是誤差會**累加**。
    實測（全庫 157 個有邊界表的檔案，149 個可 remux）：

        file 297  seg-10  上階 = 106.02s，下階 = 60.00s   （差 46 秒）
        file 297  seg-100 上階 = 1060.77s，下階 = 600.00s （差 461 秒）
        file 297  seg-708 上階 = 7112.15s，下階 = 4248.00s（差 2864 秒）

    而這兩階是**放在同一份遠端 ABR master 裡讓 hls.js 互相切換的**。
    對 hls.js 來說 seg-100 就是 seg-100 —— 它照著 MEDIA-SEQUENCE 與 EXTINF
    累加出來的時間軸挑下一段，切階之後拿到的卻是另一個 media time 的內容，
    append 進 SourceBuffer 就是重疊或倒退：**畫面短暫跳回已經看過的地方，
    hls.js 再把播放位置修回來**。使用者看到的正是這個。

    對齊到格線之後誤差**不累加**：第 i 段的邊界永遠在 `i * seg_seconds`
    的一個 keyframe 間距之內，不管播到第幾段。實測全庫 145/149 個檔案
    從「錯位」變成「對齊」。

    `-c:v copy` 仍然只能從 keyframe 起頭，所以邊界一定要**落在 keyframe 上**
    —— 這裡只是換一個挑法（挑離格線最近的那一個），不是改成算出來的時間。

    **尾巴要補。**迴圈只能收在最後一個 keyframe，而片尾還有一段沒有 keyframe
    的內容 —— 漏掉它的話播放清單會短少那幾秒，而「最後幾秒播不到」是很難
    察覺的失效。太短的尾巴（不到半個分段）併進前一段，不要在清單裡留一個
    0.x 秒的分段。

    **開頭同理，而且後果更嚴重。**第一個 keyframe 不保證落在 0（有些片源
    第一張 I-frame 在 0.3～1 秒之後）。照原樣切的話上階的第一段從 0.5 開始，
    整份播放清單的 EXTINF 總和就比下階少了那 0.5 秒 —— 於是**同一部片在
    兩階有兩條長度不同的時間軸**：hls.js 在切階時會把 currentTime 對到另一
    條軸上，播放位置與 duration 都會跳，而 seek 到 50% 也會落在兩個不同的
    地方。這正是「切了畫質之後進度亂掉」那一類問題最難查的來源。

    所以第一段一律從 0 起算（`-ss 0` 對 copy 模式也成立 —— 0 之後的第一張
    I-frame 就是 times[0]，ffmpeg 本來就會從那裡開始）。
    """
    n = len(times)
    if n < 2 or seg_seconds <= 0:
        return []
    bounds: List[Tuple[float, float, int]] = []
    i = 0
    k = 1                      # 這一段的結尾要對齊到第幾個格線
    while i < n - 1:
        target = k * seg_seconds
        # 往後走到第一個 >= 格線的 keyframe
        j = i + 1
        while j < n - 1 and times[j] < target:
            j += 1
        # 前一個 keyframe 也可能離格線更近（格線落在兩個 keyframe 中間偏後時）。
        # **取近的那一個**才是「對齊」，一律取後面那個會讓每一段都偏長。
        if j - 1 > i and abs(times[j - 1] - target) < abs(times[j] - target):
            j -= 1
        bounds.append((times[i], times[j], positions[j] - positions[i]))
        i = j
        # 下一段的格線由**實際落點**往後推，不是 k+1 ——
        # keyframe 稀疏到跨過好幾格時，k+1 會讓後面連著切出好幾個超短段。
        k = max(k + 1, int(times[j] / seg_seconds) + 1)
    # 開頭補到 0：兩階的時間軸原點必須一致（見 docstring）。
    if bounds and bounds[0][0] > 0:
        _, e0, b0 = bounds[0]
        bounds[0] = (0.0, e0, b0)
    tail_start = times[-1]
    tail_bytes = max((total_size or positions[-1]) - positions[-1], 0)
    if duration and duration - tail_start > 0.1:
        if bounds and duration - tail_start < seg_seconds * 0.5:
            s0, _, b0 = bounds[-1]
            bounds[-1] = (s0, duration, b0 + tail_bytes)
        else:
            bounds.append((tail_start, duration, tail_bytes))
    return bounds


def _backoff_cmd(source: str, is_local: bool, at: float) -> List[str]:
    """問「`ffmpeg -ss at -c:v copy` 的落點是哪裡」。

    **一定要用 ffmpeg，不能用 ffprobe 的 `-read_intervals` 代替**（試過）：
    兩者的 seek 語意不同，`-read_intervals 600.600%+4` 在會退格的 mkv 上
    照樣回 600.600，量不到退格。要量的就是 `build_remux_cmd()` 實際會用的
    那條路徑，所以這裡的參數要跟它一致（`-copyts` + 輸入端 `-ss` + `-c:v copy`）。

    **輸出 matroska 而不是 mpegts**：mpegts muxer 會把整段再往後平移約 1.4 秒
    （實測），那層平移跟退格無關，混進來會讓量測多一個常數。
    `-frames:v 1` 只要第一個封包，不必真的搬完四秒。
    """
    cmd = [media.resolve_tool("ffmpeg"), "-v", "error", "-nostdin", "-y",
           "-copyts", "-ss", f"{at:.3f}"]
    if not is_local:
        cmd += [*media.source_headers(), "-rw_timeout", "30000000"]
    cmd += ["-i", source, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-c:v", "copy", "-frames:v", "1", "-f", "matroska", "-"]
    return cmd


def measure_backoff(row: Dict[str, Any], times: List[float],
                    cancel=None) -> Optional[int]:
    """`-ss` 在這個檔案上會不會退到前一個 keyframe。回 1／0，問不出來回 None。

    **為什麼一定要量。**`build_remux_cmd()` 的 docstring 原本斷言「copy 模式下
    ffmpeg 一律退到前一個 keyframe」—— 那句話對 mkv 成立，對 mp4 不成立。
    實測把同一份視訊 `-c copy` 換個容器再測：

        abr.mp4  -ss 3.480 → 落點 3.480   （完全不退）
        abr.mkv  -ss 3.480 → 落點 0.000   （退一整格 3.48 秒）

    **同樣的視訊內容、同樣的 keyframe，只換容器就換了行為。**
    所以退格不是編碼的性質，推導不出來，只能對每個檔案實際問一次。

    **猜錯的代價是不對稱的。**該推沒推 = 接縫重疊（畫面往回跳，就是原本的 bug）；
    不該推卻推了 = 那一段開頭整個缺一格（實測 seg0 從 0.000 變成 4.880，
    開頭 4.88 秒不見）。後者比前者嚴重，所以**問不出來就回 None**，
    呼叫端當作「不退」處理。

    量測點取檔案中段的一個 keyframe：開頭那幾個可能有 edit list／負時間戳之類
    的特例，拿它當樣本會量到別的東西。
    """
    if len(times) < 6:
        return None
    probe_at = times[len(times) // 2]
    prev = times[len(times) // 2 - 1]
    source, is_local = source_for(row)
    try:
        code, out, _err = media.run_tool(_backoff_cmd(source, is_local, probe_at),
                                         timeout=120, cancel=cancel)
    except media.Cancelled:
        raise
    except Exception as e:
        log.debug("退格量測失敗 file=%s: %s", row.get("id"), e)
        return None
    if code != 0 or not out:
        return None
    land = _first_pts(out)
    if land is None:
        return None
    # 落點落在「前一個 keyframe」附近 → 會退格。容許半格的誤差：
    # 落點是封包時間，跟 keyframe 表的值可能差幾毫秒。
    tol = max((probe_at - prev) * 0.5, 0.05)
    return 1 if abs(land - prev) <= tol else 0


def _first_pts(mkv_bytes: bytes) -> Optional[float]:
    """從 `_backoff_cmd` 吐出來的 matroska 位元組裡讀出那一個封包的 pts。

    ffprobe 吃 stdin（`pipe:0`）—— 不必為了問一個數字把它落地成檔案。
    """
    import subprocess
    try:
        p = subprocess.run(
            [media.resolve_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", "pipe:0"],
            input=mkv_bytes, capture_output=True, timeout=60)
    except Exception:
        return None
    vals = []
    for line in p.stdout.decode("utf-8", "ignore").splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            vals.append(float(line))
        except ValueError:
            continue
    return min(vals) if vals else None


def seek_start_for(times: List[float], start: float,
                   end: Optional[float] = None) -> float:
    """上階要餵給 `ffmpeg -ss` 的值：`times` 裡**下一個** keyframe。

    **為什麼不是直接給 `start`。**copy 模式下 ffmpeg 一律退到前一個 keyframe
    才開始抄 —— 即使 `start` 本身就是一個貨真價實的 keyframe（實測 file 360：
    邊界 600.600 在容器裡是 `flags=K__`，`-ss 600.600` 的落點仍然是 598.598）。
    退格量修不掉（copy 需要前一個 IDR 才敢起頭，微調 `-ss` 也躲不掉，
    `media.build_remux_cmd()` 的 docstring 已經為這件事付過學費）。

    **能改的是餵給它什麼起點。**把 `-ss` 往後推一格，demux 退回來之後剛好
    落在我們宣告的邊界上。實測三種形狀都成立：

        file 360（均勻 2.0s）          接縫重疊 2.044 → 0.042 秒
        file 297（0.959~10.428s）      接縫重疊 0.251 秒
        file 438（0.133~0.934s）       接縫重疊 0.100 秒

    **推後量是查表得到的，不是算的。**全庫 172 個會發上階的檔案裡有 96 個
    `gap_max - gap_min > 0.5` —— 間距均勻不是常態，`start + gap_med` 那種算法
    在稀疏處會跳過好幾格。查表才能讓上面那三種形狀用同一條規則。

    **找不到下一格就回 `start` 本身。**那是最後一段的情形（`start` 已經是最後
    一個 keyframe，或尾巴那段的起點在所有 keyframe 之後）。這時候退格沒有東西
    可以補償，但也不會更糟 —— 最後一段後面沒有下一段，沒有接縫會重疊。

    **`end` 是必要的保險，不是選用的精修。**只跨一個 keyframe 的短段
    （`derive_bounds` 在格線附近會切出這種段）下一格就是這一段的**結尾**，
    推過去等於 `-ss X -to X`：ffmpeg 吐出一個只有兩個封包的空殼
    （實測 415,668 → 18,424 位元組），那一段的畫面整個不見。
    所以下一格只要碰到或越過 `end` 就不推 —— 退格造成的重疊只是瑕疵，
    推成空段是整段播不出來，兩者不是同一個量級。
    """
    for t in times:
        if t > start + 1e-6:
            if end is not None and t >= end - 1e-6:
                return start        # 推過去會把這一段推成空的
            return t
    return start


def bandwidth_for(bounds: List[Tuple[float, float, int]],
                  audio_kbps: int = 0) -> Tuple[int, int]:
    """回傳 (BANDWIDTH, AVERAGE-BANDWIDTH)，單位 bps，已含音訊。

    BANDWIDTH 是**單段峰值** —— HLS 規格就是這樣定的，而 remux 的峰值遠高於
    平均（實測 21,703 對 10,899 kbps，1.99 倍）。
    """
    rates = [b[2] * 8 / (b[1] - b[0]) for b in bounds if b[1] - b[0] > 0.1]
    if not rates:
        return 0, 0
    span = max(bounds[-1][1] - bounds[0][0], 0.1)
    avg = sum(b[2] for b in bounds) * 8 / span
    a = audio_kbps * 1000
    return int(max(rates) + a), int(avg + a)


def table_for(file_id: int) -> Optional[Dict[str, Any]]:
    """拿一個檔案的 keyframe 表。**只有 kf_state='ok' 才回**。

    狀態是 pending 的時候 media_keyframe 可能還留著上一版的資料
    （檔案變動時我們只把狀態打回 pending，不刪資料列）——
    照著舊資料切分段就是切在錯的位置上，所以這裡一定要查狀態。
    """
    row = db.q1("""SELECT k.times, k.positions, k.count, k.gap_min, k.gap_med, k.gap_max
                   FROM media_keyframe k JOIN media_file f ON f.id=k.file_id
                   WHERE k.file_id=? AND f.kf_state='ok'""", (file_id,))
    if not row:
        return None
    return {"times": json.loads(row["times"]), "positions": json.loads(row["positions"]),
            "count": row["count"], "gap_min": row["gap_min"],
            "gap_med": row["gap_med"], "gap_max": row["gap_max"]}


# --------------------------------------------------------------------------
# 背景佇列
# --------------------------------------------------------------------------
def backfill_stream_info(cancel=None, limit: Optional[int] = None) -> int:
    """把已經有邊界表、但還沒有 profile／level 的檔案補起來。回傳補了幾個。

    **為什麼不是把 kf_state 打回 pending。**那會讓那 97 部重新掃一次
    keyframe —— 每檔 30 秒、全庫 42 分鐘，只為了兩個欄位。這裡用的是
    `_stream_cmd`：不解碼、只讀檔頭，實測毫秒級。

    這一支存在的理由是升級路徑：profile／level 這兩欄是後來才加的，
    而 `kf_state='ok'` 的檔案不會再被排進佇列（那是對的，邊界表沒有變）。
    """
    done = 0
    while limit is None or done < limit:
        if cancel is not None and cancel.is_set():
            break
        row = db.q1("""SELECT id, ftp_path FROM media_file
                       WHERE kf_state='ok' AND video_profile IS NULL
                       ORDER BY id LIMIT 1""")
        if not row:
            break
        fid = int(row["id"])
        source, is_local = source_for(dict(row))
        prof = lvl = None
        try:
            code, out, _ = media.run_tool(_stream_cmd(source, is_local),
                                          timeout=120, cancel=cancel)
            if code == 0:
                prof, lvl = _parse_stream(out)
        except media.Cancelled:
            break
        except Exception as e:
            log.debug("回填 profile/level 失敗 file=%s: %s", fid, e)
        db.execute("UPDATE media_file SET video_profile=?, video_level=? WHERE id=?",
                   (prof or STREAM_UNKNOWN, lvl, fid))
        done += 1
    if done:
        log.info("補上 %s 個檔案的 profile／level（上階的 CODECS 要用）", done)
    return done


def backfill_seek_backoff(cancel=None, limit: Optional[int] = None) -> int:
    """把已經有邊界表、但還沒量過退格的檔案補起來。回傳補了幾個。

    跟 `backfill_stream_info()` 同一個理由（升級路徑）：`seek_backoff` 是後來
    才加的欄位，而 `kf_state='ok'` 的檔案不會再被排進佇列 —— 不補的話既有的
    172 個會發上階的檔案永遠是 NULL，而 NULL 一律當作「不退」，
    於是 R 章要修的那個重疊對它們全部不會生效。

    **比 profile／level 貴。**這一支每個檔案要真的跑一次 `ffmpeg -ss`
    （`-frames:v 1`，只搬一個封包），不是讀檔頭。所以放在 profile／level
    後面，並且照舊吃 `cancel`。
    """
    done = 0
    while limit is None or done < limit:
        if cancel is not None and cancel.is_set():
            break
        row = db.q1("""SELECT id, ftp_path FROM media_file
                       WHERE kf_state='ok' AND seek_backoff IS NULL
                       ORDER BY id LIMIT 1""")
        if not row:
            break
        fid = int(row["id"])
        tbl = table_for(fid)
        bk = None
        if tbl:
            try:
                bk = measure_backoff(dict(row), tbl["times"], cancel=cancel)
            except media.Cancelled:
                break
            except Exception as e:
                log.debug("回填退格失敗 file=%s: %s", fid, e)
        # **量不出來也要寫 0，不要留 NULL。**NULL 的意思是「還沒量過」，
        # 而這支的條件就是 NULL —— 留著會每次啟動都重跑一次，永遠補不完。
        # 寫 0 的語意（不推）正好也是量不出來時該有的保守行為。
        db.execute("UPDATE media_file SET seek_backoff=? WHERE id=?",
                   (0 if bk is None else bk, fid))
        done += 1
    if done:
        log.info("量好 %s 個檔案的 -ss 退格行為（上階的分段邊界要用）", done)
    return done


def seek_backfill_count() -> int:
    """還等著量退格的檔案數（升級路徑，見 backfill_seek_backoff）。"""
    r = db.q1("""SELECT COUNT(*) AS n FROM media_file
                 WHERE kf_state='ok' AND seek_backoff IS NULL""")
    return int(r["n"]) if r else 0


def pending_count() -> int:
    r = db.q1("SELECT COUNT(*) AS n FROM media_file WHERE kf_state IN ('pending','failed')")
    return int(r["n"]) if r else 0


def backfill_count() -> int:
    """還等著補 profile／level 的檔案數（升級路徑，見 backfill_stream_info）。"""
    r = db.q1("""SELECT COUNT(*) AS n FROM media_file
                 WHERE kf_state='ok' AND video_profile IS NULL""")
    return int(r["n"]) if r else 0


def request_soon(file_id: int) -> None:
    """開播時插隊：這個檔案還沒算好就把它排到最前面。

    開播時**不要等** —— 30 秒的等待等於開不起來。照舊發單階（轉碼），
    算好之後下一次開播才有上階。
    """
    with _priority_lock:
        if file_id not in _priority:
            _priority.insert(0, file_id)


class _Round:
    """一輪 `run_queue()` 的進度。

    **不變量：同一輪裡，每個 file id 最多嘗試一次。**
    `attempted` 擋插隊來的（插隊不照 id 順序），`after_id` 是一般掃描的游標
    （照 id 遞增往前走、不回頭）—— 兩者合起來保證這一輪一定走得完：
    游標單調遞增，插隊的每個 id 也只收一次。不用 `id NOT IN (...)`：
    SQLite 的參數上限是 999，失敗的檔案一多就爆。
    """

    def __init__(self) -> None:
        self.attempted: set = set()
        self.after_id = 0


def _next_row(cancel=None, rnd: Optional[_Round] = None) -> Optional[Dict[str, Any]]:
    if rnd is None:
        rnd = _Round()          # 單獨呼叫（測試、除錯）時就是全新的一輪
    with _priority_lock:
        while _priority:
            fid = _priority.pop(0)
            if fid in rnd.attempted:
                continue
            r = db.q1("""SELECT id, ftp_path, video_codec, kf_state FROM media_file
                         WHERE id=? AND kf_state IN ('pending','failed')""", (fid,))
            if r and runnable(fid, r["kf_state"]):
                rnd.attempted.add(fid)
                return dict(r)
    while True:
        r = db.q1("""SELECT id, ftp_path, video_codec, kf_state FROM media_file
                     WHERE kf_state IN ('pending','failed') AND probe_state='ok'
                       AND id > ?
                     ORDER BY id LIMIT 1""", (rnd.after_id,))
        if not r:
            return None
        fid = int(r["id"])
        rnd.after_id = fid
        if fid in rnd.attempted:
            continue            # 這一輪已經從插隊那邊試過了
        if r["kf_state"] == "failed" and not retry_due(fid):
            continue            # 冷卻中：跳過，但不擋住後面的檔案
        rnd.attempted.add(fid)
        return dict(r)


def run_queue(cancel=None, limit: Optional[int] = None) -> Dict[str, int]:
    """把待辦排完。**同步、單執行緒** —— 呼叫端自己決定要不要丟到背景。

    每個檔案一輪最多試一次（見 `_Round`），失敗的進冷卻期
    （`FAILED_RETRY_COOLDOWN`），所以一支永遠掃不起來的壞檔
    **既不會讓這一輪停不下來，也不會擋住排在它後面的檔案**。
    """
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    # 先補 profile／level：那是毫秒級的檔頭讀取，而下面的掃描是每檔 30 秒。
    # 放在前面，升級之後第一次跑就能讓既有的 97 部拿到 CODECS。
    try:
        backfill_stream_info(cancel=cancel)
    except Exception as e:
        log.debug("回填 profile/level 整批失敗：%s", e)
    # 退格量測放在後面：它比讀檔頭貴（每檔要真的跑一次 ffmpeg -ss）。
    try:
        backfill_seek_backoff(cancel=cancel)
    except Exception as e:
        log.debug("回填退格整批失敗：%s", e)
    done = 0
    rnd = _Round()
    while True:
        if cancel is not None and cancel.is_set():
            break
        if limit is not None and done >= limit:
            break
        row = _next_row(cancel, rnd)
        if row is None:
            break
        fid = int(row["id"])
        try:
            state = scan_one(row, cancel=cancel)
        except Exception as e:
            # 逾時、ffprobe 不見了之類。原本這會讓整個佇列中斷 ——
            # 一支檔案的問題不該讓後面的全部停擺，記成這支失敗、繼續下一支。
            msg = media.scrub(str(e))[:400] or type(e).__name__
            db.execute("UPDATE media_file SET kf_state='failed', kf_error=? WHERE id=?",
                       (msg, fid))
            log.warning("keyframe 掃描失敗 file=%s: %s", fid, msg[:160])
            state = "failed"
        if state == "failed":
            _note_failed(fid)
        if state in stats:
            stats[state] += 1
        elif state == "pending":        # 被取消
            break
        done += 1
    return stats


def start_background(cancel=None, retry_failed: bool = False) -> bool:
    """掃描結束後叫這個。已經有一條在跑就不重複開。

    `retry_failed=True`：先清掉失敗的冷卻期，讓 failed 的檔案這一輪再試一次
    （重新掃描、管理員手動重試）。開播插隊不帶這個旗標 —— 那條路徑一秒可以
    進來好幾次，正是原本 retry storm 的來源之一。
    """
    global _queue_thread
    if retry_failed:
        clear_failed_cooldown()
    with _queue_lock:
        if _queue_thread is not None and _queue_thread.is_alive():
            return False
        n = pending_count()
        b = backfill_count()
        sb = seek_backfill_count()
        # 待辦是零、但還有 profile／level 或退格要補的話也要開 —— 升級之後
        # 既有的檔案全都是 kf_state='ok'，`pending_count()` 會是零。
        if n == 0 and b == 0 and sb == 0:
            return False

        def worker():
            log.info("keyframe 佇列開始，待辦 %s 個"
                     "（另有 %s 個要補 profile／level、%s 個要量退格）", n, b, sb)
            try:
                stats = run_queue(cancel=cancel)
                log.info("keyframe 佇列結束：%s", stats)
            except Exception as e:
                log.warning("keyframe 佇列中斷：%s", e)

        _queue_thread = threading.Thread(target=worker, daemon=True,
                                         name="keyframe-queue")
        _queue_thread.start()
        return True
