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

from . import db, localfs
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
    _chosen_tier.clear()
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


def input_args(file_id: int) -> List[str]:
    """`-i` 及其前面該帶的參數。**認得的路徑直接讀檔**（第 −1 層）。

    片庫其實就在同一台機器上（`FTP_HOST=127.0.0.1`，FTP 根目錄就是 `D:\\1.FTP`），
    所以走 `source_url()` 的話每一段都是
    「HTTP → FastAPI → FtpReadStream → FTP 伺服器（loopback） → 磁碟」四層 ——
    讀的是同一顆磁碟上的同一個檔案，而且每 6 秒一段就重來一次
    （新連線、`REST` 到偏移量、重新開始讀）。

    對上階（remux）尤其重要：它一段要搬 2～3 MB 的原始位元組，
    四層搬運的成本會直接吃掉「remux 幾乎不花資源」這個前提。

    對應不到就照舊走 HTTP —— 那是「來源可以在別台機器」的抽象，
    而且本機直讀沒設定時就是它在頂著。
    """
    row = db.q1("SELECT ftp_path FROM media_file WHERE id=?", (file_id,))
    if row:
        local = localfs.resolve_local(row["ftp_path"] or "")
        if local is not None:
            return ["-i", str(local)]
    return [*source_headers(), "-rw_timeout", "30000000", "-i", source_url(file_id)]


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


class Cancelled(RuntimeError):
    """外部要求中止，子行程已經被終止。

    跟「失敗」要分開：失敗的檔案下次掃描會重試，被中止的不該被標成失敗。
    """


def _run(cmd: List[str], timeout: int = 120, cancel=None) -> Tuple[int, bytes, bytes]:
    """跑一支外部工具。傳了 cancel（threading.Event）就是可中止版本。

    為什麼需要可中止版本：subprocess.run 一旦開始就只能等它自己結束或逾時，
    而 ffprobe 的逾時是 90 秒。使用者按下「停止掃描」之後，正在跑的每一支
    都還要跑完，併發 3 就是最多等 4 分半 —— 看起來就是「按了沒反應」。
    光在函式進入點檢查旗標解決不了這件事，要能真的把子行程殺掉。
    """
    if cancel is None:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        # 統一在這裡遮掉憑證：底下所有呼叫端拿到的都已經是安全的，
        # 不必每個錯誤處理點都記得自己過濾（漏一個就前功盡棄）。
        return p.returncode, scrub_bytes(p.stdout), scrub_bytes(p.stderr)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    while True:
        try:
            # 逾時後再呼叫一次 communicate 不會掉資料（CPython 有保證），
            # 所以可以拿它當「每 0.4 秒回頭看一眼旗標」的輪詢迴圈用。
            out, err = proc.communicate(timeout=0.4)
            return proc.returncode, scrub_bytes(out), scrub_bytes(err)
        except subprocess.TimeoutExpired:
            pass
        stop = cancel.is_set()
        if not stop and time.monotonic() < deadline:
            continue
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()             # 不理 SIGTERM 的話就不客氣了
            proc.communicate()
        if stop:
            raise Cancelled("已中止")
        raise subprocess.TimeoutExpired(cmd, timeout)


def run_tool(cmd: List[str], timeout: int = 120, cancel=None) -> Tuple[int, bytes, bytes]:
    """給同一個 package 裡其他模組用的公開入口（keyframes.py 在用）。

    直接包 _run 而不是讓別人去碰私有函式：可中止與「出口一律遮憑證」
    這兩件事都在 _run 裡面，繞過它就是繞過那兩層保護。
    """
    return _run(cmd, timeout=timeout, cancel=cancel)


def _ffprobe_json(file_id: int, timeout: int = 90, cancel=None) -> Dict[str, Any]:
    # 探測也走本機直讀（第 −1 層）。`input_args()` 會回 ["-i", 路徑]，
    # 而 ffprobe 吃得下 `-i`（它只是不常這樣寫）—— 統一用同一個入口，
    # 才不會出現「轉碼讀本機、探測讀 HTTP」這種一半的狀態。
    cmd = [
        resolve_tool("ffprobe"), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        *input_args(file_id),
    ]
    code, out, err = _run(cmd, timeout=timeout, cancel=cancel)
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


def _ffmpeg_probe(file_id: int, timeout: int = 120, cancel=None) -> Dict[str, Any]:
    cmd = [resolve_tool("ffmpeg"), "-hide_banner", *input_args(file_id)]
    # 沒指定輸出檔，ffmpeg 一定以非 0 結束，資訊在 stderr —— 這是預期行為
    code, _, err = _run(cmd, timeout=timeout, cancel=cancel)
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


