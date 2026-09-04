"""媒體庫掃描：走訪 FTP → 建立/更新條目 → 背景刮削與探測。"""
from __future__ import annotations

import json
import logging
import posixpath
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from . import db, ftpclient, keyframes, media, nameparser, photo, purge, timeparse
from .config import settings
from .scraper import normalize_details, tmdb

log = logging.getLogger("filmax.scanner")


@dataclass
class ScanStatus:
    running: bool = False
    phase: str = "idle"          # listing / indexing / scraping / probing / done / error
    current: str = ""
    dirs_seen: int = 0
    files_found: int = 0
    files_new: int = 0
    items_total: int = 0
    scraped: int = 0
    probed: int = 0
    probe_total: int = 0
    photos_found: int = 0
    artwork_skipped: int = 0
    photos_read: int = 0
    photo_total: int = 0
    docs_found: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: str = ""
    log: List[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.log[:-200]


status = ScanStatus()
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_cancel = threading.Event()


def is_running() -> bool:
    return status.running


def cancel() -> None:
    _cancel.set()


def start(full: bool = False, reparse: bool = False, remanual: bool = False) -> bool:
    """開始掃描。

    full     重新刮削所有條目、重新分析所有檔案
    reparse  強制重新解析每個檔名並重新分組。改了解析規則之後要用這個 ——
             平常的掃描看到「檔案大小沒變」就早退，不會重新分組。
    remanual 連手動修正過的條目也重新刮削。**預設 false，而且要跟 full 一起用。**
             見 `_scrape_items` 的註解 —— 這個開關存在的唯一理由是「真的要換」，
             不該是「完整重掃」的副作用。
    """
    global _thread
    with _lock:
        if status.running:
            return False
        _cancel.clear()
        status.__init__()  # reset
        status.running = True
        status.phase = "listing"
        status.started_at = time.time()
        _thread = threading.Thread(target=_run, args=(full, reparse, remanual),
                                   name="scan", daemon=True)
        _thread.start()
        return True


def _run(full: bool, reparse: bool = False, remanual: bool = False) -> None:
    try:
        _scan_files(reparse)
        if _cancel.is_set():
            status.phase = "cancelled"
            return
        status.phase = "scraping"
        _scrape_items(force=full, include_manual=remanual)
        if _cancel.is_set():
            status.phase = "cancelled"
            return
        status.phase = "probing"
        _probe_files(force=full)
        if _cancel.is_set():
            status.phase = "cancelled"
            return
        # 相片放最後：影片是主要用途，先讓它可以播
        status.phase = "photos"
        _read_photos()
        status.phase = "done"
        status.note("掃描完成")
        # 邊界表放在掃描**之後**的獨立背景佇列，不當成掃描的一個階段。
        # 實測 6.2 秒／GB —— 全庫約 42 分鐘，塞進上面那串會讓「掃描」
        # 從幾分鐘變成 45 分鐘，性質完全不同。它自己可取消、可續跑
        # （狀態欄留在 media_file.kf_state，所以中斷了下一次接著跑）。
        try:
            if keyframes.start_background(cancel=_cancel):
                status.note(f"分段邊界表在背景計算中（待辦 {keyframes.pending_count()} 個）")
        except Exception as e:
            log.warning("keyframe 佇列啟動失敗：%s", e)
    except Exception as e:  # pragma: no cover
        log.exception("掃描失敗")
        status.phase = "error"
        status.error = str(e)
        status.note(f"錯誤: {e}")
    finally:
        # 清掃掛在 finally 而不是成功路徑上。
        # 「被取消」與「出錯」正是孤兒最多的兩種結局 —— 只在成功時清，
        # 等於永遠不清最需要清的那幾次。
        try:
            r = purge.sweep("fix", reason="scan_finished")
            if r.get("total"):
                status.note(f"清掉 {r['total']} 筆孤兒資料")
        except Exception as e:
            log.warning("掃描後清掃失敗：%s", e)
        status.running = False
        status.finished_at = time.time()


# --------------------------------------------------------------------------
# 1) 走訪 FTP 建立檔案索引
# --------------------------------------------------------------------------
def _scan_files(reparse: bool = False) -> None:
    _load_registry()
    seen_paths: List[str] = []
    seen_photos: List[str] = []
    seen_docs: List[str] = []
    subtitle_map: Dict[str, List[ftpclient.FtpEntry]] = {}

    # 「一集一資料夾」與「檔名尾端是不是集數」都要看群組才判斷得出來。
    # walk 是深度優先，父目錄一定先被 yield，所以邊走邊記下每個目錄的
    # 兄弟名單就夠了，不必改 ftpclient 的介面。
    siblings: Dict[str, List[str]] = {}
    # 走訪期間的寫入全部走批次。原本每個檔案一句 db.execute()，而每一句都是
    # 一個獨立交易 —— WAL 底下就是一次 fsync。109 部片加 679 張相片就是
    # 快 800 次；併成幾次之後這一段幾乎不花時間。
    with db.batch() as writes:
        for root in settings.library_roots:
            status.note(f"開始掃描 {root.path} ({root.kind})")
            for cur, dirs, files in ftpclient.walk(root.path):
                names = [d.name for d in dirs]
                for d in dirs:
                    siblings[d.path] = names
                if _cancel.is_set():
                    return
                status.dirs_seen += 1
                status.current = cur
                vids = [f for f in files if _wanted(f)]
                subs = [f for f in files if f.ext in ftpclient.SUBTITLE_EXTS]
                if subs:
                    subtitle_map[cur] = subs
                vid_names = [f.name for f in vids]
                sib_dirs = siblings.get(cur, [])
                for f in vids:
                    status.files_found += 1
                    seen_paths.append(f.path)
                    _index_file(f, root, sib_dirs, vid_names, reparse=reparse, batch=writes)

                # 排除影片封面需要「同一個目錄裡有哪些影片」，所以要整批一起看，
                # 不能一個檔案一個檔案獨立判斷
                for f in _photos_in(files, vids, cur):
                    seen_photos.append(f.path)
                    _index_photo(f, batch=writes)

                for f in files:
                    if not _wanted_doc(f):
                        continue
                    status.docs_found += 1
                    seen_docs.append(f.path)
                    _index_doc(f)

    # 從 FTP 上消失的檔案。
    #
    # 先查出 id 再交給 purge，而不是直接 DELETE：那些資料列上掛著磁碟檔
    # （media_file.thumb、photo.thumb），直接刪列的話檔案會永遠留在
    # data/images 底下。相片縮圖還是內容雜湊命名的，兩張一樣的照片共用
    # 同一個檔，所以「能不能刪檔」要數參照 —— 那個判斷只在 purge 裡有。
    gone_files = gone_photos = []
    if seen_paths and not _cancel.is_set():
        gone_files = _vanished("media_file", "_seen", seen_paths)
    if seen_photos and not _cancel.is_set():
        gone_photos = _vanished("photo", "_seenp", seen_photos)
    if gone_files or gone_photos:
        purge.purge(file_ids=gone_files, photo_ids=gone_photos, reason="scan_vanished")

    # 消失的文件直接刪列，不走 purge()。
    # purge() 存在的理由是「資料列上掛著磁碟檔」（海報、縮圖）—— 而最小版的
    # 走 purge() 而不是自己寫 DELETE。
    #
    # 最小版的文件沒有任何磁碟衍生物，所以「一句 DELETE 就夠」看起來成立 ——
    # 但 phase1 的測試就是在釘「掃描器自己不寫 DELETE」這條不變量，而它是對的：
    # 等封面縮圖進來，這裡的 DELETE 會安靜地留下一堆孤兒縮圖，
    # 而那時候沒有人會回來看這一段。現在就接上 purge()，之後只要在 purge()
    # 裡多收一個 thumb 檔名即可。
    if seen_docs and not _cancel.is_set():
        gone_docs = _vanished("document", "_seend", seen_docs)
        if gone_docs:
            purge.purge(doc_ids=gone_docs, reason="scan_vanished_doc")
            status.note(f"移除 {len(gone_docs)} 份已不存在的文件")

    # 孤兒條目清理：影片與相片是兩件獨立的事。
    # 這一段原本縮排在「if seen_photos」裡面 —— 純影片的片庫永遠不會清孤兒，
    # 相片掃描被取消或出錯時也不會清。條件只該看影片有沒有掃到。
    #
    # 刪除本身交給 purge.sweep，不在這裡自己寫 DELETE：磁碟上的海報與縮圖
    # 也要一起清，而那是外鍵管不到的部分。這裡用 grace=0 是因為索引剛做完，
    # 「還沒寫檔案列的條目」這個中間狀態已經不存在了。
    if seen_paths and not _cancel.is_set():
        r = purge.sweep("fix", grace=0, reason="scan_vanished")
        if r.get("total"):
            status.note(f"清掉 {r['total']} 筆孤兒資料")
    db.kv_set("external_subtitles", {k: [s.path for s in v] for k, v in subtitle_map.items()})
    msg = f"索引完成：{status.files_found} 個影片檔，新增 {status.files_new} 個"
    if status.photos_found:
        msg += f"；{status.photos_found} 張相片"
    if status.artwork_skipped:
        msg += f"（排除 {status.artwork_skipped} 張影片封面）"
    if status.docs_found:
        msg += f"；{status.docs_found} 份文件"
    status.note(msg)


def _vanished(table: str, tmp: str, seen: List[str]) -> List[int]:
    """回傳這張表裡「這次沒掃到」的 id。

    用暫存表而不是把幾千個路徑塞進 IN (...)：SQLite 的參數上限是 999，
    片庫大一點就會直接爆掉。
    """
    with db.tx() as conn:
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp}(p TEXT PRIMARY KEY)")
        conn.execute(f"DELETE FROM {tmp}")
        conn.executemany(f"INSERT OR IGNORE INTO {tmp}(p) VALUES(?)", [(p,) for p in seen])
        return [r["id"] for r in conn.execute(
            f"SELECT id FROM {table} WHERE ftp_path NOT IN (SELECT p FROM {tmp})")]


