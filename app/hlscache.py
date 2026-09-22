"""HLS 快取的盤點與預備（後台「播放與轉碼 → 快取」那一塊的後端）。

**為什麼不塞進 hls.py。**hls.py 是播放路徑上的東西 —— 每一段的產生、鎖、
預轉都在那裡，它的每一行都可能在使用者正在等畫面的時候被執行。這支是後台
的維運工具：盤點整個快取資料夾、排隊把整支片轉完。兩者的取捨完全相反
（前者要快、要讓路；後者可以慢、但要能看進度、能取消），混在一起的話
「維運動作不可以拖慢播放」這條界線就沒有地方可以寫下來。

盤點是**照著磁碟上實際有什麼**回答，不是照資料庫猜。快取是衍生資料，
它跟 DB 不同步是常態（LRU 汰掉過、手動刪過、升版清掉過），所以唯一可信的
來源就是資料夾本身。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import db, hls
from .config import CACHE_DIR

log = logging.getLogger("filmax.hlscache")

# 分段資料夾長這樣：v4_h720_ad_b8000_c2_m0。**版本前綴要單獨抓出來**，
# 因為「不是目前版本的殘留」正是後台最想一眼看到的東西之一。
_DIR_RE = re.compile(r"^v(?P<ver>\d+)_(?P<profile>.+)$")


def _dir_info(d: Path) -> Optional[dict]:
    """一個 profile 資料夾的盤點結果。認不得名字就回 None（不要猜）。"""
    m = _DIR_RE.match(d.name)
    if not m:
        # 沒有版本前綴 = v3 以前的舊格式。它們讀不到（seg_dir 現在一律寫
        # 帶版本的路徑），但會佔著空間，所以照樣列出來、標成 stale。
        ver, profile = 0, d.name
    else:
        ver, profile = int(m.group("ver")), m.group("profile")
    segs = 0
    bytes_ = 0
    newest = 0.0
    for p in d.glob("seg-*.ts"):
        try:
            st = p.stat()
        except OSError:
            continue
        segs += 1
        bytes_ += st.st_size
        newest = max(newest, st.st_mtime)
    return {"profile": profile, "version": ver, "dir": d.name,
            "segments": segs, "bytes": bytes_, "newest": newest,
            "stale": ver != hls.HLS_CACHE_FORMAT_VERSION}


def survey(limit: int = 200) -> dict:
    """整個快取資料夾的盤點。

    `limit` 限制的是**回傳的檔案列數**，不是統計範圍 —— 總量、檔案數、
    殘留量都是掃完整個資料夾算出來的。分頁把統計也一起截斷的話，後台顯示的
    「總共佔用 4.2 GB」就會隨著翻頁變動，那種數字沒有人敢信。
    """
    files: List[dict] = []
    total_bytes = 0
    total_segs = 0
    stale_bytes = 0
    stale_dirs = 0
    empty_dirs = 0

    if CACHE_DIR.exists():
        for fdir in CACHE_DIR.iterdir():
            if not fdir.is_dir() or not fdir.name.isdigit():
                continue
            file_id = int(fdir.name)
            profiles = []
            fbytes = 0
            fsegs = 0
            newest = 0.0
            for pdir in fdir.iterdir():
                if not pdir.is_dir():
                    continue
                info = _dir_info(pdir)
                if info is None:
                    continue
                if info["segments"] == 0:
                    empty_dirs += 1
                if info["stale"]:
                    stale_dirs += 1
                    stale_bytes += info["bytes"]
                profiles.append(info)
                fbytes += info["bytes"]
                fsegs += info["segments"]
                newest = max(newest, info["newest"])
            # **一段都沒有的檔案不要列。**空資料夾是 LRU 汰光或清過之後留下的
            # 殼，列出來就是一列「0 B、0 段」—— 佔著版面卻沒有任何可以做的決定
            # （清它不會釋出空間，預備它跟從清單外面挑一支沒有差別）。
            # 它們仍然被算進 emptyDirs，那個數字才是「該不該整理一下」的依據。
            if not profiles or fsegs == 0:
                continue
            profiles.sort(key=lambda x: -x["bytes"])
            files.append({"file_id": file_id, "bytes": fbytes, "segments": fsegs,
                          "newest": newest, "profiles": profiles})
            total_bytes += fbytes
            total_segs += fsegs

    files.sort(key=lambda x: -x["bytes"])
    shown = files[:max(1, limit)]

    # 檔名要能對得上 —— 後台列一排 file_id 是沒辦法用的。一次查回來，
    # 不要每列各問一次 DB。
    names: Dict[int, str] = {}
    if shown:
        ids = [f["file_id"] for f in shown]
        qs = ",".join("?" * len(ids))
        try:
            for r in db.q(f"SELECT id, filename FROM media_file WHERE id IN ({qs})", ids):
                names[int(r["id"])] = r["filename"]
        except Exception:
            pass
    for f in shown:
        f["filename"] = names.get(f["file_id"])

    return {
        "version": hls.HLS_CACHE_FORMAT_VERSION,
        "totalBytes": total_bytes,
        "totalSegments": total_segs,
        "fileCount": len(files),
        "staleBytes": stale_bytes,
        "staleDirs": stale_dirs,
        "emptyDirs": empty_dirs,
        "limitMb": 0,
        "files": shown,
        "truncated": len(files) > len(shown),
    }


def warm_options(file_id: int) -> dict:
    """這個檔案可以預備哪些階。

    **不要讓後台自己拼 profile 字串。**profile key 的形狀（高度／音軌／碼率／
    聲道／階別）是 `hls.profile_key()` 的實作細節，前端抄一份就是等著兩邊
    有一天不一樣 —— 而不一樣的後果是「預備了半天，播的時候用的是另一個
    資料夾」。所以候選清單由後端算好給它。
    """
    row = db.q1("SELECT id, filename, duration, height FROM media_file WHERE id=?",
                (file_id,))
    if not row:
        return {"ok": False, "message": f"找不到 file_id={file_id}"}
    dur = float(row["duration"] or 0)
    if dur <= 0:
        return {"ok": False, "message": "這個檔案還沒有長度（沒分析過？）"}

    opts = []
    for rung in hls.rungs_for(file_id, dur):
        if rung == hls.RUNG_REMUX:
            prof = hls.profile_key(0, None, 0, hls.RUNG_REMUX)
            label = "上階（原畫質 remux，最快也最大）"
        else:
            prof = hls.profile_key(None, None, 0, hls.RUNG_TRANSCODE)
            label = "下階（轉碼，遠端實際會用的那一階）"
        # **只問不要建。**`hls.seg_dir()` 會順手 mkdir —— 那是產生分段時該有的
        # 行為，但「這支片可以預備哪些階」只是在問問題，卻會在磁碟上留下一個
        # 空資料夾，然後出現在下一次盤點的列表裡（實測踩到：點開預備對話框
        # 又取消，快取列表就多一個 0 段的 profile）。這裡自己組路徑。
        d = CACHE_DIR / str(file_id) / f"v{hls.HLS_CACHE_FORMAT_VERSION}_{prof}"
        have = sum(1 for _ in d.glob("seg-*.ts")) if d.is_dir() else 0
        opts.append({"profile": prof, "label": label,
                     "total": hls.segment_count(dur, prof, file_id),
                     "cached": have})
    return {"ok": True, "file_id": file_id, "filename": row["filename"],
            "duration": dur, "options": opts}


def clear_stale() -> dict:
    """清掉不是目前版本的殘留資料夾。

    **這是唯一「不必先看清單」的清除**：舊版本的分段在定義上已經沒有人讀得到
    （`seg_dir()` 只會產生帶目前版本的路徑），刪掉它們不會讓任何一次播放
    需要重轉。其他的清除都要先看過再按。
    """
    removed_dirs = 0
    removed_bytes = 0
    if not CACHE_DIR.exists():
        return {"dirs": 0, "bytes": 0}
    for fdir in CACHE_DIR.iterdir():
        if not fdir.is_dir() or not fdir.name.isdigit():
            continue
        for pdir in list(fdir.iterdir()):
            if not pdir.is_dir():
                continue
            info = _dir_info(pdir)
            if info is None or not info["stale"]:
                continue
            for p in pdir.rglob("*"):
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                pdir.rmdir()
                removed_dirs += 1
                removed_bytes += info["bytes"]
            except OSError:
                pass
        try:
            next(fdir.iterdir())
        except StopIteration:
            try:
                fdir.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    log.info("清掉 %s 個非 v%s 的殘留資料夾（%.1f MB）",
             removed_dirs, hls.HLS_CACHE_FORMAT_VERSION, removed_bytes / 1024 / 1024)
    return {"dirs": removed_dirs, "bytes": removed_bytes}


# ----------------------------------------------------------------- 預備（warm）

@dataclass
class WarmStatus:
    running: bool = False
    file_id: int = 0
    filename: str = ""
    profile: str = ""
    done: int = 0             # 這次實際產生的段數
    skipped: int = 0          # 本來就在快取裡的
    failed: int = 0
    total: int = 0
    started: float = 0.0
    finished: float = 0.0
    phase: str = ""           # idle / running / done / cancelled / error
    error: str = ""
    errors: List[str] = field(default_factory=list)

    def dict(self) -> dict:
        el = (self.finished or time.time()) - self.started if self.started else 0.0
        left = 0.0
        if self.running and self.done and el > 0:
            left = (self.total - self.done - self.skipped) / (self.done / el)
        return {"running": self.running, "file_id": self.file_id,
                "filename": self.filename, "profile": self.profile,
                "done": self.done, "skipped": self.skipped, "failed": self.failed,
                "total": self.total, "phase": self.phase, "error": self.error,
                "errors": self.errors[-5:], "elapsed": el, "eta": left}


status = WarmStatus()
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_cancel = threading.Event()


def is_running() -> bool:
    return status.running


def cancel() -> None:
    _cancel.set()


def start(file_id: int, profile: str, head: int = 0) -> tuple[bool, str]:
    """排一支片去預備。`head>0` 只轉開頭那幾段。回 (有沒有排到, 為什麼)。

    **一次只跑一支。**預備是拿整台機器的 CPU 去換「等一下不用等」，同時跑
    兩支只會讓兩支都慢，而且會跟正在播的人搶 —— `_produce()` 的
    `background=True` 已經把優先權降低了，但降低不等於不存在。
    """
    global _thread
    with _lock:
        if status.running:
            return False, "已經有一支在預備了"
        row = db.q1("SELECT id, filename, duration FROM media_file WHERE id=?", (file_id,))
        if not row:
            return False, f"找不到 file_id={file_id}"
        dur = float(row["duration"] or 0)
        if dur <= 0:
            return False, "這個檔案還沒有長度（沒分析過？），不能算出要轉幾段"
        prof = hls.normalize_profile(profile)
        total = hls.segment_count(dur, prof, file_id)
        if head > 0:
            total = min(total, head)

        _cancel.clear()
        status.running = True
        status.file_id = file_id
        status.filename = row["filename"] or ""
        status.profile = prof
        status.done = status.skipped = status.failed = 0
        status.total = total
        status.started = time.time()
        status.finished = 0.0
        status.phase = "running"
        status.error = ""
        status.errors = []
        _thread = threading.Thread(target=_run, args=(file_id, prof, dur, total),
                                   daemon=True)
        _thread.start()
    return True, f"已開始預備 {total} 段"


def _run(file_id: int, profile: str, duration: float, total: int) -> None:
    try:
        d = hls.seg_dir(file_id, profile)
        for i in range(total):
            if _cancel.is_set():
                status.phase = "cancelled"
                return
            path = d / f"seg-{i}.ts"
            if path.exists() and path.stat().st_size > 0:
                status.skipped += 1
                continue
            lk = hls._seg_lock(f"{file_id}/{profile}/{i}")
            # 正在播的人也要這一段的話，讓他先 —— 他在等畫面，我們不急。
            if not lk.acquire(blocking=False):
                status.skipped += 1
                continue
            try:
                if path.exists() and path.stat().st_size > 0:
                    status.skipped += 1
                    continue
                hls._produce(file_id, i, profile, duration, path, background=True)
                status.done += 1
            except Exception as e:
                status.failed += 1
                msg = f"seg-{i}: {e}"
                status.errors.append(msg)
                log.warning("預備失敗 file=%s %s", file_id, msg)
                # **連續失敗就停。**同一支片每一段都失敗的話，繼續跑只是把
                # 同一個錯誤重複 1000 次，還佔著機器。
                if status.failed >= 5 and status.done == 0:
                    status.phase = "error"
                    status.error = "連續失敗，已停止（通常是片源讀不到或編碼器不可用）"
                    return
            finally:
                lk.release()
        status.phase = "done"
    except Exception as e:            # noqa: BLE001 - 背景執行緒不能讓例外逃走
        status.phase = "error"
        status.error = str(e)
        log.exception("預備整支片時爆掉 file=%s", file_id)
    finally:
        status.running = False
        status.finished = time.time()
