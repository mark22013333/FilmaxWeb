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

from . import db, keyframes, media, streamstat
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


# 這個版本號的語意是：**任何會讓既有 `.ts` 不再與目前程式相容的修改。**
# 不只是 profile key 的字串形狀（那是它原本的名字 PROFILE_KEY_VERSION 的
# 由來，而那個名字太窄了）。要升版的例子：
#
#   * 分段邊界規則（v3：上階改成對齊固定格線，見 keyframes.derive_bounds）
#   * 時間戳策略（-output_ts_offset／-avoid_negative_ts）
#   * 編碼器、音訊聲道／取樣率、mux 旗標
#   * profile key 的形狀
#
# **為什麼漏掉會很嚴重。**`get_segment()` 看到 `seg-N.ts` 存在就直接回傳，
# 不會去驗它的內容 —— 邊界規則改了以後，舊的 seg-N 仍然叫 seg-N，但它裝的
# 是另一段影片。播放清單說 seg-100 是 600 秒，快取吐出來的卻是 1060 秒的
# 內容：這正是「畫面跳回之前看過的地方」的另一條路徑，而且它跨越重新部署
# 存活，比 ABR 那條更難查。
HLS_CACHE_FORMAT_VERSION = 5    # 5 = 上階的 -ss 補償 copy 的退格，音訊跟著對齊

# 舊名字留著給還沒改的呼叫端；語意已經擴大，新的程式碼請用上面那個。
PROFILE_KEY_VERSION = HLS_CACHE_FORMAT_VERSION


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
    """這個 profile 的分段資料夾。**路徑本身帶格式版本**（`v3_h720_...`）。

    版本只放在 KV 裡是不夠的：升級的瞬間會有一小段時間新舊 binary 同時在跑
    （反向代理還沒切完、或 `migrate_cache()` 還沒跑到），而它們算出來的
    `seg-N` 是不同的內容 —— 共用同一個資料夾就是互相餵錯誤的分段。
    把版本寫進路徑，兩邊各自寫各自的，不可能混到。
    """
    d = CACHE_DIR / str(file_id) / f"v{HLS_CACHE_FORMAT_VERSION}_{normalize_profile(profile)}"
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


# 上階要能進同一份 ABR master，它的第 i 段必須跟下階的 [i*seg, (i+1)*seg)
# 是**同一段影片** —— hls.js 切階之後照著 MEDIA-SEQUENCE 挑下一段，
# seg-N 指到別的 media time 就是重疊或倒退。
#
# **舊規則（容許 1.5 個分段長 = 9 秒、而且只看 start）太鬆。**9 秒比一整段
# 還長，連「少了一整段」都會被判定成可以切換；而只看 start 的話，
# 一段從對的地方開始、卻在錯的地方結束也會過關。
#
# 現在是兩道：**段數必須一樣**（少一段就代表 seg-N 整個錯位，這一道擋掉
# 實測 172 個裡的 58 個），加上 start 與 end 都要落在 0.25 秒內。
# 0.25 秒比一個畫格寬很多，但不再容許肉眼看得到的錯位。
#
# **這一道只管遠端 ABR。**區網只發一個 rendition，沒有另一階可以切過去，
# 所以嚴格的對齊要求不會讓區網失去零轉碼路徑（實測 172 個檔案裡只有 34 個
# 進得了遠端 ABR，但區網 172 個全部照發上階）。
_ABR_ALIGN_TOLERANCE_SECONDS = 0.25


