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
    scrape_state    TEXT DEFAULT 'pending',     -- pending / ok / failed / manual / skip
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

-- 分段邊界表（J 章第 0 層的上階要用）。
--
-- 存的是**原始的 keyframe 時間與位元組偏移**，不是算好的分段邊界。
-- 理由：邊界規則還沒定案（「湊滿 >= 分段長度的最少 keyframe 數」讓段長變成
-- keyframe 間距的整數倍，實測 6 秒設定變 7.34 秒；另一種是取「離目標最近」的）。
-- 存原始資料的話換規則不必重掃 —— 而重掃是 6.2 秒/GB、全庫 42 分鐘。
-- 代價是每檔約 19 KB 而不是 4 KB，133 檔約 2.5 MB，換掉 42 分鐘很划算。
CREATE TABLE IF NOT EXISTS media_keyframe (
    file_id   INTEGER PRIMARY KEY,
    times     TEXT NOT NULL,          -- JSON: [秒數, ...]
    positions TEXT NOT NULL,          -- JSON: [位元組偏移, ...]，跟 times 同長
    count     INTEGER NOT NULL,
    gap_min   REAL,
    gap_med   REAL,
    gap_max   REAL,
    updated_at INTEGER
);

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

-- 文件庫（PDF）。刻意獨立一張表，不塞進 media_file ——
-- 那張表的每一列都假設自己有 duration、影片語意的 probe_state、play_mode
-- 與 HLS 設定檔，PDF 一個都沒有。相片當初獨立開表是對的，照著做。
--
-- 時間欄位照 A-3 的規則：Python 端一律 epoch 整數，並在建表時就加上值域
-- CHECK（ALTER TABLE 補的欄位加不了 CHECK，新表沒有這個限制）。
CREATE TABLE IF NOT EXISTS document (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ftp_path    TEXT UNIQUE NOT NULL,
    folder      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER,
    mtime       TEXT,                     -- FTP 給的原始字串，一字不改
    mtime_ts    INTEGER CHECK (mtime_ts IS NULL OR mtime_ts BETWEEN 0 AND 4102444800),
    sort_ts     INTEGER CHECK (sort_ts  IS NULL OR sort_ts  BETWEEN 0 AND 4102444800),
    pages       INTEGER,                  -- 目前不填，等封面／頁數那一期
    probe_state TEXT DEFAULT 'ok',        -- 最小版沒有探測階段，索引完就是 ok
    probe_error TEXT,
    seen_at     REAL,
    added_at    REAL
);
CREATE TABLE IF NOT EXISTS folder_rule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix     TEXT UNIQUE NOT NULL,      -- 受限的資料夾（前綴，子資料夾自動繼承）
    note       TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS folder_grant (
    rule_id INTEGER NOT NULL REFERENCES folder_rule(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,             -- app_user.id（只有 Google 帳號有 id）
    PRIMARY KEY (rule_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_grant_user ON folder_grant(user_id);
CREATE INDEX IF NOT EXISTS idx_doc_folder ON document(folder);
CREATE INDEX IF NOT EXISTS idx_doc_sort ON document(sort_ts DESC);

-- 後台儲存的系統參數。只存「跟預設值不同」的項目 —— 存一堆等於預設值的列，
-- 之後改預設值時那些列會變成隱形的覆蓋：使用者從來沒設過它，卻永遠拿不到新預設值。
-- 秘密欄位存密文（見 app/params.py 的 encrypt）。
CREATE TABLE IF NOT EXISTS config_param (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at REAL,
    updated_by TEXT
);

-- 設定變更稽核。秘密只記「已變更」，連遮罩後的值都不記 —— 長度也是情報。
CREATE TABLE IF NOT EXISTS config_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    k         TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    actor     TEXT,
    action    TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_audit_at ON config_audit(at DESC);

-- 刪除紀錄。刻意不併進 login_audit：那張表的 action 值域被 MSSQL 的
-- CHECK 約束綁住（只有帳號事件那八個），而且它是「誰動了帳號」的紀錄。
-- 媒體庫的刪除是另一件事，混在一起兩邊都會變難查。
CREATE TABLE IF NOT EXISTS purge_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    total  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_purge_at ON purge_log(at DESC);

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
    # 時間一律改存 epoch 整數秒。原本的字串欄位保留不動 ——
    # 它是唯一的事實來源，日後改判斷規則可以在不重讀檔案的前提下重跑轉換。
    "photo": [
        ("mtime_ts", "INTEGER"),
        ("taken_ts", "INTEGER"),
        # EXIF 原始的時區字串（`+09:00`），或 'local' 表示「沒有，用本機時區猜的」。
        # 兩者長得不一樣是刻意的：猜出來的時間不該假裝成算出來的。
        # NULL = 這張根本沒有拍攝時間。
        ("taken_tz", "TEXT"),
        # sort_ts = COALESCE(taken_ts, mtime_ts, added_at)，寫入時算好。
        # COALESCE 是表達式，兩邊都用不到索引；具體化之後才排得動。
        ("sort_ts", "INTEGER"),
    ],
    "login_audit": [
        ("email", "TEXT"),          # Google 帳號登入才有
        ("user_id", "INTEGER"),
        ("detail", "TEXT"),
    ],
    # JAV 刮削（javbus / fc2 / d2pass / jav321）帶回來的欄位。TMDB 沒有對應概念，
    # 所以不塞進既有欄位 —— 番號尤其不能塞 guess_key 以外的地方，它是這類作品
    # 唯一穩定的識別碼，日後要重刮、去重、比對都靠它。
    "media_item": [
        ("jav_number", "TEXT"),       # SSIS-938 / FC2-PPV-4756708 / 021326_01
        ("jav_maker", "TEXT"),        # 片商，如 S1
        ("jav_label", "TEXT"),        # 廠牌，如 S1 NO.1 STYLE
        ("jav_director", "TEXT"),
        ("jav_source", "TEXT"),       # 實際刮到的來源：javbus / fc2 / d2pass / jav321
        # 重試觀測。**先只記錄不做退避** —— 退避的閾值要等真實資料說話，
        # 現在寫死一個數字是在猜。跑幾週之後看 scrape_attempts 的分佈，
        # 真有條目累積到 20+ 次仍然 failed，那時再加條件。
        ("scrape_attempts", "INTEGER DEFAULT 0"),
        ("scrape_last_at", "INTEGER"),
    ],
    "media_file": [
        ("mtime_ts", "INTEGER"),      # FTP 的 mtime 字串解析後的 epoch
        ("pix_fmt", "TEXT"),
        ("color_transfer", "TEXT"),
        ("color_primaries", "TEXT"),
        ("color_space", "TEXT"),
        ("bit_depth", "INTEGER"),
        ("is_hdr", "INTEGER DEFAULT 0"),
        ("fps", "REAL"),
        # 邊界表的狀態。**只放狀態不放資料** —— 資料本體在 media_keyframe，
        # 因為它每檔約 19 KB，混進這張熱表會讓每個 SELECT * 都把它拖出來。
        # 狀態留在這裡的理由相反：佇列要能用一句 WHERE 撈出待辦，不必 JOIN。
        ("kf_state", "TEXT DEFAULT 'pending'"),   # pending / ok / failed / skipped
        ("kf_error", "TEXT"),
        # 上階（remux）的 CODECS 屬性要照片源的真實 profile／level 填 ——
        # 寫死 avc1.64001f（High 3.1）會讓不符的片源在 Safari 上被整階拒收。
        # 由 keyframe 掃描順手寫進來：那支掃描本來就在對同一個檔案跑 ffprobe，
        # 而且它跟邊界表是同一組前置條件（kf_state='ok' 才有上階）。
        ("video_profile", "TEXT"),      # ffprobe 的字串，如 High / Main / High 10
        ("video_level", "INTEGER"),     # ffprobe 的整數，如 40 = level 4.0
        # 上階（remux）的執行期狀態。NULL = 還沒出過問題、'failed' = 這個檔案的
        # 上階已經下線。**一段失敗就整個 file_id 下線**，不是只退那一段 ——
        # 一份播放清單裡混著 copy 與重編的分段是更糟的失效（J 章第 0 層）。
        ("remux_state", "TEXT"),
        ("remux_error", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    added = set()
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                log.info("資料庫補上欄位 %s.%s", table, name)
                added.add(f"{table}.{name}")
    if added & {"photo.sort_ts", "photo.mtime_ts", "media_file.mtime_ts"}:
        _backfill_times(conn)
    if "media_item.scrape_attempts" in added:
        _backfill_home_videos(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photo_sort ON photo(sort_ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item_jav_number ON media_item(jav_number)")
    _time_guards(conn)


# 時間值域的守門員。
#
# 為什麼是觸發程序而不是 CHECK 約束：這些欄位是靠 ALTER TABLE ADD COLUMN
# 補上去的，而 SQLite 的 ALTER TABLE 加不了 CHECK。寫在 CREATE TABLE 裡的話，
# 新建的資料庫有約束、升級上來的沒有 —— 而且看不出來。同一份程式碼在兩台
# 機器上行為不同，是最難查的那種問題。觸發程序兩邊都套得上去，所以一致。
#
# 為什麼需要它：DATETIME2 天生擋得住亂值，INTEGER 擋不住。把毫秒當秒傳
# （×1000）會安靜地存進去，然後那一筆永遠排在最前面，沒有任何錯誤訊息。
_TIME_GUARD_COLS = {
    "photo": ("mtime_ts", "taken_ts", "sort_ts"),
    "media_file": ("mtime_ts",),
}


def _time_guards(conn: sqlite3.Connection) -> None:
    from .timeparse import MIN_TS, MAX_TS
    for table, cols in _TIME_GUARD_COLS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                continue
            for op in ("INSERT", "UPDATE"):
                name = f"guard_{table}_{col}_{op.lower()}"
                cond = (f"NEW.{col} IS NOT NULL AND "
                        f"(NEW.{col} < {MIN_TS} OR NEW.{col} > {MAX_TS})")
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(
                    f"CREATE TRIGGER {name} BEFORE {op} ON {table} "
                    f"FOR EACH ROW WHEN {cond} "
                    f"BEGIN SELECT RAISE(ABORT, '{table}.{col} 超出合理時間範圍'); END")


def _backfill_home_videos(conn: sqlite3.Connection) -> None:
    """把既有的手機錄影條目改成 kind='home'。一次性，跟著新欄位觸發。

    為什麼需要這支：guess_key 從 `movie::202110022135460800::` 換成
    `home::<epoch>`，key 一變，重掃會建新條目、舊的變成孤兒被清掉 ——
    連同使用者可能已經有的播放進度。所以要就地改，不能靠重掃。

    **三個條件同時成立才動，交集就是誤傷防護：**

    1. `kind='movie'` 且 `scrape_state='failed'` —— 已經刮到的、手動修過的
       （manual）、使用者自己改過分類的，一律不碰。
    2. 底下**每一個**檔案都在 home 目錄。混合條目（一部分檔案在、一部分不在）
       整筆跳過 —— 那種情況該由人來看，不是由 migration 猜。
    3. 檔名真的解得出拍攝時間。

    再加一道 `tmdb_id IS NULL`：有 tmdb_id 表示它曾經刮到過，不管現在什麼狀態。

    任何一條不成立就整筆留著，交給重掃或使用者處理。**寧可漏，不可錯** ——
    改錯的條目事後看不出來哪些是被誤改的。
    """
    from . import nameparser
    from .config import settings

    prefixes = settings.home_dirs
    if not prefixes:
        return

    rows = conn.execute(
        "SELECT id FROM media_item "
        "WHERE kind='movie' AND scrape_state='failed' AND tmdb_id IS NULL"
    ).fetchall()
    if not rows:
        return

    changed = merged = 0
    for row in rows:
        item_id = row[0]
        files = conn.execute(
            "SELECT ftp_path, filename FROM media_file WHERE item_id=?", (item_id,)
        ).fetchall()
        if not files:
            continue
        # 條件 2：每一個檔案都要在 home 目錄底下（比對規則同 scanner._home_dir）
        def _in_home(p: str) -> bool:
            parts = (p or "").rsplit("/", 1)[0].strip("/").split("/")
            return any(seg.lower().startswith(prefixes) for seg in parts if seg)

        if not all(_in_home(f[0]) for f in files):
            continue
        # 條件 3：檔名解得出時間。同一條目的多個檔案取最早的那個當代表。
        stamps = [t for t in (nameparser.home_video_time(f[1]) for f in files) if t]
        if len(stamps) != len(files):
            continue

        ts = min(stamps)
        key = f"home::{ts}"
        # 同一支影片被放到兩個目錄時（這座庫裡有 1 組），兩個條目會算出同一個
        # key。guess_key 是 UNIQUE，硬 UPDATE 會撞 —— 而「撞到」正是它們本來
        # 就該是同一筆的證據。把檔案接到先建立的那一筆底下，多的整筆刪掉。
        keeper = conn.execute(
            "SELECT id FROM media_item WHERE guess_key=? AND id!=?", (key, item_id)
        ).fetchone()
        if keeper:
            conn.execute("UPDATE media_file SET item_id=? WHERE item_id=?",
                         (keeper[0], item_id))
            conn.execute("DELETE FROM media_item WHERE id=?", (item_id,))
            merged += 1
            continue

        lt = time.localtime(ts)
        conn.execute(
            "UPDATE media_item SET kind='home', title=?, sort_title=?, year=?, "
            "guess_key=?, scrape_state='skip', updated_at=? WHERE id=?",
            (time.strftime("%Y-%m-%d %H:%M", lt), time.strftime("%Y%m%d-%H%M%S", lt),
             lt.tm_year, key, now_i(), item_id),
        )
        changed += 1

    if changed or merged:
        log.info("把 %s 筆條目改成家庭錄影（kind=home）%s", changed,
                 f"，另有 %s 筆是同一支影片的重複條目已合併" % merged if merged else "")


def _backfill_times(conn: sqlite3.Connection) -> None:
    """把既有的時間字串轉成 epoch。

    不轉的話，升級之後既有相片的 sort_ts 全是 NULL，會一次沉到最底下 ——
    使用者的感受是「我的相片不見了」。轉換在 Python 端做，因為那五種格式
    SQLite 自己認不得（見 app/timeparse.py）。
    """
    from .timeparse import parse
    n = 0
    for table, cols in (("photo", ("mtime", "taken_at")), ("media_file", ("mtime",))):
        rows = conn.execute(
            f"SELECT id, {', '.join(cols)} FROM {table}").fetchall()
        for r in rows:
            mt = parse(r["mtime"])
            if table == "photo":
                tk = parse(r["taken_at"])
                added = conn.execute("SELECT added_at FROM photo WHERE id=?",
                                     (r["id"],)).fetchone()
                sort = tk or mt or int(added["added_at"] or 0) or None
                conn.execute("UPDATE photo SET mtime_ts=?, taken_ts=?, sort_ts=? WHERE id=?",
                             (mt, tk, sort, r["id"]))
            else:
                conn.execute("UPDATE media_file SET mtime_ts=? WHERE id=?", (mt, r["id"]))
            n += 1
    if n:
        log.info("回填 %s 筆時間欄位（字串 → epoch）", n)


# 時間點欄位（A-3：一律 epoch 整數秒）。`duration`／`position` **不在這裡** ——
# 那是時間長度，取整會讓續播每次往前跳最多一秒。
_TIME_POINT_COLS = {
    "media_file": ("seen_at", "added_at"),
    "photo": ("seen_at", "added_at"),
    "media_item": ("added_at", "updated_at"),
    "episode": ("added_at", "updated_at"),
    "document": ("seen_at", "added_at"),
    "play_state": ("updated_at",),
}


def _round_time_points(conn: sqlite3.Connection) -> None:
    """把時間點欄位裡的浮點值取整。一次性，做完在 kv 記一筆。

    A-3 說時間點一律整數秒，但寫入端一直有兩個入口（`now()` 與 `now_i()`），
    所以實機上 `media_file.seen_at` 147 列全是浮點、`photo.seen_at` 1490 列也是。
    浮點的相等比較不可靠，而這些欄位正是「上次掃描看到」這種要拿來比對的值。

    **取整不會改變任何行為**（差距 < 1 秒），但它讓「同一個概念只有一種表示」
    這件事在資料上也成立 —— 寫入端已經統一成 `now_i()` 了。
    """
    if kv_get_conn(conn, "time_points_rounded"):
        return
    total = 0
    for table, cols in _TIME_POINT_COLS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                continue
            cur = conn.execute(
                f"UPDATE {table} SET {col}=CAST({col} AS INTEGER) "
                f"WHERE {col} IS NOT NULL AND {col} != CAST({col} AS INTEGER)")
            total += cur.rowcount or 0
    conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                 ("time_points_rounded", json.dumps(1)))
    if total:
        log.info("時間點欄位取整完成，%s 個值（A-3：時間點一律整數秒）", total)


def kv_get_conn(conn: sqlite3.Connection, key: str) -> Any:
    """kv 讀取，但用呼叫端給的連線 —— 遷移中不能再去拿一次連線。"""
    try:
        r = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not r:
        return None
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return None


def init_db() -> None:
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _round_time_points(conn)
        conn.commit()


def q(sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    with tx() as conn:
        return conn.execute(sql, tuple(params))


# --------------------------------------------------------------------------
# 批次寫入
# --------------------------------------------------------------------------
# 每一句 db.execute() 都自己開一個交易並 commit。WAL 底下那是一次 fsync，
# 一次掃描 109 部片 + 679 張相片就是快 800 次。改成累積到一定筆數再一起寫，
# 同樣的資料只要幾次 fsync。
#
# **哪些表可以批次是有講究的**：
#
#   media_file / photo   可以 —— 純 upsert，寫完不需要當場拿到 id。
#   media_item / episode 不行 —— 要先有 item_id 才寫得了檔案列。
#
# 硬把 media_item 做成批次只會製造難修的競態：兩個執行緒同時建立同一個
# guess_key，同一部影集就會變成兩張海報，一張 12 集一張 1 集。那 15 個條目
# 序列化的成本本來就是零，不值得為它冒這個險。
#
# 批次順序：緩衝區照呼叫順序存，flush 時把**連續**同一句 SQL 併成一次
# executemany。不重排順序 —— 重排就得證明「這批裡沒有兩筆動到同一列」，
# 而那是掃描器的事，不該由這一層假設。
_FILE_UPSERT = """
INSERT INTO media_file(item_id, episode_id, ftp_path, filename, size, mtime,
                       mtime_ts, ext, probe_state, seen_at, added_at)
VALUES(?,?,?,?,?,?,?,?,'pending',?,?)
ON CONFLICT(ftp_path) DO UPDATE SET
    item_id=excluded.item_id, episode_id=excluded.episode_id,
    size=excluded.size, mtime=excluded.mtime, mtime_ts=excluded.mtime_ts,
    ext=excluded.ext, seen_at=excluded.seen_at, probe_state='pending',
    -- 檔案變了就要重算邊界表。**吃跟 probe_state 同一個訊號**，不要另外
    -- 發明一套判斷 —— 兩套判斷遲早會不一致，而不一致的那一邊會安靜地留著舊資料。
    kf_state='pending', kf_error=NULL,
    -- 上階的下線紀錄也一起清掉：檔案換了，之前那個失敗的理由不再適用。
    remux_state=NULL, remux_error=NULL
"""
_FILE_TOUCH = "UPDATE media_file SET seen_at=? WHERE id=?"
_PHOTO_UPSERT = """
INSERT INTO photo(ftp_path, folder, filename, ext, size, mtime, mtime_ts, sort_ts,
                  probe_state, seen_at, added_at)
VALUES(?,?,?,?,?,?,?,?,'pending',?,?)
ON CONFLICT(ftp_path) DO UPDATE SET
    size=excluded.size, mtime=excluded.mtime, mtime_ts=excluded.mtime_ts,
    -- 已經讀過 EXIF 的話 taken_ts 才是對的排序依據，不要被 mtime 蓋掉
    sort_ts=COALESCE(photo.taken_ts, excluded.mtime_ts, photo.sort_ts, excluded.sort_ts),
    probe_state='pending', probe_error=NULL, seen_at=excluded.seen_at
"""
_PHOTO_TOUCH = "UPDATE photo SET seen_at=? WHERE id=?"


class Batch:
    """累積寫入，滿了就一次送出。用 db.batch() 取得，不要自己 new。"""

    def __init__(self, size: int = 500):
        self.size = max(1, size)
        self._buf: List[tuple] = []
        self.written = 0
        self.flushes = 0

    # ---- 呼叫端用的 ----
    def upsert_file(self, *, item_id, episode_id, path, name, size, mtime,
                    mtime_ts, ext, at) -> None:
        self._add(_FILE_UPSERT, (item_id, episode_id, path, name, size, mtime,
                                 mtime_ts, ext, at, at))

    def touch_file(self, file_id: int, at: float) -> None:
        self._add(_FILE_TOUCH, (at, file_id))

    def upsert_photo(self, *, path, folder, name, ext, size, mtime,
                     mtime_ts, sort_ts, at) -> None:
        self._add(_PHOTO_UPSERT, (path, folder, name, ext, size, mtime,
                                  mtime_ts, sort_ts, at, at))

    def touch_photo(self, photo_id: int, at: float) -> None:
        self._add(_PHOTO_TOUCH, (at, photo_id))

    # ---- 內部 ----
    def _add(self, sql: str, params: tuple) -> None:
        self._buf.append((sql, params))
        if len(self._buf) >= self.size:
            self.flush()

    def flush(self) -> int:
        if not self._buf:
            return 0
        buf, self._buf = self._buf, []
        n = 0
        with tx() as conn:
            run_sql, run = buf[0][0], []
            for sql, params in buf:
                if sql is not run_sql:
                    conn.executemany(run_sql, run)
                    n += len(run)
                    run_sql, run = sql, []
                run.append(params)
            conn.executemany(run_sql, run)
            n += len(run)
        self.written += n
        self.flushes += 1
        return n


@contextmanager
def batch(size: int = 500):
    """批次寫入。正常離開時強制寫出，出例外時丟掉還沒寫出的部分。

    出例外不寫的理由：已經 flush 出去的都在，沒 flush 的最多是最後幾百筆，
    下次掃描本來就會重新索引到。反過來「出錯了還硬把緩衝寫出去」才難解釋 ——
    使用者看到的會是一個沒人知道停在哪裡的半成品。
    """
    b = Batch(size)
    try:
        yield b
    except Exception:
        b._buf.clear()
        raise
    b.flush()


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


def now_i() -> int:
    """整數秒。時間點欄位一律用這個 —— 浮點的相等比較不可靠。"""
    return int(time.time())


def log_login(event: str, role: Optional[str], ip: str, loc: Dict[str, Any],
              email: Optional[str] = None, user_id: Optional[int] = None,
              detail: Optional[str] = None) -> None:
    """保留舊介面，實作已經搬到 app/audit.py（那邊還要同時寫 MSSQL）。"""
    from . import audit
    audit.record(event, role=role, ip=ip, loc=loc, email=email,
                 user_id=user_id, detail=detail)
