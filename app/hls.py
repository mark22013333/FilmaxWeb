"""隨選分段 HLS：播放清單依總長度預先算好，每段第一次被要的時候才用 ffmpeg 產生並快取。

好處：無狀態、拖曳進度條可以跳到任何位置、重播時直接吃快取。
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import db, keyframes, media
from .config import CACHE_DIR, settings

log = logging.getLogger("filmax.hls")

# 最近幾段的轉碼速度（影片秒數 / 實際耗時）。用來判斷還有沒有餘裕做預轉。
_speed_samples: List[float] = []
_speed_lock = threading.Lock()


def record_speed(video_seconds: float, elapsed: float) -> None:
    if elapsed <= 0:
        return
    with _speed_lock:
        _speed_samples.append(video_seconds / elapsed)
        del _speed_samples[:-8]


def should_record_speed(rung: int, background: bool) -> bool:
    """這一段的耗時該不該算進速度樣本。

    **上階的樣本不能記。**remux 一段只要 0.35 秒（6 秒影片 ÷ 0.35 ≈ 17x），
    而 `_speed_samples` 是全域的、不分檔案也不分階別 —— 灌進去會讓
    `_schedule_prefetch()` 以為機器很閒，然後在 4K 那種 1.2x 的片子上照樣開預轉。
    那個後果 `_schedule_prefetch` 的註解裡已經寫過（同一個檔案單獨跑 1.9 秒，
    三個並行時變成 10.9 秒）。預轉本身也不記，理由一樣：它是低優先權跑的。
    """
    return not background and rung != RUNG_REMUX


def recent_speed() -> Optional[float]:
    with _speed_lock:
        if not _speed_samples:
            return None
        return sum(_speed_samples) / len(_speed_samples)


_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_prefetch_pool = threading.Semaphore(1)   # 預轉一次只跑一個，不跟前景搶


def _seg_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


# 階別（J 章第 0 層）：0 = 下階（轉碼），1 = 上階（remux，`-c:v copy`）。
RUNG_TRANSCODE = 0
RUNG_REMUX = 1


def profile_key(height: Optional[int], audio_index: Optional[int],
                bitrate_kbps: int = 0, rung: int = RUNG_TRANSCODE) -> str:
    # height=0 代表「不縮放」，是有意義的值；只有 None 才算沒指定。
    h = settings.max_height if height is None else height
    a = audio_index if audio_index is not None else "d"
    # 聲道數也要進 key：不然改了 AUDIO_CHANNELS 之後，舊的立體聲分段會跟新的
    # 5.1 分段混在同一個播放清單裡，播到一半就爆掉。
    c = max(0, int(settings.audio_channels))
    # 階別也一定要進 key，理由跟聲道數一模一樣，而且更嚴重：上階的分段邊界
    # 由 keyframe 決定（實測 7.34 秒）、下階是固定 6 秒。key 少了這一維，
    # 兩種分段會落進同一個資料夾、混進同一份播放清單。
    m = RUNG_REMUX if int(rung or 0) == RUNG_REMUX else RUNG_TRANSCODE
    return f"h{h}_a{a}_b{max(0, int(bitrate_kbps))}_c{c}_m{m}"


def normalize_profile(profile: Optional[str]) -> str:
    """把外部傳進來的 profile 正規化成安全的字串。

    這個值會直接拿去當快取資料夾名稱，不能讓 ../ 之類的東西混進來。
    做法是「解析完重建」而不是「檢查完放行」—— 重建出來的字串必然只由
    我們自己產生的字元組成，就不必去想還有哪種繞過寫法沒擋到。
    認不得的內容會退回預設 profile。
    """
    return profile_key(*_parse_profile(profile or ""))


def seg_dir(file_id: int, profile: str) -> Path:
    d = CACHE_DIR / str(file_id) / normalize_profile(profile)
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# 兩階（J 章第 0 層）
# --------------------------------------------------------------------------
def bounds_for(file_id: int, duration: float) -> List[Tuple[float, float, int]]:
    """上階的分段邊界（開始、結束、位元組大小）。沒有邊界表就回空清單。

    邊界規則不是存在 DB 裡的 —— `media_keyframe` 存的是原始 keyframe 時間與
    偏移，規則在 `keyframes.derive_bounds()`。所以改規則不必重掃那 42 分鐘，
    但**每次要用都得算一次**（一部片幾百個點，微秒級，不值得再加一層快取）。
    """
    tbl = keyframes.table_for(file_id)
    if not tbl:
        return []
    row = db.q1("SELECT size FROM media_file WHERE id=?", (file_id,))
    return keyframes.derive_bounds(
        tbl["times"], tbl["positions"], settings.hls_segment_seconds,
        duration=duration, total_size=(row["size"] if row else 0) or 0)


def rungs_for(file_id: int, duration: float) -> List[int]:
    """這個檔案有哪幾階可用，最好的排前面。下階永遠都在。

    **判斷集中在這一個函式**：`build_master()` 決定發幾階、`build_playlist()`
    決定要不要接受 `m1`，兩邊問的必須是同一個答案 —— 不然會發出一份
    「master 說有上階、index 卻給不出來」的播放清單。
    """
    rungs = [RUNG_TRANSCODE]
    if not settings.hls_two_rung:
        return rungs
    row = db.q1("""SELECT f.kf_state, f.remux_state, k.gap_med
                   FROM media_file f
                   LEFT JOIN media_keyframe k ON k.file_id=f.id
                   WHERE f.id=?""", (file_id,))
    if not row or row["kf_state"] != "ok":
        return rungs
    # 這個檔案的上階已經下線（某一段 remux 失敗過）。**整個檔案下線，
    # 不是只退那一段** —— 一份播放清單裡混著 copy 與重編的分段是更糟的失效。
    if row["remux_state"] == "failed":
        return rungs
    # keyframe 間距比分段長度還大的片源（實測 4K DV 那部是 10.01 秒 > 6）
    # 只發下階：每段只裝得下一個 keyframe，段長會被片源綁死。
    gap = row["gap_med"] or 0
    if gap <= 0 or gap > settings.hls_segment_seconds:
        return rungs
    if not bounds_for(file_id, duration):
        return rungs
    return [RUNG_REMUX, RUNG_TRANSCODE]


# ffprobe 的 profile 字串 → avc1.PPCCLL 的前四位（PP=profile_idc、CC=約束旗標）。
# **認不得就不要猜**：規格允許整個 CODECS 屬性省略，而填錯會讓 Safari
# 直接拒收那一階 —— 省略只是少一點資訊，填錯是整階不能播。
_AVC1_PREFIX = {
    "high": "6400", "main": "4d40", "baseline": "4200",
    "constrained baseline": "42e0", "high 10": "6e00", "high 4:2:2": "7a00",
}


def codecs_attr(video_profile: Optional[str], video_level: Optional[int],
                audio: str = "mp4a.40.2") -> Optional[str]:
    """組出 `CODECS` 的值；推不出視訊那一半就整個回 None。"""
    pre = _AVC1_PREFIX.get((video_profile or "").strip().lower())
    if not pre or not video_level or video_level <= 0:
        return None
    return f"avc1.{pre[:4]}{int(video_level):02x}," + audio


def segment_count(duration: float, profile: str = "", file_id: int = 0) -> int:
    """這一階有幾段。上階由邊界表決定，下階是固定長度切出來的。"""
    if profile and file_id and _parse_profile(profile)[3] == RUNG_REMUX:
        n = len(bounds_for(file_id, duration))
        if n:
            return n
    return max(1, math.ceil(duration / settings.hls_segment_seconds))


def build_playlist(file_id: int, duration: float, profile: str) -> str:
    seg = settings.hls_segment_seconds
    rung = _parse_profile(profile)[3]

    # 上階（remux）的邊界只能由 keyframe 決定 —— `-c:v copy` 只能從 keyframe
    # 起頭，而 `i * seg` 幾乎永遠不落在 keyframe 上（`-force_key_frames` 在
    # copy 模式下無效）。實測段長因此變成 7.34 秒（keyframe 中位 3.17 × 2），
    # 比設定的 6 秒長 22% —— 所以 TARGETDURATION 也要照實際的最長段填，
    # 不能填設定值，填小了是規格違反。
    spans: List[Tuple[float, float]] = []
    if rung == RUNG_REMUX:
        spans = [(a, b) for a, b, _ in bounds_for(file_id, duration)]
    if not spans:
        # 下階：維持固定短段。**兩階刻意不共用邊界** —— 遠端唯一會用到的是
        # 下階（上階峰值 21.7 Mbps 遠端選不到），段變長就是開播變慢。
        n = max(1, math.ceil(duration / seg))
        spans = [(i * seg, min((i + 1) * seg, duration)) for i in range(n)]

    target = max(1, math.ceil(max(b - a for a, b in spans)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-ALLOW-CACHE:YES",
    ]
    for i, (a, b) in enumerate(spans):
        lines.append(f"#EXTINF:{max(b - a, 0.001):.3f},")
        lines.append(f"seg-{i}.ts?p={profile}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _stream_inf(bw: int, avg: int, w: int, h: int, codecs: Optional[str]) -> str:
    """一行 EXT-X-STREAM-INF。`CODECS` 推不出來就整個屬性省略（規格允許）。"""
    parts = [f"BANDWIDTH={int(bw)}"]
    if avg > 0:
        parts.append(f"AVERAGE-BANDWIDTH={int(avg)}")
    parts.append(f"RESOLUTION={int(w)}x{int(h)}")
    if codecs:
        parts.append(f'CODECS="{codecs}"')
    return "#EXT-X-STREAM-INF:" + ",".join(parts)


def build_master(file_id: int, profile: str, remote: bool = False,
                 duration: float = 0.0) -> str:
    """master playlist。**區網發一階、遠端發兩階。**

    區網不需要 ABR：鏈路夠寬，發單一的上階（remux）就是零轉碼路徑 ——
    這是整個設計最確定會兌現的那一塊，所以它是規則不是最佳化。
    遠端保留兩階讓客戶端自己按緩衝水位選（伺服器不猜頻寬 —— 那正是
    Netflix 在 50 萬使用者上驗證後放棄的做法）。
    """
    row = db.q1("""SELECT width, height, bitrate, size, video_profile, video_level
                   FROM media_file WHERE id=?""", (file_id,))
    w = (row["width"] if row else None) or 1920
    h = (row["height"] if row else None) or 1080
    prof_h, prof_a, prof_b, _ = _parse_profile(profile)
    want = settings.max_height if prof_h is None else prof_h
    target_h = min(want, h) if want else h        # 0 = 不縮放
    target_w = int(w * target_h / h / 2) * 2 if h else 1920

    rungs = rungs_for(file_id, duration) if duration > 0 else [RUNG_TRANSCODE]
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]

    upper = None
    if RUNG_REMUX in rungs:
        bounds = bounds_for(file_id, duration)
        peak, avg = keyframes.bandwidth_for(bounds, audio_kbps=settings.audio_bitrate_kbps)
        if peak > 0:
            # **BANDWIDTH 要填峰值。**實測全檔峰值 21,703 kbps 對平均 10,899
            # —— 1.99 倍。填平均會讓 hls.js 在 12 Mbps 的鏈路上選了上階，
            # 然後在每一個峰值卡一次，那比不給上階更糟。
            upper = _stream_inf(peak, avg, w, h,
                                codecs_attr(row["video_profile"] if row else None,
                                            row["video_level"] if row else None))
            upper_profile = profile_key(0, prof_a, 0, RUNG_REMUX)

    lower_profile = profile_key(prof_h, prof_a, prof_b, RUNG_TRANSCODE)
    # 下階的 BANDWIDTH 填 VBV 上限（保證的天花板）、AVERAGE 填目標碼率。
    lower_cap = prof_b * 1000 if prof_b else max(1_500_000, int(target_w * target_h * 4))
    lower_avg = prof_b * 1000 if prof_b else 0
    lower = _stream_inf(lower_cap, lower_avg, target_w, target_h,
                        codecs_attr("High", _level_int(target_h)))

    if upper and not remote:
        # 區網：只發上階。播放全程不會有任何 ffmpeg 被啟動。
        lines += [upper, f"index.m3u8?p={upper_profile}"]
    elif upper:
        lines += [upper, f"index.m3u8?p={upper_profile}", lower,
                  f"index.m3u8?p={lower_profile}"]
    else:
        lines += [lower, f"index.m3u8?p={lower_profile}"]
    return "\n".join(lines) + "\n"


def _level_int(height: int) -> int:
    """下階是我們自己編的，level 由 media.h264_level_for() 決定（如 "4.0"）。"""
    try:
        major, minor = media.h264_level_for(height).split(".")
        return int(major) * 10 + int(minor)
    except Exception:
        return 40


def _parse_profile(profile: str) -> Tuple[Optional[int], Optional[int], int, int]:
    height: Optional[int] = None
    audio: Optional[int] = None
    bitrate = 0
    rung = RUNG_TRANSCODE       # 沒寫 m 的舊網址一律當下階，相容且安全
    for part in profile.split("_"):
        if part.startswith("h") and part[1:].isdigit():
            height = int(part[1:])
        elif part.startswith("a") and part[1:].isdigit():
            audio = int(part[1:])
        elif part.startswith("b") and part[1:].isdigit():
            bitrate = int(part[1:])
        elif part.startswith("m") and part[1:].isdigit():
            rung = RUNG_REMUX if int(part[1:]) == RUNG_REMUX else RUNG_TRANSCODE
    return height, audio, bitrate, rung


def get_segment(file_id: int, index: int, profile: str, duration: float) -> Path:
    d = seg_dir(file_id, profile)
    path = d / f"seg-{index}.ts"
    if path.exists() and path.stat().st_size > 0:
        os.utime(path, None)
        _schedule_prefetch(file_id, index, profile, duration)
        return path

    key = f"{file_id}/{profile}/{index}"
    with _seg_lock(key):
        if path.exists() and path.stat().st_size > 0:
            return path
        _produce(file_id, index, profile, duration, path)
    _schedule_prefetch(file_id, index, profile, duration)
    return path


def _low_priority_kwargs(background: bool) -> dict:
    """把預轉的 ffmpeg 降到低優先權，讓使用者正在等的那一段先拿到 CPU。"""
    if not background:
        return {}
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)}
    return {"preexec_fn": lambda: os.nice(10)}


def _produce(file_id: int, index: int, profile: str, duration: float, path: Path,
             background: bool = False) -> None:
    seg = settings.hls_segment_seconds
    height, audio_index, bitrate, rung = _parse_profile(profile)
    if rung == RUNG_REMUX:
        # 上階的第 i 段不是 [i*seg, (i+1)*seg)，而是邊界表算出來的那一段。
        # 這裡跟 build_playlist() 必須用同一份邊界，不然播放清單說的長度
        # 跟實際產生的內容會對不上（播到那一段就跳）。
        spans = bounds_for(file_id, duration)
        if index >= len(spans):
            raise RuntimeError(f"上階沒有第 {index} 段（共 {len(spans)} 段）")
        start, end, _ = spans[index]
        length = max(end - start, 0.05)
    else:
        start = index * seg
        length = min(seg, max(duration - start, 0.05))
    tmp = path.with_suffix(".ts.part")

    def run(force_software: bool) -> Optional[str]:
        """回傳 None 表示成功，否則回傳錯誤訊息。"""
        if rung == RUNG_REMUX:
            cmd = media.build_remux_cmd(file_id, start, length, audio_index)
        else:
            cmd = media.build_transcode_cmd(file_id, start, length, height, audio_index,
                                            force_software=force_software,
                                            bitrate_kbps=bitrate)
        try:
            with open(tmp, "wb") as fh:
                proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE,
                                      timeout=max(120, int(seg * 30)),
                                      **_low_priority_kwargs(background))
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            return "轉碼逾時"
        if proc.returncode != 0 or tmp.stat().st_size == 0:
            # 這段訊息會被塞進 HTTP 500 的回應裡，一定要先遮掉內部憑證
            err = media.scrub_bytes(proc.stderr or b"").decode("utf-8", "ignore")[:400]
            tmp.unlink(missing_ok=True)
            return err or f"ffmpeg exit {proc.returncode}"
        return None

    t0 = time.time()
    try:
        if rung == RUNG_REMUX:
            err = run(force_software=False)
            if err:
                # **remux 失敗不要「退回 CPU 重試」** —— copy 根本沒有用到編碼器，
                # 再試一次只會再失敗一次。失敗的原因是片源本身（非單調 DTS、
                # 時間戳、容器），那是整個檔案的性質，所以把整個檔案的上階下線，
                # 讓 master 改發單階。播放不會中斷（客戶端會重抓 master）。
                take_remux_offline(file_id, err)
                raise RuntimeError(f"上階 remux 失敗 seg {index}: {err}")
            tmp.replace(path)
            log.info("remux file=%s seg=%s (%.1fs 影片 / %.2fs 耗時)",
                     file_id, index, length, time.time() - t0)
            return
        hw = media.resolve_hwaccel()
        err = run(force_software=False)
        if err and hw != "none":
            # 硬體編碼在執行期掛掉（驅動、GPU 忙碌、參數不合）→ 用 CPU 再試一次，
            # 不要讓整段播放直接死掉。
            log.warning("硬體轉碼失敗 (%s)，改用 CPU 重試 seg %s: %s", hw, index, err.splitlines()[0][:160])
            err2 = run(force_software=True)
            if err2 is None:
                media.disable_hwaccel(f"執行期失敗：{err.splitlines()[0][:160]}")
                err = None
            else:
                err = err2
        if err:
            raise RuntimeError(f"轉碼失敗 seg {index}: {err}")
        tmp.replace(path)
        elapsed = time.time() - t0
        if should_record_speed(rung, background):
            record_speed(length, elapsed)
        log.info("轉碼 file=%s seg=%s (%.1fs 影片 / %.1fs 耗時 = %.2fx)%s",
                 file_id, index, length, elapsed, length / elapsed if elapsed else 0,
                 " [預轉]" if background else "")
    finally:
        _enforce_cache_limit()


def take_remux_offline(file_id: int, reason: str) -> None:
    """把一個檔案的上階下線，並留下後台看得到的紀錄。

    這條路徑的性質是「安靜地降級」：使用者只會看到畫質從原檔變成轉碼，
    不會看到任何錯誤。所以**紀錄是把這個功能開起來的條件之一** ——
    沒有清單的話，上階可能整批下線了而沒有人知道（後台「媒體庫」頁看得到）。
    """
    msg = (reason or "").strip().splitlines()[0][:400] if reason else "未知原因"
    try:
        db.execute("UPDATE media_file SET remux_state='failed', remux_error=? WHERE id=?",
                   (msg, file_id))
    except Exception as e:
        log.debug("寫入 remux 下線狀態失敗 file=%s: %s", file_id, e)
    log.warning("上階 remux 下線 file=%s：%s（改發單階轉碼，播放不中斷）", file_id, msg[:200])


def _schedule_prefetch(file_id: int, index: int, profile: str, duration: float) -> None:
    n = settings.hls_prefetch
    if n <= 0:
        return
    # 上階不預轉。預轉的存在理由是「轉碼跟不上播放」，而 remux 是 17x ——
    # 它需要的是磁碟頻寬，那正是使用者正在播的那一段也需要的東西。
    if _parse_profile(profile)[3] == RUNG_REMUX:
        return
    # 轉碼本身就快跟不上了就別再預轉 —— 多開的 ffmpeg 會跟使用者正在等的那一段搶 CPU，
    # 讓情況更糟。實測過：同一個檔案單獨跑 1.9 秒，三個並行時變成 10.9 秒。
    sp = recent_speed()
    if sp is not None:
        if sp < settings.prefetch_min_speed:
            log.debug("轉碼速度只有 %.2fx，暫停預轉", sp)
            return
        # 速度普通就只預轉一段，很快才做滿
        n = min(n, 1 if sp < settings.prefetch_min_speed * 2 else n)
    total = segment_count(duration, profile, file_id)
    targets = [i for i in range(index + 1, min(index + 1 + n, total))]
    targets = [i for i in targets if not (seg_dir(file_id, profile) / f"seg-{i}.ts").exists()]
    if not targets:
        return

    def worker():
        for i in targets:
            if not _prefetch_pool.acquire(blocking=False):
                return
            try:
                p = seg_dir(file_id, profile) / f"seg-{i}.ts"
                if p.exists():
                    continue
                lk = _seg_lock(f"{file_id}/{profile}/{i}")
                if not lk.acquire(blocking=False):
                    continue
                try:
                    if not p.exists():
                        _produce(file_id, i, profile, duration, p, background=True)
                except Exception as e:
                    log.debug("預轉失敗 %s: %s", i, e)
                finally:
                    lk.release()
            finally:
                _prefetch_pool.release()

    threading.Thread(target=worker, daemon=True).start()


_last_cleanup = 0.0


def _enforce_cache_limit() -> None:
    global _last_cleanup
    if time.time() - _last_cleanup < 60:
        return
    _last_cleanup = time.time()
    limit = settings.hls_cache_max_mb * 1024 * 1024
    files: List[Tuple[float, int, Path]] = []
    total = 0
    for p in CACHE_DIR.rglob("*.ts"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, p))
        total += st.st_size
    if total <= limit:
        return
    files.sort()
    for _, size, p in files:
        try:
            p.unlink()
            total -= size
        except OSError:
            pass
        if total <= limit * 0.8:
            break
    log.info("HLS 快取清理完成，剩 %.0f MB", total / 1024 / 1024)


# profile key 的形狀改過就要清一次快取。改的是**資料夾名稱**，舊分段不會被
# 誤用（normalize 之後對不到那個名字），但它們會一直躺在那裡佔著上限，
# 直到 LRU 把它們汰掉 —— 而在那之前，快取上限對真正在用的分段就變小了。
# 版本號往上加一次就清一次；快取本來就是可重建的，清掉沒有風險。
PROFILE_KEY_VERSION = 2         # 2 = 加了階別維度 m0／m1（J 章第 0 層）


def migrate_cache() -> None:
    """啟動時呼叫一次。profile key 換過形狀就把舊分段清掉。"""
    try:
        was = int(db.kv_get("hls_profile_key_version", 1) or 1)
    except Exception:
        was = 1
    if was >= PROFILE_KEY_VERSION:
        return
    clear_cache()
    db.kv_set("hls_profile_key_version", PROFILE_KEY_VERSION)
    log.info("profile key 從 v%s 換成 v%s，已清掉舊的 HLS 分段快取",
             was, PROFILE_KEY_VERSION)


def clear_cache(file_id: Optional[int] = None) -> None:
    target = CACHE_DIR / str(file_id) if file_id else CACHE_DIR
    if not target.exists():
        return
    for p in target.rglob("*"):
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
