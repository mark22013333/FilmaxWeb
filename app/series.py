"""TV identity consolidation. File IDs and playback state survive every merge."""
from __future__ import annotations

import inspect
import logging
import sqlite3
import threading
from functools import wraps
from uuid import uuid4

from . import db

log = logging.getLogger("filmax.series")
# Indexing buffers reference item/episode IDs until its final flush.
mutation_lock = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        with mutation_lock:
            return fn(*args, **kwargs)
    run.__signature__ = inspect.signature(fn, eval_str=True)
    return run


def _backup(conn) -> None:
    if conn.in_transaction:
        raise RuntimeError("Series backup requires a committed database")
    folder = db.DB_PATH.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"library-before-series-{db.now_i()}-{uuid4().hex[:8]}.db"
    with sqlite3.connect(path) as target:
        conn.backup(target)
    log.info("影集整合前備份 %s", path)


def _members(conn, item_id: int, changes: dict | None = None) -> list:
    row = conn.execute("SELECT * FROM media_item WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return []
    item = dict(row)
    item.update(changes or {})
    if item["kind"] != "tv" or not item.get("tmdb_id"):
        return [row]
    return conn.execute("SELECT * FROM media_item WHERE id=? OR (kind='tv' AND tmdb_id=?)",
                        (item_id, item["tmdb_id"])).fetchall()


def _missing(value) -> bool:
    return value is None or value == "" or value == "[]"


def _fill_missing(conn, table: str, keeper: dict, donor: dict, fields: tuple) -> None:
    changes = {field: donor[field] for field in fields
               if _missing(keeper[field]) and not _missing(donor[field])}
    if changes:
        conn.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in changes)
                     + " WHERE id=?", [*changes.values(), keeper["id"]])
        keeper.update(changes)


def _merge(conn, item_id: int) -> int:
    rows = _members(conn, item_id)
    if not rows:
        return item_id
    rows.sort(key=lambda r: (r["scrape_state"] != "manual",
                            -(r["updated_at"] or 0) if r["scrape_state"] == "manual" else 0,
                            r["id"]))
    keeper = dict(rows[0])
    if keeper["kind"] != "tv" or not keeper.get("tmdb_id"):
        return keeper["id"]
    for row in rows:
        key = row["guess_key"]
        if key:
            conn.execute("INSERT INTO media_item_alias(guess_key,item_id) VALUES(?,?) "
                         "ON CONFLICT(guess_key) DO UPDATE SET item_id=excluded.item_id",
                         (key, keeper["id"]))
    for row in rows[1:]:
        donor = dict(row)
        _fill_missing(conn, "media_item", keeper, donor,
                      ("title", "original_title", "sort_title", "year", "overview", "poster",
                       "backdrop", "rating", "runtime", "genres", "cast_json"))
        for ep in conn.execute("SELECT * FROM episode WHERE item_id=? ORDER BY id", (donor["id"],)).fetchall():
            target = conn.execute("SELECT * FROM episode WHERE item_id=? AND season=? AND episode=?",
                                  (keeper["id"], ep["season"], ep["episode"])).fetchone()
            if target is None:
                conn.execute("UPDATE episode SET item_id=? WHERE id=?", (keeper["id"], ep["id"]))
            else:
                _fill_missing(conn, "episode", dict(target), dict(ep), ("title", "overview", "still", "air_date"))
                conn.execute("UPDATE media_file SET episode_id=? WHERE episode_id=?", (target["id"], ep["id"]))
                conn.execute("DELETE FROM episode WHERE id=?", (ep["id"],))
        conn.execute("UPDATE media_file SET item_id=? WHERE item_id=?", (keeper["id"], donor["id"]))
        conn.execute("UPDATE media_item_alias SET item_id=? WHERE item_id=?", (keeper["id"], donor["id"]))
        conn.execute("DELETE FROM media_item WHERE id=?", (donor["id"],))
    if len(rows) > 1:
        log.info("影集 %s 整合 %s 筆為 #%s", keeper["tmdb_id"], len(rows), keeper["id"])
    return keeper["id"]


@serialized
def save_metadata(item_id: int, changes: dict) -> int:
    """Apply trusted internal metadata and consolidate in one SQL transaction."""
    with db._write_lock:
        conn = db.get_conn()
        if len(_members(conn, item_id, changes)) > 1:
            _backup(conn)
        with db.tx() as conn:
            if changes:
                conn.execute("UPDATE media_item SET " + ",".join(f"{key}=?" for key in changes)
                             + " WHERE id=?", [*changes.values(), item_id])
            return _merge(conn, item_id)


def consolidate(item_id: int) -> int:
    return save_metadata(item_id, {})


@serialized
def consolidate_existing() -> int:
    with db._write_lock:
        conn = db.get_conn()
        groups = conn.execute("SELECT MIN(id) id FROM media_item WHERE kind='tv' AND tmdb_id IS NOT NULL "
                              "GROUP BY tmdb_id HAVING COUNT(*)>1").fetchall()
        if not groups:
            return 0
        _backup(conn)
        with db.tx() as conn:
            for group in groups:
                _merge(conn, group["id"])
        return len(groups)