def ffprobe(file_id: int, timeout: int = 90, cancel=None) -> Dict[str, Any]:
    """優先用 ffprobe；沒有 ffprobe 時退回解析 ffmpeg 的輸出。

    掃描時把 cancel 事件傳進來，按下「停止掃描」才能真的把子行程殺掉，
    而不是等滿 90 秒的逾時。
    """
    global _warned_fallback
    try:
        return _ffprobe_json(file_id, timeout=timeout, cancel=cancel)
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
    return _ffmpeg_probe(file_id, timeout=max(timeout, 120), cancel=cancel)


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
                # ffmpeg 4.x 的常數列沒有數值那一欄，5.x 之後才有。
                # 數值寫成必填的話，4.x 上這裡永遠抓到空清單，於是 pick_option()
                # 一律回 None —— preset 與 rc 都選不到，NVENC 的 tier 退到「僅 cq」，
                # 而那組沒有 -rc vbr 也沒有 -b:v 0，等於 CRF 對硬體編碼失效。
                #   ffmpeg 4.2   →      slow                    E..V..... hq 2 passes
                #   ffmpeg 5.x+  →      p4          15          E..V....... medium
                vm = re.match(r"^([A-Za-z0-9_.\-]+)\s+(?:[-]?\d+\s+)?[EDAVS.]{6,}", stripped)
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


def _rate_args(encoder: str, target_kbps: int) -> List[str]:
    """這一段要怎麼控制位元率：有目標碼率就綁碼率，沒有就綁品質。

    **有目標碼率時 NVENC 要走碼率導向，不要走 cq。**兩個理由：

    1. v4 量到 cq 模式下有「更多碼率、更低分」的異常 —— cq 28 時「參數調滿」
       花 3,305 kbps 拿到 VMAF 92.8，而「現況參數」花 2,579 kbps 拿到 94.0。
       最可能是 `-spatial-aq`（還沒查清）。**綁碼率的那兩條階梯不受這個影響**
       —— v4／v5 的兩條獨立階梯在同碼率上量到同一個分數（差 0.1～0.4）。
    2. 兩階 HLS 的下階要在 master playlist 宣告 `BANDWIDTH`，
       而 `BANDWIDTH` 是承諾的上界。**cq 導向給不出上界**（同一個 cq 在
       不同片源上吐 1,093 到 10,195 kbps 都有），碼率導向才給得出來。

    x264 則是「capped CRF」：`-crf` 決定畫質、`-maxrate` 只當天花板 ——
    v4／v5 的量測就是用這個組合跑的，所以照它。實測 CRF 21 在片源 A 只吐
    3,174 kbps，上限 12000 完全咬不到；真正決定畫質的是 CRF，上限是保險。
    """
    if target_kbps > 0:
        cap = [f"-maxrate", f"{target_kbps}k", "-bufsize", f"{target_kbps * 2}k"]
        if encoder == "h264_nvenc":
            # 碼率導向：-b:v 就是目標，不再給 -cq
            return ["-b:v", f"{target_kbps}k"] + cap
        if encoder == "libx264":
            # capped CRF：畫質由 CRF 決定，碼率只是天花板
            return ["-crf", str(settings.crf)] + cap
        return cap
    if encoder == "h264_nvenc":
        # **NVENC 的 -cq 不是 x264 的 -crf。**實測同一部 1080p 片源、同樣寫 21：
        # x264 veryfast 用 3,238 kbps 拿到 SSIM 0.9742，NVENC 用 10,195 kbps
        # （原檔是 10,891）才拿到 0.9767 —— 幾乎等於沒有壓縮。
        # 所以硬體編碼有自己的一把尺，用 NVENC_CQ，不要跟 TRANSCODE_CRF 共用。
        #
        # `-b:v 0` 一定要帶。少了它，ffmpeg 的預設位元率就變成實際目標，
        # cq 給多少都沒有用 —— 這是「畫質設定沒有作用」那一類的失效。
        return ["-cq", str(settings.nvenc_cq), "-b:v", "0"]
    if encoder == "libx264":
        return ["-crf", str(settings.crf)]
    return []


