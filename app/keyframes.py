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
    db.execute("""UPDATE media_file SET kf_state='ok', kf_error=NULL,
                         video_profile=?, video_level=? WHERE id=?""",
               (prof or STREAM_UNKNOWN, lvl, fid))
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

    規則：每段取「湊滿 >= seg_seconds 的最少 keyframe 數」。

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
    if n < 2:
        return []
    bounds: List[Tuple[float, float, int]] = []
    i = 0
    while i < n - 1:
        j = i + 1
        while j < n - 1 and times[j] - times[i] < seg_seconds:
            j += 1
        bounds.append((times[i], times[j], positions[j] - positions[i]))
        i = j
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


def _next_row(cancel=None) -> Optional[Dict[str, Any]]:
    with _priority_lock:
        while _priority:
            fid = _priority.pop(0)
            r = db.q1("""SELECT id, ftp_path, video_codec FROM media_file
                         WHERE id=? AND kf_state IN ('pending','failed')""", (fid,))
            if r:
                return dict(r)
    r = db.q1("""SELECT id, ftp_path, video_codec FROM media_file
                 WHERE kf_state IN ('pending','failed') AND probe_state='ok'
                 ORDER BY id LIMIT 1""")
    return dict(r) if r else None


def run_queue(cancel=None, limit: Optional[int] = None) -> Dict[str, int]:
    """把待辦排完。**同步、單執行緒** —— 呼叫端自己決定要不要丟到背景。"""
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    # 先補 profile／level：那是毫秒級的檔頭讀取，而下面的掃描是每檔 30 秒。
    # 放在前面，升級之後第一次跑就能讓既有的 97 部拿到 CODECS。
    try:
        backfill_stream_info(cancel=cancel)
    except Exception as e:
        log.debug("回填 profile/level 整批失敗：%s", e)
    done = 0
    while True:
        if cancel is not None and cancel.is_set():
            break
        if limit is not None and done >= limit:
            break
        row = _next_row(cancel)
        if row is None:
            break
        state = scan_one(row, cancel=cancel)
        if state in stats:
            stats[state] += 1
        elif state == "pending":        # 被取消
            break
        done += 1
    return stats


def start_background(cancel=None) -> bool:
    """掃描結束後叫這個。已經有一條在跑就不重複開。"""
    global _queue_thread
    with _queue_lock:
        if _queue_thread is not None and _queue_thread.is_alive():
            return False
        n = pending_count()
        b = backfill_count()
        # 待辦是零、但還有 profile／level 要補的話也要開 —— 升級之後
        # 既有的檔案全都是 kf_state='ok'，`pending_count()` 會是零。
        if n == 0 and b == 0:
            return False

        def worker():
            log.info("keyframe 佇列開始，待辦 %s 個（另有 %s 個要補 profile／level）", n, b)
            try:
                stats = run_queue(cancel=cancel)
                log.info("keyframe 佇列結束：%s", stats)
            except Exception as e:
                log.warning("keyframe 佇列中斷：%s", e)

        _queue_thread = threading.Thread(target=worker, daemon=True,
                                         name="keyframe-queue")
        _queue_thread.start()
        return True