def _wanted_photo(f) -> bool:
    """圖片要不要收進相片庫。

    副檔名排除清單對圖片一樣有效，但**大小門檻不套用** —— MIN_FILE_MB 是
    為了濾掉預告片與樣本檔而設的（預設 50MB），拿來套在照片上會把幾乎
    所有圖片都濾掉。改用一個小很多的門檻，濾掉圖示與版面用的小圖。
    """
    ext = (f.ext or "").lower()
    if ext not in ftpclient.IMAGE_EXTS:
        return False
    if ext in settings.exclude_exts:
        return False
    only = settings.only_exts
    if only and ext not in only:
        return False
    return f.size >= settings.min_photo_kb * 1024


# 文件的大小門檻。影片用 MIN_FILE_MB（50MB）會把所有 PDF 濾掉，
# 相片的 MIN_PHOTO_KB（40KB）又偏大 —— 一份純文字的規格書可能只有 20KB。
# 這個值只是用來擋掉壞檔與 0 位元組的殘骸，不需要做成設定。
MIN_DOC_BYTES = 4 * 1024


def _wanted_doc(f) -> bool:
    """這個檔案要不要收進文件庫。"""
    ext = (f.ext or "").lower()
    if ext not in ftpclient.DOC_EXTS:
        return False
    if ext in settings.exclude_exts:
        return False
    only = settings.only_exts
    if only and ext not in only:
        return False
    return f.size >= MIN_DOC_BYTES