def _quality_arg_tiers(encoder: str,
                       target_kbps: int = 0) -> List[Tuple[str, List[str]]]:
    """同一個編碼器的參數組合，由好到保守。

    硬體編碼器在不同 ffmpeg 版本、不同驅動下能吃的參數差很多。與其猜，
    不如按順序實際試 —— 第一個過的就是這台機器實際能用的最好設定。

    `target_kbps` 只換掉「位元率怎麼控制」那幾個參數（見 `_rate_args`），
    **階層的名字與數量不變** —— `_test_encoder` 記住的是層級的名字，
    不是那一串數字，所以探測時（沒有目標碼率）記下的「完整」，
    在實際轉碼時（有目標碼率）仍然對應到同一層。
    """
    crf = str(settings.crf)
    if encoder == "h264_nvenc":
        rate = _rate_args(encoder, target_kbps)
        preset = pick_option(encoder, "preset", ["p5", "p4", "slow", "hq", "medium"])
        rc = pick_option(encoder, "rc", ["vbr", "vbr_hq", "constqp"])
        pre = ["-preset", preset] if preset else []
        tiers: List[Tuple[str, List[str]]] = []
        if rc:
            tiers.append(("完整", pre + ["-rc", rc] + rate + ["-bf", "0"]))
            tiers.append(("無 bf", pre + ["-rc", rc] + rate))
        tiers.append(("無 rc", pre + rate))
        tiers.append(("僅品質", rate))
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
    return [("完整", ["-preset", settings.x264_preset]
                    + _rate_args("libx264", target_kbps))]


# 實測通過的那一組參數，之後就固定用它
_chosen_args: Dict[str, List[str]] = {}
# 通過的是「哪一層」，不是「哪幾個數字」。記標籤才不會把畫質數字凍住 ——
# 見 _encoder_quality_args 的說明。
_chosen_tier: Dict[str, str] = {}


def _encoder_quality_args(encoder: str, target_kbps: int = 0) -> List[str]:
    """回傳這台機器實測可用的畫質參數。沒測過就用最好的那一組。

    **不能直接回傳實測當下那份 list。**那份 list 裡的 `-cq 21`／`-crf 21`
    是探測那一刻的 settings 值；之後在後台把 NVENC_CQ 改成 28，
    UI 會說「已生效」，而硬體編碼仍然在用 21 —— 正是 B+ 節點名的
    「最容易悄悄漂移」那一項。所以記住的是**通過的層級**（標籤），
    每次呼叫都用當下的設定重新組一次。

    `target_kbps` 走同一條路：層級記憶不變，只是那一層的位元率參數
    換成碼率導向（見 `_rate_args`）。探測是在沒有目標碼率的情況下做的，
    這樣才不會為了每一個不同的碼率各探測一次 —— 而 `-b:v` 與 `-cq`
    的差別不影響「這張卡吃不吃 `-rc`／`-bf`」，那才是探測在問的事。
    """
    tiers = _quality_arg_tiers(encoder, target_kbps)
    label = _chosen_tier.get(encoder)
    if label:
        for name, args in tiers:
            if name == label:
                return list(args)
        # 找不到同名的層（例如換了 ffmpeg 之後可用選項變了）→ 用實測那份保底
        return list(_chosen_args.get(encoder, tiers[0][1]))
    return list(tiers[0][1])


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
        _chosen_tier[encoder] = label
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


def hdr_mode_for(src_w: Optional[int], src_h: Optional[int],
                 setting: Optional[str] = None) -> str:
    """決定這個片源要用哪種 HDR 處理。

    auto 的判斷是成本導向：完整 tonemap 慢約 1.8 倍，4K 片源在 CPU 上
    會掉到即時速度以下而卡頓，所以 4K 自動改用 fast。

    setting 讓呼叫端指定要試哪一種（轉碼實測會逐一試過）。
    """
    mode = (setting or settings.hdr_tonemap or "auto").lower()
    if mode in ("off", "fast", "quality"):
        return mode
    pixels = (src_w or 1920) * (src_h or 1080)
    return "fast" if pixels >= 3840 * 1600 else "quality"


def quality_class(width: Optional[int], height: Optional[int]) -> str:
    """把解析度講成一般人認得的級別。**依寬度判，不是依高度。**

    片庫裡真正 1920×1080 的只有 32 部，而 1920 寬、高度 800／802／804／960 的
    有 88 部 —— 那些是 2.35:1～2.4:1 的寬螢幕片，黑邊在壓製時就裁掉了，
    所以畫面本來就比 1080 矮。**照高度標的話，一部 1920×804 的藍光會被寫成
    「804p」**，看起來像被降級了，其實它就是原檔。Plex／Jellyfin 也是照寬度判。

    回傳的是級別字串（"1080p"），不是解析度 —— 兩個都要給使用者看，
    級別讓人知道「這是哪一檔的畫質」，解析度讓人知道「實際上是什麼」。
    """
    w = int(width or 0)
    h = int(height or 0)
    if w >= 3840 or h >= 2000:
        return "4K"
    if w >= 2560 or h >= 1400:
        return "1440p"
    if w >= 1900 or h >= 1000:
        return "1080p"
    if w >= 1280 or h >= 700:
        return "720p"
    if w >= 854 or h >= 460:
        return "480p"
    return f"{h}p" if h else "—"


