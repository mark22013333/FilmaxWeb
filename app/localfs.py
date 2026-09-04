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
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("filmax.localfs")


def enabled() -> bool:
    return bool(settings.library_local_roots)


def resolve_local(ftp_path: str) -> Optional[Path]:
    """認得就回本機檔案路徑，否則回 None。

    回 None 的每一種情況都是「照舊走 FTP」，不是錯誤。

    回傳的是 `resolve()` 之後的路徑：Windows 的 8.3 短檔名
    （`C:\\Users\\ADMINI~1\\...`）會被展開成長路徑。拿它去跟「自己組出來的
    字串」做等值比對前要記得這件事 —— 對 ffmpeg 沒有影響，兩種寫法都開得起來。
    """
    roots = settings.library_local_roots
    if not roots or not ftp_path:
        return None

    # 正規化：反斜線一律當分隔線（掃描來源可能是 Windows 風格的路徑），
    # 去掉空段與 "."，開頭補上 /
    norm = ftp_path.replace("\\", "/")
    if "\x00" in norm:                      # NUL 截斷，直接拒
        return None
    parts = [seg for seg in norm.split("/") if seg and seg != "."]
    if any(seg == ".." for seg in parts):
        # 不試著解析 ".."，直接拒絕。能走到這裡代表資料本身不對。
        log.warning("localfs 拒絕含 .. 的路徑")
        return None
    full = "/" + "/".join(parts)

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
        try:
            base = Path(local).resolve()
            cand = base.joinpath(*rel).resolve()
        except (OSError, ValueError):
            continue
        # resolve() 之後才比較 —— 這樣連結出去的路徑也擋得住
        if not cand.is_relative_to(base):
            log.warning("localfs 擋掉逃出 root 的路徑")
            continue
        if cand.is_file():
            return cand
    return None