def abr_alignable(file_id: int, duration: float) -> Tuple[bool, str]:
    """上階能不能跟轉碼階梯放進同一份 ABR master。回 (可以嗎, 為什麼)。

    **這跟「有沒有上階」是兩件事**，所以不併進 `rungs_for()`：
    區網的單一 rendition、原畫質、使用者明確指定的那幾條路都只發一階，
    沒有另一階可以切過去，對不對齊根本不影響它們。會出事的只有
    「同一份 master 裡有好幾階、hls.js 可以互相切」這一種形狀。

    對不齊的代價是使用者實際看到的那個症狀（畫面跳回已經看過的地方），
    所以這裡的取捨很清楚：**寧可少一階畫質，也不要壞掉的時間軸。**
    """
    seg = settings.hls_segment_seconds
    spans = bounds_for(file_id, duration)
    if not spans:
        return False, "沒有邊界表"

    expected = max(1, math.ceil(duration / seg))
    if len(spans) != expected:
        return False, (f"上階有 {len(spans)} 段、下階有 {expected} 段；"
                       "段數不同代表 seg-N 已經不是同一段影片")

    tol = _ABR_ALIGN_TOLERANCE_SECONDS
    worst = 0.0
    worst_i = 0
    worst_edge = "start"
    for i, (start, end, _b) in enumerate(spans):
        expected_start = i * seg
        expected_end = min((i + 1) * seg, duration)
        for edge, actual, wanted in (
            ("start", start, expected_start),
            ("end", end, expected_end),
        ):
            drift = abs(actual - wanted)
            if drift > worst:
                worst, worst_i, worst_edge = drift, i, edge

    if worst > tol:
        return False, (f"上階 seg-{worst_i} 的 {worst_edge} 與固定格線差 {worst:.3f} 秒"
                       f"（容許 {tol:.2f}），不能放進同一份 ABR master")
    return True, f"所有分段起訖最大偏差 {worst:.3f} 秒"


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


# 遠端的轉碼階梯（J 章第 1 層）。每一項是「相對主階的高度、相對主階的碼率」。
# **1.0 那一項一定要在，而且要排第一** —— 它就是原本唯一的那一階，
# 使用者在畫質選單挑的值（chosen_h／chosen_b）落在它身上。
#
# 為什麼是比例不是絕對值：主階由 REMOTE_MAX_HEIGHT 與使用者的選擇決定，
# 寫死 480／360 的話，主階被調成 480p 時階梯會變成 480／480／360 —— 
# 兩階一模一樣，ABR 白切一次。
_ABR_STEPS: List[Tuple[float, float]] = [
    (1.0, 1.0),        # 主階：使用者選的（或遠端預設）那一檔
    (0.667, 0.45),     # 中階：720p → 480p、2800 → 1260 kbps
    (0.5, 0.25),       # 低階：720p → 360p、2800 → 700 kbps
]
# 階與階之間至少要差這麼多高度，否則就不發這一階。兩階只差 60px 的話
# ABR 切過去省不到頻寬，卻要多養一份轉碼快取。
_ABR_MIN_GAP = 100
# 低於這個高度就不再往下發 —— 再低畫質已經不能看，不如讓 ABR 卡在低階
# 重試（那至少還播得動），而不是給一個沒有人想看的畫面。
_ABR_MIN_HEIGHT = 240


def _high_rung(src_h: int, base_h: int, base_b: int,
               auto: bool = True) -> Optional[Tuple[int, int]]:
    """比主階更高的那一階（B 案）。不該發就回 None。

    **這一階只放寬解析度，不放寬碼率** —— 碼率上限（REMOTE_BITRATE_KBPS）才是
    真正保護上傳頻寬的東西，解析度上限只決定畫面多大。同樣吃 2800 kbps，
    1080p 在 27 吋螢幕上比 720p 清楚得多，而佔用的上傳頻寬**一模一樣**。

    網路不夠的裝置不會因此變卡：ABR 會自己降回主階（手機在 4G 上幾乎不會
    停在這一階）。所以這是「讓大螢幕拿得到」，不是「強迫所有人拿」。
    """
    if not settings.remote_high_rung:
        return None
    # **使用者手動挑過畫質就不要自作主張往上加。**挑「480p」的意思是
    # 「我最多要 480p」（省流量、或那條線路就是不行），不是「從 480p 開始爬」。
    # 在上面補一階 1080p 等於把他按掉的東西又塞回去。
    if not auto:
        return None
    if base_h <= 0:
        return None               # 主階已經是「不縮放」，沒有更高的可給
    if base_b <= 0:
        return None               # 主階已經不鎖峰值 —— 使用者手動要了原畫質那一類
    # 天花板取兩個之中低的：片源本身，以及全域的 TRANSCODE_MAX_HEIGHT。
    # **不能超過 TRANSCODE_MAX_HEIGHT** —— 那是整個服務的解析度上限，
    # 遠端的階梯沒有理由比區網還高。
    mh = int(settings.max_height or 0)
    ceiling = min(src_h, mh) if mh > 0 else src_h
    if ceiling - base_h < _ABR_MIN_GAP:
        return None               # 跟主階差太少，多發一階換不到畫質
    return (ceiling - ceiling % 2, base_b)


