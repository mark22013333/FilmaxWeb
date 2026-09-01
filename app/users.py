"""使用者帳號（Google 登入用）。

欄位刻意跟 db/sql/V1__create_users.sql 的 dbo.filmax_users 對齊，
未來要把儲存換成 MSSQL 時，改的是這一個檔案，不會動到其他地方。

角色有兩套名字，這裡要說清楚免得混淆：
  資料庫    owner / viewer     （V1 的 CHECK 限制就是這兩個值）
  程式內    admin / viewer     （auth.ADMIN / auth.VIEWER）
owner 對應 admin，其餘一律 viewer。轉換只在 role_to_auth() 一個地方發生。

狀態 status：pending（待審核）/ approved（可用）/ rejected / disabled。
只有 approved 才進得來。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from . import db

log = logging.getLogger("filmax.users")

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
    sess_ver           INTEGER DEFAULT 0,   -- +1 就讓這個人已發出的 session 立刻失效
    note               TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_status ON app_user(status);
"""

OWNER, VIEWER = "owner", "viewer"
DB_ROLES = (OWNER, VIEWER)
STATUSES = ("pending", "approved", "rejected", "disabled")

PUBLIC_FIELDS = ("id", "email", "display_name", "picture_url", "role", "status",
                 "created_at", "registered_ip", "registered_country", "registered_city",
                 "approved_at", "approved_by", "last_login_at", "last_login_ip",
                 "last_login_country", "last_login_city", "last_login_ua",
                 "login_count", "note")


def init() -> None:
    conn = db.get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def role_to_auth(db_role: str) -> str:
    from . import auth
    return auth.ADMIN if db_role == OWNER else auth.VIEWER


# ---------------------------------------------------------------- 查詢
def by_email(email: str) -> Optional[Dict[str, Any]]:
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE email=? COLLATE NOCASE",
                                (email,)))


def by_id(uid: int) -> Optional[Dict[str, Any]]:
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE id=?", (uid,)))


def by_sub(sub: str) -> Optional[Dict[str, Any]]:
    if not sub:
        return None
    return db.row_to_dict(db.q1("SELECT * FROM app_user WHERE google_sub=?", (sub,)))


