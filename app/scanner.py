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

from . import db, ftpclient, media, nameparser
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


def start(full: bool = False) -> bool:
    global _thread
    with _lock:
        if status.running:
            return False
        _cancel.clear()
        status.__init__()  # reset
        status.running = True
        status.phase = "listing"
        status.started_at = time.time()
        _thread = threading.Thread(target=_run, args=(full,), name="scan", daemon=True)
        _thread.start()
        return True


def _run(full: bool) -> None:
    try:
        _scan_files()
        if _cancel.is_set():
            status.phase = "cancelled"
            return
        status.phase = "scraping"
        _scrape_items(force=full)
        if _cancel.is_set():
            status.phase = "cancelled"
            return
        status.phase = "probing"
        _probe_files(force=full)
        status.phase = "done"
        status.note("掃描完成")
    except Exception as e:  # pragma: no cover
        log.exception("掃描失敗")
        status.phase = "error"
        status.error = str(e)
        status.note(f"錯誤: {e}")
    finally:
        status.running = False
        status.finished_at = time.time()


# --------------------------------------------------------------------------
# 1) 走訪 FTP 建立檔案索引
# --------------------------------------------------------------------------
def _scan_files() -> None:
    seen_paths: List[str] = []
    subtitle_map: Dict[str, List[ftpclient.FtpEntry]] = {}

    for root in settings.library_roots:
        status.note(f"開始掃描 {root.path} ({root.kind})")
        for cur, dirs, files in ftpclient.walk(root.path):
            if _cancel.is_set():
                return
            status.dirs_seen += 1
            status.current = cur
            vids = [f for f in files if f.ext in ftpclient.VIDEO_EXTS and f.size >= settings.min_file_mb * 1024 * 1024]
            subs = [f for f in files if f.ext in ftpclient.SUBTITLE_EXTS]
            if subs:
                subtitle_map[cur] = subs
            for f in vids:
                status.files_found += 1
                seen_paths.append(f.path)
                _index_file(f, root)

    # 標記已消失的檔案
    if seen_paths and not _cancel.is_set():
        with db.tx() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen(p TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _seen")
            conn.executemany("INSERT OR IGNORE INTO _seen(p) VALUES(?)", [(p,) for p in seen_paths])
            conn.execute("DELETE FROM media_file WHERE ftp_path NOT IN (SELECT p FROM _seen)")
            conn.execute("DELETE FROM media_item WHERE id NOT IN (SELECT DISTINCT item_id FROM media_file WHERE item_id IS NOT NULL)")
    db.kv_set("external_subtitles", {k: [s.path for s in v] for k, v in subtitle_map.items()})
    status.note(f"索引完成：{status.files_found} 個影片檔，新增 {status.files_new} 個")


def _index_file(entry: ftpclient.FtpEntry, root) -> None:
    existing = db.q1("SELECT id, size FROM media_file WHERE ftp_path=?", (entry.path,))
    if existing and (existing["size"] or 0) == entry.size:
        db.execute("UPDATE media_file SET seen_at=? WHERE id=?", (db.now(), existing["id"]))
        return

    parents = nameparser.dirs_of(entry.path, root.path)
    parsed = nameparser.parse(entry.name, parents)

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

    if existing:
        db.execute(
            """UPDATE media_file SET item_id=?, episode_id=?, size=?, mtime=?, ext=?,
                   probe_state='pending', seen_at=? WHERE id=?""",
            (item_id, episode_id, entry.size, entry.mtime, entry.ext, db.now(), existing["id"]),
        )
    else:
        db.execute(
            """INSERT INTO media_file(item_id, episode_id, ftp_path, filename, size, mtime, ext,
                                      probe_state, seen_at, added_at)
               VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
            (item_id, episode_id, entry.path, entry.name, entry.size, entry.mtime,
             entry.ext, db.now(), db.now()),
        )
        status.files_new += 1


def _upsert_item(kind: str, key: str, parsed) -> int:
    row = db.q1("SELECT id FROM media_item WHERE guess_key=?", (key,))
    if row:
        return row["id"]
    title = parsed.title or parsed.raw
    cur = db.execute(
        """INSERT INTO media_item(kind, title, original_title, sort_title, year, guess_key,
                                  scrape_state, added_at, updated_at)
           VALUES(?,?,?,?,?,?,'pending',?,?)""",
        (kind, title, parsed.alt_title or "", title.lower(), parsed.year, key, db.now(), db.now()),
    )
    return int(cur.lastrowid)


def _upsert_episode(item_id: int, season: int, episode: int) -> int:
    row = db.q1("SELECT id FROM episode WHERE item_id=? AND season=? AND episode=?",
                (item_id, season, episode))
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO episode(item_id, season, episode) VALUES(?,?,?)",
                     (item_id, season, episode))
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# 2) TMDB 刮削
# --------------------------------------------------------------------------
def _scrape_items(force: bool = False) -> None:
    if not tmdb.enabled:
        status.note("未設定 TMDB_API_KEY，跳過刮削（仍可用檔名瀏覽與播放）")
        return
    where = "" if force else "WHERE scrape_state IN ('pending','failed')"
    rows = db.q(f"SELECT * FROM media_item {where} ORDER BY id")
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
                       (db.now(), row["id"]))
        status.scraped += 1


def _scrape_one(item: dict) -> None:
    kind = item["kind"]
    hit = tmdb.search(kind, item["title"], item.get("year"), item.get("original_title") or "")
    if not hit:
        db.execute("UPDATE media_item SET scrape_state='failed', updated_at=? WHERE id=?",
                   (db.now(), item["id"]))
        status.note(f"找不到: {item['title']}")
        return
    detail = tmdb.details(kind, hit["id"])
    if not detail:
        db.execute("UPDATE media_item SET scrape_state='failed', updated_at=? WHERE id=?",
                   (db.now(), item["id"]))
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
         json.dumps(n["cast"], ensure_ascii=False), db.now(), item["id"]),
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
    with ThreadPoolExecutor(max_workers=max(1, settings.probe_concurrency)) as pool:
        list(pool.map(_probe_one, [dict(r) for r in rows]))


def _probe_one(row: dict) -> None:
    if _cancel.is_set():
        return
    fid = row["id"]
    try:
        info = media.summarize(media.ffprobe(fid), row.get("ext") or "")
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
        # 沒有海報的條目，用影片截圖當封面
        item = db.q1("SELECT id, poster FROM media_item WHERE id=?", (row.get("item_id"),)) \
            if row.get("item_id") else None
        if item and not item["poster"] and info["duration"]:
            name = f"thumb_{fid}.jpg"
            if media.make_thumbnail(fid, min(info["duration"] * 0.25, 600), name):
                db.execute("UPDATE media_file SET thumb=? WHERE id=?", (name, fid))
                db.execute("UPDATE media_item SET backdrop=COALESCE(backdrop,?) WHERE id=?",
                           (name, item["id"]))
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