def abr_ladder(src_w: int, src_h: int, base_h: int, base_b: int,
               auto: bool = True) -> List[Tuple[int, int]]:
    """遠端轉碼階梯：回傳 [(高度, 碼率kbps), ...]，最好的排前面。

    `base_h` 0 代表「不縮放」；那種情況下階梯要從片源的實際高度往下算，
    不然 0 乘以任何比例都還是 0，整條階梯會退化成三個「不縮放」。

    `auto` 是「使用者沒有手動挑畫質」。手動挑過的話高畫質頂階不發 ——
    他挑的那一檔就是他要的上限（見 `_high_rung`）。
    """
    top = base_h if base_h > 0 else src_h
    # base_b 0 是「不鎖峰值」。階梯下面幾階一定要有一個數字才算得出碼率，
    # 用片源尺寸推一個保守值（跟 build_master 的 lower_cap 同一條公式）。
    top_b = base_b if base_b > 0 else max(1500, int(top * top * 16 / 9 * 4 / 1000))

    out: List[Tuple[int, int]] = []
    # 高畫質頂階排在主階前面（master 裡最好的要排前面）。
    high = _high_rung(src_h, base_h, base_b, auto=auto)
    if high:
        out.append(high)
    for i, (fh, fb) in enumerate(_ABR_STEPS):
        h = top if i == 0 else int(top * fh) // 2 * 2
        b = base_b if i == 0 else max(300, int(top_b * fb))
        if i == 0:
            out.append((base_h, b))       # 主階照原樣傳回（0 = 不縮放要保住）
            continue
        if h < _ABR_MIN_HEIGHT or h >= src_h:
            continue
        # 跟已經收進來的每一階都要拉開距離（主階是 0 時比的是片源高度）
        prev = [(top if ph == 0 else ph) for ph, _ in out]
        if any(abs(h - x) < _ABR_MIN_GAP for x in prev):
            continue
        out.append((h, b))
    return out


def quality_policy(remote: bool, h: Optional[int]) -> Tuple[int, int]:
    """這條鏈路 ＋ 使用者的選擇 → (解析度上限, 位元率上限kbps)。

    **兩個端點必須問同一個函式。**`/api/play` 決定 `hls_url` 要帶什麼、
    `master.m3u8` 決定階梯的頂端是哪一檔 —— 各算各的話，master 會發出一份
    跟播放資訊講好的不一樣的階梯（實際發生過的形狀：master 少了位元率上限，
    遠端的頂階變成「不鎖峰值」，REMOTE_BITRATE_KBPS 整個失效）。

    h 是使用者在畫質選單挑的：None = 自動、0 = 原畫質（不縮放）、其餘 = 指定高度。
    """
    # 這幾個設定裡 0 是「不縮放／不鎖」，不是「沒設定」。
    mh = int(settings.max_height or 0)
    rmh = int(settings.remote_max_height or 0)
    if remote:
        caps = [x for x in (rmh, mh) if x > 0]
        default_h = min(caps) if caps else 0
        default_b = settings.remote_bitrate_kbps
    else:
        default_h = mh
        default_b = settings.lan_bitrate_kbps

    chosen_h = default_h if h is None else max(0, int(h))
    if h is None:
        chosen_b = default_b
    elif chosen_h == 0 or (mh and chosen_h >= mh):
        chosen_b = 0                      # 明確要原畫質（或已達上限）就不鎖峰值
    else:
        chosen_b = default_b
    return chosen_h, chosen_b


