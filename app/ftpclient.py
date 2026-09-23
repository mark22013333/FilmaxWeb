"""FTP 存取層：連線池、目錄遞迴列舉、支援 Range 的位元組串流。

所有 FTP 通訊都在 Python 這一層處理（含檔名編碼），
ffmpeg 一律改讀本服務自己的 HTTP /stream 端點，
避免 ffmpeg 內建 ftp protocol 對 GBK/Big5 檔名的相容性問題。
"""
from __future__ import annotations

import ftplib
import logging
import posixpath
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional, Tuple

from .config import settings

log = logging.getLogger("filmax.ftp")

VIDEO_EXTS = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "ts", "m2ts", "mts",
    "m4v", "rmvb", "rm", "webm", "mpg", "mpeg", "vob", "3gp", "iso", "asf", "f4v",
}
SUBTITLE_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
# 文件庫。目前只有 PDF —— cbz/epub 是另一種排版模型與另一種閱讀器，
# 混進來只會讓兩邊都做不好（見規格書 I 節的「刻意不做」）。
DOC_EXTS = {"pdf"}

# 這些資料夾直接跳過
SKIP_DIRS = {
    "@eadir", "#recycle", ".recycle", "$recycle.bin", "system volume information",
    "extras", "featurettes", "sample", "samples", "bdmv", "certificate",
    "#snapshot", ".ds_store", "lost+found",
}


class FtpError(RuntimeError):
    pass


_REPLY_RE = re.compile(r"(?<!\d)([1-5]\d\d)(?!\d)")


def describe_error(exc: BaseException) -> Tuple[str, Optional[str], str]:
    """把一個 FTP 失敗拆成 (例外類別, FTP 回覆碼, 訊息) —— 給伺服器端 log 用。

    `FtpError` 是包裝，真正的原因在 `__cause__`（`ftplib.error_perm('550 ...')`、
    `ConnectionResetError`、`socket.timeout`…）。**回覆碼是分辨處方的那個數字**：
    550 = 檔案不在（DB 過期、檔案被搬走），421 = 連線太多或逾時，
    502/504 = 伺服器不支援 REST。只看得到「502 Bad Gateway」的話這三種長得一樣。

    訊息只來自 ftplib 的回覆與例外字串，不含帳密（`_new_ftp()` 的錯誤訊息
    只帶 host:port）。
    """
    root = exc.__cause__ or exc
    msg = str(root)
    code: Optional[str] = None
    if isinstance(root, ftplib.Error):
        m = _REPLY_RE.match(msg.strip())
        code = m.group(1) if m else None
    return type(root).__name__, code, msg[:200]


def _new_ftp() -> ftplib.FTP:
    s = settings
    cls = ftplib.FTP_TLS if s.ftp_tls else ftplib.FTP
    ftp = cls(timeout=s.ftp_timeout, encoding=s.ftp_encoding)
    try:
        ftp.connect(s.ftp_host, s.ftp_port, timeout=s.ftp_timeout)
        ftp.login(s.ftp_user or "anonymous", s.ftp_password or "")
        if s.ftp_tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.set_pasv(s.ftp_passive)
        try:
            ftp.voidcmd("TYPE I")
        except Exception:
            pass
        # 盡量讓伺服器用 UTF-8（若設定就是 utf-8）
        if s.ftp_encoding.lower().replace("-", "") == "utf8":
            try:
                ftp.sendcmd("OPTS UTF8 ON")
            except Exception:
                pass
        return ftp
    except Exception as e:  # pragma: no cover
        try:
            ftp.close()
        except Exception:
            pass
        raise FtpError(f"FTP 連線失敗 {s.ftp_host}:{s.ftp_port} - {e}") from e