def _index_doc(entry: ftpclient.FtpEntry) -> None:
    """把 PDF 登記進文件庫。

    這裡不走 db.batch()：批次是為了「一次掃描 800 次 fsync」那個問題而存在的，
    而文件的數量是幾十筆等級，直接 upsert 反而少一層要維護的介面。
    """
    folder = posixpath.dirname(entry.path) or "/"
    mts = timeparse.parse(entry.mtime)
    now = db.now_i()
    db.execute("""INSERT INTO document(ftp_path, folder, filename, ext, size, mtime,
                                       mtime_ts, sort_ts, probe_state, seen_at, added_at)
                  VALUES(?,?,?,?,?,?,?,?, 'ok', ?, ?)
                  ON CONFLICT(ftp_path) DO UPDATE SET
                      folder=excluded.folder, filename=excluded.filename,
                      size=excluded.size, mtime=excluded.mtime,
                      mtime_ts=excluded.mtime_ts,
                      sort_ts=COALESCE(excluded.mtime_ts, document.sort_ts),
                      seen_at=excluded.seen_at""",
               (entry.path, folder, entry.name, (entry.ext or "").lower(),
                entry.size, entry.mtime, mts, mts or now, now, now))


def _wanted(f) -> bool:
    """這個檔案要不要收進媒體庫。

    順序是刻意的：先看副檔名限制，再看大小。排除清單優先於白名單 ——
    兩邊都寫到同一個副檔名時，使用者的意思幾乎都是「不要」。
    """
    ext = (f.ext or "").lower()
    if ext not in ftpclient.VIDEO_EXTS:
        return False
    if ext in settings.exclude_exts:
        return False
    only = settings.only_exts
    if only and ext not in only:
        return False
    return f.size >= settings.min_file_mb * 1024 * 1024