def direct_ok_for_remote(src_bitrate_bps: Optional[int], has_remux: bool) -> Tuple[bool, str]:
    """遠端這個檔案該不該走 direct。回 (可以嗎, 為什麼)。

    **原本的條件是「有 remux 上階才改走 HLS」，那個條件是反的。**
    有沒有 remux 上階講的是「改走 HLS 之後畫質會不會掉」，
    而該不該離開 direct 講的是「這條鏈路餵不餵得動原始檔」—— 兩件事。

    direct 的性質是：**沒有第二階可選。**鏈路不夠時它不會變模糊，
    它會停住。所以判斷只能看片源位元率對不對得起遠端的安全頻寬：

      * 片源夠小（≤ REMOTE_DIRECT_MAX_KBPS）→ direct 最省、畫質最好，繼續走。
      * 片源太大 ＋ 有 remux 上階 → 走 HLS。畫質**一模一樣**（上階就是原檔
        照抄），而且不夠時降得下去。這是純賺。
      * 片源太大 ＋ 沒有 remux 上階 → 還是走 HLS。這一步有代價（要重編、
        畫質有損），但代價是「畫質降一階」，而留在 direct 的代價是「播不動」。
        規格 J 的取捨一路都是**順暢播放優先**，這裡照同一條。

    區網不走這個函式 —— 鏈路夠寬，direct 一直都是那裡的最佳解。
    """
    cap = int(getattr(settings, "remote_direct_max_kbps", 0) or 0)
    if cap <= 0:
        return True, "未設定遠端 direct 上限，照舊"
    kbps = int((src_bitrate_bps or 0) / 1000)
    if kbps <= 0:
        # 位元率不明（沒 probe 過、或 probe 沒抓到）。**不要賭。**
        # 這裡猜錯的方向不對稱：誤判成「可以 direct」的代價是整段播不動，
        # 誤判成「不能」的代價只是多走一次 remux（有上階時畫質還一樣）。
        return (not has_remux), "片源位元率不明"
    if kbps <= cap:
        return True, f"片源 {kbps} kbps 在遠端安全範圍內（上限 {cap}）"
    return False, (f"片源 {kbps} kbps 超過遠端上限 {cap}，direct 無階可降"
                   + ("，改走 remux 上階（畫質相同）" if has_remux else "，改走轉碼 HLS"))