def scaled_size(src_w: Optional[int], src_h: Optional[int],
                target_h: int) -> Tuple[int, int]:
    """縮到 target_h 之後的實際解析度（跟 build_video_filters 用同一條規則）。"""
    sw = int(src_w or 1920)
    sh = int(src_h or 1080)
    out_h = min(target_h, sh) if target_h > 0 else sh
    out_h -= out_h % 2
    out_w = int(sw * out_h / sh / 2) * 2 if sh else sw
    return out_w, out_h


def build_video_filters(src_w: Optional[int], src_h: Optional[int], target_h: Optional[int],
                        is_hdr: bool, tonemap: Optional[str] = None) -> Tuple[List[str], int]:
    """組出影像濾鏡鏈，並回傳實際的輸出高度。

    tonemap 是「這一次要用哪種 HDR 處理」，只給轉碼實測用。
    原本實測是暫時改掉全域的 settings.hdr_tonemap 再改回來 —— 那有兩個問題：
    實測進行中**每一個正在看片的人**都會跟著換設定，而且設定現在是即時解析的
    property，根本不能指派。改成把值當參數傳下去。
    """
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

    mode = hdr_mode_for(src_w, src_h, tonemap) if is_hdr else "off"
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

    results = []
    for label, dec, hdr in combos:
        vf, out_h = build_video_filters(src_w, src_h, height, is_hdr, tonemap=hdr)
        cmd = [ff, "-v", "error", "-nostdin", "-y"]
        if dec != "none":
            cmd += ["-hwaccel", dec]
        # 實測要跟生產走同一條讀取路徑，否則量到的倍速不是實際的倍速
        cmd += ["-ss", f"{start:.3f}", *input_args(file_id), "-t", f"{seconds:.3f}",
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
        *input_args(file_id),
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


def subtitle_cached(file_id: int, stream_index: int,
                    mtime: Any = None, size: Any = None) -> bool:
    """這一軌是不是已經抽好躺在快取裡了。

    給 /api/play 回報用。前端拿它決定「要不要在背景預抽」——
    沒有這個欄位的話，每次開播都會對已經抽好的檔案再跑一次 ffmpeg：
    伺服器最後雖然還是走 FileResponse，但前端已經先發出一個沒必要的請求，
    而且沒辦法在設定面板上顯示哪一軌已經就緒。

    只 stat 一個路徑，成本可以忽略。
    """
    try:
        return subtitle_cache_path(file_id, stream_index, mtime, size).exists()
    except Exception:
        return False


def _sub_cmd(file_id: int, stream_index: int) -> List[str]:
    return [
        resolve_tool("ffmpeg"), "-v", "error",
        *input_args(file_id),
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


def audio_can_copy(file_id: int, audio_index: Optional[int]) -> bool:
    """這個音軌能不能直接 copy 到上階去。

    上階的視訊是 `-c:v copy`，音訊則要看情況：mkv 裡常見的是 AC3／DTS／TrueHD，
    瀏覽器不吃，一定要轉 AAC。已經是 AAC 的話就 copy —— **但聲道數必須符合
    `AUDIO_CHANNELS`**，否則「降混成立體聲」這個設定會在上階整個失效
    （5.1 的 AAC 包在 mpegts 裡，各家瀏覽器與 hls.js 的支援度並不一致，
    那正是 AUDIO_CHANNELS 存在的理由）。
    """
    row = db.q1("SELECT audio_tracks, audio_codec FROM media_file WHERE id=?", (file_id,))
    if not row:
        return False
    want_ch = max(0, int(settings.audio_channels))
    tracks = json.loads(row["audio_tracks"] or "[]") if row["audio_tracks"] else []
    track = None
    if audio_index is not None:
        track = next((t for t in tracks if t.get("index") == audio_index), None)
    elif tracks:
        track = next((t for t in tracks if t.get("default")), tracks[0])
    codec = ((track or {}).get("codec") or row["audio_codec"] or "").lower()
    if codec not in ("aac",):
        return False
    if want_ch <= 0:
        return True                     # 0 = 不動原始聲道
    ch = (track or {}).get("channels")
    return bool(ch) and int(ch) == want_ch


def build_remux_cmd(file_id: int, start: float, duration: float,
                    audio_index: Optional[int]) -> List[str]:
    """上階的單一分段：視訊照抄，音訊按需轉 AAC（J 章第 0 層）。

    **為什麼不走 build_transcode_cmd 的分支而是自己一支。**兩者共用的部分
    （來源、`-ss`／`-t`、`-map`、mpegts 輸出）不到一半，而不共用的部分是
    「絕對不能出現」的那些：`-vf`、編碼器參數、`-profile:v`／`-level`、
    `-force_key_frames`、`-sc_threshold`。混在一個函式裡用 if 擋，
    將來加一個轉碼參數就有機會漏進 copy 的路徑 —— 而那不會報錯，
    只會讓「零損失」這句話悄悄變成假的。

    **`-ss` 一定要在 `-i` 前面（輸入端 seek）。**copy 模式只能從 keyframe 起頭，
    而我們的 `start` 就是邊界表裡的 keyframe 時間戳，所以兩者剛好吻合。
    放到輸出端 seek 的話 ffmpeg 會從 0 開始解，然後丟掉前面 —— 慢，而且
    每一段都要從頭掃一次。

    **`-avoid_negative_ts` 一定要是 `disabled`，不可以是 `make_zero`。**
    這兩個選項會互相抵銷：`make_zero` 的語意是「把這一段的時間軸平移到 0」，
    而它是在 `-output_ts_offset` **之後**才套用的 —— 於是 offset 寫進去的
    絕對位置被整個抹掉，每一段都從 0 開始。

    實測後果（file 433，2:33:55 的片）：1540 段每段宣告 6 秒、實際各自從 0
    起算，hls.js 只好把它們一段接一段地串起來，於是 MediaSource 的 duration
    變成 1540 × 4.816 = 7416 秒 —— **播放器右下角的總時間就從 2:33:55 縮成
    2:03:36**，而且 seek 到 95% 會直接落在「它以為的片尾」而觸發 ended。
    這正是「總時間突然變短、拖到後面就跳片尾」的後端根因。

    更嚴重的是下階（transcode）**沒有**這個旗標，它的時間戳是對的 ——
    所以兩階的時間軸原本是不一致的：切一次畫質，currentTime 就會對到
    另一條軸上。

    `disabled` 明確要求 muxer 不要改時間戳。非單調 DTS 的片源仍然靠
    `-ss`（輸入端 seek，從 keyframe 起頭）與 mpegts 本身的容忍度處理；
    真的整段 muxer 吐錯時 `_produce()` 會把該檔案的上階下線，那是既有的
    退路，不需要用「把時間軸弄壞」來換。
    """
    pre = [resolve_tool("ffmpeg"), "-v", "error", "-nostdin", "-y",
           "-ss", f"{start:.3f}",
           *input_args(file_id),
           "-t", f"{duration:.3f}",
           "-map", "0:v:0",
           "-map", f"0:{audio_index}" if audio_index is not None else "0:a:0?",
           "-sn", "-dn",
           "-c:v", "copy"]
    if audio_can_copy(file_id, audio_index):
        pre += ["-c:a", "copy"]
    else:
        pre += ["-c:a", "aac", "-b:a", f"{max(64, settings.audio_bitrate_kbps)}k",
                "-ar", "48000"]
        if settings.audio_channels > 0:
            pre += ["-ac", str(settings.audio_channels)]
    pre += ["-avoid_negative_ts", "disabled",
            "-muxdelay", "0", "-muxpreload", "0",
            "-output_ts_offset", f"{start:.3f}",
            "-f", "mpegts", "pipe:1"]
    return pre


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
        *input_args(file_id),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0",
        "-map", f"0:{audio_index}" if audio_index is not None else "0:a:0?",
        "-sn", "-dn",
    ]

    row = db.q1("SELECT width, height, is_hdr FROM media_file WHERE id=?", (file_id,))
    src_w = row["width"] if row else None
    src_h = row["height"] if row else None
    is_hdr = bool(row["is_hdr"]) if row else False

    # height=0 是「不縮放」，不是「沒指定」—— 用 `or` 會把它退回 max_height。
    vf, out_h = build_video_filters(src_w, src_h,
                                    s.max_height if height is None else height, is_hdr)
    if vf:
        pre += ["-vf", ",".join(vf)]

    encoder = ENCODER_OF.get(hw, "libx264")
    # 位元率控制整個交給 _encoder_quality_args ——「上限」與「目標」是同一件事的
    # 兩面，分兩個地方加會出現 `-maxrate` 出現兩次（後者靜默勝出）這種難查的狀況。
    # 遠端觀看時這裡就是把峰值鎖住的地方，避免高動態畫面瞬間爆掉上傳頻寬。
    pre += ["-c:v", encoder] + _encoder_quality_args(encoder, bitrate_kbps)

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