# 影片旁邊常見的封面檔名（不分大小寫）
ARTWORK_NAMES = {"poster", "fanart", "cover", "folder", "thumb", "thumbnail",
                 "banner", "backdrop", "landscape", "clearart", "disc", "logo"}


def _stem(name: str) -> str:
    return (name.rsplit(".", 1)[0] if "." in name else name).strip().lower()


def _is_video_artwork(f, video_stems: set, folder_name: str) -> bool:
    """這張圖是不是「影片的封面」而不是使用者的相片。

    三條規則，符合任一就算。第 1、3 條**只在同目錄有影片時**才套用 ——
    影片與相片真的混在一起的資料夾（外拍、寫真集裡附影片）是正常內容，
    不能因為有影片就把整個資料夾的相片都排掉。

    第 2 條（常見封面檔名）不需要這個前提：叫 poster.jpg 的檔案
    在任何情境下都不會是使用者想收藏的相片。
    """
    stem = _stem(f.name)
    if stem in ARTWORK_NAMES:
        return True
    if not video_stems:
        return False
    if folder_name and stem == folder_name.strip().lower():
        return True                      # 2024 人生複本 S01.jpg 放在同名資料夾裡
    if stem in video_stems:
        return True                      # 電影.mkv 旁邊的 電影.jpg
    return False


def _photos_in(files, vids, cur: str):
    """從這個目錄的檔案裡挑出真正該收進相片庫的圖片。"""
    out = []
    video_stems = ({_stem(v.name) for v in vids}
                   if settings.photo_exclude_artwork else set())
    folder_name = posixpath.basename(cur.rstrip("/")) or ""
    for f in files:
        if not _wanted_photo(f):
            continue
        if settings.photo_exclude_artwork and _is_video_artwork(f, video_stems, folder_name):
            status.artwork_skipped += 1
            continue
        status.photos_found += 1
        out.append(f)
    return out


def _index_photo(entry: ftpclient.FtpEntry, *, batch) -> None:
    """把圖片登記進相片庫。真正讀 EXIF 與產縮圖是後面那一輪做的。"""
    existing = db.q1("SELECT id, size FROM photo WHERE ftp_path=?", (entry.path,))
    if existing and (existing["size"] or 0) == entry.size:
        batch.touch_photo(existing["id"], db.now_i())
        return
    folder = posixpath.dirname(entry.path) or "/"
    # FTP 的 mtime 有五種格式（MLSD 的 ISO、LIST 的兩種、DOS、空字串），
    # 原始字串保留在 mtime，排序用的數字存 mtime_ts。
    mts = timeparse.parse(entry.mtime)
    now = db.now_i()
    batch.upsert_photo(path=entry.path, folder=folder, name=entry.name,
                       ext=(entry.ext or "").lower(), size=entry.size,
                       mtime=entry.mtime, mtime_ts=mts, sort_ts=mts or now, at=now)