def build_master(file_id: int, profile: str, remote: bool = False,
                 duration: float = 0.0, auto: bool = True) -> str:
    """master playlist。**區網發一階（remux）；遠端發轉碼階梯，對得齊才加上階。**

    區網不需要 ABR：鏈路夠寬，發單一的上階（remux）就是零轉碼路徑 ——
    這是整個設計最確定會兌現的那一塊，所以它是規則不是最佳化。
    遠端發多階讓客戶端自己按緩衝水位選（伺服器不猜頻寬 —— 那正是
    Netflix 在 50 萬使用者上驗證後放棄的做法）。

    **為什麼遠端的轉碼階不只一階**（J 章第 1 層）：上階的峰值是 21.7 Mbps，
    行動網路根本選不到它 —— 也就是說在只有「上階＋單一轉碼階」的形狀下，
    遠端實際上只有一階可用，卡頓時無階可降。手機在收訊起伏的地方看片，
    需要的正是那條往下的路。

    **遠端的上階要先過 `abr_alignable()`。**同一份 master 裡的 seg-N 必須是
    同一段影片，而 remux 的邊界只能落在 keyframe 上 —— 片源的 keyframe 除不盡
    分段長度時就拼不出跟轉碼階一樣的格線（實測 172 個檔案裡只有 34 個對得齊）。
    對不齊就不發上階：畫質少一階，好過 ABR 一切就倒退畫面。

    **曾經有一版是「遠端一律不發上階、連區網的 mkv 也改走轉碼」**，
    理由是「即使不切階也會出現 frame regression」。那個 regression 的根因
    後來查清楚了：copy 模式的 `-ss` 會退到前一個 keyframe，而多抄的那段沒有
    被丟掉（見 `media.build_remux_cmd()`）—— 跟切不切階無關，也跟容器無關。
    修掉之後就不需要用「整批降級成轉碼」來換安全：實測區網 172 個檔案
    全部保住零轉碼路徑。

    `auto` = 使用者沒有手動挑畫質。手動挑過的話不發高畫質頂階（那一檔就是
    他要的上限）。預設 True 是為了讓既有呼叫端與測試不必全部改。
    """
    row = db.q1("""SELECT width, height, bitrate, size, video_profile, video_level
                   FROM media_file WHERE id=?""", (file_id,))
    w = (row["width"] if row else None) or 1920
    h = (row["height"] if row else None) or 1080
    prof_h, prof_a, prof_b, _ = _parse_profile(profile)
    want = settings.max_height if prof_h is None else prof_h
    target_h = min(want, h) if want else h        # 0 = 不縮放

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

    def transcode_inf(step_h: int, step_b: int) -> Tuple[str, str]:
        """一階轉碼的 (STREAM-INF, 播放清單網址)。

        解析度一律走 `media.scaled_size()` —— 它跟 `build_video_filters()` 是
        同一條規則，所以 master 上寫的 RESOLUTION 就是 ffmpeg 真的會吐出來的
        尺寸。自己再算一次 `w * h / 2 * 2` 遲早會跟濾鏡鏈分岔。
        """
        out_w, out_h = media.scaled_size(w, h, step_h)
        # BANDWIDTH 填 VBV 上限（保證的天花板）、AVERAGE 填目標碼率。
        cap = step_b * 1000 if step_b else max(1_500_000, int(out_w * out_h * 4))
        avg_b = step_b * 1000 if step_b else 0
        inf = _stream_inf(cap, avg_b, out_w, out_h,
                          codecs_attr("High", _level_int(out_h)))
        return inf, f"index.m3u8?p={profile_key(step_h, prof_a, step_b, RUNG_TRANSCODE)}"

    if upper and not remote:
        # 區網：只發上階。播放全程不會有任何 ffmpeg 被啟動。
        # **這條路只有一個 rendition，所以不必問對齊** —— 沒有另一階可以
        # 切過去，`abr_alignable()` 要擋的那種失效在這裡不存在。
        #
        # **MKV 不再被排除。**曾經有一版連區網的 mkv 都改走轉碼，理由是
        # 「單一 remux rendition 也可能畫格回退，等有真實 segment PTS 驗證
        # 再開回來」。那個回退的根因已經查清楚並修掉了：copy 模式的 `-ss`
        # 會退到前一個 keyframe，而多抄的那段沒有被丟掉，於是每個接縫都重疊
        # 一整個 keyframe 間距（實測 file 360 是 2.044／2.169 秒）。
        # 修法見 `media.build_remux_cmd()` 與 `keyframes.measure_backoff()`；
        # 驗證就是它當時要等的那份 segment PTS：起點誤差 ±0.000、
        # 接縫重疊降到 0.042~0.167 秒，三種 keyframe 形狀都量過。
        lines += [upper, f"index.m3u8?p={upper_profile}"]
        return "\n".join(lines) + "\n"

    if upper:
        # 遠端要把上階跟轉碼階梯放進同一份 master，hls.js 會在它們之間切 ——
        # 所以這裡是唯一需要「兩階的 seg-N 是不是同一段」的地方。
        ok, why = abr_alignable(file_id, duration)
        if ok:
            lines += [upper, f"index.m3u8?p={upper_profile}"]
        else:
            # 對不齊就不發。**畫質少一階，好過 ABR 一切就倒退畫面。**
            log.info("file=%s 的上階不進遠端 ABR master：%s", file_id, why)

    # 遠端發整條階梯讓 ABR 有得降；區網（走到這裡代表沒有上階可給）維持單一階
    # —— 鏈路夠寬，多發幾階只是多養幾份轉碼快取，換不到東西。
    base_h = prof_h if prof_h is not None else target_h
    steps = (abr_ladder(w, h, base_h, prof_b, auto=auto) if remote
             else [(base_h, prof_b)])
    for step_h, step_b in steps:
        inf, url = transcode_inf(step_h, step_b)
        lines += [inf, url]
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


