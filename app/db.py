"""SQLite 資料層。單檔資料庫，放在 data/library.db。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .config import DB_PATH

log = logging.getLogger("filmax.db")
_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS media_item (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,              -- movie / tv
    title           TEXT NOT NULL,
    original_title  TEXT,
    sort_title      TEXT,
    year            INTEGER,
    tmdb_id         INTEGER,
    overview        TEXT,
    poster          TEXT,                       -- 本地快取檔名
    backdrop        TEXT,
    rating          REAL,
    runtime         INTEGER,
    genres          TEXT,                       -- JSON array
    cast_json       TEXT,                       -- JSON array
    scrape_state    TEXT DEFAULT 'pending',     -- pending / ok / failed / manual
    guess_key       TEXT UNIQUE,                -- 用來合併同一部作品
    added_at        REAL,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_item_kind ON media_item(kind);
CREATE INDEX IF NOT EXISTS idx_item_title ON media_item(title);

CREATE TABLE IF NOT EXISTS episode (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES media_item(id) ON DELETE CASCADE,
    season      INTEGER NOT NULL,
    episode     INTEGER NOT NULL,
    title       TEXT,
    overview    TEXT,
    still       TEXT,
    air_date    TEXT,
    UNIQUE(item_id, season, episode)
);

CREATE TABLE IF NOT EXISTS media_file (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER REFERENCES media_item(id) ON DELETE CASCADE,
    episode_id   INTEGER REFERENCES episode(id) ON DELETE SET NULL,
    ftp_path     TEXT NOT NULL UNIQUE,
    filename     TEXT NOT NULL,
    size         INTEGER,
    mtime        TEXT,
    ext          TEXT,
    -- ffprobe 結果
    duration     REAL,
    container    TEXT,
    video_codec  TEXT,
    audio_codec  TEXT,
    width        INTEGER,
    height       INTEGER,
    bitrate      INTEGER,
    subtitles    TEXT,          -- JSON: [{index,codec,lang,title}]
    audio_tracks TEXT,          -- JSON
    play_mode    TEXT,          -- direct / hls
    -- 色彩資訊，決定要不要做 HDR tonemap
    pix_fmt         TEXT,
    color_transfer  TEXT,       -- smpte2084 = HDR10 / arib-std-b67 = HLG
    color_primaries TEXT,
    color_space     TEXT,
    bit_depth       INTEGER,
    is_hdr          INTEGER DEFAULT 0,
    probe_state  TEXT DEFAULT 'pending',   -- pending / ok / failed
    probe_error  TEXT,
    thumb        TEXT,
    seen_at      REAL,
    added_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_file_item ON media_file(item_id);
CREATE INDEX IF NOT EXISTS idx_file_probe ON media_file(probe_state);

CREATE TABLE IF NOT EXISTS play_state (
    file_id     INTEGER PRIMARY KEY REFERENCES media_file(id) ON DELETE CASCADE,
    position    REAL DEFAULT 0,
    duration    REAL DEFAULT 0,
    finished    INTEGER DEFAULT 0,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 相片庫。跟影片分開一張表：欄位差太多，硬塞同一張表只會兩邊都難用。
CREATE TABLE IF NOT EXISTS photo (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ftp_path    TEXT UNIQUE NOT NULL,
    folder      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER,
    mtime       TEXT,
    width       INTEGER,
    height      INTEGER,
    format      TEXT,          -- JPEG / PNG / WEBP…
    mode        TEXT,          -- RGB / RGBA / L…
    taken_at    TEXT,          -- EXIF 拍攝時間
    camera      TEXT,          -- 廠牌 + 型號
    lens        TEXT,
    exposure    TEXT,          -- 1/250s
    aperture    TEXT,          -- f/2.8
    iso         INTEGER,
    focal_len   TEXT,          -- 35mm
    orientation INTEGER,
    thumb       TEXT,
    probe_state TEXT DEFAULT 'pending',   -- pending / ok / failed
    probe_error TEXT,
    seen_at     REAL,
    added_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_photo_folder ON photo(folder);
CREATE INDEX IF NOT EXISTS idx_photo_taken ON photo(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_photo_probe ON photo(probe_state);

-- 登入紀錄。之後接上 MSSQL 帳號系統時會改存那邊，但本機這份仍然有用：
-- 資料庫連不上的時候，至少還看得到誰在什麼時候從哪裡登入。
CREATE TABLE IF NOT EXISTS login_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL    NOT NULL,
    event       TEXT    NOT NULL,      -- success / failed / locked
    role        TEXT,
    ip          TEXT,
    country     TEXT,
    region      TEXT,
    city        TEXT,
    timezone    TEXT,
    latitude    REAL,                  -- 城市中心點，不是使用者的實際位置
    longitude   REAL,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_at ON login_audit(at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


_write_lock = threading.RLock()


@contextmanager
def tx():
    conn = get_conn()
    with _write_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# 既有資料庫要補的欄位。SQLite 沒有 IF NOT EXISTS 的 ADD COLUMN，
# 所以自己比對一次；這比要求使用者砍掉重建資料庫友善得多。
_ADDED_COLUMNS = {
    "login_audit": [
        ("email", "TEXT"),          # Google 帳號登入才有
        ("user_id", "INTEGER"),
        ("detail", "TEXT"),
    ],
    "media_file": [
        ("pix_fmt", "TEXT"),
        ("color_transfer", "TEXT"),
        ("color_primaries", "TEXT"),
        ("color_space", "TEXT"),
        ("bit_depth", "INTEGER"),
        ("is_hdr", "INTEGER DEFAULT 0"),
        ("fps", "REAL"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("資料庫補上欄位 %s.%s", table, name)


def init_db() -> None:
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def q(sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    with tx() as conn:
        return conn.execute(sql, tuple(params))


def kv_get(key: str, default: Any = None) -> Any:
    row = q1("SELECT v FROM kv WHERE k=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for jf in ("genres", "cast_json", "subtitles", "audio_tracks"):
        if jf in d and isinstance(d[jf], str):
            try:
                d[jf] = json.loads(d[jf])
            except Exception:
                d[jf] = []
    return d


def now() -> float:
    return time.time()


def log_login(event: str, role: Optional[str], ip: str, loc: Dict[str, Any],
              email: Optional[str] = None, user_id: Optional[int] = None,
              detail: Optional[str] = None) -> None:
    """寫一筆登入事件。

    寫失敗絕對不能影響登入本身 —— 稽核紀錄再重要，也不該讓人因為它壞掉而登不進來。
    """
    try:
        execute(
            """INSERT INTO login_audit
                   (at, event, role, ip, country, region, city, timezone,
                    latitude, longitude, user_agent, email, user_id, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), event, role, ip, loc.get("country"), loc.get("region"),
             loc.get("city"), loc.get("timezone"), loc.get("latitude"),
             loc.get("longitude"), loc.get("user_agent"), email, user_id,
             (detail or None) and str(detail)[:300]),
        )
    except Exception as e:
        log.warning("登入紀錄寫入失敗: %s", e)