def _read_photos() -> None:
    """把還沒讀過的相片抓下來讀 EXIF 並產縮圖。

    圖片檔小，整個抓下來比做 Range 請求單純，也才讀得到尾端的 EXIF。
    但還是要有上限：FTP 上偶爾會出現幾百 MB 的 TIFF 或掃描檔。
    """
    rows = db.q("SELECT id, ftp_path, size FROM photo WHERE probe_state!='ok' ORDER BY id")
    status.photo_total = len(rows)
    limit = settings.max_photo_mb * 1024 * 1024
    for r in rows:
        if _cancel.is_set():
            return
        pid, path, size = r["id"], r["ftp_path"], r["size"] or 0
        try:
            if size > limit:
                raise ValueError(f"檔案過大（{size / 1048576:.0f} MB，上限 {settings.max_photo_mb} MB）")
            stream = ftpclient.FtpReadStream(path, 0)
            data = b"".join(stream.iter_chunks(limit=limit))
            info = photo.read_info(data)
            if not info["width"]:
                raise ValueError("不是能辨識的圖片格式")
            # 檔名交給 make_thumb 用內容雜湊決定，不要用 pid ——
            # 還原備份或重掃之後 pid 會被重用，舊縮圖會被當成新照片的
            thumb = photo.make_thumb(data)
            # taken_at 保留原始 EXIF 字串（唯一的事實來源），
            # taken_ts 是解析後的數字，sort_ts 是具體化的排序鍵。
            #
            # taken_tz 記的是「這個數字怎麼來的」：EXIF 有給時區就存那個字串，
            # 沒給就存 'local' 表示用本機時區猜的。出國拍的照片這一猜可以差
            # 十幾個小時，把猜的跟算的混在同一個欄位就再也分不出來了。
            off = info.get("taken_offset")
            tks = timeparse.parse(info["taken_at"], tz_offset=off)
            tz = None
            if tks is not None:
                tz = off if timeparse.offset_seconds(off) is not None else "local"
            db.execute("""UPDATE photo SET width=?, height=?, format=?, mode=?,
                              taken_at=?, taken_ts=?, taken_tz=?,
                              sort_ts=COALESCE(?, mtime_ts, sort_ts, CAST(added_at AS INTEGER)),
                              camera=?, lens=?, exposure=?, aperture=?,
                              iso=?, focal_len=?, orientation=?, thumb=?,
                              probe_state='ok', probe_error=NULL WHERE id=?""",
                       (info["width"], info["height"], info["format"], info["mode"],
                        info["taken_at"], tks, tz, tks,
                        info["camera"], info["lens"], info["exposure"],
                        info["aperture"], info["iso"], info["focal_len"],
                        info["orientation"], thumb, pid))
        except Exception as e:
            db.execute("UPDATE photo SET probe_state='failed', probe_error=? WHERE id=?",
                       (str(e)[:300], pid))
            log.warning("讀相片失敗 %s: %s", path, e)
        finally:
            status.photos_read += 1


def _index_file(entry: ftpclient.FtpEntry, root, sibling_dirs=None,
                sibling_files=None, reparse: bool = False, *, batch) -> None:
    existing = db.q1("SELECT id, size FROM media_file WHERE ftp_path=?", (entry.path,))
    # 檔案大小沒變就早退 —— 但改了解析器之後這個捷徑會讓既有檔案永遠
    # 不重新分組。reparse=True 時強制重新解析。
    if existing and (existing["size"] or 0) == entry.size and not reparse:
        batch.touch_file(existing["id"], db.now_i())
        return

    parents = nameparser.dirs_of(entry.path, root.path)
    parsed = nameparser.parse(entry.name, parents,
                              sibling_dirs=sibling_dirs, sibling_files=sibling_files)

    if root.kind == "movie":
        kind = "movie"
        parsed.is_tv = False
        parsed.season = parsed.episode = None
    elif root.kind == "tv":
        kind = "tv"
        parsed.is_tv = True
        parsed.season = parsed.season or 1
        parsed.episode = parsed.episode or 1
    else:
        kind = "tv" if parsed.is_tv else "movie"

    key = nameparser.guess_key(parsed, kind)
    item_id = _upsert_item(kind, key, parsed)

    episode_id = None
    if kind == "tv":
        episode_id = _upsert_episode(item_id, parsed.season or 1, parsed.episode or 1)

    batch.upsert_file(item_id=item_id, episode_id=episode_id, path=entry.path,
                      name=entry.name, size=entry.size, mtime=entry.mtime,
                      mtime_ts=timeparse.parse(entry.mtime), ext=entry.ext,
                      at=db.now_i())
    if not existing:
        status.files_new += 1


# 條目與集數**不做批次**，改用行程內註冊表。
#
# 理由是寫檔案列之前一定要先有 item_id ——「先緩衝、之後再一起寫」拿不到 id。
# 硬做成批次只會製造難修的競態：兩個地方同時建立同一個 guess_key，
# 同一部影集就變成兩張海報，一張 12 集一張 1 集。
#
# 註冊表解決的是另一件事：原本每個檔案都要 SELECT 一次 guess_key。
# 109 個檔案就是 109 次查詢，而答案只有十幾種。掃描開始時整批載進記憶體，
# 之後全部在記憶體裡查；新建立的順手記下來。鎖是為了序列化建立那一步 ——
# 那 15 個條目序列化的成本本來就是零，不值得為它冒競態的險。
_reg_lock = threading.Lock()
_item_reg: Dict[str, int] = {}
_ep_reg: Dict[tuple, int] = {}


def _load_registry() -> None:
    """掃描開始時把既有的 guess_key 與集數載進記憶體。"""
    with _reg_lock:
        _item_reg.clear()
        _ep_reg.clear()
        for r in db.q("SELECT id, guess_key FROM media_item WHERE guess_key IS NOT NULL"):
            _item_reg[r["guess_key"]] = r["id"]
        for r in db.q("SELECT id, item_id, season, episode FROM episode"):
            _ep_reg[(r["item_id"], r["season"], r["episode"])] = r["id"]
    log.debug("註冊表載入 %s 個條目、%s 集", len(_item_reg), len(_ep_reg))