def segment_cached(file_id: int, index: int, profile: str) -> bool:
    """這一段現在就拿得到嗎（沒有要產生它）。只給量測用，不影響播放路徑。"""
    try:
        p = seg_dir(file_id, profile) / f"seg-{index}.ts"
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


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
        # **只有真的會退格的檔案才把 `-ss` 往後推。**退格是容器的性質不是編碼的
        # （實測同一份視訊 `-c copy` 換容器：mkv 退一整格、mp4 完全不退），
        # 所以由 `seek_backoff` 這個量出來的欄位決定，不是一律推。
        # 猜錯的代價不對稱：該推沒推只是接縫重疊，不該推卻推了會讓那一段
        # 開頭整個缺一格 —— 所以 NULL（還沒量過）一律當作不退。
        #
        # **在這裡查好傳進去**，不要讓 build_remux_cmd() 自己查：那個函式目前
        # 是純組指令的，讓它也去查表就會變成兩邊各查一次、可能拿到不同的答案。
        # `end` 也一定要傳：只跨一個 keyframe 的短段，下一格就是這一段的結尾，
        # 推過去會變成 `-ss X -to X`（空段，畫面整個不見）。
        seek_at = start
        _bk = db.q1("SELECT seek_backoff FROM media_file WHERE id=?", (file_id,))
        if _bk and _bk["seek_backoff"]:
            tbl = keyframes.table_for(file_id)
            if tbl:
                seek_at = keyframes.seek_start_for(tbl["times"], start, end)
    else:
        start = index * seg
        length = min(seg, max(duration - start, 0.05))
        seek_at = start
    tmp = path.with_suffix(".ts.part")

    def run(force_software: bool) -> Optional[str]:
        """回傳 None 表示成功，否則回傳錯誤訊息。"""
        if rung == RUNG_REMUX:
            cmd = media.build_remux_cmd(file_id, start, length, audio_index,
                                        seek_at=seek_at)
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
            el = time.time() - t0
            streamstat.record_produce(file_id, index, rung, length, el, background)
            log.info("remux file=%s seg=%s (%.1fs 影片 / %.2fs 耗時)",
                     file_id, index, length, el)
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
        # 診斷的取樣**不套 should_record_speed 的過濾**：那個過濾是為了不要污染
        # 預轉的決策，而診斷要看的正是「上階多快、預轉佔掉多少」——
        # 把它們濾掉的話診斷頁就看不到預轉在跟前景搶 CPU。
        streamstat.record_produce(file_id, index, rung, length, elapsed, background)
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


def migrate_cache() -> None:
    """啟動時呼叫一次。快取格式換過版就把舊分段清掉。

    **不要求使用者自己記得刪。**HLS 快取是衍生資料，重建的成本只是再跑一次
    ffmpeg，而留著不相容的分段是會播出錯誤畫面的資料一致性 bug ——
    兩邊的代價差了一個數量級，所以這裡一律清掉，不問。
    """
    try:
        was = int(db.kv_get("hls_profile_key_version", 1) or 1)
    except Exception:
        was = 1
    if was >= HLS_CACHE_FORMAT_VERSION:
        return
    clear_cache()
    db.kv_set("hls_profile_key_version", HLS_CACHE_FORMAT_VERSION)
    log.info("HLS 快取格式從 v%s 換成 v%s，已清掉舊的分段（衍生資料，會自動重建）",
             was, HLS_CACHE_FORMAT_VERSION)


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
