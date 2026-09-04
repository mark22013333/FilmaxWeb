"""使用者資料的 SQLite 後端。

欄位刻意跟 db/sql/V1__create_users.sql 的 dbo.filmax_users 對齊，
所以同一份上層程式碼換成 users_mssql 也能跑。

時間欄位在這裡是 epoch 秒（float）—— 上層與前端看到的都是這個型別，
MSSQL 那邊的 DATETIME2 也會被轉成同樣的東西。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import db

log = logging.getLogger("filmax.users")

NAME = "sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    google_sub         TEXT UNIQUE,
    display_name       TEXT,
    picture_url        TEXT,
    role               TEXT NOT NULL DEFAULT 'viewer',   -- owner / viewer
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/disabled
    created_at         REAL,
    registered_ip      TEXT,
    registered_country TEXT,
    registered_city    TEXT,
    approved_at        REAL,
    approved_by        TEXT,
    last_login_at      REAL,
    last_login_ip      TEXT,
    last_login_country TEXT,
    last_login_city    TEXT,
    last_login_ua      TEXT,
    login_count        INTEGER DEFAULT 0,
    sess_ver           INTEGER DEFAULT 0,
    note               TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_status ON app_user(status);
"""


def init() -> None:
    conn = db.get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def describe() -> str:
    from .config import DB_PATH
    return f"SQLite：{DB_PATH}（app_user）"


# ---------------------------------------------------------------- 讀
def by_id(uid: int) -> Optional[Dict[str, Any]]:
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE id=?", (uid,)))


def by_email(email: str) -> Optional[Dict[str, Any]]:
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE email=? COLLATE NOCASE",
                                (email,)))


def by_sub(sub: str) -> Optional[Dict[str, Any]]:
    if not sub:
        return None
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE google_sub=?", (sub,)))


def listing(status: Optional[str], limit: int) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM app_user"
    params: List[Any] = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    # 待審核排最前面，那是後台最常要處理的一群
    sql += (" ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
            " COALESCE(last_login_at, created_at) DESC LIMIT ?")
    params.append(limit)
    return [db.row_to_dict(r) for r in db.q(sql, params)]


def counts() -> Dict[str, int]:
    return {r["status"]: r["n"] for r in
            db.q("SELECT status, COUNT(*) n FROM app_user GROUP BY status")}


def owner_count() -> int:
    r = db.q1("SELECT COUNT(*) c FROM app_user WHERE role='owner' AND status='approved'")
    return int(r["c"]) if r else 0


def state(uid: int) -> Optional[Dict[str, Any]]:
    return db.row_to_dict(
        db.q1("SELECT id, email, role, status, sess_ver FROM app_user WHERE id=?", (uid,)))


# ---------------------------------------------------------------- 寫
def create(email: str, sub: str, name: str, picture: Optional[str], role: str,
           status: str, ip: str, loc: Dict[str, Any], approved_by: Optional[str]) -> int:
    now = db.now_i()
    cur = db.execute(
        """INSERT INTO app_user
               (email, google_sub, display_name, picture_url, role, status, created_at,
                registered_ip, registered_country, registered_city,
                approved_at, approved_by, login_count, sess_ver)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
        (email, sub or None, name, picture, role, status, now,
         ip, loc.get("country"), loc.get("city"),
         now if status == "approved" else None, approved_by))
    return int(cur.lastrowid)


def touch_profile(uid: int, name: str, picture: Optional[str],
                  sub: Optional[str] = None, email: Optional[str] = None) -> None:
    sets, params = ["display_name=?", "picture_url=?"], [name, picture]
    if sub:
        sets.append("google_sub=?"); params.append(sub)
    if email:
        sets.append("email=?"); params.append(email)
    params.append(uid)
    db.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE id=?", params)


def promote(uid: int, role: str, status: str, approved_by: str) -> None:
    db.execute(
        """UPDATE app_user SET role=?, status=?, approved_at=?, approved_by=?,
                               sess_ver=COALESCE(sess_ver,0)+1
            WHERE id=?""",
        (role, status, db.now_i(), approved_by, uid))


def record_login(uid: int, ip: str, loc: Dict[str, Any]) -> None:
    db.execute(
        """UPDATE app_user
              SET last_login_at=?, last_login_ip=?, last_login_country=?,
                  last_login_city=?, last_login_ua=?,
                  login_count=COALESCE(login_count,0)+1
            WHERE id=?""",
        (db.now_i(), ip, loc.get("country"), loc.get("city"), loc.get("user_agent"), uid))


def set_status(uid: int, status: str, by: Optional[str]) -> None:
    approved = status == "approved"
    db.execute(
        """UPDATE app_user SET status=?, approved_at=?, approved_by=?,
                               sess_ver=COALESCE(sess_ver,0)+1
            WHERE id=?""",
        (status, db.now_i() if approved else None, by if approved else None, uid))


def set_role(uid: int, db_role: str) -> None:
    db.execute("UPDATE app_user SET role=?, sess_ver=COALESCE(sess_ver,0)+1 WHERE id=?",
               (db_role, uid))


def set_note(uid: int, note: Optional[str]) -> None:
    db.execute("UPDATE app_user SET note=? WHERE id=?", (note, uid))


def delete(uid: int) -> None:
    db.execute("DELETE FROM app_user WHERE id=?", (uid,))