def _upsert_item(kind: str, key: str, parsed) -> int:
    hit = _item_reg.get(key)
    if hit is not None:
        return hit
    with _reg_lock:
        hit = _item_reg.get(key)          # 拿到鎖之後再看一次，可能別人已經建好了
        if hit is not None:
            return hit
        title = parsed.title or parsed.raw
        cur = db.execute(
            """INSERT INTO media_item(kind, title, original_title, sort_title, year, guess_key,
                                      scrape_state, added_at, updated_at)
               VALUES(?,?,?,?,?,?,'pending',?,?)""",
            (kind, title, parsed.alt_title or "", title.lower(), parsed.year, key,
             db.now_i(), db.now_i()),
        )
        _item_reg[key] = int(cur.lastrowid)
        return _item_reg[key]


def reassign_episodes(item_id: int) -> int:
    """把條目底下的檔案重新指派集數。給「類型改成影集」用。

    電影條目的檔案沒有 episode_id，改成影集之後如果不補，那個條目就會是
    「是影集、但一集都沒有」—— 前端的季／集清單會是空的。

    先用 `nameparser` 從檔名解析；解析不出集數的，就按檔名排序給
    S01E01、S01E02…。**這個 fallback 是刻意的**：一個被誤判成電影的多檔條目，
    檔名通常本來就沒有集數標記（那正是它被誤判的原因），與其留空，
    不如給一個看得懂、而且順序正確的編號。

    回傳指派了幾個檔案。
    """
    files = db.q("SELECT id, ftp_path, filename FROM media_file WHERE item_id=? "
                 "ORDER BY filename COLLATE NOCASE", (item_id,))
    n = 0
    with _reg_lock:
        _ep_reg.clear()
        for r in db.q("SELECT id, item_id, season, episode FROM episode"):
            _ep_reg[(r["item_id"], r["season"], r["episode"])] = r["id"]
    auto = 0
    for r in files:
        parsed = nameparser.parse(r["filename"], [])
        season, ep = parsed.season, parsed.episode
        if not ep:
            auto += 1
            season, ep = season or 1, auto
        ep_id = _upsert_episode(item_id, season or 1, ep)
        db.execute("UPDATE media_file SET episode_id=? WHERE id=?", (ep_id, r["id"]))
        n += 1
    return n


def clear_episodes(item_id: int) -> int:
    """把條目底下的集數清掉。給「類型改成電影」用。

    走 purge()（A-4 的單一刪除進入點）而不是自己 DELETE ——
    集數有 still（劇照）檔在磁碟上，自己刪會留孤兒。
    """
    ep_ids = [r["id"] for r in db.q("SELECT id FROM episode WHERE item_id=?", (item_id,))]
    if not ep_ids:
        return 0
    purge.purge(episode_ids=ep_ids, reason="kind_changed_to_movie")
    return len(ep_ids)


def _upsert_episode(item_id: int, season: int, episode: int) -> int:
    k = (item_id, season, episode)
    hit = _ep_reg.get(k)
    if hit is not None:
        return hit
    with _reg_lock:
        hit = _ep_reg.get(k)
        if hit is not None:
            return hit
        cur = db.execute("INSERT INTO episode(item_id, season, episode) VALUES(?,?,?)",
                         (item_id, season, episode))
        _ep_reg[k] = int(cur.lastrowid)
        return _ep_reg[k]