def listing(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM app_user"
    params: List[Any] = []
    if status in STATUSES:
        sql += " WHERE status=?"
        params.append(status)
    # 待審核的排最前面，這是後台最常要處理的一群
    sql += (" ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
            " COALESCE(last_login_at, created_at) DESC LIMIT ?")
    params.append(max(1, min(int(limit), 1000)))
    return [db.row_to_dict(r) for r in db.q(sql, params)]


def counts() -> Dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in db.q("SELECT status, COUNT(*) n FROM app_user GROUP BY status"):
        out[r["status"]] = r["n"]
    out["total"] = sum(out[s] for s in STATUSES)
    return out


def has_owner() -> bool:
    r = db.q1("SELECT 1 FROM app_user WHERE role='owner' AND status='approved' LIMIT 1")
    return r is not None


# ---------------------------------------------------------------- 寫入
def upsert_from_google(claims: Dict[str, Any], ip: str, loc: Dict[str, Any]) -> Dict[str, Any]:
    """第一次登入就建帳號，之後只更新名字與頭像。

    比對優先用 google_sub（Google 帳號的永久 ID），沒有再用 email。
    理由：email 是會變的（改網域、改別名），sub 不會。反過來用 email 當主鍵，
    對方換了顯示用的 email 就會多出一個帳號，權限也跟著掉。
    """
    from .config import settings

    sub = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").strip()
    name = (claims.get("name") or email.split("@")[0])[:200]
    picture = (claims.get("picture") or "")[:500] or None

    rec = by_sub(sub) or by_email(email)
    if rec:
        sets = ["display_name=?", "picture_url=?"]
        params: List[Any] = [name, picture]
        if email.lower() in settings.admin_emails and (
                rec.get("role") != OWNER or rec.get("status") != "approved"):
            # GOOGLE_ADMIN_EMAILS 是最後的逃生門。
            # 沒有這一段的話會有一個解不開的死結：某人先登入一次變成待審核，
            # 管理員這時才把他的信箱寫進 .env —— 但他已經有帳號了，
            # 上面的新帳號邏輯不會跑到，於是所有人永遠卡在待審核、沒有人能核准。
            sets += ["role=?", "status=?", "approved_at=?", "approved_by=?",
                     "sess_ver=COALESCE(sess_ver,0)+1"]
            params += [OWNER, "approved", db.now(), "system"]
            log.warning("%s 在 GOOGLE_ADMIN_EMAILS 名單內，已提升為管理員", email)
        if not rec.get("google_sub") and sub:
            # 管理員可以先用 email 把人建好，對方第一次登入才補上 sub
            sets.append("google_sub=?")
            params.append(sub)
        if rec.get("email", "").lower() != email.lower() and email:
            sets.append("email=?")
            params.append(email)
        params.append(rec["id"])
        db.execute(f"UPDATE app_user SET {', '.join(sets)} WHERE id=?", params)
        invalidate(rec["id"])
        return by_id(rec["id"])

    # 新帳號
    is_admin_email = email.lower() in settings.admin_emails
    # 第一個進來的人如果還沒有任何管理員，而且他在 GOOGLE_ADMIN_EMAILS 裡，就是管理員。
    role = OWNER if is_admin_email else VIEWER
    status = "approved" if (is_admin_email or settings.google_auto_approve) else "pending"
    now = db.now()
    cur = db.execute(
        """INSERT INTO app_user
               (email, google_sub, display_name, picture_url, role, status, created_at,
                registered_ip, registered_country, registered_city,
                approved_at, approved_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (email, sub or None, name, picture, role, status, now,
         ip, loc.get("country"), loc.get("city"),
         now if status == "approved" else None,
         "system" if status == "approved" else None))
    log.info("新帳號 %s（%s / %s），來源 %s", email, role, status, ip)
    return by_id(cur.lastrowid)


def record_login(uid: int, ip: str, loc: Dict[str, Any]) -> None:
    db.execute(
        """UPDATE app_user
              SET last_login_at=?, last_login_ip=?, last_login_country=?,
                  last_login_city=?, last_login_ua=?, login_count=COALESCE(login_count,0)+1
            WHERE id=?""",
        (db.now(), ip, loc.get("country"), loc.get("city"), loc.get("user_agent"), uid))
    invalidate(uid)


def set_status(uid: int, status: str, by: str = "") -> Optional[Dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError("狀態不合法")
    approved = status == "approved"
    db.execute(
        """UPDATE app_user SET status=?, approved_at=?, approved_by=?,
                               sess_ver=COALESCE(sess_ver,0)+1
            WHERE id=?""",
        (status, db.now() if approved else None, (by or None) if approved else None, uid))
    invalidate(uid)
    return by_id(uid)


def set_role(uid: int, db_role: str) -> Optional[Dict[str, Any]]:
    if db_role not in DB_ROLES:
        raise ValueError("角色不合法")
    # sess_ver +1：降權要立刻生效。不然對方手上那張寫著 owner 的 token
    # 還能用到過期為止，「拔掉權限」就成了空話。
    db.execute("UPDATE app_user SET role=?, sess_ver=COALESCE(sess_ver,0)+1 WHERE id=?",
               (db_role, uid))
    invalidate(uid)
    return by_id(uid)


def set_note(uid: int, note: str) -> Optional[Dict[str, Any]]:
    db.execute("UPDATE app_user SET note=? WHERE id=?", ((note or "")[:500] or None, uid))
    invalidate(uid)
    return by_id(uid)


def delete(uid: int) -> None:
    db.execute("DELETE FROM app_user WHERE id=?", (uid,))
    invalidate(uid)


# ---------------------------------------------------------------- session 用的快取
# 每一個請求（包含每一個 HLS 分段）都要確認這個人還在、還是這個角色。
# 直接查資料庫也不是不行，但播放時每秒好幾發，沒必要。
# 這裡放一個很短的 TTL，加上任何異動都主動清掉 —— 停權幾乎是立即生效，
# 又不會讓串流變成一直在敲資料庫。
_CACHE_TTL = 30.0
_cache: Dict[int, tuple] = {}
_cache_lock = threading.Lock()


def invalidate(uid: Optional[int] = None) -> None:
    with _cache_lock:
        if uid is None:
            _cache.clear()
        else:
            _cache.pop(int(uid), None)


def session_state(uid: int) -> Optional[Dict[str, Any]]:
    """回傳 {role, status, sess_ver, email}，查不到就 None。"""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(uid)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    row = db.q1("SELECT id, email, role, status, sess_ver FROM app_user WHERE id=?", (uid,))
    val = db.row_to_dict(row)
    with _cache_lock:
        _cache[uid] = (now, val)
        if len(_cache) > 2048:          # 別讓它無限長大
            _cache.clear()
    return val