class FtpPool:
    """簡單的 FTP 連線池。控制連線都從這裡借；資料連線（下載）另外開。"""

    def __init__(self, size: int = 4):
        self._pool: "queue.LifoQueue[ftplib.FTP]" = queue.LifoQueue()
        self._size = size
        self._created = 0
        self._lock = threading.Lock()

    def _acquire(self) -> ftplib.FTP:
        try:
            ftp = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                self._created += 1
            return _new_ftp()
        try:
            ftp.voidcmd("NOOP")
            return ftp
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass
            return _new_ftp()

    def _release(self, ftp: ftplib.FTP, broken: bool = False) -> None:
        if broken:
            try:
                ftp.close()
            except Exception:
                pass
            return
        if self._pool.qsize() >= self._size:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
            return
        self._pool.put(ftp)

    class _Ctx:
        def __init__(self, pool: "FtpPool"):
            self.pool = pool
            self.ftp: Optional[ftplib.FTP] = None
            self.broken = False

        def __enter__(self) -> ftplib.FTP:
            self.ftp = self.pool._acquire()
            return self.ftp

        def __exit__(self, exc_type, exc, tb):
            if self.ftp is not None:
                self.pool._release(self.ftp, broken=exc_type is not None)
            return False

    def conn(self) -> "_Ctx":
        return FtpPool._Ctx(self)

    def close_all(self) -> None:
        while True:
            try:
                ftp = self._pool.get_nowait()
            except queue.Empty:
                return
            try:
                ftp.close()
            except Exception:
                pass


pool = FtpPool(size=4)


@dataclass
class FtpEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: str = ""

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


_LIST_RE = re.compile(
    r"^(?P<perm>[\-dlbcps][rwxstST\-]{9})[\.\+]?\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"(?P<date>\w{3}\s+\d{1,2}\s+(?:\d{4}|\d{1,2}:\d{2}))\s+(?P<name>.+)$"
)
_DOS_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{2}:\d{2}(?:[AP]M)?)\s+"
    r"(?P<dir><DIR>)?\s*(?P<size>\d+)?\s+(?P<name>.+)$"
)


def _parse_list_line(line: str, parent: str) -> Optional[FtpEntry]:
    line = line.rstrip("\r\n")
    if not line:
        return None
    m = _LIST_RE.match(line)
    if m:
        name = m.group("name")
        if " -> " in name:  # symlink
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            return None
        is_dir = m.group("perm").startswith("d")
        return FtpEntry(name, posixpath.join(parent, name), is_dir, int(m.group("size") or 0), m.group("date"))
    m = _DOS_RE.match(line)
    if m:
        name = m.group("name")
        if name in (".", ".."):
            return None
        is_dir = bool(m.group("dir"))
        return FtpEntry(name, posixpath.join(parent, name), is_dir, int(m.group("size") or 0),
                        f"{m.group('date')} {m.group('time')}")
    return None


def list_dir(path: str) -> List[FtpEntry]:
    """列出目錄。優先 MLSD，退回 LIST 解析。"""
    path = "/" + path.strip("/") if path.strip("/") else "/"
    with pool.conn() as ftp:
        # 1) MLSD
        try:
            out: List[FtpEntry] = []
            for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
                if name in (".", ".."):
                    continue
                t = (facts.get("type") or "").lower()
                if t in ("cdir", "pdir"):
                    continue
                is_dir = t == "dir"
                mtime = facts.get("modify", "")
                if mtime and len(mtime) >= 14:
                    try:
                        mtime = datetime.strptime(mtime[:14], "%Y%m%d%H%M%S").isoformat()
                    except Exception:
                        pass
                out.append(FtpEntry(name, posixpath.join(path, name), is_dir,
                                    int(facts.get("size") or 0), mtime))
            return out
        except (ftplib.error_perm, ftplib.error_proto, AttributeError, ValueError):
            pass
        # 2) LIST
        lines: List[str] = []
        try:
            ftp.retrlines(f"LIST {path}", lines.append)
        except ftplib.error_perm as e:
            raise FtpError(f"無法列出目錄 {path}: {e}") from e
        entries = [e for e in (_parse_list_line(ln, path) for ln in lines) if e]
        if entries:
            return entries
        # 3) NLST 最後手段（無法判斷目錄/大小）
        names = []
        try:
            names = ftp.nlst(path)
        except ftplib.error_perm:
            return []
        out2: List[FtpEntry] = []
        for n in names:
            base = posixpath.basename(n.rstrip("/"))
            if base in ("", ".", ".."):
                continue
            full = posixpath.join(path, base)
            size = 0
            is_dir = True
            try:
                size = ftp.size(full) or 0
                is_dir = False
            except Exception:
                is_dir = True
            out2.append(FtpEntry(base, full, is_dir, size, ""))
        return out2