# --------------------------------------------------------------------------
# 2) TMDB 刮削
# --------------------------------------------------------------------------
def _scrape_items(force: bool = False, include_manual: bool = False) -> None:
    """刮削。

    **`scrape_state='manual'` 的條目預設永遠不刮，連 force 也不刮。**

    原本 force 的 where 是空字串 —— 於是「完整重掃」會把使用者一個一個手動指定
    好的條目，拿去重新自動比對再蓋掉。而自動比對之所以會挑錯，通常是檔名的問題，
    那個原因不會因為重掃而改變，所以它會挑錯第二次，一模一樣。
    整件事沒有任何提示，使用者只會發現「我修好的片又變回錯的了」。

    實測（Yu 的資料庫）：一般掃描會刮 9 筆，force 會刮 23 筆 ——
    手動修正過的 2 筆也在裡面。

    真的要重刮手動修正過的條目時，走 include_manual=True（後台一個預設關閉的
    勾選）。那必須是一個明確的動作，不是「完整重掃」的副作用。
    """
    if not tmdb.enabled:
        status.note("未設定 TMDB_API_KEY，跳過刮削（仍可用檔名瀏覽與播放）")
        return
    if force:
        where = "" if include_manual else "WHERE scrape_state != 'manual'"
    else:
        where = "WHERE scrape_state IN ('pending','failed')"
    rows = db.q(f"SELECT * FROM media_item {where} ORDER BY id")
    if force and not include_manual:
        kept = db.q1("SELECT COUNT(*) c FROM media_item WHERE scrape_state='manual'")["c"]
        if kept:
            status.note(f"保留 {kept} 筆手動修正過的條目（沒有重新刮削）")
    status.items_total = len(rows)
    for row in rows:
        if _cancel.is_set():
            return
        status.current = row["title"]
        try:
            _scrape_one(dict(row))
        except Exception as e:
            log.warning("刮削失敗 %s: %s", row["title"], e)
            db.execute("UPDATE media_item SET scrape_state='failed', updated_at=? WHERE id=?",
                       (db.now_i(), row["id"]))
        status.scraped += 1


def _scrape_one(item: dict) -> None:
    kind = item["kind"]
    hit = tmdb.search(kind, item["title"], item.get("year"), item.get("original_title") or "")
    if not hit:
        db.execute("UPDATE media_item SET scrape_state='failed', updated_at=? WHERE id=?",
                   (db.now_i(), item["id"]))
        status.note(f"找不到: {item['title']}")
        return
    detail = tmdb.details(kind, hit["id"])
    if not detail:
        db.execute("UPDATE media_item SET scrape_state='failed', updated_at=? WHERE id=?",
                   (db.now_i(), item["id"]))
        return
    n = normalize_details(kind, detail)
    poster = tmdb.download_image(n["poster_path"], "w500")
    backdrop = tmdb.download_image(n["backdrop_path"], "w1280")
    db.execute(
        """UPDATE media_item SET title=?, original_title=?, sort_title=?, year=?, tmdb_id=?,
               overview=?, poster=?, backdrop=?, rating=?, runtime=?, genres=?, cast_json=?,
               scrape_state='ok', updated_at=? WHERE id=?""",
        (n["title"] or item["title"], n["original_title"], (n["title"] or "").lower(),
         n["year"] or item.get("year"), n["tmdb_id"], n["overview"], poster, backdrop,
         n["rating"], n["runtime"], json.dumps(n["genres"], ensure_ascii=False),
         json.dumps(n["cast"], ensure_ascii=False), db.now_i(), item["id"]),
    )
    status.note(f"刮到: {n['title']} ({n['year'] or '-'})")

    if kind == "tv":
        _scrape_seasons(item["id"], n["tmdb_id"])


def _scrape_seasons(item_id: int, tmdb_id: int) -> None:
    seasons = [r["season"] for r in db.q(
        "SELECT DISTINCT season FROM episode WHERE item_id=? ORDER BY season", (item_id,))]
    for s in seasons:
        data = tmdb.season(tmdb_id, s)
        if not data:
            continue
        for ep in data.get("episodes") or []:
            num = ep.get("episode_number")
            row = db.q1("SELECT id FROM episode WHERE item_id=? AND season=? AND episode=?",
                        (item_id, s, num))
            if not row:
                continue
            still = tmdb.download_image(ep.get("still_path"), "w300")
            db.execute(
                "UPDATE episode SET title=?, overview=?, still=?, air_date=? WHERE id=?",
                (ep.get("name"), (ep.get("overview") or "").strip(), still,
                 ep.get("air_date"), row["id"]),
            )


# --------------------------------------------------------------------------
# 3) ffprobe 探測（決定 direct / hls）
# --------------------------------------------------------------------------
def _probe_files(force: bool = False) -> None:
    where = "" if force else "WHERE probe_state IN ('pending','failed')"
    rows = db.q(f"SELECT id, ext, item_id, duration FROM media_file {where} ORDER BY id")
    status.probe_total = len(rows)
    if not rows:
        return
    # 用 submit + 逐一取消，而不是 pool.map。
    # map 會把全部工作一次排進去，按下取消之後排隊中的那些照樣會執行；
    # 而且 ffprobe 有 90 秒 timeout，使用者會覺得「按了沒反應」。
    with ThreadPoolExecutor(max_workers=max(1, settings.probe_concurrency)) as pool:
        futures = [pool.submit(_probe_one, dict(r)) for r in rows]
        for f in futures:
            if _cancel.is_set():
                f.cancel()          # 還沒開始的直接取消，已經在跑的靠 _probe_one 內部檢查
        for f in futures:
            try:
                f.result()
            except Exception:
                pass
    _make_covers()


