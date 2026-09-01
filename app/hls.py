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

from . import db, media
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


def profile_key(height: Optional[int], audio_index: Optional[int],
                bitrate_kbps: int = 0) -> str:
    h = height or settings.max_height
    a = audio_index if audio_index is not None else "d"
    # 聲道數也要進 key：不然改了 AUDIO_CHANNELS 之後，舊的立體聲分段會跟新的
    # 5.1 分段混在同一個播放清單裡，播到一半就爆掉。
    c = max(0, int(settings.audio_channels))
    return f"h{h}_a{a}_b{max(0, int(bitrate_kbps))}_c{c}"


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


def segment_count(duration: float) -> int:
    return max(1, math.ceil(duration / settings.hls_segment_seconds))


def build_playlist(file_id: int, duration: float, profile: str) -> str:
    seg = settings.hls_segment_seconds
    n = segment_count(duration)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{seg}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-ALLOW-CACHE:YES",
    ]
    for i in range(n):
        d = min(seg, duration - i * seg)
        lines.append(f"#EXTINF:{max(d, 0.001):.3f},")
        lines.append(f"seg-{i}.ts?p={profile}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def build_master(file_id: int, profile: str) -> str:
    row = db.q1("SELECT width, height, bitrate FROM media_file WHERE id=?", (file_id,))
    w = (row["width"] if row else None) or 1920
    h = (row["height"] if row else None) or 1080
    prof_h, _, prof_b = _parse_profile(profile)
    target_h = min(prof_h or settings.max_height or h, h)
    target_w = int(w * target_h / h / 2) * 2 if h else 1920
    bw = prof_b * 1000 if prof_b else max(1_500_000, int(target_w * target_h * 4))
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={target_w}x{target_h},CODECS="avc1.64001f,mp4a.40.2"\n'
        f"index.m3u8?p={profile}\n"
    )


def _parse_profile(profile: str) -> Tuple[Optional[int], Optional[int], int]:
    height: Optional[int] = None
    audio: Optional[int] = None
    bitrate = 0
    for part in profile.split("_"):
        if part.startswith("h") and part[1:].isdigit():
            height = int(part[1:])
        elif part.startswith("a") and part[1:].isdigit():
            audio = int(part[1:])
        elif part.startswith("b") and part[1:].isdigit():
            bitrate = int(part[1:])
    return height, audio, bitrate


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
    start = index * seg
    length = min(seg, max(duration - start, 0.05))
    height, audio_index, bitrate = _parse_profile(profile)
    tmp = path.with_suffix(".ts.part")

    def run(force_software: bool) -> Optional[str]:
        """回傳 None 表示成功，否則回傳錯誤訊息。"""
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
        if not background:
            record_speed(length, elapsed)
        log.info("轉碼 file=%s seg=%s (%.1fs 影片 / %.1fs 耗時 = %.2fx)%s",
                 file_id, index, length, elapsed, length / elapsed if elapsed else 0,
                 " [預轉]" if background else "")
    finally:
        _enforce_cache_limit()


def _schedule_prefetch(file_id: int, index: int, profile: str, duration: float) -> None:
    n = settings.hls_prefetch
    if n <= 0:
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
    total = segment_count(duration)
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