def _skip_dir(name: str) -> bool:
    """這個資料夾要不要整個跳過。

    三道：內建的垃圾目錄清單、隱藏目錄、使用者自訂的名稱前綴。
    比對的是資料夾「名稱」而非完整路徑，所以同名資料夾放在哪一層都會被跳過。
    整個子樹都不會被走訪 —— 這正是重點：省下的是整棵樹的列目錄時間。
    """
    low = name.lower()
    if low in SKIP_DIRS or name.startswith("."):
        return True
    prefixes = settings.exclude_dir_prefixes
    return bool(prefixes and low.startswith(prefixes))


def walk(root: str, max_depth: int = 8, on_dir=None) -> Iterator[Tuple[str, List[FtpEntry], List[FtpEntry]]]:
    """遞迴走訪。yield (目前路徑, 子目錄清單, 檔案清單)。"""
    stack: List[Tuple[str, int]] = [("/" + root.strip("/") if root.strip("/") else "/", 0)]
    visited = set()
    while stack:
        cur, depth = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if on_dir:
            on_dir(cur)
        try:
            entries = list_dir(cur)
        except Exception as e:
            log.warning("列目錄失敗 %s: %s", cur, e)
            continue
        dirs = [e for e in entries if e.is_dir and not _skip_dir(e.name)]
        files = [e for e in entries if not e.is_dir]
        yield cur, dirs, files
        if depth < max_depth:
            for d in reversed(dirs):
                stack.append((d.path, depth + 1))


def file_size(path: str) -> int:
    with pool.conn() as ftp:
        try:
            ftp.voidcmd("TYPE I")
            return ftp.size(path) or 0
        except Exception as e:
            raise FtpError(f"取得檔案大小失敗 {path}: {e}") from e


class FtpReadStream:
    """從指定 offset 開始讀檔的串流（獨立資料連線，用完即關）。"""

    def __init__(self, path: str, offset: int = 0):
        self.path = path
        self.offset = offset
        self._ftp = _new_ftp()
        try:
            self._ftp.voidcmd("TYPE I")
            self._sock: socket.socket = self._ftp.transfercmd(f"RETR {path}", rest=offset if offset else None)
        except Exception as e:
            self.close()
            raise FtpError(f"開啟串流失敗 {path}@{offset}: {e}") from e

    def read(self, n: int) -> bytes:
        return self._sock.recv(n)

    def iter_chunks(self, chunk_size: Optional[int] = None,
                    limit: Optional[int] = None) -> Iterator[bytes]:
        """一塊一塊搬。`chunk_size` 不給就用 STREAM_CHUNK_KB（預設 256 KiB）。

        **預設值刻意沒有改。**規格 J 第 7 節寫得很明白：看到 256 KiB 就改成
        4 MiB 然後宣稱效能修好了，那是把運氣當證據。這裡做的是讓它「可以被調」
        並且「被量到」—— 調成多少要看 /api/diagnostics/stream 的 ftp_mbps。
        """
        if chunk_size is None:
            chunk_size = max(16, int(getattr(settings, "stream_chunk_kb", 256) or 256)) * 1024
        sent = 0
        t0 = time.monotonic()
        try:
            while True:
                want = chunk_size if limit is None else min(chunk_size, limit - sent)
                if want <= 0:
                    return
                data = self._sock.recv(want)
                if not data:
                    return
                sent += len(data)
                yield data
        finally:
            # 取樣寫在 finally：中途被瀏覽器切斷（拖進度條、換畫質）也要留下
            # 那一段的吞吐 —— 那正是最需要看的情境。
            try:
                from . import streamstat
                streamstat.record_ftp(self.path, sent, time.monotonic() - t0)
            except Exception:
                pass
            self.close()

    def close(self) -> None:
        for attr in ("_sock", "_ftp"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass
            setattr(self, attr, None)


def test_connection() -> dict:
    try:
        with pool.conn() as ftp:
            welcome = ftp.getwelcome()
            pwd = ftp.pwd()
        return {"ok": True, "welcome": welcome, "pwd": pwd}
    except Exception as e:
        return {"ok": False, "error": str(e)}