# 探測階段登記「這個條目需要封面」，值是 (file_id, duration)。
# 只留第一個候選就好 —— 哪一集的截圖當底圖都可以。
_cover_wanted: dict = {}
_cover_lock = threading.Lock()


def _make_covers() -> None:
    """把探測階段登記的封面候選一次做完。單執行緒，不會重複截圖。"""
    with _cover_lock:
        wanted = dict(_cover_wanted)
        _cover_wanted.clear()
    if not wanted:
        return
    made = 0
    for item_id, (fid, duration) in wanted.items():
        if _cancel.is_set():
            break
        item = db.q1("SELECT id, poster, backdrop FROM media_item WHERE id=?", (item_id,))
        if not item or item["poster"] or item["backdrop"]:
            continue        # 已經有海報或底圖，不需要截圖
        name = f"thumb_{fid}.jpg"
        if media.make_thumbnail(fid, min(duration * 0.25, 600), name):
            db.execute("UPDATE media_file SET thumb=? WHERE id=?", (name, fid))
            db.execute("UPDATE media_item SET backdrop=? WHERE id=? AND backdrop IS NULL",
                       (name, item_id))
            made += 1
    if made:
        status.note(f"產生 {made} 張封面截圖")


def _probe_one(row: dict) -> None:
    if _cancel.is_set():
        return
    fid = row["id"]
    try:
        info = media.summarize(media.ffprobe(fid, cancel=_cancel), row.get("ext") or "")
        db.execute(
            """UPDATE media_file SET duration=?, container=?, video_codec=?, audio_codec=?,
                   width=?, height=?, bitrate=?, subtitles=?, audio_tracks=?, play_mode=?,
                   pix_fmt=?, color_transfer=?, color_primaries=?, color_space=?,
                   bit_depth=?, is_hdr=?, fps=?,
                   probe_state='ok', probe_error=NULL WHERE id=?""",
            (info["duration"], info["container"], info["video_codec"], info["audio_codec"],
             info["width"], info["height"], info["bitrate"],
             json.dumps(info["subtitles"], ensure_ascii=False),
             json.dumps(info["audio_tracks"], ensure_ascii=False),
             info["play_mode"],
             info.get("pix_fmt"), info.get("color_transfer"), info.get("color_primaries"),
             info.get("color_space"), info.get("bit_depth"), info.get("is_hdr", 0),
             info.get("fps"),
             fid),
        )
        # 封面候選只登記，不在這裡產生。
        #
        # 原本是每個工作各自檢查「這個條目有沒有封面」然後截圖回填。
        # 一部 98 集的影集會有 98 個工作同時看到「還沒有封面」，
        # 於是好幾個工作各跑一次 ffmpeg 截圖 —— 重複轉碼、多出來的檔案
        # 立刻變成孤兒（COALESCE 只保證不覆蓋欄位，擋不住重複產生檔案）。
        # 改成登記候選，全部探測完之後單執行緒處理一次。
        if row.get("item_id") and info.get("duration"):
            with _cover_lock:
                _cover_wanted.setdefault(row["item_id"], (fid, info["duration"]))
    except media.Cancelled:
        # 使用者按了停止，子行程已經被殺掉。這不是「失敗」——
        # 標成 failed 會讓錯誤清單塞滿一堆其實沒問題的檔案，
        # 而 probe_state 留在 pending 下次掃描本來就會再試。
        return
    except Exception as e:
        # 存進 DB 之前再遮一次：probe_error 會透過 /api/play、/api/diagnostics
        # 等端點回給前端，是個很容易被忽略的外洩出口。
        db.execute("UPDATE media_file SET probe_state='failed', probe_error=? WHERE id=?",
                   (media.scrub(str(e))[:400], fid))
        log.warning("探測失敗 file=%s: %s", fid, e)
    finally:
        status.probed += 1


def status_dict() -> dict:
    d = asdict(status)
    d["elapsed"] = (time.time() - status.started_at) if status.started_at else 0
    return d
