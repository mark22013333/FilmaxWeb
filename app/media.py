"""ffprobe 探測、播放模式判定、縮圖、字幕轉檔。

重點：ffmpeg / ffprobe 一律讀本服務的 http://127.0.0.1:PORT/api/stream/{id}?raw=1
（支援 Range），不直接讀 FTP，藉此完全避開檔名編碼與 FTPS 相容性問題。
"""
from __future__ import annotations

import anyio
import glob
import hashlib
import json
import re
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

from . import db
from .config import IMAGE_DIR, SUB_DIR, settings

log = logging.getLogger("filmax.media")

# 瀏覽器 <video> 可直接解的編碼
BROWSER_VIDEO = {"h264", "avc1", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
BROWSER_CONTAINER = {"mp4", "m4v", "mov", "webm"}
# mov,mp4,m4a,3gp,3g2,mj2 是 ffprobe 對 mp4 家族的 format_name
MP4_FORMAT_NAMES = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm"}


# ---------------- ffmpeg / ffprobe 定位 ----------------
_tool_cache: Dict[str, str] = {}
EXE = ".exe" if os.name == "nt" else ""


def _win_candidates(exe: str) -> List[str]:
    """Windows 上常見的 ffmpeg 安裝位置（含 winget/choco/scoop 的實際套件目錄）。"""
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    home = os.path.expanduser("~")
    pats: List[str] = []
    if la:
        pats += [
            rf"{la}\Microsoft\WinGet\Links\{exe}",
            # winget 的 shim 有時只掛 ffmpeg.exe，真正的執行檔在 Packages 底下
            rf"{la}\Microsoft\WinGet\Packages\*\*\bin\{exe}",
            rf"{la}\Microsoft\WinGet\Packages\*\*\*\bin\{exe}",
            rf"{la}\Microsoft\WinGet\Packages\*\{exe}",
        ]
    pats += [
        rf"C:\ProgramData\chocolatey\bin\{exe}",
        rf"{home}\scoop\shims\{exe}",
        rf"{home}\scoop\apps\ffmpeg\current\bin\{exe}",
        rf"{pf}\ffmpeg\bin\{exe}",
        rf"C:\ffmpeg\bin\{exe}",
    ]
    return pats


def _runnable(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        p = subprocess.run([path, "-version"], capture_output=True, timeout=20)
        return path if p.returncode == 0 else None
    except Exception:
        return None


def _candidate_dirs() -> List[str]:
    """可能放著 ffmpeg / ffprobe 的目錄，依可信度排序。"""
    dirs: List[str] = []

    def add(path: Optional[str]) -> None:
        if path:
            d = str(Path(path).parent)
            if d and d not in dirs:
                dirs.append(d)

    # .env 指定的完整路徑（若是完整路徑的話）
    for configured in (settings.ffmpeg, settings.ffprobe):
        if configured and (os.sep in configured or "/" in configured):
            add(configured)
    # PATH 上的
    add(shutil.which("ffmpeg"))
    add(shutil.which("ffprobe"))
    # 常見安裝位置
    if os.name == "nt":
        for name in ("ffprobe", "ffmpeg"):
            for pattern in _win_candidates(name + EXE):
                for cand in sorted(glob.glob(pattern), reverse=True):
                    add(cand)
    return dirs


def _resolve_all() -> Dict[str, str]:
    """一次把兩支都定位好。

    重點：**優先挑同時擁有 ffmpeg 和 ffprobe 的目錄**。
    很多軟體會夾帶一支自己的 ffmpeg.exe 丟進 PATH（而且往往是好幾年前的版本、
    也沒附 ffprobe.exe）。如果各自獨立解析，就會變成用 A 程式的舊 ffmpeg
    配 B 程式的新 ffprobe，版本不一致更難查。
    """
    found: Dict[str, str] = {}
    dirs = _candidate_dirs()

    # 第一輪：找兩支都齊全的目錄
    for d in dirs:
        m = _runnable(str(Path(d) / ("ffmpeg" + EXE)))
        if not m:
            continue
        pr = _runnable(str(Path(d) / ("ffprobe" + EXE)))
        if pr:
            log.info("在 %s 找到完整的 ffmpeg + ffprobe", d)
            return {"ffmpeg": m, "ffprobe": pr}

    # 第二輪：退而求其次，各自單獨找
    for kind in ("ffmpeg", "ffprobe"):
        configured = settings.ffmpeg if kind == "ffmpeg" else settings.ffprobe
        for cand in (configured, shutil.which(configured) if configured else None,
                     shutil.which(kind)):
            got = _runnable(cand)
            if got:
                found[kind] = got
                break
        else:
            for d in dirs:
                got = _runnable(str(Path(d) / (kind + EXE)))
                if got:
                    found[kind] = got
                    break
    return found


def resolve_tool(kind: str) -> str:
    """找出 ffmpeg / ffprobe 的實際可執行路徑（結果會快取）。"""
    cached = _tool_cache.get(kind)
    if cached:
        return cached
    found = _resolve_all()
    for k in ("ffmpeg", "ffprobe"):
        configured = settings.ffmpeg if k == "ffmpeg" else settings.ffprobe
        _tool_cache[k] = found.get(k) or configured or k
        if k not in found:
            log.error("找不到 %s", k)
    return _tool_cache[kind]


def has_tool(kind: str) -> bool:
    return _runnable(resolve_tool(kind)) is not None


def reset_encoder_cache() -> None:
    """重新實測硬體編碼器（換了驅動或 ffmpeg 之後用）。"""
    global _hw_cache
    _hw_cache = None
    _chosen_args.clear()
    _hw_probe.clear()
    _enc_opt_cache.clear()


def reset_tool_cache() -> None:
    """清掉定位快取。使用者中途才裝好 ffmpeg 時，重新診斷一次就能抓到，不必重啟服務。"""
    _tool_cache.clear()


class ToolMissing(RuntimeError):
    """ffmpeg / ffprobe 找不到時丟這個，訊息直接可以拿去給使用者看。"""


def _missing_msg(kind: str) -> str:
    configured = settings.ffmpeg if kind == "ffmpeg" else settings.ffprobe
    env_key = "FFMPEG_PATH" if kind == "ffmpeg" else "FFPROBE_PATH"
    return (
        f"找不到 {kind} 執行檔（目前設定 {env_key}={configured or kind!r}）。"
        f"ffmpeg 和 ffprobe 是兩支獨立的程式，只裝其中一支不夠。"
        f"請安裝完整的 ffmpeg（winget install Gyan.FFmpeg），"
        f"或在 .env 把 {env_key} 設成 {kind} 執行檔的完整路徑，然後重新啟動服務。"
    )


def tool_status() -> Dict[str, Any]:
    """給診斷頁用：兩支工具各自的路徑與版本。"""
    out: Dict[str, Any] = {}
    for kind in ("ffmpeg", "ffprobe"):
        path = resolve_tool(kind)
        entry: Dict[str, Any] = {"configured": settings.ffmpeg if kind == "ffmpeg" else settings.ffprobe,
                                 "resolved": path, "ok": False, "version": None}
        try:
            p = subprocess.run([path, "-version"], capture_output=True, timeout=20)
            if p.returncode == 0:
                entry["ok"] = True
                entry["version"] = p.stdout.decode("utf-8", "ignore").splitlines()[0][:120]
        except Exception as e:
            entry["error"] = str(e)
        if not entry["ok"]:
            entry["hint"] = _missing_msg(kind)
        out[kind] = entry
    # 只有 ffmpeg 也還能動：改用解析 ffmpeg 輸出的備援方式
    if not out["ffprobe"]["ok"] and out["ffmpeg"]["ok"]:
        out["ffprobe"]["fallback"] = True
        out["ffprobe"]["hint"] = (
            "找不到 ffprobe，目前改用解析 ffmpeg 輸出的備援方式取得影片資訊（可以正常播放，"
            "但取得的資訊較少）。建議安裝完整的 ffmpeg：winget install Gyan.FFmpeg —— "
            "裝完在 .env 把 FFMPEG_PATH / FFPROBE_PATH 指向新的 bin 目錄，或直接留空讓程式自己找。"
        )
    return out


def source_url(file_id: int) -> str:
    return f"{settings.self_base_url()}/api/stream/{file_id}?raw=1"


def source_headers() -> List[str]:
    """ffmpeg 讀本機來源檔要帶的標頭。

    內部 token 一定要走標頭，不能放在網址裡：ffmpeg/ffprobe 失敗時會把完整
    URL 原樣印進 stderr，那段訊息會被存進 probe_error 再回給前端，等於把
    一把不會過期、也撤銷不了的萬能鑰匙公開出去。
    """
    from .auth import INTERNAL_TOKEN      # 延後 import，避免循環相依
    return ["-headers", f"X-Filmax-Internal: {INTERNAL_TOKEN}\r\n"]


# 內部憑證現在只出現在 -headers 參數裡，理論上不會被 ffmpeg 印出來。
# 但錯誤訊息會被存進 probe_error 再回給前端，這條路徑一旦漏就是全權外洩，
# 所以出口再擋一層，順便連舊格式的 tok=... 一起遮掉。
_TOKEN_RE = re.compile(r"(X-Filmax-Internal:\s*)\S+", re.I)
_TOKEN_RE_B = re.compile(rb"(X-Filmax-Internal:\s*)\S+", re.I)
_TOK_QS = re.compile(r"([?&]tok=)[^&\s]+", re.I)
_TOK_QS_B = re.compile(rb"([?&]tok=)[^&\s]+", re.I)


def scrub(text: str) -> str:
    """把訊息裡的內部憑證遮掉，才能拿去記錄或回給前端。"""
    if not text:
        return text
    return _TOK_QS.sub(r"\1***", _TOKEN_RE.sub(r"\1***", text))


def scrub_bytes(data: bytes) -> bytes:
    if not data:
        return data
    return _TOK_QS_B.sub(rb"\1***", _TOKEN_RE_B.sub(rb"\1***", data))


def _run(cmd: List[str], timeout: int = 120) -> Tuple[int, bytes, bytes]:
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    # 統一在這裡遮掉憑證：底下所有呼叫端拿到的都已經是安全的，
    # 不必每個錯誤處理點都記得自己過濾（漏一個就前功盡棄）。
    return p.returncode, scrub_bytes(p.stdout), scrub_bytes(p.stderr)


def _ffprobe_json(file_id: int, timeout: int = 90) -> Dict[str, Any]:
    cmd = [
        resolve_tool("ffprobe"), "-v", "error",
        *source_headers(),
        "-rw_timeout", "30000000",
        "-print_format", "json",
        "-show_format", "-show_streams",
        source_url(file_id),
    ]
    code, out, err = _run(cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError((err or b"").decode("utf-8", "ignore")[:500] or f"ffprobe exit {code}")
    return json.loads(out.decode("utf-8", "ignore"))


# ---- 只有 ffmpeg 沒有 ffprobe 時的備援 ----------------------------------
# 不少軟體會夾帶一支 ffmpeg.exe 卻沒附 ffprobe.exe。ffmpeg -i 在沒有輸出檔時
# 會把完整的串流資訊印到 stderr，解析它就能拼出跟 ffprobe JSON 一樣的結構。
_RE_INPUT = re.compile(r"Input #0,\s*(?P<fmt>.+?),\s*from ")
_RE_DUR = re.compile(r"Duration:\s*(?P<h>\d+):(?P<m>\d\d):(?P<s>\d\d(?:\.\d+)?)")
_RE_BITRATE = re.compile(r"bitrate:\s*(?P<kb>\d+)\s*kb/s")
_RE_STREAM = re.compile(
    r"Stream #0:(?P<idx>\d+)(?:\[[^\]]*\])?(?:\((?P<lang>[A-Za-z]{2,3})\))?:\s*"
    r"(?P<type>Video|Audio|Subtitle):\s*(?P<codec>[A-Za-z0-9_]+)(?P<rest>.*)"
)
_RE_RES = re.compile(r"(?<![\d])(?P<w>\d{2,5})x(?P<h>\d{2,5})(?![\d])")
_RE_TITLE = re.compile(r"^\s*title\s*:\s*(?P<t>.+)$")
_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "5.0": 5,
             "5.1": 6, "6.1": 7, "7.1": 8}


def _ffmpeg_probe(file_id: int, timeout: int = 120) -> Dict[str, Any]:
    cmd = [resolve_tool("ffmpeg"), "-hide_banner", "-rw_timeout", "30000000",
           *source_headers(), "-i", source_url(file_id)]
    # 沒指定輸出檔，ffmpeg 一定以非 0 結束，資訊在 stderr —— 這是預期行為
    code, _, err = _run(cmd, timeout=timeout)
    text = (err or b"").decode("utf-8", "ignore")
    if "Input #0" not in text:
        raise RuntimeError(text.strip().splitlines()[-1][:400] if text.strip()
                           else f"ffmpeg 讀不到這個來源 (exit {code})")

    fmt: Dict[str, Any] = {}
    m = _RE_INPUT.search(text)
    if m:
        fmt["format_name"] = m.group("fmt").strip()
    m = _RE_DUR.search(text)
    if m:
        fmt["duration"] = str(int(m.group("h")) * 3600 + int(m.group("m")) * 60 + float(m.group("s")))
    m = _RE_BITRATE.search(text)
    if m:
        fmt["bit_rate"] = str(int(m.group("kb")) * 1000)

    streams: List[Dict[str, Any]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        sm = _RE_STREAM.search(line)
        if not sm:
            continue
        rest = sm.group("rest") or ""
        st: Dict[str, Any] = {
            "index": int(sm.group("idx")),
            "codec_type": sm.group("type").lower(),
            "codec_name": sm.group("codec").lower(),
            "disposition": {"attached_pic": 1 if "attached pic" in rest.lower() else 0},
            "tags": {},
        }
        if sm.group("lang"):
            st["tags"]["language"] = sm.group("lang")
        # 緊接在後面的縮排區塊可能有 title / Metadata
        for follow in lines[i + 1:i + 5]:
            if _RE_STREAM.search(follow) or follow.startswith("Input #"):
                break
            tm = _RE_TITLE.match(follow)
            if tm:
                st["tags"]["title"] = tm.group("t").strip()
                break
        if st["codec_type"] == "video":
            rm = _RE_RES.search(rest)
            if rm:
                st["width"] = int(rm.group("w"))
                st["height"] = int(rm.group("h"))
            # "Video: hevc (Main 10) (hvc1 ...), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x1920"
            cm = re.search(r",\s*(?P<pix>[a-z0-9]+)\s*\((?P<tags>[^)]*)\)", rest)
            if cm:
                st["pix_fmt"] = cm.group("pix")
                tags = [t.strip() for t in cm.group("tags").replace("/", ",").split(",")]
                for t in tags:
                    if t in ("smpte2084", "arib-std-b67", "bt709", "bt2020-10", "bt470bg"):
                        st.setdefault("color_transfer", t)
                    elif t in ("bt2020", "bt709", "bt470bg", "smpte170m"):
                        st.setdefault("color_primaries", t)
                    elif t in ("bt2020nc", "bt2020c", "bt709", "smpte170m"):
                        st.setdefault("color_space", t)
            elif re.search(r",\s*([a-z0-9]+le|yuv[a-z0-9]+|gbrp\w*)\s*,", rest):
                st["pix_fmt"] = re.search(r",\s*([a-z0-9]+le|yuv[a-z0-9]+|gbrp\w*)\s*,", rest).group(1)
        elif st["codec_type"] == "audio":
            for word, ch in _CHANNELS.items():
                if word in rest.lower():
                    st["channels"] = ch
                    break
        streams.append(st)

    if not streams:
        raise RuntimeError("ffmpeg 輸出裡找不到任何串流資訊")
    return {"format": fmt, "streams": streams}


_warned_fallback = False


def ffprobe(file_id: int, timeout: int = 90) -> Dict[str, Any]:
    """優先用 ffprobe；沒有 ffprobe 時退回解析 ffmpeg 的輸出。"""
    global _warned_fallback
    try:
        return _ffprobe_json(file_id, timeout=timeout)
    except FileNotFoundError:
        pass          # ffprobe 不存在 → 走備援
    except OSError as e:
        if getattr(e, "winerror", None) != 2 and e.errno not in (2,):
            raise
    if not has_tool("ffmpeg"):
        raise ToolMissing(_missing_msg("ffprobe"))
    if not _warned_fallback:
        _warned_fallback = True
        log.warning("找不到 ffprobe，改用解析 ffmpeg 輸出的備援方式取得影片資訊。"
                    "建議還是安裝完整的 ffmpeg（winget install Gyan.FFmpeg）。")
    return _ffmpeg_probe(file_id, timeout=max(timeout, 120))


# 只有文字字幕轉得成 WebVTT。PGS/VobSub 這類是「圖片」字幕，要嘛燒進畫面、
# 要嘛做 OCR，兩條路都不便宜，目前不支援。
#
# 這裡列的是「不支援的」而不是「支援的」：ffmpeg 的文字字幕格式有二十幾種
# （sami、microdvd、mpl2、realtext…），列白名單一定會漏，漏掉就變成使用者
# 明明有字幕卻被擋住。點陣字幕格式反而是固定的少數幾種，列這邊才列得完。
# 清單對照 `ffmpeg -decoders` 的實際輸出，同時收錄 codec 名與 decoder 名
# （例如 hdmv_pgs_subtitle / pgssub），不同版本回報的名稱不一樣。
GRAPHIC_SUBTITLE_CODECS = (
    "hdmv_pgs_subtitle", "pgssub",      # 藍光 PGS
    "dvd_subtitle", "dvdsub",           # DVD VobSub
    "dvb_subtitle", "dvbsub",           # DVB
    "dvb_teletext", "libzvbi_teletextdec",   # 圖文電視，要 libzvbi 才解得開
    "xsub",                             # DivX
)


def is_text_subtitle(codec: Any) -> bool:
    return str(codec or "").lower() not in GRAPHIC_SUBTITLE_CODECS


def _fps(v: Dict[str, Any]) -> Optional[float]:
    """ffprobe 的影格率是 "24000/1001" 這種分數字串，要自己算。"""
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(v.get(key) or "")
        if "/" not in raw:
            continue
        num, _, den = raw.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            continue
        if d > 0 and n > 0:
            return round(n / d, 3)
    return None


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def summarize(probe: Dict[str, Any], ext: str) -> Dict[str, Any]:
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    def _tag(s, k):
        return (s.get("tags") or {}).get(k)

    duration = None
    for src in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            duration = float(src)
            break
        except (TypeError, ValueError):
            continue

    # HDR 判定看轉換特性：smpte2084 = PQ (HDR10/DV)、arib-std-b67 = HLG
    v = video or {}
    transfer = (v.get("color_transfer") or "").lower()
    pix = (v.get("pix_fmt") or "").lower()
    depth = 10 if ("10" in pix or "p010" in pix) else (12 if "12" in pix else 8)
    try:
        depth = int(v.get("bits_per_raw_sample") or depth)
    except (TypeError, ValueError):
        pass

    info: Dict[str, Any] = {
        "pix_fmt": v.get("pix_fmt"),
        "color_transfer": v.get("color_transfer"),
        "color_primaries": v.get("color_primaries"),
        "color_space": v.get("color_space"),
        "bit_depth": depth,
        "is_hdr": 1 if transfer in ("smpte2084", "arib-std-b67") else 0,
        "duration": duration,
        "container": (fmt.get("format_name") or "").split(",")[0],
        "video_codec": (video or {}).get("codec_name"),
        "audio_codec": (audios[0].get("codec_name") if audios else None),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "bitrate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "fps": _fps(video or {}),
        "audio_tracks": [
            {"index": s.get("index"), "codec": s.get("codec_name"),
             "lang": _tag(s, "language"), "title": _tag(s, "title"),
             "channels": s.get("channels"),
             "channel_layout": s.get("channel_layout"),
             "sample_rate": _int_or_none(s.get("sample_rate")),
             "bitrate": _int_or_none(s.get("bit_rate")),
             "default": 1 if (s.get("disposition") or {}).get("default") else 0}
            for s in audios
        ],
        "subtitles": [
            {"index": s.get("index"), "codec": s.get("codec_name"),
             "lang": _tag(s, "language"), "title": _tag(s, "title"),
             "text": is_text_subtitle(s.get("codec_name")),
             "forced": 1 if (s.get("disposition") or {}).get("forced") else 0}
            for s in subs
        ],
    }
    info["play_mode"] = decide_play_mode(info, ext)
    return info


def decide_play_mode(info: Dict[str, Any], ext: str) -> str:
    """能直接丟給 <video> 就 direct，否則走 HLS 轉碼。"""
    ext = (ext or "").lower()
    fmt = (info.get("container") or "").lower()
    v = (info.get("video_codec") or "").lower()
    a = (info.get("audio_codec") or "").lower()
    if ext not in BROWSER_CONTAINER:
        return "hls"
    if fmt not in MP4_FORMAT_NAMES:
        return "hls"
    if v and v not in BROWSER_VIDEO:
        return "hls"
    if a and a not in BROWSER_AUDIO:
        return "hls"
    # mkv 即使是 h264/aac 也不是所有瀏覽器都吃，交給 HLS
    if ext == "mkv":
        return "hls"
    return "direct"


# ---------------- 硬體轉碼偵測 ----------------
_hw_cache: Optional[str] = None
HW_ENCODERS = [("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"),
               ("amf", "h264_amf"), ("videotoolbox", "h264_videotoolbox")]


_enc_opt_cache: Dict[str, Dict[str, List[str]]] = {}


def encoder_option_values(encoder: str, option: str) -> List[str]:
    """問 ffmpeg 這個編碼器的某個選項實際接受哪些值。

    不同 ffmpeg 版本差異很大 —— 例如 h264_nvenc 的 p1~p7 preset 是 ffmpeg 5.0
    才有的，4.x 只認 default/slow/medium/fast/hp/hq/ll/llhq...。硬寫死就會在
    舊版上炸掉，所以一律問過再用。
    """
    cached = _enc_opt_cache.get(encoder)
    if cached is None:
        cached = {}
        try:
            code, out, err = _run([resolve_tool("ffmpeg"), "-hide_banner",
                                   "-h", f"encoder={encoder}"], timeout=30)
            text = (out + err).decode("utf-8", "ignore")
        except Exception:
            text = ""
        current: Optional[str] = None
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r"^-([A-Za-z0-9_-]+)\s+<", stripped)
            if m:
                current = m.group(1)
                cached.setdefault(current, [])
                continue
            # 選項底下縮排的列舉值："  fast   3   E..V......."
            if current and line.startswith((" " * 5, "\t")):
                vm = re.match(r"^([A-Za-z0-9_.\-]+)\s+[-]?\d+\s+[EDAVS.]{6,}", stripped)
                if vm:
                    cached[current].append(vm.group(1))
                    continue
            if stripped.startswith("-"):
                current = None
        _enc_opt_cache[encoder] = cached
    return cached.get(option, [])


def pick_option(encoder: str, option: str, preferences: List[str]) -> Optional[str]:
    """從偏好清單裡挑第一個這個 ffmpeg 版本真的支援的值。"""
    supported = encoder_option_values(encoder, option)
    if not supported:
        return None
    for want in preferences:
        if want in supported:
            return want
    return None


_hw_probe: Dict[str, Dict[str, Any]] = {}


def _quality_arg_tiers(encoder: str) -> List[Tuple[str, List[str]]]:
    """同一個編碼器的參數組合，由好到保守。

    硬體編碼器在不同 ffmpeg 版本、不同驅動下能吃的參數差很多。與其猜，
    不如按順序實際試 —— 第一個過的就是這台機器實際能用的最好設定。
    """
    crf = str(settings.crf)
    if encoder == "h264_nvenc":
        preset = pick_option(encoder, "preset", ["p4", "medium", "fast", "default"])
        rc = pick_option(encoder, "rc", ["vbr", "vbr_hq", "constqp"])
        pre = ["-preset", preset] if preset else []
        tiers: List[Tuple[str, List[str]]] = []
        if rc:
            tiers.append(("完整", pre + ["-rc", rc, "-cq", crf, "-b:v", "0", "-bf", "0"]))
            tiers.append(("無 bf", pre + ["-rc", rc, "-cq", crf, "-b:v", "0"]))
            tiers.append(("無 b:v", pre + ["-rc", rc, "-cq", crf]))
        tiers.append(("僅 cq", pre + ["-cq", crf]))
        tiers.append(("僅 preset", pre))
        tiers.append(("預設值", []))
        return tiers
    if encoder == "h264_qsv":
        preset = pick_option(encoder, "preset", ["faster", "fast", "medium"])
        pre = ["-preset", preset] if preset else []
        return [("完整", ["-global_quality", crf] + pre),
                ("僅品質", ["-global_quality", crf]),
                ("預設值", [])]
    if encoder == "h264_amf":
        q = pick_option(encoder, "quality", ["speed", "balanced"])
        rc = pick_option(encoder, "rc", ["cqp", "vbr_peak"])
        pre = ["-quality", q] if q else []
        out: List[Tuple[str, List[str]]] = []
        if rc:
            out.append(("完整", pre + ["-rc", rc, "-qp_i", crf, "-qp_p", crf]))
        out.append(("僅品質", pre))
        out.append(("預設值", []))
        return out
    if encoder == "h264_videotoolbox":
        return [("完整", ["-q:v", str(max(1, min(100, 100 - settings.crf * 2)))]),
                ("預設值", [])]
    return [("完整", ["-preset", settings.x264_preset, "-crf", crf])]


# 實測通過的那一組參數，之後就固定用它
_chosen_args: Dict[str, List[str]] = {}


def _encoder_quality_args(encoder: str) -> List[str]:
    """回傳這台機器實測可用的畫質參數。沒測過就用最好的那一組。"""
    if encoder in _chosen_args:
        return list(_chosen_args[encoder])
    return list(_quality_arg_tiers(encoder)[0][1])


ENCODER_OF = {"nvenc": "h264_nvenc", "qsv": "h264_qsv",
              "amf": "h264_amf", "videotoolbox": "h264_videotoolbox"}


# 這些字眼出現的那一行通常才是真正的原因；ffmpeg 7+ 的執行緒排程器
# 會把 "Task finished with error code" 印在最後，直接取末幾行會抓錯重點。
_CAUSE_HINTS = ("cannot load", "driver does not", "no capable", "not supported",
                "unsupported", "no device", "failed to", "unable to", "openencodesession",
                "invalid param", "unloaded", "no such", "permission")


def _pick_cause(stderr_text: str, encoder: str) -> str:
    """從 ffmpeg 的輸出裡挑出真正說明原因的那幾行。"""
    lines = [l.strip() for l in stderr_text.splitlines() if l.strip()]
    named = [l for l in lines if encoder in l or encoder.split("_")[-1] in l.lower()]
    causes = [l for l in named if any(h in l.lower() for h in _CAUSE_HINTS)]
    if not causes:
        causes = [l for l in lines if any(h in l.lower() for h in _CAUSE_HINTS)]
    picked = causes[:2] or named[:2] or lines[-2:]
    return " / ".join(picked)[:400]


def _test_encoder(encoder: str) -> bool:
    """真的丟一小段去編一次，而且用的是實際轉碼會下的那組參數。

    先用最保守的參數當「這個硬體到底在不在」的閘門 —— 沒有 Intel 內顯的機器
    測 QSV 就只會花一次，不會把六組參數全跑一遍。閘門過了才從最好的參數往下找。
    """
    rec: Dict[str, Any] = {"encoder": encoder, "ok": False, "tried": []}
    _hw_probe[encoder] = rec
    ff = resolve_tool("ffmpeg")

    def attempt(label: str, args: List[str]) -> bool:
        cmd = ([ff, "-v", "error", "-f", "lavfi",
                "-i", "color=c=black:s=320x240:d=0.3:r=10",
                "-c:v", encoder] + args +
               ["-pix_fmt", "yuv420p", "-frames:v", "3", "-f", "null", "-"])
        entry: Dict[str, Any] = {"tier": label, "args": " ".join(args) or "(無)"}
        try:
            code, _, err = _run(cmd, timeout=45)
            if code == 0:
                entry["ok"] = True
                rec["tried"].append(entry)
                return True
            msg = (err or b"").decode("utf-8", "ignore").strip()
            entry["ok"] = False
            entry["error"] = _pick_cause(msg, encoder) or f"exit {code}"
        except Exception as e:
            entry["ok"] = False
            entry["error"] = f"{type(e).__name__}: {e}"
        rec["tried"].append(entry)
        return False

    def choose(label: str, args: List[str]) -> bool:
        rec["ok"] = True
        rec["tier"] = label
        rec["args"] = " ".join(args) or "(無)"
        _chosen_args[encoder] = args
        return True

    tiers = _quality_arg_tiers(encoder)
    gate_label, gate_args = tiers[-1]

    # 閘門：最保守的一組都不過，就是硬體不在或驅動不支援，不必再試其他組合
    if not attempt(gate_label, gate_args):
        rec["error"] = rec["tried"][-1].get("error")
        rec["verdict"] = "硬體不可用"
        log.info("%s 不可用：%s", encoder, str(rec["error"])[:160])
        return False

    # 硬體在，從畫質最好的往下找第一個能用的
    for label, args in tiers[:-1]:
        if attempt(label, args):
            if label != tiers[0][0]:
                log.warning("%s：最佳參數不被接受，改用「%s」（%s）",
                            encoder, label, " ".join(args))
            return choose(label, args)

    log.warning("%s：只有最保守的參數能用，畫質控制會失效", encoder)
    rec["verdict"] = "僅預設參數可用"
    return choose(gate_label, gate_args)


def resolve_hwaccel() -> str:
    """FFMPEG_HWACCEL=auto 時自動挑一個真的能用的硬體編碼器。結果會快取。"""
    global _hw_cache
    if _hw_cache is not None:
        return _hw_cache
    want = (settings.hwaccel or "none").lower()
    if want != "auto":
        if want != "none":
            enc = dict(HW_ENCODERS).get(want)
            if enc and not _test_encoder(enc):
                log.warning("指定的 %s (%s) 實測不可用，改用 CPU 轉碼", want, enc)
                _hw_cache = "none"
                return _hw_cache
            log.info("硬體轉碼：使用 %s (%s)", want, enc)
        _hw_cache = want
        return _hw_cache

    try:
        code, out, err = _run([resolve_tool("ffmpeg"), "-hide_banner", "-encoders"], timeout=30)
        listed = (out + err).decode("utf-8", "ignore")
    except Exception:
        listed = ""
    for name, enc in HW_ENCODERS:
        if enc in listed and _test_encoder(enc):
            log.info("硬體轉碼：自動選用 %s (%s)", name, enc)
            _hw_cache = name
            return name
    log.info("硬體轉碼：找不到可用的編碼器，使用 CPU (libx264)")
    _hw_cache = "none"
    return "none"


_hw_disabled_reason: Optional[str] = None


def disable_hwaccel(reason: str) -> None:
    """執行期發現硬體編碼不可靠時，整個關掉改用 CPU（避免每一段都白試一次）。"""
    global _hw_cache, _hw_disabled_reason
    if _hw_cache != "none":
        log.warning("停用硬體轉碼，之後一律用 CPU (libx264)。原因：%s", reason)
    _hw_cache = "none"
    _hw_disabled_reason = reason


def hwaccel_disabled_reason() -> Optional[str]:
    return _hw_disabled_reason


_filters_cache: Optional[set] = None


def available_filters() -> set:
    global _filters_cache
    if _filters_cache is None:
        try:
            code, o, e = _run([resolve_tool("ffmpeg"), "-hide_banner", "-filters"], timeout=30)
            text = (o + e).decode("utf-8", "ignore")
            _filters_cache = {m.group(1) for m in re.finditer(r"^\s*\S+\s+(\S+)\s+", text, re.M)}
        except Exception:
            _filters_cache = set()
    return _filters_cache


def can_tonemap() -> bool:
    """HDR 轉 SDR 需要 zscale（libzimg）與 tonemap 兩個濾鏡。"""
    f = available_filters()
    return "zscale" in f and "tonemap" in f


def h264_level_for(height: int) -> str:
    """依輸出解析度選 H.264 level。

    原本寫死 4.1 —— 那個等級只到 1080p30，4K 輸出會變成不合規串流，
    有些裝置會拒播。
    """
    if height <= 1088:
        return "4.1"
    if height <= 1440:
        return "5.0"
    if height <= 2160:
        return "5.1"
    return "5.2"


def hdr_mode_for(src_w: Optional[int], src_h: Optional[int]) -> str:
    """決定這個片源要用哪種 HDR 處理。

    auto 的判斷是成本導向：完整 tonemap 慢約 1.8 倍，4K 片源在 CPU 上
    會掉到即時速度以下而卡頓，所以 4K 自動改用 fast。
    """
    mode = (settings.hdr_tonemap or "auto").lower()
    if mode in ("off", "fast", "quality"):
        return mode
    pixels = (src_w or 1920) * (src_h or 1080)
    return "fast" if pixels >= 3840 * 1600 else "quality"


def build_video_filters(src_w: Optional[int], src_h: Optional[int], target_h: Optional[int],
                        is_hdr: bool) -> Tuple[List[str], int]:
    """組出影像濾鏡鏈，並回傳實際的輸出高度。"""
    src_h = src_h or 1080
    src_w = src_w or 1920
    want = target_h if (target_h and target_h > 0) else src_h
    out_h = min(want, src_h)
    out_h = out_h - (out_h % 2)          # 編碼器要偶數
    chain: List[str] = []

    if out_h < src_h:
        # 先縮放再做色彩轉換：在 CPU 上這樣快得多。
        # 嚴格說 tonemap 應該在線性光下對原解析度做，但那個代價在這台機器上划不來。
        chain.append(f"scale=-2:{out_h}:flags=bicubic")

    mode = hdr_mode_for(src_w, src_h) if is_hdr else "off"
    if mode != "off" and can_tonemap():
        if mode == "fast":
            # 直接做轉換特性/色域轉換，不進線性光。高光會被削掉而不是滾降，
            # 但色彩是對的，而且幾乎不花額外時間（實測只慢約 19%）。
            chain.append("zscale=p=bt709:t=bt709:m=bt709:r=tv:dither=none")
        else:
            # 標準做法：轉進線性光 → tonemap → 轉回 BT.709。畫質最好，
            # 但實測慢約 1.8 倍，4K 片源在 CPU 上會掉到即時速度以下。
            algo = settings.hdr_tonemap_algo or "hable"
            chain += [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                f"tonemap=tonemap={algo}:desat=0",
                "zscale=p=bt709:t=bt709:m=bt709:r=tv",
            ]

    # 10-bit / 非 4:2:0 的片源都要降成瀏覽器吃得下的格式
    chain.append("format=yuv420p")
    return chain, out_h


def bench_transcode(file_id: int, start: float = 60.0, seconds: float = 6.0,
                    height: Optional[int] = 1080) -> Dict[str, Any]:
    """對同一段影片跑幾種組合並計時，用來分離「到底是哪個設定拖慢的」。

    一次改兩個變數就沒辦法歸因 —— 這支的存在就是為了不要再用猜的。
    """
    row = db.q1("SELECT filename, width, height, is_hdr, video_codec, duration "
                "FROM media_file WHERE id=?", (file_id,))
    if not row:
        raise ValueError("找不到檔案")
    src_w, src_h = row["width"], row["height"]
    is_hdr = bool(row["is_hdr"])
    ff = resolve_tool("ffmpeg")

    combos = [("純軟體解碼 + 不轉色彩", "none", "off")]
    if settings.decode_hwaccel and settings.decode_hwaccel != "none":
        combos.append((f"硬體解碼({settings.decode_hwaccel}) + 不轉色彩", settings.decode_hwaccel, "off"))
    if is_hdr:
        combos += [("純軟體解碼 + fast 轉色彩", "none", "fast"),
                   ("純軟體解碼 + quality tonemap", "none", "quality")]
        if settings.decode_hwaccel and settings.decode_hwaccel != "none":
            combos += [(f"硬體解碼 + fast 轉色彩", settings.decode_hwaccel, "fast"),
                       (f"硬體解碼 + quality tonemap", settings.decode_hwaccel, "quality")]

    saved = settings.hdr_tonemap
    results = []
    try:
        for label, dec, hdr in combos:
            settings.hdr_tonemap = hdr
            vf, out_h = build_video_filters(src_w, src_h, height, is_hdr)
            cmd = [ff, "-v", "error", "-nostdin", "-y"]
            if dec != "none":
                cmd += ["-hwaccel", dec]
            cmd += ["-ss", f"{start:.3f}", "-rw_timeout", "30000000",
                    *source_headers(), "-i", source_url(file_id), "-t", f"{seconds:.3f}",
                    "-map", "0:v:0", "-an", "-sn", "-dn"]
            if vf:
                cmd += ["-vf", ",".join(vf)]
            cmd += ["-c:v", "libx264"] + _encoder_quality_args("libx264") + [
                "-profile:v", "high", "-level", h264_level_for(out_h),
                "-f", "null", "-"]
            t0 = time.time()
            try:
                code, _, err = _run(cmd, timeout=300)
                dt = time.time() - t0
                results.append({"設定": label, "耗時秒": round(dt, 1),
                                "倍速": round(seconds / dt, 2) if dt else None,
                                "ok": code == 0,
                                "錯誤": None if code == 0 else _pick_cause(
                                    (err or b"").decode("utf-8", "ignore"), "libx264")})
            except Exception as e:
                results.append({"設定": label, "ok": False, "錯誤": str(e)[:200]})
    finally:
        settings.hdr_tonemap = saved

    return {
        "檔案": row["filename"],
        "片源": f'{row["video_codec"]} {src_w}x{src_h}{" HDR" if is_hdr else ""}',
        "輸出高度": height or src_h,
        "x264_preset": settings.x264_preset,
        "目前設定": {"decode_hwaccel": settings.decode_hwaccel,
                    "hdr_tonemap": settings.hdr_tonemap,
                    "auto解析為": hdr_mode_for(src_w, src_h) if is_hdr else "不適用"},
        "結果": results,
        "判讀": "倍速 < 1.0 就會卡。挑最快且色彩正確的那一組寫進 .env",
    }


def gpu_info() -> Dict[str, Any]:
    """問顯示卡自己：型號與目前驅動版本。

    nvidia-smi 隨驅動一起安裝，通常在 PATH 或 System32。拿到確切版本才能
    判斷「要不要更新」以及「這張卡支不支援新的驅動分支」。
    """
    out: Dict[str, Any] = {"available": False}
    candidates = ["nvidia-smi"]
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        candidates.append(rf"{sysroot}\System32\nvidia-smi.exe")
    for exe in candidates:
        try:
            code, o, e = _run([exe, "--query-gpu=name,driver_version,memory.total",
                               "--format=csv,noheader"], timeout=20)
        except Exception:
            continue
        if code == 0 and o.strip():
            rows = [r.strip() for r in o.decode("utf-8", "ignore").splitlines() if r.strip()]
            gpus = []
            for r in rows:
                parts = [x.strip() for x in r.split(",")]
                gpus.append({"name": parts[0] if parts else r,
                             "driver": parts[1] if len(parts) > 1 else None,
                             "memory": parts[2] if len(parts) > 2 else None})
            out = {"available": True, "tool": exe, "gpus": gpus}
            break
    return out


def deep_probe_encoder(encoder: str, verbosity: str = "verbose") -> Dict[str, Any]:
    """單一編碼器的深度診斷：用高詳細度跑，回傳**完整**輸出。

    平時的實測用 -v error 又只留末幾行，ffmpeg 7 之後新的執行緒排程器會把
    「Task finished with error code」印在最後，真正的原因（驅動版本不足、
    找不到裝置…）反而在前面被截掉。這支專門用來看那段。
    """
    ff = resolve_tool("ffmpeg")
    variants = [
        ("最小 color 來源", ["-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.3:r=10"]),
        ("testsrc2 來源",   ["-f", "lavfi", "-i", "testsrc2=s=640x480:d=0.3:r=10"]),
    ]
    out: Dict[str, Any] = {"encoder": encoder, "ffmpeg": ff, "runs": []}
    for label, src in variants:
        cmd = [ff, "-hide_banner", "-v", verbosity] + src + [
            "-c:v", encoder, "-pix_fmt", "yuv420p", "-frames:v", "3", "-f", "null", "-"]
        run: Dict[str, Any] = {"variant": label, "cmd": " ".join(cmd)}
        try:
            code, o, e = _run(cmd, timeout=60)
            text = (o + e).decode("utf-8", "ignore")
            run["exit"] = code
            run["ok"] = code == 0
            # 只留跟編碼器有關、或看起來像錯誤的行，避免整包 verbose 洗版
            keep = [l for l in text.splitlines()
                    if any(k in l.lower() for k in (
                        encoder.split("_")[-1], "nvenc", "qsv", "amf", "cuda", "driver",
                        "error", "fail", "unsupported", "invalid", "device", "not "))]
            run["output"] = "\n".join(keep[:40])[:3000] or text[-1500:]
        except Exception as ex:
            run["ok"] = False
            run["output"] = f"{type(ex).__name__}: {ex}"
        out["runs"].append(run)
        if run.get("ok"):
            break
    return out


def hwaccel_probe_report() -> Dict[str, Any]:
    """把硬體編碼器的實測過程攤開來，不用猜為什麼沒吃到 GPU。"""
    resolve_hwaccel()          # 確保已經測過
    out: Dict[str, Any] = {"chosen": _hw_cache, "setting": settings.hwaccel,
                           "disabled_reason": _hw_disabled_reason, "candidates": []}
    try:
        code, o, e = _run([resolve_tool("ffmpeg"), "-hide_banner", "-encoders"], timeout=30)
        listed_text = (o + e).decode("utf-8", "ignore")
    except Exception:
        listed_text = ""
    for name, enc in HW_ENCODERS:
        rec = dict(_hw_probe.get(enc) or {})
        rec.setdefault("encoder", enc)
        rec["name"] = name
        rec["listed_by_ffmpeg"] = enc in listed_text
        rec["presets_supported"] = encoder_option_values(enc, "preset")[:24]
        if not rec.get("listed_by_ffmpeg") and "error" not in rec:
            rec["error"] = "這個 ffmpeg 沒有編進這個編碼器"
        out["candidates"].append(rec)
    out["chosen_args"] = {k: " ".join(v) or "(無)" for k, v in _chosen_args.items()}
    return out


def make_thumbnail(file_id: int, at_seconds: float, out_name: str, width: int = 480) -> Optional[str]:
    dest = IMAGE_DIR / out_name
    if dest.exists() and dest.stat().st_size > 0:
        return out_name
    cmd = [
        resolve_tool("ffmpeg"), "-v", "error", "-y",
        "-ss", f"{max(at_seconds, 0):.2f}",
        "-rw_timeout", "30000000",
        *source_headers(), "-i", source_url(file_id),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "4",
        str(dest),
    ]
    try:
        code, _, err = _run(cmd, timeout=120)
        if code == 0 and dest.exists() and dest.stat().st_size > 0:
            return out_name
        log.warning("縮圖失敗 file=%s: %s", file_id, (err or b"").decode("utf-8", "ignore")[:200])
    except subprocess.TimeoutExpired:
        log.warning("縮圖逾時 file=%s", file_id)
    return None


def subtitle_cache_path(file_id: int, stream_index: int,
                        mtime: Any = None, size: Any = None) -> Path:
    """抽好的字幕存這裡。檔名帶 mtime/size 的雜湊，來源檔換過就自動失效。

    mtime 可能是 epoch 數字，也可能是 ISO 字串（不同 FTP 伺服器格式不一），
    所以不要假設型別，直接雜湊字串內容。
    """
    tag = hashlib.sha1(f"{mtime}|{size}".encode("utf-8")).hexdigest()[:12]
    return SUB_DIR / f"{file_id}_s{stream_index}_{tag}.vtt"


def _sub_cmd(file_id: int, stream_index: int) -> List[str]:
    return [
        resolve_tool("ffmpeg"), "-v", "error",
        "-rw_timeout", "30000000",
        *source_headers(), "-i", source_url(file_id),
        "-map", f"0:{stream_index}",
        "-c:s", "webvtt", "-f", "webvtt",
        # 逐筆送出，不要在 stdio buffer 裡壓著。少了這個就算改成串流回應，
        # 前面幾句字幕還是要等湊滿一個 buffer 才會吐出來。
        "-flush_packets", "1",
        "pipe:1",
    ]


class _SubtitleExtract:
    """抽字幕的 ffmpeg 行程 + 快取檔。同步與非同步兩種讀法共用這一份收尾邏輯。"""

    def __init__(self, file_id: int, stream_index: int, cache_path: Optional[Path] = None):
        self.file_id = file_id
        self.index = stream_index
        self.cache_path = cache_path
        self.tmp: Optional[Path] = None
        self.fh = None
        self.proc: Optional[subprocess.Popen] = None
        self.ok = False

    def start(self) -> "_SubtitleExtract":
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.tmp = self.cache_path.with_name(
                f"{self.cache_path.name}.{os.getpid()}.{time.time_ns()}.part")
            self.fh = open(self.tmp, "wb")
        self.proc = subprocess.Popen(_sub_cmd(self.file_id, self.index),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self

    def read(self) -> bytes:
        # read1 而不是 read：read(n) 會一直等到湊滿 n 個位元組才回來，
        # 那就等於又把輸出攢成一塊塊的，串流就沒意義了。read1 有多少給多少。
        chunk = self.proc.stdout.read1(65536)
        if chunk and self.fh:
            self.fh.write(chunk)
        return chunk

    def wait(self) -> None:
        try:
            code = self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            code = -1
        if code != 0:
            err = scrub_bytes(self.proc.stderr.read() or b"").decode("utf-8", "ignore")[:300]
            log.warning("抽字幕失敗 file=%s idx=%s: %s", self.file_id, self.index, err)
        self.ok = code == 0

    def close(self) -> None:
        p = self.proc
        if p is not None:
            if p.poll() is None:
                log.info("字幕串流中斷，收掉 ffmpeg file=%s idx=%s", self.file_id, self.index)
                try:
                    p.kill()
                except Exception:
                    pass
            for st in (p.stdout, p.stderr):
                try:
                    st.close()
                except Exception:
                    pass
        if self.fh:
            self.fh.close()
            self.fh = None
        if self.tmp:
            # 只有完整抽完才留下快取，半截的快取比沒有快取更糟
            if self.ok and self.tmp.exists() and self.tmp.stat().st_size > 0:
                try:
                    os.replace(self.tmp, self.cache_path)
                    prune_subtitle_cache()
                except Exception:
                    self.tmp.unlink(missing_ok=True)
            else:
                self.tmp.unlink(missing_ok=True)
            self.tmp = None


def prune_subtitle_cache(max_files: int = 500) -> None:
    """字幕快取單檔很小（幾十 KB），但來源檔改過就會留下一個舊的孤兒檔，
    長期下來會愈積愈多。超過上限就把最久沒被讀取的刪掉。"""
    try:
        files = []
        for p in SUB_DIR.glob("*.vtt"):
            try:
                files.append((p.stat().st_atime, p))
            except OSError:
                pass
        if len(files) <= max_files:
            return
        files.sort()
        for _, p in files[:len(files) - max_files]:
            try:
                p.unlink()
            except OSError:
                pass
        log.info("字幕快取清理完成，剩 %d 個", max_files)
    except Exception as e:
        log.warning("字幕快取清理失敗: %s", e)


def clear_subtitle_cache(file_id: Optional[int] = None) -> None:
    pattern = f"{file_id}_s*.vtt" if file_id else "*.vtt"
    for p in SUB_DIR.glob(pattern):
        try:
            p.unlink()
        except OSError:
            pass


def iter_subtitle_vtt(file_id: int, stream_index: int,
                      cache_path: Optional[Path] = None) -> Iterator[bytes]:
    """一邊抽字幕一邊吐出來，同時寫進快取。

    內嵌字幕的封包散在整個檔案裡，ffmpeg 必須把整部片 demux 過一遍才收得齊，
    大檔案又透過 FTP 讀，整個抽完可能要好幾分鐘。所以不要等抽完才回應 ——
    邊抽邊送，播放器就能先顯示前面的字幕，後面的在背景繼續補。
    """
    ex = _SubtitleExtract(file_id, stream_index, cache_path).start()
    try:
        while True:
            chunk = ex.read()
            if not chunk:
                break
            yield chunk
        ex.wait()
    finally:
        ex.close()


async def aiter_subtitle_vtt(file_id: int, stream_index: int,
                             cache_path: Optional[Path] = None) -> "AsyncIterator[bytes]":
    """給 HTTP 串流回應用的非同步版本。

    這裡一定要是「非同步」產生器：使用者換軌或關掉頁面時，Starlette 是靠取消
    stream_response 來收尾的。同步產生器被丟到執行緒池裡跑，取消不會傳進去，
    ffmpeg 就會繼續把整部片讀完，白白佔著 FTP 頻寬。非同步產生器則會在 await
    的地方收到取消、走進 finally，才能真的把行程收掉。
    """
    ex = _SubtitleExtract(file_id, stream_index, cache_path).start()
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(ex.read)
            if not chunk:
                break
            yield chunk
        await anyio.to_thread.run_sync(ex.wait)
    finally:
        ex.close()


def extract_subtitle_vtt(file_id: int, stream_index: int) -> Optional[bytes]:
    """整段抽完才回傳。給不需要串流的呼叫端用。"""
    out = b"".join(iter_subtitle_vtt(file_id, stream_index))
    return out or None


def convert_subtitle_bytes(data: bytes, src_ext: str) -> Optional[bytes]:
    """把外掛字幕檔（srt/ass/ssa/sub）轉成 WebVTT。"""
    if src_ext == "vtt":
        return data
    cmd = [resolve_tool("ffmpeg"), "-v", "error", "-f", src_ext if src_ext != "sub" else "microdvd",
           "-i", "pipe:0", "-c:s", "webvtt", "-f", "webvtt", "pipe:1"]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception as e:
        log.warning("字幕轉檔失敗: %s", e)
    return None


def build_transcode_cmd(file_id: int, start: float, duration: float,
                        height: Optional[int], audio_index: Optional[int],
                        force_software: bool = False,
                        bitrate_kbps: int = 0) -> List[str]:
    """產生單一 HLS 分段的 ffmpeg 指令（輸出 mpegts 到 stdout）。"""
    s = settings
    hw = "none" if force_software else resolve_hwaccel()
    pre: List[str] = [resolve_tool("ffmpeg"), "-v", "error", "-nostdin", "-y"]

    # 解碼加速獨立於編碼加速 —— NVDEC 和 NVENC 是不同的硬體單元，
    # 驅動版本擋掉編碼時，解碼往往還能用。auto 讓 ffmpeg 自己挑，
    # 解不了的片源會自動退回 CPU 解碼，不會整段失敗。
    dec = s.decode_hwaccel
    if dec and dec != "none":
        pre += ["-hwaccel", dec]

    pre += [
        "-ss", f"{start:.3f}",
        "-rw_timeout", "30000000",
        *source_headers(), "-i", source_url(file_id),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0",
        "-map", f"0:{audio_index}" if audio_index is not None else "0:a:0?",
        "-sn", "-dn",
    ]

    row = db.q1("SELECT width, height, is_hdr FROM media_file WHERE id=?", (file_id,))
    src_w = row["width"] if row else None
    src_h = row["height"] if row else None
    is_hdr = bool(row["is_hdr"]) if row else False

    vf, out_h = build_video_filters(src_w, src_h, height if height else s.max_height, is_hdr)
    if vf:
        pre += ["-vf", ",".join(vf)]

    encoder = ENCODER_OF.get(hw, "libx264")
    pre += ["-c:v", encoder] + _encoder_quality_args(encoder)
    if bitrate_kbps > 0:
        # 遠端觀看時把峰值鎖住，避免高動態畫面瞬間爆掉上傳頻寬
        pre += ["-maxrate", f"{bitrate_kbps}k", "-bufsize", f"{bitrate_kbps * 2}k"]

    pre += [
        "-profile:v", "high", "-level", h264_level_for(out_h),
        "-force_key_frames", "expr:gte(t,0)",
        "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", f"{max(64, s.audio_bitrate_kbps)}k", "-ar", "48000",
        "-muxdelay", "0", "-muxpreload", "0",
        "-output_ts_offset", f"{start:.3f}",
    ]
    if s.audio_channels > 0:
        pre += ["-ac", str(s.audio_channels)]
    pre += ["-f", "mpegts", "pipe:1"]
    return pre
