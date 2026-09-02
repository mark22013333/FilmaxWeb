"""使用者資料的 MSSQL 後端（dbo.filmax_users）。

跟 users_sqlite 是同一組介面，users.py 依設定挑一個用。

要注意的四件事：

1. **不建表也不改表。** 服務用的 filmax_app 帳號被明確 DENY 掉 DDL
   （見 db/00_create_database.sql），結構一律由 Flyway 套用。這裡的 init()
   只做檢查，缺東西就講清楚缺什麼，不會偷偷幫你建。

2. **時間進出都換算。** 資料庫存 DATETIME2（UTC），程式裡一律 epoch 秒。

3. **email 比對強制轉小寫。** 資料庫的定序通常不分大小寫，但不能賭 ——
   萬一是 CS 定序，Mark@x.com 跟 mark@x.com 會變成兩個帳號、兩套權限。
   這張表只有幾十筆，不走索引沒差。

4. **CHAR(2) 會補空白。** country 讀回來是 'TW' 沒問題，但單字元值會變成 'T '，
   所以讀出來一律 rstrip。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import mssql

log = logging.getLogger("filmax.users")

NAME = "mssql"
TABLE = "dbo.filmax_users"

COLS = ("id, email, google_sub, display_name, picture_url, role, status, created_at, "
        "registered_ip, registered_country, registered_city, approved_at, approved_by, "
        "last_login_at, last_login_ip, last_login_country, last_login_city, "
        "last_login_ua, login_count, sess_ver, note")

_TIME_FIELDS = ("created_at", "approved_at", "last_login_at")
_CHAR_FIELDS = ("registered_country", "last_login_country")


def _norm(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    for f in _TIME_FIELDS:
        if f in row:
            row[f] = mssql.to_epoch(row[f])
    for f in _CHAR_FIELDS:
        v = row.get(f)
        if isinstance(v, str):
            row[f] = v.rstrip() or None
    return row


def describe() -> str:
    return f"MSSQL：{mssql.safe_target()}（{TABLE}）"


def init() -> None:
    """只檢查，不建立。缺什麼就丟例外，讓啟動時就吵，而不是等有人登入才爆。"""
    st = mssql.ping()
    if st.get("error"):
        raise mssql.MssqlUnavailable(st["error"])
    if st.get("missing"):
        raise mssql.MssqlUnavailable(
            "資料庫缺少：" + "、".join(st["missing"]) + "。請先跑 db/migrate.bat 套用 Flyway 遷移。")


# ---------------------------------------------------------------- 讀
def by_id(uid: int) -> Optional[Dict[str, Any]]:
    return _norm(mssql.q1(f"SELECT {COLS} FROM {TABLE} WHERE id = ?", (uid,)))


def by_email(email: str) -> Optional[Dict[str, Any]]:
    return _norm(mssql.q1(
        f"SELECT {COLS} FROM {TABLE} WHERE LOWER(email) = LOWER(?)", (email,)))


def by_sub(sub: str) -> Optional[Dict[str, Any]]:
    if not sub:
        return None
    return _norm(mssql.q1(f"SELECT {COLS} FROM {TABLE} WHERE google_sub = ?", (sub,)))


def listing(status: Optional[str], limit: int) -> List[Dict[str, Any]]:
    where = "WHERE status = ?" if status else ""
    params: List[Any] = [limit]
    if status:
        params.append(status)
    sql = (f"SELECT TOP (?) {COLS} FROM {TABLE} {where} "
           "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, "
           "COALESCE(last_login_at, created_at) DESC")
    return [_norm(r) for r in mssql.q(sql, params)]


def counts() -> Dict[str, int]:
    return {r["status"]: int(r["n"]) for r in
            mssql.q(f"SELECT status, COUNT(*) AS n FROM {TABLE} GROUP BY status")}


def owner_count() -> int:
    return int(mssql.scalar(
        f"SELECT COUNT(*) FROM {TABLE} WHERE role = 'owner' AND status = 'approved'") or 0)


def state(uid: int) -> Optional[Dict[str, Any]]:
    return mssql.q1(
        f"SELECT id, email, role, status, sess_ver FROM {TABLE} WHERE id = ?", (uid,))


# ---------------------------------------------------------------- 寫
def create(email: str, sub: str, name: str, picture: Optional[str], role: str,
           status: str, ip: str, loc: Dict[str, Any], approved_by: Optional[str]) -> int:
    now = mssql.now_dt()
    # OUTPUT INSERTED.id 比 SCOPE_IDENTITY() 可靠：後者在有觸發程序或
    # 同一批次多句時容易拿錯，OUTPUT 直接由這一句的插入結果產出。
    new_id = mssql.scalar(
        f"""INSERT INTO {TABLE}
                (email, google_sub, display_name, picture_url, role, status, created_at,
                 registered_ip, registered_country, registered_city,
                 approved_at, approved_by, login_count, sess_ver)
            OUTPUT INSERTED.id
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)""",
        (email, sub or None, name, picture, role, status, now,
         ip, loc.get("country"), loc.get("city"),
         now if status == "approved" else None, approved_by))
    return int(new_id)


def touch_profile(uid: int, name: str, picture: Optional[str],
                  sub: Optional[str] = None, email: Optional[str] = None) -> None:
    sets, params = ["display_name = ?", "picture_url = ?"], [name, picture]
    if sub:
        sets.append("google_sub = ?"); params.append(sub)
    if email:
        sets.append("email = ?"); params.append(email)
    params.append(uid)
    mssql.execute(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE id = ?", params)


def promote(uid: int, role: str, status: str, approved_by: str) -> None:
    mssql.execute(
        f"""UPDATE {TABLE}
               SET role = ?, status = ?, approved_at = ?, approved_by = ?,
                   sess_ver = COALESCE(sess_ver, 0) + 1
             WHERE id = ?""",
        (role, status, mssql.now_dt(), approved_by, uid))


def record_login(uid: int, ip: str, loc: Dict[str, Any]) -> None:
    mssql.execute(
        f"""UPDATE {TABLE}
               SET last_login_at = ?, last_login_ip = ?, last_login_country = ?,
                   last_login_city = ?, last_login_ua = ?,
                   login_count = COALESCE(login_count, 0) + 1
             WHERE id = ?""",
        (mssql.now_dt(), ip, loc.get("country"), loc.get("city"),
         loc.get("user_agent"), uid))


def set_status(uid: int, status: str, by: Optional[str]) -> None:
    approved = status == "approved"
    mssql.execute(
        f"""UPDATE {TABLE}
               SET status = ?, approved_at = ?, approved_by = ?,
                   sess_ver = COALESCE(sess_ver, 0) + 1
             WHERE id = ?""",
        (status, mssql.now_dt() if approved else None, by if approved else None, uid))


def set_role(uid: int, db_role: str) -> None:
    mssql.execute(
        f"UPDATE {TABLE} SET role = ?, sess_ver = COALESCE(sess_ver, 0) + 1 WHERE id = ?",
        (db_role, uid))


def set_note(uid: int, note: Optional[str]) -> None:
    mssql.execute(f"UPDATE {TABLE} SET note = ? WHERE id = ?", (note, uid))


def delete(uid: int) -> None:
    mssql.execute(f"DELETE FROM {TABLE} WHERE id = ?", (uid,))
