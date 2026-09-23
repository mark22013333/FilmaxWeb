"""FTP 路徑 → 本機路徑的捷徑（規格：J 章第 −1 層）。

片庫其實就在同一台機器上：`FTP_HOST` 是 `127.0.0.1`，FTP 的根目錄 `/` 就是
`D:\\1.FTP`。所以現在每一段轉碼走的是

    ffmpeg → HTTP 127.0.0.1/api/stream/{id} → FastAPI → FtpReadStream
           → FTP 伺服器（loopback） → D:\\ 上的檔案

**四層搬運，讀的是同一顆磁碟上的同一個檔案。**這個模組是那條捷徑：
認得的路徑直接回本機 `Path`，認不得就回 `None`，呼叫端照舊走 FTP
（別台機器掛遠端 FTP 時就是靠這個 fallback）。

--------------------------------------------------------------------------
**這個模組會擴大服務讀得到的範圍，所以每一個判斷都往嚴的那一邊倒。**

`ftp_path` 來自資料庫，而資料庫的內容來自掃描到的檔名 —— 不是使用者當場輸入的，
但也不是我們自己產生的常數。用「先正規化再確認落在 root 之內」而不是
「檢查有沒有壞東西」：白名單式的判斷不必去想還有哪種繞過寫法沒擋到。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .config import settings

log = logging.getLogger("filmax.localfs")

# resolve_local_info() 的 reason。"ok" 以外的每一種都是「照舊走 FTP」。
REASONS = {
    "ok": "本機直讀",
    "disabled": "沒有設定 LIBRARY_LOCAL_ROOTS",
    "empty": "沒有路徑",
    "invalid": "路徑不合法（含 NUL 或 ..）",
    "no_prefix": "沒有任何一條 LIBRARY_LOCAL_ROOTS 涵蓋這個 FTP 路徑",
    "escape": "resolve 之後逃出了本機根目錄",
    "not_found": "對應到的本機檔案不存在",
    "not_file": "對應到的本機路徑不是檔案",
    "unreadable": "本機檔案存在，但服務帳號讀不到",
    "os_error": "解析本機路徑時出錯",
}


def enabled() -> bool:
    return bool(settings.library_local_roots)


def resolve_local(ftp_path: str) -> Optional[Path]:
    """認得就回本機檔案路徑，否則回 None。

    回 None 的每一種情況都是「照舊走 FTP」，不是錯誤。
    想知道**為什麼**是 None，用 `resolve_local_info()`。

    回傳的是 `resolve()` 之後的路徑：Windows 的 8.3 短檔名
    （`C:\\Users\\ADMINI~1\\...`）會被展開成長路徑。拿它去跟「自己組出來的
    字串」做等值比對前要記得這件事 —— 對 ffmpeg 沒有影響，兩種寫法都開得起來。
    """
    info = resolve_local_info(ftp_path)
    return info["path"] if info["reason"] == "ok" else None


def resolve_local_info(ftp_path: str) -> Dict[str, Any]:
    """跟 `resolve_local()` 同一套判斷，但回傳**為什麼**。

    回 `{"reason", "prefix", "path"}`：`reason` 是 `REASONS` 的鍵，
    `prefix` 是命中的那一條對應（沒有命中就是 None），`path` 只有
    `reason == "ok"` 時才有值；對不到但算得出候選路徑時放在 `candidate`
    （給後台診斷看，不給一般使用者）。

    **存在的理由。**`resolve_local()` 只回 `Path | None`，於是「根本沒有
    對應」「對應到了但檔案被搬走」「權限不夠」全都長得一樣 —— 唯一的線索是
    ffmpeg 的輸入變成 `http://127.0.0.1/.../api/stream`。production 上
    `/Yu` 那一整個 FTP mount（2761 支影片）就是這樣安靜地退回 FTP loopback 的。
    """
    out: Dict[str, Any] = {"reason": "disabled", "prefix": None, "path": None,
                           "candidate": None}
    roots = settings.library_local_roots
    if not roots:
        return out
    if not ftp_path:
        out["reason"] = "empty"
        return out

    # 正規化：反斜線一律當分隔線（掃描來源可能是 Windows 風格的路徑），
    # 去掉空段與 "."，開頭補上 /
    norm = ftp_path.replace("\\", "/")
    if "\x00" in norm:                      # NUL 截斷，直接拒
        out["reason"] = "invalid"
        return out
    parts = [seg for seg in norm.split("/") if seg and seg != "."]
    if any(seg == ".." for seg in parts):
        # 不試著解析 ".."，直接拒絕。能走到這裡代表資料本身不對。
        log.warning("localfs 拒絕含 .. 的路徑")
        out["reason"] = "invalid"
        return out
    full = "/" + "/".join(parts)

    # 多條都命中時（"/" 與 "/媒體資料庫"），失敗原因記第一條（最長的前綴）——
    # 那是使用者心裡「應該要對到」的那一條。
    out["reason"] = "no_prefix"
    first_miss: Optional[Dict[str, Any]] = None
    for prefix, local in roots:
        if prefix == "/":
            rel = parts
        elif full == prefix:
            rel = []
        elif full.startswith(prefix + "/"):
            rel = parts[len([s for s in prefix.split("/") if s]):]
        else:
            continue
        if not rel:
            continue
        miss: Dict[str, Any] = {"prefix": prefix, "candidate": None}
        try:
            base = Path(local).resolve()
            cand = base.joinpath(*rel).resolve()
        except (OSError, ValueError):
            miss["reason"] = "os_error"
            first_miss = first_miss or miss
            continue
        miss["candidate"] = str(cand)
        # resolve() 之後才比較 —— 這樣連結出去的路徑也擋得住
        if not cand.is_relative_to(base):
            log.warning("localfs 擋掉逃出 root 的路徑")
            miss["reason"] = "escape"
            miss["candidate"] = None        # 逃出去的路徑不要回給任何人
            first_miss = first_miss or miss
            continue
        if cand.is_file():
            if not os.access(cand, os.R_OK):
                miss["reason"] = "unreadable"
                first_miss = first_miss or miss
                continue
            return {"reason": "ok", "prefix": prefix, "path": cand,
                    "candidate": str(cand)}
        miss["reason"] = "not_file" if cand.exists() else "not_found"
        first_miss = first_miss or miss
    if first_miss:
        out.update(first_miss)
    return out
