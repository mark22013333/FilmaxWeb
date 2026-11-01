"""HIF 轉 JPG：在來源目錄寫 JPG，原檔依拍攝日期搬進 `YYYYMMDD原檔/`。規格見 S 章。

第一個會改動來源目錄的功能，所以每一步都以「原檔永遠還在」為前提排序：
寫暫存檔 → 驗證 → 改名成 JPG → 才搬原檔。永遠不刪除、不覆蓋。

每一張的進度由檔案系統決定，不靠記憶，所以中途斷掉（服務重啟、網路斷線）
再按一次就接得上：我們自己產的 JPG 內嵌一段註解 `filmax-hif:<原檔名>:<原檔大小>`，
看到它就知道「JPG 是我們剛寫好的，只差搬原檔」；沒有它就是別人的同名 JPG（例如
相機同時存的），照決策 5 跳過。
"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

from PIL import Image

from . import db, ftpclient, photo, purge, scanner
from .config import settings

log = logging.getLogger("filmax.hifconvert")

MARKER_PREFIX = "filmax-hif:"
_JPEG_EXTS = (".jpg", ".jpeg")


# ------------------------------------------------------------------ 來源檔案系統
class SourceFS:
    """來源目錄的寫入介面。FTP 與 SFTP 各一份；轉檔邏輯只看這個介面。"""

    def listdir(self, path: str) -> List[str]: ...
    def exists(self, path: str) -> bool: ...
    def read(self, path: str, limit: int) -> bytes: ...
    def write(self, path: str, data: bytes) -> None: ...
    def rename(self, src: str, dst: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def rmdir(self, path: str) -> None: ...
    def set_mtime(self, path: str, epoch: int) -> None: ...


def _own_tmp(path: str) -> bool:
    """只允許刪我們自己產的暫存檔，其他任何檔案都不刪。"""
    base = posixpath.basename(path)
    return base.endswith(".part") or base.startswith(".filmax-probe")


class SftpFS(SourceFS):
    def __init__(self, sftp):
        self._s = sftp

    def listdir(self, path):
        return self._s.listdir(path)

    def exists(self, path):
        try:
            self._s.stat(path)
            return True
        except IOError:
            return False

    def read(self, path, limit):
        with self._s.open(path, "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"檔案超過 {limit // 1048576} MB")
        return data

    def write(self, path, data):
        self._s.putfo(io.BytesIO(data), path)

    def rename(self, src, dst):
        self._s.rename(src, dst)           # SFTP v3 的 rename 目標存在會失敗，剛好是我們要的

    def mkdir(self, path):
        if not self.exists(path):
            self._s.mkdir(path)

    def remove(self, path):
        assert _own_tmp(path), path
        self._s.remove(path)

    def rmdir(self, path):
        assert _own_tmp(path), path
        self._s.rmdir(path)

    def set_mtime(self, path, epoch):
        try:
            self._s.utime(path, (epoch, epoch))
        except Exception as e:
            log.debug("設定修改時間失敗 %s: %s", path, e)


class FtpFS(SourceFS):
    def __init__(self, ftp):
        self._f = ftp

    def listdir(self, path):
        return [e.name for e in ftpclient.list_dir(path)]

    def exists(self, path):
        try:
            self._f.size(path)
            return True
        except Exception:
            pass
        cur = None
        try:
            cur = self._f.pwd()
            self._f.cwd(path)
            return True
        except Exception:
            return False
        finally:
            if cur is not None:
                try:
                    self._f.cwd(cur)
                except Exception:
                    pass

    def read(self, path, limit):
        stream = ftpclient.FtpReadStream(path, 0)
        data = b"".join(stream.iter_chunks(limit=limit + 1))
        if len(data) > limit:
            raise ValueError(f"檔案超過 {limit // 1048576} MB")
        return data

    def write(self, path, data):
        self._f.storbinary(f"STOR {path}", io.BytesIO(data))

    def rename(self, src, dst):
        self._f.rename(src, dst)

    def mkdir(self, path):
        if not self.exists(path):
            self._f.mkd(path)

    def remove(self, path):
        assert _own_tmp(path), path
        self._f.delete(path)

    def rmdir(self, path):
        assert _own_tmp(path), path
        self._f.rmd(path)

    def set_mtime(self, path, epoch):
        try:
            ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(epoch))
            self._f.voidcmd(f"MFMT {ts} {path}")
        except Exception as e:             # 很多伺服器不支援 MFMT，不是錯誤
            log.debug("設定修改時間失敗 %s: %s", path, e)


@contextmanager
def open_fs() -> Iterator[SourceFS]:
    if settings.ftp_protocol == "sftp":
        from .sftpclient import connection
        with connection() as (sftp, _ssh):
            yield SftpFS(sftp)
        return
    ftp = ftpclient._new_ftp()
    try:
        yield FtpFS(ftp)
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def explain(exc: BaseException) -> str:
    """把來源端的例外翻成使用者看得懂的一句話。"""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, PermissionError) or "permission" in low or "denied" in low \
            or re.search(r"\b55[03]\b", msg) or getattr(exc, "errno", None) == 13:
        return "來源帳號沒有寫入權限（請在來源端給這個帳號寫入、建立資料夾與改名的權限）"
    if "no space" in low or "quota" in low or re.search(r"\b552\b", msg):
        return "來源端空間不足"
    return msg[:200] or type(exc).__name__


# ------------------------------------------------------------------ 單張處理
@dataclass
class Item:
    photo_id: int
    name: str
    state: str = "pending"      # pending → converted → archived；skipped／failed 是終點
    reason: str = ""
    jpg_path: str = ""
    archive_path: str = ""


def marker_for(name: str, size: int) -> str:
    return f"{MARKER_PREFIX}{name}:{size}"


def encode_jpeg(data: bytes, quality: int, marker: str) -> Tuple[bytes, Tuple[int, int]]:
    """HEIF 位元組 → 全解析度 JPEG。保留 EXIF 與色彩描述檔，品質照設定、色度不縮減。"""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        size = im.size
        exif = im.getexif()
        if 0x0112 in exif:
            exif[0x0112] = 1           # libheif 已經把像素轉正，標籤要歸位，否則會轉兩次
        icc = im.info.get("icc_profile")
        rgb = im if im.mode == "RGB" else im.convert("RGB")
        kw = dict(format="JPEG", quality=int(quality), subsampling=0,
                  comment=marker.encode("utf-8"), icc_profile=icc)
        buf = io.BytesIO()
        try:
            rgb.save(buf, exif=exif.tobytes(), **kw)
        except Exception:
            # EXIF 超過 JPEG 單一區段的 64KB（相機的 MakerNote 偶爾會這樣）：
            # 退回只留機型與時間，照片本身不受影響
            slim = Image.Exif()
            for tag in (0x010F, 0x0110, 0x0132):
                if tag in exif:
                    slim[tag] = exif[tag]
            sub = exif.get_ifd(0x8769)
            if 0x9003 in sub:
                slim.get_ifd(0x8769)[0x9003] = sub[0x9003]
            buf = io.BytesIO()
            rgb.save(buf, exif=slim.tobytes(), **kw)
    return buf.getvalue(), size


def verify_jpeg(jpg: bytes, size: Tuple[int, int]) -> None:
    with Image.open(io.BytesIO(jpg)) as im:
        im.load()
        if im.size != size:
            raise ValueError(f"驗證失敗：轉出 {im.size}，原圖 {size}")


def jpeg_marker(jpg: bytes) -> str:
    try:
        with Image.open(io.BytesIO(jpg)) as im:
            c = im.info.get("comment")
        return c.decode("utf-8", "replace") if isinstance(c, bytes) else str(c or "")
    except Exception:
        return ""


_YMD = re.compile(r"(\d{4})\D(\d{2})\D(\d{2})")


def archive_ymd(taken_at: Optional[str], mtime_ts: Optional[int]) -> str:
    """拍攝日期（EXIF），讀不到才用檔案修改時間。"""
    m = _YMD.match(taken_at or "")
    if m and m.group(1) != "0000":
        return "".join(m.groups())
    return datetime.fromtimestamp(mtime_ts or time.time()).strftime("%Y%m%d")


def convert_one(fs: SourceFS, row: dict, siblings: Dict[str, str],
                quality: int) -> Tuple[Item, Optional[bytes]]:
    """處理一張。`siblings` 是這個資料夾現有檔名（小寫 → 實際檔名），會被就地更新。

    回傳 (結果, 轉好的 JPG 位元組或 None)。丟例外代表這一張失敗，原檔沒動。
    """
    src = row["ftp_path"]
    folder, name = posixpath.split(src)
    stem = name.rsplit(".", 1)[0]
    item = Item(photo_id=row["id"], name=name)

    if name.lower() not in siblings:
        item.state, item.reason = "skipped", "原檔已不在原位"
        return item, None

    limit = int(settings.max_photo_mb) * 1024 * 1024
    data = fs.read(src, limit)
    info = photo.read_info(data)
    if not info["width"]:
        raise ValueError("不是能辨識的圖片格式")
    marker = marker_for(name, len(data))

    existing = next((siblings[stem.lower() + e] for e in _JPEG_EXTS
                     if stem.lower() + e in siblings), None)
    jpg_bytes: Optional[bytes] = None
    if existing:
        jpg_path = posixpath.join(folder, existing)
        if not jpeg_marker(fs.read(jpg_path, limit * 2)).startswith(marker):
            item.state, item.reason = "skipped", f"已有同名 JPG（{existing}）"
            return item, None
        item.jpg_path = jpg_path            # 上次寫好的，只差搬原檔
    else:
        jpg_bytes, size = encode_jpeg(data, quality, marker)
        verify_jpeg(jpg_bytes, size)
        jpg_path = posixpath.join(folder, f"{stem}.jpg")
        part = posixpath.join(folder, f".{stem}.jpg.part")
        if fs.exists(part):
            fs.remove(part)                 # 上次寫到一半的暫存檔，是我們自己的
        fs.write(part, jpg_bytes)
        fs.rename(part, jpg_path)
        fs.set_mtime(jpg_path, row.get("mtime_ts") or int(time.time()))
        siblings[f"{stem}.jpg".lower()] = f"{stem}.jpg"
        item.jpg_path = jpg_path
    item.state = "converted"

    adir = posixpath.join(folder, archive_ymd(info["taken_at"], row.get("mtime_ts"))
                          + ftpclient.ARCHIVE_DIR_SUFFIX)
    fs.mkdir(adir)
    target, n = posixpath.join(adir, name), 1
    while fs.exists(target):
        n += 1
        target = posixpath.join(adir, f"{stem}_{n}.{name.rsplit('.', 1)[-1]}")
    fs.rename(src, target)
    siblings.pop(name.lower(), None)
    item.archive_path = target
    item.state = "archived"
    return item, jpg_bytes


# ------------------------------------------------------------------ 工作
@dataclass
class Job:
    id: int
    folder: str
    total: int
    items: List[Item] = field(default_factory=list)
    state: str = "running"      # running → done | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    current: str = ""

    def counts(self) -> Dict[str, int]:
        out = {"archived": 0, "skipped": 0, "failed": 0}
        for i in self.items:
            if i.state in out:
                out[i.state] += 1
        return out

    def as_dict(self) -> dict:
        return {"id": self.id, "folder": self.folder, "state": self.state,
                "total": self.total, "done": len(self.items), "current": self.current,
                "counts": self.counts(), "started_at": self.started_at,
                "finished_at": self.finished_at,
                "items": [{"name": i.name, "state": i.state, "reason": i.reason,
                           "jpg": posixpath.basename(i.jpg_path),
                           "archive": posixpath.dirname(i.archive_path).rsplit("/", 1)[-1]}
                          for i in self.items if i.state != "archived"]}


class Busy(RuntimeError):
    pass


_lock = threading.Lock()
_job: Optional[Job] = None
_seq = 0
_cancel = threading.Event()


def is_running() -> bool:
    return _job is not None and _job.state == "running"


def current() -> Optional[Job]:
    return _job


def cancel() -> None:
    _cancel.set()


def hif_rows(folder: str) -> List[dict]:
    """這一層（不含子資料夾）收進相片庫的 HIF。"""
    exts = sorted(ftpclient.HEIF_EXTS)
    marks = ",".join("?" * len(exts))
    return [dict(r) for r in db.q(
        f"SELECT id, ftp_path, folder, filename, size, mtime_ts FROM photo "
        f"WHERE folder=? AND LOWER(ext) IN ({marks}) ORDER BY filename",
        (folder, *exts))]


def rows_by_ids(ids: List[int]) -> List[dict]:
    if not ids:
        return []
    exts = sorted(ftpclient.HEIF_EXTS)
    return [dict(r) for r in db.q(
        f"SELECT id, ftp_path, folder, filename, size, mtime_ts FROM photo "
        f"WHERE id IN ({','.join('?' * len(ids))}) "
        f"AND LOWER(ext) IN ({','.join('?' * len(exts))}) ORDER BY folder, filename",
        (*ids, *exts))]


def start(rows: List[dict], folder: str) -> Job:
    global _job, _seq
    if not rows:
        raise ValueError("沒有可轉的 HIF")
    with _lock:
        if is_running():
            raise Busy("已經有一個轉檔工作在進行")
        if scanner.is_running():
            raise Busy("掃描進行中，請等掃描結束再轉檔")
        _seq += 1
        _cancel.clear()
        _job = Job(id=_seq, folder=folder, total=len(rows))
        threading.Thread(target=_run, args=(_job, rows), name="hifconvert",
                         daemon=True).start()
        return _job


def _register(item: Item, jpg: bytes, old_id: int, mtime: str) -> None:
    """JPG 進相片庫、HIF 那一列退場，使用者不必等下一次掃描就看到結果。"""
    entry = ftpclient.FtpEntry(posixpath.basename(item.jpg_path), item.jpg_path,
                               False, len(jpg), mtime)
    with db.batch() as b:
        scanner._index_photo(entry, batch=b)
    new = db.q1("SELECT id FROM photo WHERE ftp_path=?", (item.jpg_path,))
    if new:
        scanner.fill_photo(new["id"], jpg)
    purge.purge(photo_ids=[old_id], reason="hif_converted")


def _run(job: Job, rows: List[dict]) -> None:
    quality = int(settings.photo_convert_quality)
    try:
        by_folder: Dict[str, List[dict]] = {}
        for r in rows:
            by_folder.setdefault(r["folder"], []).append(r)
        for folder, group in by_folder.items():
            if _cancel.is_set():
                break
            try:
                with open_fs() as fs:
                    siblings = {n.lower(): n for n in fs.listdir(folder)}
                    for r in group:
                        if _cancel.is_set():
                            break
                        job.current = r["filename"]
                        try:
                            item, jpg = convert_one(fs, r, siblings, quality)
                        except Exception as e:
                            log.warning("轉檔失敗 %s: %s", r["ftp_path"], e)
                            item = Item(photo_id=r["id"], name=r["filename"],
                                        state="failed", reason=explain(e))
                            jpg = None
                        if item.state == "archived" and jpg is not None:
                            try:
                                _register(item, jpg, r["id"], _mtime_str(r))
                            except Exception as e:
                                log.warning("登記進相片庫失敗 %s: %s（下次掃描會補上）",
                                            item.jpg_path, e)
                        job.items.append(item)
            except Exception as e:             # 連不上來源：這一批剩下的都記失敗
                log.warning("轉檔連線失敗 %s: %s", folder, e)
                done = {i.photo_id for i in job.items}
                for r in group:
                    if r["id"] not in done:
                        job.items.append(Item(photo_id=r["id"], name=r["filename"],
                                              state="failed", reason=explain(e)))
        job.state = "cancelled" if _cancel.is_set() else "done"
    except Exception:                         # pragma: no cover
        log.exception("轉檔工作異常結束")
        job.state = "done"
    finally:
        job.current = ""
        job.finished_at = time.time()


def _mtime_str(r: dict) -> str:
    ts = r.get("mtime_ts")
    return time.strftime("%Y%m%d%H%M%S", time.gmtime(ts)) if ts else ""


# ------------------------------------------------------------------ 寫入權限偵測
def probe(folder: str) -> Tuple[bool, str]:
    """按下按鈕之前先試一次：寫檔、改名、建資料夾、刪掉自己的暫存。"""
    tag = f".filmax-probe-{int(time.time())}"
    a, b, d = (posixpath.join(folder, tag + s) for s in (".a", ".b", ".dir"))
    try:
        with open_fs() as fs:
            try:
                fs.write(a, b"x")
                fs.rename(a, b)
                fs.remove(b)
                fs.mkdir(d)
                fs.rmdir(d)
            except Exception as e:
                for p in (a, b):
                    try:
                        if fs.exists(p):
                            fs.remove(p)
                    except Exception:
                        pass
                return False, explain(e)
    except Exception as e:
        return False, explain(e)
    return True, ""
