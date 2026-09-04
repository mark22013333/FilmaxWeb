"""影片畫質量測工具：這台機器能用什麼編碼器，以及不同設定實際差多少。

為什麼要有這支工具：「畫質不好」有三個完全不同的來源 ——
片源被重新編碼一次、位元率上限把 CRF 蓋掉、編碼器效率不夠。
三者的處理方式不一樣，猜錯了就是白調參數。這支工具把後兩者量出來。

用法（在專案根目錄）：
    python -m app.vidprobe                     # 只探測環境（快，約 30 秒）
    python -m app.vidprobe "D:\\影片\\某部片.mkv"  # 加上實測（約 2～5 分鐘）
    python -m app.vidprobe --batch 清單檔 --ladder  # 多個檔案 ＋ 位元率階梯
    python -m app.vidprobe --json 檔案          # 輸出 JSON

實測做什麼：**每一個變體與比較用的參考片段都直接從原檔以 -ss/-t 取樣**，
不先切成中間檔（先用 -c copy 切一段當參考踩過雷：帶 Dolby Vision 的 4K mkv
時間戳是負的，切出來解不開，於是每一組都量到 24 kbps／SSIM 1.0000）。
回報「位元率 ＋ 速度 ＋ 與原片段的 VMAF／SSIM」。
**同一個 CRF 在不同 preset 下畫質並不相同**，所以只比 kb/s 會得到反向結論。

`--ladder` 另外跑一條 ABR 曲線（碼率綁住，x264 與 NVENC 各一條）。
CRF ＋ 上限那種形狀量不出「上限開到多少就夠」—— 上限只在咬得到的時候
才有作用，不同上限會量到一條平線。碼率綁住才有曲線可以找轉折點。
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:                                    # 有設 FFMPEG_PATH 就照它
    from .config import settings
    FFMPEG = settings.ffmpeg or "ffmpeg"
    FFPROBE = settings.ffprobe or "ffprobe"
    DEFAULT_HEIGHT = int(settings.max_height or 1080)
    # **預設值一律從 .env 讀，不要硬寫。**上一輪 --cap 硬寫 2800，而 .env 已經
    # 改成 12000 —— 於是報告裡標「現況」的那一組其實不是現況，而那一列正是
    # 拿來當基準的。設定與量測工具各寫一份數字，就是「量了但量錯對象」。
    DEFAULT_CRF = int(settings.crf or 21)
    DEFAULT_CAP = int(settings.remote_bitrate_kbps or 12000)
    DEFAULT_NVENC_CQ = int(settings.nvenc_cq or 28)
    DEFAULT_X264_PRESET = settings.x264_preset or "veryfast"
    DEFAULT_SEG = int(settings.hls_segment_seconds or 6)
except Exception:                       # 單獨拿去別台機器跑也要能動
    FFMPEG, FFPROBE = "ffmpeg", "ffprobe"
    DEFAULT_HEIGHT = 1080
    DEFAULT_CRF, DEFAULT_CAP, DEFAULT_NVENC_CQ = 21, 12000, 28
    DEFAULT_X264_PRESET = "veryfast"
    DEFAULT_SEG = 6

try:                                    # 用生產環境同一條濾鏡鏈，數字才有可比性
    from . import media as _media
except Exception:
    _media = None

CLIP_START = 300.0          # 從第 5 分鐘開始取樣，避開片頭黑畫面與 logo
CLIP_SECONDS = 30.0   # VBV 需要一段時間才收斂，太短的取樣會「超出上限」

# libvmaf 預設是單執行緒，而一輪要評分十幾組變體 × 兩個檔案。
# 不開多執行緒的話光評分就會比編碼還久 —— 這是「工具慢到沒人願意跑」的那種失效。
VMAF_THREADS = max(1, min(16, os.cpu_count() or 4))

# 位元率階梯：同一組生產參數、只換位元率上限，看分數從哪裡開始不再上升。
# 這是回答「REMOTE_BITRATE_KBPS 開到多少就夠」的唯一方法 —— 單一個上限量不出曲線。
LADDER_KBPS = [2800, 4000, 6000, 8000, 12000]
# 品質階梯：不綁碼率，只動品質旋鈕，看它自己想花多少、換到幾分。
# 這是回答「TRANSCODE_CRF 與 NVENC_CQ 該設多少」的那一張表 ——
# 位元率階梯答的是「上限該多少」，兩件事不一樣：
# v4 量到 CRF 21 只吐 1,093 kbps／VMAF 92.2，而同一個片源綁 2800k 有 94.6，
# 也就是 **CRF 太保守，把該花的碼率省掉了**，而上限根本沒有咬到。
CRF_LADDER = [15, 17, 19, 21]
# NVENC 的 cq 不是 x264 的 crf（決策 15），所以要有自己的一組值。
CQ_LADDER = [22, 25, 28, 31]
# VMAF 95 以上一般看不出差異；邊際增益小於這個值就當「加碼率買不到畫質」。
VMAF_GOOD_ENOUGH = 95.0
VMAF_MARGINAL = 0.5

# 參考片段用無損重編落地成檔案。ultrafast + qp 0 是實測的甜蜜點
# （8 秒 1920x960：ultrafast 70 MB／2.0 秒，veryfast 62.7 MB／6.0 秒，ffv1 108 MB／4.2 秒）。
# 60 秒 1080p 大約 300~500 MB，跑完就跟著暫存目錄一起刪掉。
LOSSLESS_ARGS = ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0"]
# 對齊自檢：無損重編跟參考比，分數低於這個值就代表兩邊沒有逐格對齊。
SELFCHECK_VMAF_MIN = 98.0
SELFCHECK_SSIM_MIN = 0.99


def _run(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="ignore", timeout=timeout)


def _ffmpeg_version() -> tuple[str, Optional[float]]:
    """回傳 (完整版本字串, 主版號.次版號)。認不出來就 (字串, None)。"""
    try:
        line = _run([FFMPEG, "-hide_banner", "-version"], timeout=30).stdout.splitlines()[0]
    except Exception as e:
        return f"(叫不動 ffmpeg：{e})", None
    m = re.search(r"version\s+n?(\d+)\.(\d+)", line)
    return line, (float(f"{m.group(1)}.{m.group(2)}") if m else None)


# ffmpeg 常常被別的軟體順便裝進 PATH（ImageMagick、OBS、Krita…）。
# 那些附帶的版本通常很舊，而且沒有人會去更新它。
_PIGGYBACK = ("imagemagick", "obs", "krita", "gimp", "handbrake", "shotcut",
              "audacity", "blender", "davinci", "moviepy", "anaconda")


def _all_ffmpeg_on_path() -> List[str]:
    """PATH 上所有的 ffmpeg，不只第一個 —— 用來看有沒有更新的版本沒被用到。"""
    found, seen = [], set()
    exts = os.environ.get("PATHEXT", ".EXE;.BAT").split(os.pathsep) if os.name == "nt" else [""]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for ext in exts:
            cand = Path(d) / ("ffmpeg" + ext.lower())
            try:
                if cand.is_file():
                    key = os.path.realpath(cand).lower()
                    if key not in seen:
                        seen.add(key)
                        found.append(str(cand))
            except OSError:
                continue
    return found


_ENC_OPT_CACHE: Dict[str, Dict[str, List[str]]] = {}


def _encoder_options(enc: str) -> Dict[str, List[str]]:
    """問 ffmpeg 這個編碼器實際接受哪些選項與哪些值。

    不能寫死：NVENC 的 p1~p7 preset、-multipass、-tune 都是 ffmpeg 5.0 之後
    才有的，4.x 只認 hq/hp/ll/medium…。硬寫死就是在舊版上整段轉碼失敗。
    """
    if enc in _ENC_OPT_CACHE:
        return _ENC_OPT_CACHE[enc]
    out: Dict[str, List[str]] = {}
    try:
        r = _run([FFMPEG, "-hide_banner", "-h", f"encoder={enc}"], timeout=30)
        text = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return out
    current = None
    for line in text.splitlines():
        st = line.strip()
        m = re.match(r"^-([A-Za-z0-9_.\-]+)\s+<", st)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current and line.startswith((" " * 5, "\t")):
            # ffmpeg 4.x 的常數列沒有數值那一欄（5.x 之後才有），
            # 數值寫成必填就會抓到空清單 —— app/media.py 目前正是這個 bug。
            vm = re.match(r"^([A-Za-z0-9_.\-]+)\s+(?:[-]?\d+\s+)?[EDAVS.]{6,}", st)
            if vm:
                out[current].append(vm.group(1))
                continue
        if st.startswith("-"):
            current = None
    _ENC_OPT_CACHE[enc] = out
    return out


def _has_opt(opts: Dict[str, List[str]], *names: str) -> Optional[str]:
    for n in names:
        if n in opts:
            return n
    return None


def _pick_value(opts: Dict[str, List[str]], option: str, prefer: List[str]) -> Optional[str]:
    """從列舉值裡挑第一個支援的。列舉抓不到或選項不存在都回 None。"""
    if option not in opts:
        return None
    for p in prefer:
        if p in (opts[option] or []):
            return p
    return None


def _try_args(enc: str, args: List[str]) -> bool:
    """把這組參數真的丟去編 10 格，成功才算能用。"""
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostats",
           "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
           "-frames:v", "10", "-pix_fmt", "yuv420p", "-c:v", enc] + args + \
          ["-f", "null", "-"]
    try:
        return _run(cmd, timeout=90).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _pick_verified(enc: str, opts: Dict[str, List[str]], option: str,
                   prefer: List[str]) -> Optional[str]:
    """先看列舉，列舉抓不到就一個一個真的試。

    **不要賭。**這支工具的前一版在 ffmpeg 4.2 上就是賭 `p4`，
    結果整組 NVENC 參數失敗（`Undefined constant ... 'p4'`）——
    而生產環境賭錯的下場是「硬體轉碼失敗、悄悄退回 CPU」。
    """
    v = _pick_value(opts, option, prefer)
    if v is not None:
        return v
    if option not in opts:
        return None
    for cand in prefer:
        if _try_args(enc, [f"-{option}", cand]):
            return cand
    return None


def _has_filter(name: str) -> bool:
    try:
        out = _run([FFMPEG, "-hide_banner", "-filters"], timeout=30).stdout
    except Exception:
        return False
    return re.search(rf"^\s*\S*\s+{re.escape(name)}\s", out, re.M) is not None


# --------------------------------------------------------------------------
# 環境探測
# --------------------------------------------------------------------------
HW_ENCODERS = ["h264_nvenc", "hevc_nvenc", "av1_nvenc",
               "h264_qsv", "hevc_qsv", "av1_qsv",
               "h264_amf", "hevc_amf", "h264_videotoolbox"]
SW_ENCODERS = ["libx264", "libx265", "libsvtav1", "libaom-av1"]


def probe_env() -> Dict[str, Any]:
    resolved = shutil.which(FFMPEG) or FFMPEG
    env: Dict[str, Any] = {"ffmpeg": resolved}
    env["version"], env["ver_num"] = _ffmpeg_version()
    if env["ver_num"] is None and "叫不動" in env["version"]:
        return env
    low = resolved.lower()
    env["piggyback"] = next((n for n in _PIGGYBACK if n in low), None)
    env["others"] = [p for p in _all_ffmpeg_on_path() if p.lower() != low]

    listed = _run([FFMPEG, "-hide_banner", "-encoders"], timeout=30).stdout
    env["listed"] = [e for e in HW_ENCODERS + SW_ENCODERS
                     if re.search(rf"\s{re.escape(e)}\s", listed)]

    # 列在清單裡不代表能用 —— 沒有顯卡的機器一樣會列出 h264_nvenc。
    # 真的丟一小段去編一次才算。
    env["usable"] = {}
    for enc in [e for e in HW_ENCODERS if e in env["listed"]]:
        ok, why, fps = _test_encoder(enc)
        env["usable"][enc] = {"ok": ok, "原因": why, "fps": fps}

    env["nvidia_smi"] = _nvidia_smi()
    env["vmaf"] = _has_filter("libvmaf")
    env["ssim"] = _has_filter("ssim")
    env["nvenc_prod"] = _prod_nvenc_options()
    return env


def _prod_nvenc_options() -> Optional[Dict[str, Optional[str]]]:
    """問 **app/media.py 自己那份解析器** 對 h264_nvenc 挑到什麼。

    這是 G-6 的直接驗證，而且驗的是生產程式碼而不是這支工具的副本：
    G-6 的成因是「常數列的數值欄在 4.x 沒有、在 5.x 有」，regex 已經改成
    數值欄可有可無 —— 但換版之後要再確認一次真的抓得到。
    挑到 `p5`／`hq`／`fullres` 就是抓到了；挑到 `medium` 或 None 就是又壞了。
    """
    if _media is None:
        return None
    try:
        return {
            "preset": _media.pick_option("h264_nvenc", "preset",
                                         ["p5", "p4", "slow", "hq", "medium"]),
            "rc": _media.pick_option("h264_nvenc", "rc", ["vbr", "vbr_hq", "constqp"]),
            "tune": _media.pick_option("h264_nvenc", "tune", ["hq"]),
            "multipass": _media.pick_option("h264_nvenc", "multipass",
                                            ["fullres", "qres"]),
        }
    except Exception:
        return None


def _test_encoder(enc: str) -> tuple[bool, Optional[str], Optional[float]]:
    """實際編 60 格 1080p，回傳 (可用, 失敗原因, fps)。"""
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostats",
           "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
           "-frames:v", "60", "-pix_fmt", "yuv420p", "-c:v", enc, "-f", "null", "-"]
    t0 = time.time()
    try:
        r = _run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "逾時", None
    dt = time.time() - t0
    if r.returncode != 0:
        line = next((l for l in (r.stderr or "").splitlines() if l.strip()), "")
        return False, line[:160], None
    return True, None, round(60 / dt, 1) if dt else None


def _nvidia_smi() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = _run([exe, "--query-gpu=name,driver_version,memory.total",
                  "--format=csv,noheader"], timeout=30)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# 單一檔案的實測
# --------------------------------------------------------------------------
BROWSER_VIDEO = {"h264", "avc1", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}


def probe_file(path: str) -> Dict[str, Any]:
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = _run(cmd, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffprobe 失敗")[:300])
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = d.get("format", {})
    info = {
        "檔名": Path(path).name,
        "容器": fmt.get("format_name"),
        "視訊": v.get("codec_name"),
        "音訊": a.get("codec_name"),
        "解析度": f'{v.get("width")}x{v.get("height")}',
        "位元深度": v.get("bits_per_raw_sample") or (
            10 if "10" in str(v.get("pix_fmt") or "") else 8),
        "pix_fmt": v.get("pix_fmt"),
        "色彩轉換": v.get("color_transfer"),
        "總位元率_kbps": round(float(fmt.get("bit_rate") or 0) / 1000) or None,
        "長度_秒": round(float(fmt.get("duration") or 0), 1) or None,
    }
    # 能不能不重新編碼就給瀏覽器（remux / direct stream）
    vc = (info["視訊"] or "").lower()
    ac = (info["音訊"] or "").lower()
    info["視訊可直傳"] = vc in BROWSER_VIDEO
    info["音訊可直傳"] = ac in BROWSER_AUDIO
    info["remux 可行"] = info["視訊可直傳"]      # 音訊不合就只轉音訊，視訊照抄
    return info


def _pipeline_filters(finfo: Dict[str, Any], height: int) -> List[str]:
    """生產環境會套的濾鏡鏈（縮放 ＋ HDR→SDR）。

    直接呼叫 media.build_video_filters —— 這樣量到的速度就是實際播放時的速度，
    而不是「把 4K HDR 原尺寸硬編」的速度（那會慢很多，量了會誤導）。
    """
    try:
        w, h = (int(x) for x in str(finfo.get("解析度") or "0x0").split("x"))
    except Exception:
        w = h = 0
    is_hdr = str(finfo.get("色彩轉換") or "").lower() in ("smpte2084", "arib-std-b67")
    if _media is not None:
        try:
            vf, _out_h = _media.build_video_filters(w or None, h or None,
                                                    height or None, is_hdr)
            return list(vf or [])
        except Exception:
            pass
    if height and h and h > height:
        return [f"scale=-2:{height}"]
    return []


def sample_start(finfo: Dict[str, Any]) -> float:
    """取樣起點。從片子中間拿，避開片頭黑畫面與 logo。"""
    dur = finfo.get("長度_秒") or 0
    if dur > CLIP_START + CLIP_SECONDS + 10:
        return CLIP_START
    return max(0.0, dur / 3)


def build_reference(src: str, start: float, work: Path,
                    vf: Optional[List[str]]) -> tuple[Optional[Path], Optional[str]]:
    """把參考片段**無損重編**成一個檔案，之後所有變體都跟它比。

    這是這支工具踩過的第二個、也是最貴的一個雷，值得寫清楚：

    * 第一版拿 `-c copy` 切一段當參考 —— 帶 Dolby Vision 的 4K mkv 時間戳是負的，
      切出來解不開，六組全部量到 24 kbps／SSIM 1.0000。
    * 第二版改成「參考直接從原檔以 -ss/-t 取樣」，DTS 的問題確實沒了，
      **但是換來一個更難發現的錯**：`-ss` 落在兩格之間時，解碼端會**把第一格複製一份**
      再往下吐，於是參考變成 [F0, F0, F1, F2…] 而編碼那邊是 [F0, F1, F2…] ——
      整段錯開一格，每一格都在跟前一格比。
      實測：把片段**無損**重編（`-qp 0`，本來該是 100 分）用那個方法量出 **VMAF 17.2**。
      分數不會爆掉、不會報錯，只是一路偏低而且對位元率幾乎沒有反應
      （2.8→12 Mbps 只動 1.0 分），看起來就像「這個編碼器就是爛」。

    正解是兩邊都不要在比對時 seek：先用同一條命令、同一條濾鏡鏈把參考**解碼後重編**
    成無損檔（重編過所以沒有 DTS 問題，無損所以畫素不變），
    之後每個變體都跟這個檔案比 —— 兩邊都是從第一格開始，位置對位置。

    * 第三個雷藏在第二個底下：參考落地成檔案之後**還是量到 72 分**，
      而那兩個檔案的影格逐格 md5 完全相同、格數也一樣。原因是
      **時基**：參考寫成 `.mkv` 時 matroska 把時間戳四捨五入到毫秒
      （0.042、0.083…），變體寫成 `.mp4` 是精確的 1/24（0.041667、0.083333…），
      而 `libvmaf` 是**按時間戳配對影格**的，累積的捨入誤差會讓它配錯格。
      所以參考的容器要跟變體一樣。

    實測驗證（同一段、同一個 -ss）：

    | 做法 | 無損重編應得 100 | CRF 21 |
    |---|---|---|
    | 比對時才 seek 參考（第二版） | **22.4** | 22.4 |
    | 參考落地成 .mkv、變體是 .mp4 | **72.0** | — |
    | 參考落地成 .mp4、變體也是 .mp4 | **99.99** | 91.2 |

    這就是為什麼變體清單的第一列是「對齊自檢」：無損重編只能是 ~100，
    不是的話整張表都在跟錯的影格比 —— 而這種錯不會報錯，只會讓分數一路偏低。
    """
    # **副檔名一定要跟變體一樣（.mp4）。**見 docstring 第三點：
    # matroska 把時間戳四捨五入到毫秒，mp4 存的是精確的 1/24 ——
    # libvmaf 是按時間戳配對影格的，兩邊的時基不同就會配錯。
    dst = work / "ref-lossless.mp4"
    cmd = ([FFMPEG, "-hide_banner", "-v", "error", "-nostats", "-y",
            "-ss", f"{start:.3f}", "-t", f"{CLIP_SECONDS:.3f}", "-i", src,
            "-an", "-sn", "-dn"]
           + (["-vf", ",".join(vf)] if vf else [])
           + LOSSLESS_ARGS + [str(dst)])
    try:
        r = _run(cmd, timeout=2400)
    except subprocess.TimeoutExpired:
        return None, "逾時"
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        line = next((l for l in (r.stderr or "").splitlines() if l.strip()), "")
        return None, line[:200] or "產不出參考片段"
    return dst, None


def _score(ref: Path, test: Path, use_vmaf: bool) -> Optional[str]:
    """兩個**檔案**逐格比對。這裡刻意不 seek、也不套濾鏡。

    參考已經是 build_reference() 產的無損檔，跟變體走過同一條濾鏡鏈、
    同一個 -ss/-t，所以兩邊的第一格就是同一格。比對時再 seek 一次
    就是上面那個雷的來源。
    """
    def build(metric: str) -> List[str]:
        return [FFMPEG, "-hide_banner", "-i", str(test), "-i", str(ref),
                "-lavfi", metric, "-f", "null", "-"]

    if use_vmaf:
        r = _run(build(f"libvmaf=n_threads={VMAF_THREADS}"), timeout=1800)
        m = re.search(r"VMAF score:\s*([\d.]+)", (r.stderr or "") + (r.stdout or ""))
        if m:
            return f"VMAF {float(m.group(1)):.1f}"
    r = _run(build("ssim"), timeout=1800)
    m = re.search(r"All:([\d.]+)", (r.stderr or "") + (r.stdout or ""))
    return f"SSIM {float(m.group(1)):.4f}" if m else None


def _x264(preset: str, crf: int, cap_kbps: Optional[int]) -> List[str]:
    """x264 的生產形狀：CRF ＋（可選的）VBV 上限。"""
    args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
    if cap_kbps:
        args += ["-maxrate", f"{cap_kbps}k", "-bufsize", f"{cap_kbps * 2}k"]
    return args


def variants(env: Dict[str, Any], crf: int, cap_kbps: int,
             vf: Optional[List[str]] = None, *,
             nvenc_cq: Optional[int] = None,
             x264_preset: Optional[str] = None) -> List[Dict[str, Any]]:
    """要比的設定組合。第一組刻意就是目前 .env 的設定。

    CRF、上限、NVENC 的 cq、x264 的 preset 全部從 `.env` 來（見檔頭的
    DEFAULT_* ）。工具自己寫一份數字就會量錯對象 —— 上一輪 `--cap` 硬寫 2800、
    而 `.env` 已經是 12000，那一列標著「現況」卻不是現況。

    x264 那組固定拿「生產用的 preset」對「慢一級」：決策 8（preset 維持
    veryfast）當初是用 SSIM 判的，而 SSIM 在這個區間會飽和，
    所以有了 VMAF 之後這一項必須重新看一次，不能沿用。
    """
    nvenc_cq = DEFAULT_NVENC_CQ if nvenc_cq is None else nvenc_cq
    x264_preset = x264_preset or DEFAULT_X264_PRESET
    slower = "medium" if x264_preset != "medium" else "slow"
    out = [
        # **這一列是整張表的信任基礎。**它是無損重編，跟無損參考比只能是 ~100；
        # 不是的話就代表兩邊沒有逐格對齊，底下每一個數字都是在跟錯的影格比。
        # 上一輪就是這樣：所有分數看起來只是「偏低」，沒有任何東西會報錯。
        {"名稱": "對齊自檢：無損重編（不是 ~100 就代表整張表都不能信）",
         "args": list(LOSSLESS_ARGS), "自檢": True},
        {"名稱": f"現況：{x264_preset} + 上限 {cap_kbps}k",
         "args": _x264(x264_preset, crf, cap_kbps)},
        {"名稱": f"只把 preset 換成 {slower}（上限不變）",
         "args": _x264(slower, crf, cap_kbps)},
        {"名稱": f"{x264_preset}，不設位元率上限",
         "args": _x264(x264_preset, crf, None)},
        {"名稱": f"{slower}，不設位元率上限",
         "args": _x264(slower, crf, None)},
    ]
    usable = {k for k, v in (env.get("usable") or {}).items() if v.get("ok")}
    if "h264_nvenc" in usable:
        out += _nvenc_variants("h264_nvenc", nvenc_cq, vf)
    if "h264_qsv" in usable:
        out.append({"名稱": "QSV（global_quality）",
                    "args": ["-c:v", "h264_qsv", "-global_quality", str(crf),
                             "-preset", "medium"]})
    return out


def _nvenc_tuned_args(enc: str, cq: Optional[int],
                      target_kbps: Optional[int] = None) -> Optional[List[str]]:
    """這一版 ffmpeg 能給 NVENC 的最好參數。

    參數一律從 `ffmpeg -h encoder=` 問出來再組 —— p1~p7、-multipass、-tune
    都是 5.0 之後才有的，寫死會在舊版上整組失敗，而失敗的原因會被
    「硬體轉碼失敗就退回 CPU」那條路徑吃掉，看起來只是變慢。

    `target_kbps` 有值就是 ABR（綁住碼率，給位元率階梯用），
    沒有值就是「品質導向 ＋ 不限碼率」（-cq ＋ -b:v 0）。
    """
    opts = _encoder_options(enc)
    preset = _pick_verified(enc, opts, "preset", ["p6", "p5", "slow", "hq", "medium"])
    rc = _pick_verified(enc, opts, "rc", ["vbr_hq", "vbr", "constqp"])
    head = ["-c:v", enc]
    if preset:
        head += ["-preset", preset]
    if _pick_value(opts, "tune", ["hq"]):
        head += ["-tune", "hq"]
    if rc:
        head += ["-rc", rc]

    if target_kbps:
        rate = ["-b:v", f"{target_kbps}k",
                "-maxrate", f"{int(target_kbps * 1.05)}k",
                "-bufsize", f"{target_kbps * 2}k"]
    else:
        rate = ["-cq", str(cq), "-b:v", "0"]

    extras: List[str] = ["-bf", "3"]
    mp = _pick_value(opts, "multipass", ["fullres", "qres"])
    if mp:
        extras += ["-multipass", mp]
    elif "2pass" in opts:
        extras += ["-2pass", "1"]
    la = _has_opt(opts, "rc-lookahead", "rc_lookahead")
    if la:
        extras += [f"-{la}", "32"]
    aq = _has_opt(opts, "spatial-aq", "spatial_aq")
    if aq:
        extras += [f"-{aq}", "1"]
        st = _has_opt(opts, "aq-strength", "aq_strength")
        if st:
            extras += [f"-{st}", "8"]

    # 整組真的丟去編一次才算；某個 flag 不吃就往回退，不要整組失敗。
    for tail in (rate + extras, rate + ["-bf", "3"], rate):
        if _try_args(enc, tail):
            return head + tail
    return None


def _nvenc_variants(enc: str, nvenc_cq: int,
                    vf: Optional[List[str]]) -> List[Dict[str, Any]]:
    """NVENC 的兩組：程式現在實際會下的參數，以及這版 ffmpeg 能給的最好參數。

    **cq 用 `NVENC_CQ` 而不是 `TRANSCODE_CRF`**（決策 15）：實測 `-cq 21`
    讓 NVENC 吐出 10.2 Mbps、幾乎沒有壓縮，硬體編碼有自己的一把尺。
    """
    opts = _encoder_options(enc)
    # media.py 的 tier 順序：preset 先試 p5、p4，再退 slow/hq/medium
    cur_preset = _pick_verified(enc, opts, "preset", ["p5", "p4", "slow", "hq", "medium"])
    cur_rc = _pick_verified(enc, opts, "rc", ["vbr", "vbr_hq", "constqp"])
    cur = ["-c:v", enc]
    if cur_preset:
        cur += ["-preset", cur_preset]
    if cur_rc:
        cur += ["-rc", cur_rc]
    cur += ["-cq", str(nvenc_cq), "-b:v", "0", "-bf", "0"]

    tuned = _nvenc_tuned_args(enc, nvenc_cq) or cur

    out = [
        {"名稱": f"NVENC 現況（{' '.join(cur[2:]) or '預設值'}）", "args": cur},
        {"名稱": f"NVENC 這版能給的最好（{' '.join(tuned[2:])}）", "args": tuned},
    ]

    # 同碼率對照移到「位元率階梯」那一段（ladder_variants）——
    # 一個碼率點答不出「開到多少就夠」，要一整條曲線。

    # 硬體解碼只有在**整條鏈都留在 GPU** 時才是加速。混合模式（GPU 解 → 下載到
    # CPU 濾鏡 → 上傳回 GPU 編）每一幀要過兩次 PCIe，實測反而更慢
    # （1080p 11.6x → 9.7x、4K 0.44x → 0.34x）。所以只在沒有 CPU 濾鏡時才測。
    droppable = not vf or all(f.startswith("format=") for f in vf)
    if _cuda_decode_ok() and droppable:
        out.append({"名稱": "NVENC 最好參數 ＋ CUDA 全 GPU（解碼與編碼都不落地）",
                    "pre": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"],
                    "vf_override": [], "args": tuned})
    return out


def ladder_variants(env: Dict[str, Any], caps: List[int],
                    vf: Optional[List[str]] = None, *,
                    x264_preset: Optional[str] = None) -> List[Dict[str, Any]]:
    """位元率階梯：**綁住碼率**，兩個編碼器各跑同一條曲線。

    為什麼不是「CRF ＋ 不同上限」：上限只在它咬得到的時候才有作用。
    CRF 21 自然吐 3.2 Mbps 的片段，把上限從 4000 開到 12000 完全不會改變輸出 ——
    量到的會是一條平線，而那不是「12 Mbps 夠不夠」的答案。
    改成 ABR（`-b:v` 綁目標）之後，每一級才真的花掉那個碼率，
    於是這一張表同時回答兩件事：

    * 同一個碼率下 x264 與 NVENC 誰好（→ 各該用在什麼情況）
    * VMAF 從哪一級開始不再上升（→ 上限開到多少就夠）
    """
    x264_preset = x264_preset or DEFAULT_X264_PRESET
    out: List[Dict[str, Any]] = []
    for cap in caps:
        out.append({
            "名稱": f"階梯 x264 {x264_preset} 目標 {cap}k",
            "args": ["-c:v", "libx264", "-preset", x264_preset,
                     "-b:v", f"{cap}k", "-maxrate", f"{int(cap * 1.05)}k",
                     "-bufsize", f"{cap * 2}k"],
            "階梯": ("x264", cap),
        })
    usable = {k for k, v in (env.get("usable") or {}).items() if v.get("ok")}
    if "h264_nvenc" in usable:
        for cap in caps:
            args = _nvenc_tuned_args("h264_nvenc", None, target_kbps=cap)
            if args:
                out.append({"名稱": f"階梯 NVENC 目標 {cap}k",
                            "args": args, "階梯": ("nvenc", cap)})
    return out


def quality_ladder_variants(env: Dict[str, Any], crfs: List[int], cqs: List[int],
                            vf: Optional[List[str]] = None, *,
                            x264_preset: Optional[str] = None) -> List[Dict[str, Any]]:
    """品質階梯：**不設碼率上限**，只動品質旋鈕。

    跟位元率階梯是兩個不同的問題：
    * 位元率階梯（綁碼率）→ 上限開到多少就夠、同碼率誰的畫質好
    * 品質階梯（不綁碼率）→ `TRANSCODE_CRF`／`NVENC_CQ` 該設多少

    刻意不設上限：要看的就是「這個品質旋鈕自己想花多少碼率」。
    上限在這裡只會把答案切掉。
    """
    x264_preset = x264_preset or DEFAULT_X264_PRESET
    out: List[Dict[str, Any]] = []
    for crf in crfs:
        out.append({"名稱": f"品質 x264 {x264_preset} crf {crf}（不設上限）",
                    "args": _x264(x264_preset, crf, None),
                    "品質": ("x264", crf)})
    usable = {k for k, v in (env.get("usable") or {}).items() if v.get("ok")}
    if "h264_nvenc" in usable:
        for cq in cqs:
            args = _nvenc_tuned_args("h264_nvenc", cq)
            if args:
                out.append({"名稱": f"品質 NVENC cq {cq}（不設上限）",
                            "args": args, "品質": ("nvenc", cq)})
    return out


def _cuda_decode_ok() -> bool:
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-nostats", "-hwaccel", "cuda",
           "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-frames:v", "5",
           "-f", "null", "-"]
    try:
        return _run(cmd, timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def run_variants(src: str, start: float, work: Path, vars_: List[Dict[str, Any]],
                 use_vmaf: bool, vf: Optional[List[str]] = None,
                 ref: Optional[Path] = None) -> List[Dict[str, Any]]:
    rows = []
    for i, v in enumerate(vars_):
        dst = work / f"v{i}.mp4"
        # vf_override = [] 代表這組刻意不套 CPU 濾鏡（例如整條鏈留在 GPU 上）
        chain = v["vf_override"] if "vf_override" in v else vf
        cmd = ([FFMPEG, "-hide_banner", "-v", "error", "-nostats", "-y"]
               + list(v.get("pre") or [])
               + ["-ss", f"{start:.3f}", "-t", f"{CLIP_SECONDS:.3f}", "-i", src, "-an"]
               + (["-vf", ",".join(chain)] if chain else [])
               + v["args"] + [str(dst)])
        t0 = time.time()
        try:
            r = _run(cmd, timeout=2400)
        except subprocess.TimeoutExpired:
            rows.append({"設定": v["名稱"], "階梯": v.get("階梯"), "品質": v.get("品質"),
                         "自檢": v.get("自檢"), "結果": "逾時"})
            continue
        dt = time.time() - t0
        if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            line = next((l for l in (r.stderr or "").splitlines() if l.strip()), "")
            rows.append({"設定": v["名稱"], "階梯": v.get("階梯"), "品質": v.get("品質"),
                         "自檢": v.get("自檢"), "結果": "失敗", "錯誤": line[:160]})
            continue
        size = dst.stat().st_size
        kbps = round(size * 8 / CLIP_SECONDS / 1000)
        rows.append({
            "設定": v["名稱"],
            "階梯": v.get("階梯"),
            "品質": v.get("品質"),
            "自檢": v.get("自檢"),
            "位元率_kbps": kbps,
            "耗時_秒": round(dt, 1),
            "倍速": round(CLIP_SECONDS / dt, 2) if dt else None,
            # 位元率低到不合理就是解碼出了問題，分數會是假的（SSIM 1.0000）
            "分數": ("（片段解不出畫面，數字無效）" if kbps < 100
                    else (_score(ref, dst, use_vmaf) if ref else "（沒有參考片段）")),
        })
        dst.unlink(missing_ok=True)
    return rows


# --------------------------------------------------------------------------
# Direct Stream（remux）的可行性檢查
# --------------------------------------------------------------------------
def remux_check(path: str, finfo: Dict[str, Any], seg_seconds: int = 6,
                window: float = 180.0) -> Dict[str, Any]:
    """remux 動工前該先知道的兩件事。

    1. **keyframe 間距。**`-c:v copy` 沒辦法在任意位置切 —— 段落只能從
       keyframe 開始。所以「硬切 6 秒」會切在非 keyframe 上，那一段開頭就是壞畫面。
       間距 <= 分段長度才有可能對齊；不然就得改走 fMP4 + byte-range。
    2. **時間戳。**mkv 的 DTS 不保證單調，`-c copy` 出去常常噴
       Non-monotonous DTS，播起來就是掉幀或音影不同步。要先確認會不會發生。
    """
    out: Dict[str, Any] = {"可行": bool(finfo.get("remux 可行"))}
    start = CLIP_START if (finfo.get("長度_秒") or 0) > CLIP_START + window else 0.0

    # --- keyframe 間距 ---
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-read_intervals", f"{start:.0f}%+{window:.0f}",
           "-show_entries", "frame=pkt_pts_time,pts_time,best_effort_timestamp_time",
           "-of", "json", path]
    times: List[float] = []
    try:
        r = _run(cmd, timeout=300)
        for fr in (json.loads(r.stdout or "{}").get("frames") or []):
            for k in ("pts_time", "pkt_pts_time", "best_effort_timestamp_time"):
                v = fr.get(k)
                if v not in (None, "N/A"):
                    try:
                        times.append(float(v))
                    except ValueError:
                        pass
                    break
    except Exception as e:
        out["keyframe 錯誤"] = str(e)[:160]
    if len(times) >= 3:
        times.sort()
        gaps = [round(b - a, 3) for a, b in zip(times, times[1:]) if b > a]
        if gaps:
            gaps_sorted = sorted(gaps)
            out["keyframe 數"] = len(times)
            out["間距_最短"] = gaps_sorted[0]
            out["間距_中位"] = gaps_sorted[len(gaps_sorted) // 2]
            out["間距_最長"] = gaps_sorted[-1]
            out["可對齊分段"] = gaps_sorted[-1] <= seg_seconds + 0.05
    # --- 試切一段 ---
    with tempfile.TemporaryDirectory(prefix="filmax_remux_") as tmp:
        dst = Path(tmp) / "seg.ts"
        need_audio_transcode = not finfo.get("音訊可直傳")
        acodec = ["-c:a", "aac", "-b:a", "192k"] if need_audio_transcode else ["-c:a", "copy"]
        cmd = ([FFMPEG, "-hide_banner", "-v", "warning", "-nostats", "-y",
                "-ss", f"{start:.3f}", "-t", str(seg_seconds), "-i", path,
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"] + acodec +
               ["-f", "mpegts", str(dst)])
        t0 = time.time()
        try:
            r = _run(cmd, timeout=300)
            dt = time.time() - t0
            out["試切"] = "成功" if r.returncode == 0 and dst.exists() else "失敗"
            out["試切_耗時"] = round(dt, 2)
            if dst.exists():
                out["試切_大小_MB"] = round(dst.stat().st_size / 1048576, 1)
            warn = [l.strip() for l in (r.stderr or "").splitlines()
                    if re.search(r"non-?monoton|dts|timestamp|invalid", l, re.I)]
            out["警告"] = warn[:4]
            out["音訊處理"] = "轉成 AAC" if need_audio_transcode else "照抄"
        except subprocess.TimeoutExpired:
            out["試切"] = "逾時"
    return out


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------
def _p(s: str = "") -> None:
    print(s, flush=True)


def report(env: Dict[str, Any], finfo: Optional[Dict[str, Any]],
           rows: Optional[List[Dict[str, Any]]], crf: int, cap: int,
           rmx: Optional[Dict[str, Any]] = None,
           kfs: Optional[Dict[str, Any]] = None) -> None:
    if not env:                     # batch 模式下環境只印一次
        _report_file(finfo, rows, crf, cap, rmx, kfs)
        return
    _p("=" * 68)
    _p("FilmaxWeb 影片畫質量測")
    _p("=" * 68)
    _p(f"ffmpeg   : {env.get('ffmpeg')}")
    _p(f"版本     : {env.get('version')}")
    _p(f"顯卡     : {env.get('nvidia_smi') or '（nvidia-smi 問不到，可能沒有 NVIDIA 顯卡）'}")
    _p(f"客觀評分 : {'VMAF 可用' if env.get('vmaf') else ('只有 SSIM' if env.get('ssim') else '兩個都沒有')}")

    warns: List[str] = []
    ver = env.get("ver_num")
    if ver is not None and ver < 5.0:
        warns.append(
            f"這個 ffmpeg 是 {ver:g} 版（2020 年前後）。NVENC 的 p1~p7 preset、-tune hq、"
            "-multipass 都是 5.0 之後才有的，所以現在拿不到 NVENC 的畫質上限；"
            "libsvtav1 與 libvmaf 通常也是新版才附。換一個新版 ffmpeg 是這裡投報率最高的一步。")
    if env.get("piggyback"):
        warns.append(
            f"這個 ffmpeg 是 {env['piggyback']} 順便裝進 PATH 的那一份，不是獨立安裝的 ffmpeg。"
            "那種附屬版本沒有人會更新，也常常少編譯器與濾鏡。")
    if env.get("others"):
        warns.append("PATH 上還有其他 ffmpeg，目前沒被用到：\n      " +
                     "\n      ".join(env["others"]) +
                     "\n      （要指定用哪一個，在 .env 設 FFMPEG_PATH 的完整路徑）")
    # NVENC 的 API 版本檢查失敗，是「換了 ffmpeg 才壞掉」最典型的一種 ——
    # 而且訊息本身不會告訴你該怎麼辦。實際遇過：ffmpeg 9.0 是拿
    # Video Codec SDK 13.1 的標頭編的（要驅動 610 以上），而 GTX 1070 是 Pascal，
    # R580 是 NVIDIA 給 Maxwell／Pascal／Volta 的最後一個驅動分支 —— 610 永遠不會來。
    apifail = [f"{enc}：{st.get('原因')}"
               for enc, st in (env.get("usable") or {}).items()
               if not st.get("ok") and re.search(r"required nvenc API version",
                                                 str(st.get("原因") or ""), re.I)]
    if apifail:
        warns.append(
            "NVENC 失敗在 **API 版本**，不是顯卡也不是驅動裝壞了："
            + "；".join(apifail) +
            "。這個 ffmpeg 是拿比驅動更新的 nv-codec-headers 編的。"
            "驅動能不能再往上要看世代 —— Maxwell／Pascal／Volta 停在 R580，"
            "再往上的分支不會有。解法是換一個用舊標頭編的 build："
            "BtbN 每個 ffmpeg 分支釘不同的標頭版本（8.0／8.1 用 SDK 13.0、"
            "9.0 與 master 用 SDK 13.1），所以退到 n8.1 就能用，"
            "而 libvmaf、libsvtav1、p1~p7、-tune hq、-multipass 一個都不會少。")

    gpu = (env.get("nvidia_smi") or "")
    if re.search(r"GTX\s*(9\d0|10\d0|TITAN X)", gpu, re.I):
        warns.append(
            "這張是 Pascal／Maxwell 世代的 NVENC。它很快，但同位元率的畫質明顯不如 "
            "Turing（RTX 20）之後的世代 —— 必須重新編碼時，CPU 的 x264 medium 很可能比它好看，"
            "NVENC 的價值在於同時服務多人時的速度。消費卡另外有並行編碼工作階段的上限"
            "（依驅動版本大約 2～5 條）。")
    if warns:
        _p("")
        _p("注意")
        for w in warns:
            _p(f"  ! {w}")
    _p("")
    _p("硬體編碼器（列出 ≠ 能用，下面是實際編過一次的結果）")
    if not env.get("usable"):
        _p("  這個 ffmpeg 沒有列出任何硬體編碼器")
    for enc, st in (env.get("usable") or {}).items():
        if st["ok"]:
            _p(f"  ✓ {enc:20} 1080p 約 {st['fps']} fps")
        else:
            _p(f"  ✗ {enc:20} {st['原因']}")
    sw = [e for e in SW_ENCODERS if e in (env.get("listed") or [])]
    _p(f"  軟體編碼器：{', '.join(sw) or '（只有內建的）'}")

    prod = env.get("nvenc_prod")
    if prod is not None:
        _p("")
        _p("G-6 檢查（app/media.py 自己的解析器對 h264_nvenc 挑到什麼）")
        _p("  " + "　".join(f"{k}: {v if v else '（挑不到）'}" for k, v in prod.items()))
        pre = prod.get("preset") or ""
        if pre.startswith("p"):
            _p("  → 挑到 p 系列 preset，代表常數列的解析在這一版是好的（G-6 沒有回頭）。")
        else:
            _p("  ! 沒有挑到 p1~p7。要嘛這個 ffmpeg 還是 4.x，")
            _p("    要嘛 -h encoder= 的輸出格式又變了、regex 需要再修一次（見 G-6）。")
        if not prod.get("tune") and not prod.get("multipass"):
            _p("  ! -tune 與 -multipass 都挑不到 —— 這兩個要 5.0 以上才有。")

    _report_file(finfo, rows, crf, cap, rmx, kfs)
    if rows is None and finfo is None:
        _p("")
        _p("下一步：把一部片子拖到 測試畫質.bat 上（或加上檔案路徑當參數），")
        _p("        才會實測「目前設定 vs 建議設定」差多少。")
    _p("")
    _p("=" * 68)


def _report_file(finfo, rows, crf: int, cap: int, rmx, kfs=None) -> None:
    if finfo:
        _p("")
        _p("-" * 68)
        _p("片源")
        _p("-" * 68)
        for k in ("檔名", "容器", "視訊", "音訊", "解析度", "位元深度", "pix_fmt",
                  "色彩轉換", "總位元率_kbps", "長度_秒", "取樣起點_秒", "濾鏡鏈",
                  "參考片段_MB"):
            _p(f"  {k:12}: {finfo.get(k)}")
        if finfo["remux 可行"]:
            extra = "" if finfo["音訊可直傳"] else "（音訊要轉成 AAC，視訊照抄）"
            _p(f"  → 這部片可以 remux 直傳，不必重新編碼{extra}。畫質 = 原檔。")
        else:
            _p(f"  → 視訊是 {finfo['視訊']}，瀏覽器不一定吃，可能真的需要重新編碼。")

    if rmx:
        _p("")
        _p("-" * 68)
        _p("Direct Stream（remux）可行性")
        _p("-" * 68)
        if not rmx.get("可行"):
            _p("  這部片的視訊編碼瀏覽器不一定吃，remux 不適用（要重新編碼）。")
        for k in ("keyframe 數", "間距_最短", "間距_中位", "間距_最長",
                  "可對齊分段", "音訊處理", "試切", "試切_耗時", "試切_大小_MB"):
            if k in rmx:
                _p(f"  {k:12}: {rmx[k]}")
        if rmx.get("警告"):
            _p("  警告        :")
            for w in rmx["警告"]:
                _p(f"      {w[:150]}")
        elif "試切" in rmx:
            _p("  警告        : 沒有時間戳相關的警告")
        if rmx.get("可對齊分段") is False:
            _p("  → keyframe 間距超過分段長度，硬切會切在非 keyframe 上。")
            _p("    這種片源要走 fMP4 + byte-range，或按 keyframe 決定段長。")
        elif rmx.get("可對齊分段"):
            _p("  → keyframe 間距在分段長度之內，照 keyframe 切就能對齊。")

    if kfs:
        _report_keyframes(kfs)

    lad = [r for r in (rows or []) if r.get("階梯")]
    qua = [r for r in (rows or []) if r.get("品質")]
    chk = [r for r in (rows or []) if r.get("自檢")]
    rows = [r for r in (rows or []) if not r.get("階梯") and not r.get("品質")
            and not r.get("自檢")]
    if chk:
        _report_selfcheck(chk[0])
    if rows:
        _p("")
        _p("-" * 68)
        _p(f"設定對照（CRF {crf}、{CLIP_SECONDS:.0f} 秒取樣、分數是與原片段比對）")
        _p("-" * 68)
        for r in rows:
            if r.get("結果"):
                _p(f"  {r['設定']}")
                _p(f"      {r['結果']}：{r.get('錯誤', '')}")
                continue
            _p(f"  {r['設定']}")
            _p(f"      位元率 {r['位元率_kbps']:>6} kbps ｜ {r['倍速']}x 即時 ｜ {r['分數'] or '（沒量到）'}")
        _p("")
        _p("怎麼讀這張表：")
        _p("  * 分數越高越接近原檔。VMAF 95 以上一般看不出差異，90 以下開始明顯。")
        _p("  * 同一個 CRF 在不同 preset 下畫質並不相同，所以不要只比位元率。")
        _p("  * 「不設上限」那兩組如果位元率遠高於上限，就代表上限正在砍畫質。")
        _p("  * 倍速要 > 1 才追得上即時播放；預轉還要更多餘裕。")
        _p("  * NVENC 的 -cq 與 x264 的 -crf 不是同一把尺 —— 同樣寫 21，NVENC 的位元率")
        _p("    可能是 x264 的三倍。要比效率看下面的「位元率階梯」，那裡碼率是綁住的。")
        _p("  * 取樣越短，實測位元率越容易「超出上限」—— VBV 一開始有一整個緩衝可以花，")
        _p("    要跑久一點才會收斂到上限。要看真實平均值就把 --seconds 開大。")

    if lad:
        _report_ladder(lad)
    if qua:
        _report_quality(qua)


def _selfcheck_ok(row: Dict[str, Any]) -> Optional[bool]:
    """自檢過了沒有。判不出來（沒分數、失敗）回 None。"""
    sc = row.get("分數")
    if not isinstance(sc, str):
        return None
    try:
        if sc.startswith("VMAF "):
            return float(sc.split()[1]) >= SELFCHECK_VMAF_MIN
        if sc.startswith("SSIM "):
            return float(sc.split()[1]) >= SELFCHECK_SSIM_MIN
    except (IndexError, ValueError):
        return None
    return None


def _report_selfcheck(row: Dict[str, Any]) -> None:
    """對齊自檢。這一段要印在所有數字前面 —— 它決定後面那些數字算不算數。"""
    _p("")
    _p("-" * 68)
    _p("對齊自檢（無損重編 vs 無損參考，應該 ≈ 100）")
    _p("-" * 68)
    if row.get("結果"):
        _p(f"  {row['結果']}：{row.get('錯誤', '')}")
        _p("  ! 自檢這一組都跑不起來，底下的分數請一律當作不可信。")
        return
    _p(f"  位元率 {row['位元率_kbps']:>6} kbps ｜ {row['倍速']}x ｜ {row['分數'] or '（沒量到）'}")
    ok = _selfcheck_ok(row)
    if ok:
        _p("  → 通過。參考片段與變體是逐格對齊的，底下的分數可以採信。")
    elif ok is None:
        _p("  ! 判不出來（沒有拿到分數）。底下的分數請當作不可信。")
    else:
        _p("  ! **沒有通過。無損重編本來只能是 ~100，量到的卻不是** ——")
        _p("    代表參考與變體沒有逐格對齊，底下每一列都是在跟錯的影格比。")
        _p("    這種錯不會報錯、不會爆掉，只會讓所有分數一路偏低而且對位元率沒反應。")
        _p("    **不要拿底下任何數字下結論**，先把對齊修好再跑一次。")


def _vmaf_of(row: Dict[str, Any]) -> Optional[float]:
    """從分數字串取出 VMAF 數值。SSIM、失敗、數字無效都回 None。"""
    sc = row.get("分數")
    if not isinstance(sc, str) or not sc.startswith("VMAF "):
        return None
    if (row.get("位元率_kbps") or 0) < 100:      # 片段解不出畫面，分數是假的
        return None
    try:
        return float(sc.split()[1])
    except (IndexError, ValueError):
        return None


ENC_LABEL = {"x264": "x264（CPU）", "nvenc": "NVENC（GPU）"}
KNOB_LABEL = {"x264": "TRANSCODE_CRF", "nvenc": "NVENC_CQ"}


def keyframe_scan(path: str, seg_seconds: int,
                  duration: Optional[float] = None) -> Dict[str, Any]:
    """全檔 keyframe 掃描：兩階 HLS 的前置量測。

    要回答三件事（J 章第 0 層列為動工前置條件）：

    1. **成本。**`ffprobe` 兩種問法都要 demux 整個檔案。如果是幾十秒，
       邊界表只能放在掃描時算，不能放在開播時算。
    2. **邊界表長什麼樣。**上階 `-c:v copy` 只能從 keyframe 起頭，所以邊界由它決定，
       下階再跟著同一組時間切 —— 兩階的 #EXTINF 才會完全相同、才切得動。
    3. **`BANDWIDTH` 的真實值。**HLS 的 BANDWIDTH 是**單段峰值**不是平均。
       實測過 6 秒段 15.4 MB ≈ 20.5 Mbps 而該檔平均只有 10.9 ——
       填平均會讓 hls.js 在 12 Mbps 的鏈路上選上階，然後在每個峰值卡一次。
       keyframe 的位元組偏移（`pos`）順便就把每段大小算出來了。

    兩種問法都量：`-show_packets` 只 demux 不解碼；`-skip_frame nokey` 會解 keyframe。
    """
    out: Dict[str, Any] = {"分段秒數": seg_seconds}

    # --- 方法 A：只 demux，不解碼。K flag 在 Python 端過濾 ---
    t0 = time.time()
    r = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_packets",
              "-show_entries", "packet=pts_time,pos,flags",
              "-of", "csv=p=0", path], timeout=3600)
    out["A_show_packets_秒"] = round(time.time() - t0, 1)
    kf: List[tuple] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(",")
        if len(parts) < 3:
            continue
        ts, pos, flags = parts[0], parts[1], parts[2]
        if "K" not in flags:
            continue
        try:
            kf.append((float(ts), int(pos)))
        except ValueError:
            continue
    kf.sort()
    out["A_keyframe 數"] = len(kf)

    # --- 方法 B：解碼端跳過非 keyframe ---
    t0 = time.time()
    r2 = _run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
               "-show_entries", "frame=pts_time", "-of", "csv=p=0", path], timeout=3600)
    out["B_skip_frame_秒"] = round(time.time() - t0, 1)
    out["B_keyframe 數"] = sum(1 for l in (r2.stdout or "").splitlines() if l.strip(","))
    out["兩種問法一致"] = out["A_keyframe 數"] == out["B_keyframe 數"]

    if len(kf) < 2:
        out["結果"] = "抓不到足夠的 keyframe，這個檔案不能走上階"
        return out

    gaps = [kf[i + 1][0] - kf[i][0] for i in range(len(kf) - 1)]
    out["間距_最短"] = round(min(gaps), 3)
    out["間距_中位"] = round(sorted(gaps)[len(gaps) // 2], 3)
    out["間距_最長"] = round(max(gaps), 3)

    # --- 邊界表：每段取「湊滿 >= 分段長度的最少 keyframe 數」---
    try:
        size = os.path.getsize(path)
    except OSError:
        size = kf[-1][1]
    bounds: List[tuple] = []          # (起, 迄, 位元組)
    i, cnt = 0, len(kf)
    while i < cnt - 1:
        j = i + 1
        while j < cnt - 1 and kf[j][0] - kf[i][0] < seg_seconds:
            j += 1
        bounds.append((kf[i][0], kf[j][0], kf[j][1] - kf[i][1]))
        i = j
    # **尾巴。**上面的迴圈收在最後一個 keyframe，而片尾還有一段沒有 keyframe 的內容。
    # 漏掉它的話播放清單會短少那幾秒 —— 而那是「最後幾秒播不到」這種很難察覺的失效。
    tail_start, tail_bytes = kf[-1][0], max(size - kf[-1][1], 0)
    if duration and duration - tail_start > 0.1:
        if bounds and duration - tail_start < seg_seconds * 0.5:
            # 太短的尾巴併進最後一段，不要在清單裡留一個 0.x 秒的分段
            s0, _, b0 = bounds[-1]
            bounds[-1] = (s0, duration, b0 + tail_bytes)
        else:
            bounds.append((tail_start, duration, tail_bytes))
    out["段數"] = len(bounds)
    durs = [b[1] - b[0] for b in bounds if b[1] > b[0]]
    if durs:
        out["EXTINF_最短"] = round(min(durs), 3)
        out["EXTINF_中位"] = round(sorted(durs)[len(durs) // 2], 3)
        out["EXTINF_最長"] = round(max(durs), 3)
    rates = [b[2] * 8 / (b[1] - b[0]) / 1000 for b in bounds if b[1] - b[0] > 0.1]
    if rates:
        out["視訊_峰值_kbps"] = round(max(rates))
        out["視訊_平均_kbps"] = round(sum(b[2] for b in bounds) * 8
                                   / max(bounds[-1][1] - bounds[0][0], 0.1) / 1000)
        out["峰值是平均的幾倍"] = round(max(rates) / max(out["視訊_平均_kbps"], 1), 2)
    # 邊界表要存進 DB，所以順便量它有多大
    out["邊界表_JSON_bytes"] = len(json.dumps([round(b[0], 3) for b in bounds]))
    out["可對齊分段"] = out["間距_最長"] <= seg_seconds
    return out


def _report_keyframes(kfs: Dict[str, Any]) -> None:
    _p("")
    _p("-" * 68)
    _p(f"分段邊界（兩階 HLS 的前置量測，分段長度 {kfs.get('分段秒數')} 秒）")
    _p("-" * 68)
    for k in ("A_show_packets_秒", "A_keyframe 數", "B_skip_frame_秒", "B_keyframe 數",
              "兩種問法一致", "間距_最短", "間距_中位", "間距_最長", "可對齊分段",
              "段數", "EXTINF_最短", "EXTINF_中位", "EXTINF_最長",
              "視訊_峰值_kbps", "視訊_平均_kbps", "峰值是平均的幾倍",
              "邊界表_JSON_bytes", "結果"):
        if k in kfs:
            _p(f"  {k:<18}: {kfs[k]}")
    a, b = kfs.get("A_show_packets_秒"), kfs.get("B_skip_frame_秒")
    if a is not None and b is not None:
        cheap = "-show_packets（不解碼）" if a <= b else "-skip_frame nokey"
        _p(f"  → 比較便宜的問法是 {cheap}")
    if a is not None:
        if min(a, b or a) > 10:
            _p("  → 超過 10 秒，邊界表只能在**掃描時**算好存進 DB，不能在開播時算。")
        else:
            _p("  → 夠快，第一次開播時算也可以（但存起來還是划算，重播就免了）。")
    if kfs.get("可對齊分段") is False:
        _p("  → keyframe 間距超過分段長度，這個檔案沒有上階可以給（只能發單階轉碼）。")
    if "視訊_峰值_kbps" in kfs:
        _p(f"  → 上階的 BANDWIDTH 要填 {kfs['視訊_峰值_kbps']} kbps ＋音訊碼率，"
           f"不是平均的 {kfs['視訊_平均_kbps']}。")
    mid, seg = kfs.get("EXTINF_中位"), kfs.get("分段秒數")
    if mid and seg and mid > seg * 1.15:
        _p(f"  ! 段長變成 {mid} 秒，比設定的 {seg} 秒長 {(mid / seg - 1) * 100:.0f}%。")
        _p("    「湊滿 >= 分段長度的最少 keyframe 數」這條規則會讓段長變成 keyframe")
        _p("    間距的整數倍。段變長 = 開播多等一點、ABR 換階的顆粒也變粗。")
        _p("    另一種規則是取「離目標最近」的 keyframe（可能比目標短）——")
        _p("    要不要改用它，看這個數字大到什麼程度再決定。")


def _report_quality(qua: List[Dict[str, Any]]) -> None:
    """品質階梯：這一張表直接對應 .env 裡那兩個數字該填多少。"""
    _p("")
    _p("-" * 68)
    _p(f"品質階梯（不設碼率上限、{CLIP_SECONDS:.0f} 秒取樣）")
    _p("-" * 68)
    for enc in ("x264", "nvenc"):
        curve = [r for r in qua if (r.get("品質") or (None,))[0] == enc]
        if not curve:
            continue
        curve.sort(key=lambda r: -r["品質"][1])      # 數字越小畫質越好，由差到好
        _p(f"  {ENC_LABEL.get(enc, enc)}　旋鈕：{KNOB_LABEL.get(enc, '?')}")
        for r in curve:
            knob = r["品質"][1]
            if r.get("結果"):
                _p(f"      {knob:>3} ｜ {r['結果']}：{r.get('錯誤', '')}")
                continue
            _p(f"      {knob:>3} ｜ {r['位元率_kbps']:>6} kbps ｜ {r['倍速']}x"
               f" ｜ {r['分數'] or '（沒量到）'}")
        pts = [(r["品質"][1], _vmaf_of(r), r.get("位元率_kbps"), r.get("倍速"))
               for r in curve]
        pts = [(k, v, b, sp) for k, v, b, sp in pts if v is not None]
        if pts:
            good = [(k, v, b, sp) for k, v, b, sp in pts if v >= VMAF_GOOD_ENOUGH]
            if good:
                k, v, b, sp = max(good, key=lambda t: t[0])   # 到得了 95 的最省的那一格
                _p(f"  → 到 VMAF {VMAF_GOOD_ENOUGH:g} 最省的一格是 {KNOB_LABEL.get(enc)}={k}"
                   f"（{b} kbps、{sp}x）")
            else:
                _p(f"  → 這條曲線都沒到 VMAF {VMAF_GOOD_ENOUGH:g}"
                   f"（最高 {max(v for _, v, _, _ in pts):.1f}）—— 旋鈕還要再往下調")
        _p("")
    _p("怎麼讀這張表：")
    _p("  * 數字越小畫質越好、碼率越高。要找的是「剛好到得了目標分數」的那一格，")
    _p("    再往下調就是多花頻寬買不到畫質。")
    _p("  * 這裡刻意不設碼率上限 —— 要看的是這個旋鈕自己想花多少。")
    _p("    上限該開多少是另一張表（位元率階梯）。")
    _p("  * x264 的 crf 與 NVENC 的 cq 不是同一把尺，兩條曲線的數字不能互相對照。")


def _report_ladder(lad: List[Dict[str, Any]]) -> None:
    """位元率階梯。碼率是綁住的，所以這一張表可以橫向比、也可以縱向找轉折。"""
    _p("")
    _p("-" * 68)
    _p(f"位元率階梯（ABR 綁住目標碼率、{CLIP_SECONDS:.0f} 秒取樣）")
    _p("-" * 68)
    for enc in ("x264", "nvenc"):
        curve = [r for r in lad if (r.get("階梯") or (None,))[0] == enc]
        if not curve:
            continue
        curve.sort(key=lambda r: r["階梯"][1])
        _p(f"  {ENC_LABEL.get(enc, enc)}")
        prev: Optional[float] = None
        for r in curve:
            target = r["階梯"][1]
            if r.get("結果"):
                _p(f"      目標 {target:>6}k ｜ {r['結果']}：{r.get('錯誤', '')}")
                continue
            v = _vmaf_of(r)
            delta = "" if (v is None or prev is None) else f" ｜ 比上一級 {v - prev:+.2f}"
            _p(f"      目標 {target:>6}k ｜ 實測 {r['位元率_kbps']:>6} kbps"
               f" ｜ {r['倍速']}x ｜ {r['分數'] or '（沒量到）'}{delta}")
            if v is not None:
                prev = v
        _p("")

    # 兩個讀數：夠好的門檻、以及加碼率買不到畫質的那一級。
    # 只有 VMAF 算得出來 —— SSIM 在這個區間會飽和（實測四組落在 0.9986~0.9989、
    # 差 0.0003 而肉眼差異明顯），拿它找轉折點會找到噪音。
    if not any(_vmaf_of(r) is not None for r in lad):
        _p("  轉折點需要 VMAF 才算得出來，這一輪只有 SSIM。")
        _p("  SSIM 在高畫質區間會飽和，用它找轉折點會找到噪音而不是轉折。")
        _p("  → 換一個帶 libvmaf 的 ffmpeg（見 J-3），再跑一次這一輪。")
    for enc in ("x264", "nvenc"):
        pts = [(r["階梯"][1], _vmaf_of(r)) for r in lad
               if (r.get("階梯") or (None,))[0] == enc]
        pts = sorted((k, v) for k, v in pts if v is not None)
        if len(pts) < 2:
            continue
        name = ENC_LABEL.get(enc, enc)
        hit = next((k for k, v in pts if v >= VMAF_GOOD_ENOUGH), None)
        _p(f"  {name}：VMAF 第一次到 {VMAF_GOOD_ENOUGH:g} 的目標碼率 = "
           + (f"{hit}k" if hit else f"這條曲線都沒到（最高 {max(v for _, v in pts):.1f}）"))
        knee = next((pts[i][0] for i in range(1, len(pts))
                     if pts[i][1] - pts[i - 1][1] < VMAF_MARGINAL), None)
        _p(f"  {name}：邊際增益掉到 {VMAF_MARGINAL} 以下的第一級 = "
           + (f"{knee}k（再往上加碼率買不到畫質）" if knee else "還沒出現，曲線仍在上升"))
    _p("")
    _p("怎麼讀這張表：")
    _p("  * 碼率是綁住的，所以同一列的 x264 與 NVENC 是「同樣的成本」，可以直接比分數。")
    _p("  * 「實測」和「目標」差太多代表 VBV 沒收斂 —— 取樣太短，把 --seconds 開大。")
    _p("  * 位元率 < 100 kbps 的列一律是「片段解不出畫面」，分數是假的，不列入判斷。")
    _p("  * 這是 ABR，不是生產環境的 CRF ＋ 上限。上限只在咬得到的時候才有作用，")
    _p("    所以「上限該開多少」要看這條曲線的轉折點，加上上面「不設上限」那一列的實際碼率。")


def _measure(path: str, args, env: Dict[str, Any]) -> tuple:
    """一個檔案的完整量測。回傳 (片源資訊, 對照表, remux 檢查)。"""
    finfo = probe_file(path)
    vf = _pipeline_filters(finfo, args.height)
    finfo["濾鏡鏈"] = ",".join(vf) if vf else "（無，原尺寸直接編）"
    rmx = remux_check(path, finfo) if args.remux_check else None
    kfs = (keyframe_scan(path, args.segment_seconds, finfo.get("長度_秒"))
           if args.keyframe_scan else None)
    rows = None
    if not args.no_encode:
        start = sample_start(finfo)
        finfo["取樣起點_秒"] = round(start, 1)
        vars_ = variants(env, args.crf, args.cap, vf,
                         nvenc_cq=args.nvenc_cq, x264_preset=args.x264_preset)
        if args.ladder:
            vars_ += ladder_variants(env, args.ladder_kbps, vf,
                                     x264_preset=args.x264_preset)
        if args.crf_ladder:
            vars_ += quality_ladder_variants(env, args.crf_list, args.cq_list, vf,
                                             x264_preset=args.x264_preset)
        with tempfile.TemporaryDirectory(prefix="filmax_q_") as tmp:
            ref, err = build_reference(path, start, Path(tmp), vf)
            if ref is None:
                rows = [{"設定": "參考片段", "結果": "失敗", "錯誤": err or ""}]
            else:
                finfo["參考片段_MB"] = round(ref.stat().st_size / 1048576, 1)
                rows = run_variants(path, start, Path(tmp), vars_,
                                    bool(env.get("vmaf")), vf, ref)
    return finfo, rows, rmx, kfs


def _why_missing(path: str) -> List[str]:
    """路徑不存在時，指出斷在哪一層、那一層裡面有什麼。

    「找不到這個檔案，跳過」這句話沒有可操作性 —— 而實際發生過的原因是
    **片庫資料夾被改名**（`APPLE` → `1.APPLE`、`0.電影` → `0.Movie`），
    於是清單裡兩條路徑同時失效，一整輪量測跑完只印出兩行「找不到」。
    指出斷點與同一層的實際內容，改清單的人一眼就知道要改哪一段。
    """
    p = Path(path)
    alive = next((d for d in p.parents if d.is_dir()), None)
    if alive is None:
        return [f"    這條路徑連最上層都不存在（{p.anchor or p} 沒掛上？）"]
    # alive 底下第一個不存在的名字，就是斷點
    try:
        broken = p.relative_to(alive).parts[0]
    except ValueError:
        broken = p.name
    out = [f"    斷在：{alive}{os.sep}{broken} 不存在"]
    try:
        names = sorted(x.name for x in alive.iterdir())
    except OSError as e:
        return out + [f"    （{alive} 列不出來：{e}）"]
    near = difflib.get_close_matches(broken, names, n=3, cutoff=0.4)
    if near:
        out.append(f"    同一層裡名字最接近的：{'、'.join(near)}")
    shown = names[:20]
    out.append(f"    {alive} 裡面有（{len(names)} 項）：{'、'.join(shown)}"
               + ("…" if len(names) > len(shown) else ""))
    return out


def _run_batch(args, env: Dict[str, Any]) -> int:
    global CLIP_SECONDS
    try:
        lines = io.open(args.batch, encoding="utf-8-sig").read().splitlines()
    except Exception as e:
        print(f"讀不到清單檔 {args.batch}：{e}", file=sys.stderr)
        return 2
    jobs = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        secs = args.seconds
        if "|" in line:
            line, _, tail = line.rpartition("|")
            line = line.strip()
            try:
                secs = float(tail.strip())
            except ValueError:
                pass
        jobs.append((line, secs))
    if not jobs:
        print("清單檔裡沒有可用的路徑", file=sys.stderr)
        return 2

    report(env, None, None, args.crf, args.cap)
    for i, (path, secs) in enumerate(jobs, 1):
        _p("")
        _p("#" * 68)
        _p(f"# 第 {i}/{len(jobs)} 個檔案（取樣 {secs:.0f} 秒）")
        _p(f"# {path}")
        _p("#" * 68)
        if not os.path.exists(path):
            _p("  找不到這個檔案，跳過。")
            for line in _why_missing(path):
                _p(line)
            continue
        CLIP_SECONDS = max(5.0, min(secs, 120.0))
        try:
            finfo, rows, rmx, kfs = _measure(path, args, env)
        except Exception as e:
            _p(f"  量測失敗：{e}")
            continue
        report({}, finfo, rows, args.crf, args.cap, rmx, kfs)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    global CLIP_SECONDS
    ap = argparse.ArgumentParser(description="量測這台機器的編碼能力與各設定的實際畫質")
    ap.add_argument("file", nargs="?", help="要實測的影片檔（本機路徑）。留空只探測環境")
    ap.add_argument("--crf", type=int, default=DEFAULT_CRF,
                    help=f"要測的 CRF（預設 {DEFAULT_CRF}，讀 .env 的 TRANSCODE_CRF）")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP,
                    help=f"要測的位元率上限 kbps（預設 {DEFAULT_CAP}，讀 .env 的 REMOTE_BITRATE_KBPS）")
    ap.add_argument("--nvenc-cq", type=int, default=DEFAULT_NVENC_CQ,
                    help=f"NVENC 的 cq（預設 {DEFAULT_NVENC_CQ}，讀 .env 的 NVENC_CQ。"
                         "刻意跟 CRF 分開，見決策 15）")
    ap.add_argument("--x264-preset", default=DEFAULT_X264_PRESET,
                    help=f"x264 的 preset（預設 {DEFAULT_X264_PRESET}，讀 .env 的 X264_PRESET）")
    ap.add_argument("--ladder", action="store_true",
                    help="加跑位元率階梯（ABR 綁住碼率，x264 與 NVENC 各一條曲線）。"
                         "這是回答「上限開到多少就夠」的那一張表，但會多花不少時間")
    ap.add_argument("--ladder-kbps", default=",".join(str(k) for k in LADDER_KBPS),
                    help="階梯的碼率，逗號分隔（預設 "
                         + ",".join(str(k) for k in LADDER_KBPS) + "）")
    ap.add_argument("--crf-ladder", action="store_true",
                    help="加跑品質階梯（不設上限，只動 CRF／cq）。"
                         "這是回答「TRANSCODE_CRF 與 NVENC_CQ 該設多少」的那一張表")
    ap.add_argument("--crf-list", default=",".join(str(k) for k in CRF_LADDER),
                    help="x264 要測的 CRF（預設 " + ",".join(str(k) for k in CRF_LADDER) + "）")
    ap.add_argument("--cq-list", default=",".join(str(k) for k in CQ_LADDER),
                    help="NVENC 要測的 cq（預設 " + ",".join(str(k) for k in CQ_LADDER) + "）")
    ap.add_argument("--seconds", type=float, default=CLIP_SECONDS, help="取樣秒數")
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                    help="轉碼目標高度（預設跟 TRANSCODE_MAX_HEIGHT 一致；0 = 不縮放）")
    ap.add_argument("--keyframe-scan", action="store_true",
                    help="全檔 keyframe 掃描：量邊界表的取得成本，並算出上階的 "
                         "BANDWIDTH 峰值。這是兩階 HLS 動工前的前置量測")
    ap.add_argument("--segment-seconds", type=int, default=DEFAULT_SEG,
                    help=f"分段長度（預設 {DEFAULT_SEG}，讀 .env 的 HLS_SEGMENT_SECONDS）")
    ap.add_argument("--remux-check", action="store_true",
                    help="檢查 Direct Stream 可行性（keyframe 間距 ＋ 試切一段）")
    ap.add_argument("--no-encode", action="store_true",
                    help="只做 remux 檢查，不跑設定對照（快很多）")
    ap.add_argument("--batch", metavar="清單檔",
                    help="從 UTF-8 文字檔讀多個影片路徑（每行一個，可用 路徑|秒數 指定取樣長度）"
                         " —— 路徑寫在檔案裡而不是 .bat 裡，才不會被 cmd 的碼頁弄壞")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 而不是報告")
    args = ap.parse_args(argv)
    CLIP_SECONDS = max(5.0, min(args.seconds, 120.0))
    try:
        args.ladder_kbps = sorted({int(x) for x in str(args.ladder_kbps).split(",")
                                   if x.strip()})
    except ValueError:
        print(f"--ladder-kbps 只能是逗號分隔的整數：{args.ladder_kbps}", file=sys.stderr)
        return 2
    if not args.ladder_kbps:
        args.ladder = False
    try:
        args.crf_list = sorted({int(x) for x in str(args.crf_list).split(",") if x.strip()})
        args.cq_list = sorted({int(x) for x in str(args.cq_list).split(",") if x.strip()})
    except ValueError:
        print("--crf-list／--cq-list 只能是逗號分隔的整數", file=sys.stderr)
        return 2
    if not args.crf_list and not args.cq_list:
        args.crf_ladder = False

    env = probe_env()
    finfo = rows = rmx = kfs = None

    if args.batch:
        return _run_batch(args, env)

    if args.file:
        if not os.path.exists(args.file):
            print(f"找不到檔案：{args.file}", file=sys.stderr)
            return 2
        finfo, rows, rmx, kfs = _measure(args.file, args, env)

    if args.json:
        print(json.dumps({"env": env, "file": finfo, "rows": rows, "remux": rmx,
                          "keyframes": kfs}, ensure_ascii=False, indent=2))
    else:
        report(env, finfo, rows, args.crf, args.cap, rmx, kfs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
