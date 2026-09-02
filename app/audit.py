"""稽核紀錄：誰、什麼時候、從哪裡、做了什麼。

寫兩個地方：

* **本機 SQLite 的 login_audit** —— 永遠寫。資料庫連不上、還沒設定、或設定
  寫錯的時候，這份仍然看得到誰在什麼時候從哪裡進來。稽核紀錄在「出事時」
  最需要，而出事時外部相依往往正是壞掉的那一個。

* **MSSQL 的 dbo.filmax_audit** —— 設定了才寫，失敗只記警告。

兩邊都不能讓寫入失敗影響到登入本身。紀錄再重要，也不該讓人因為它壞掉而登不進來。

action 的值受 dbo.filmax_audit 的 CHECK 限制（見 V2），只能是那八個之一，
所以程式內部的事件名要先對應過去，不能直接丟。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import db
from .config import settings

log = logging.getLogger("filmax.audit")

# 程式內部的事件 → dbo.filmax_audit 允許的 action
_ACTION = {
    "success": "login",
    "failed": "login_denied",
    "locked": "login_denied",
    "pending": "login_denied",
    "register": "register",
    "approve": "approve",
    "reject": "reject",
    "disable": "disable",
    "enable": "enable",
    "forbidden": "forbidden",
    "role_change": "role_change",
}
# 值域由 dbo.filmax_audit 的 CK_filmax_audit_action 決定
# （V2 建立、V5 加上 role_change）。這裡多送一個值整句 INSERT 就會失敗。
ALLOWED = ("register", "login", "login_denied", "approve", "reject",
           "disable", "enable", "forbidden", "role_change")


def record(event: str, *, role: Optional[str] = None, ip: str = "",
           loc: Optional[Dict[str, Any]] = None, email: Optional[str] = None,
           user_id: Optional[int] = None, detail: Optional[str] = None,
           ip_source: Optional[str] = None) -> None:
    loc = loc or {}
    _sqlite(event, role, ip, loc, email, user_id, detail)
    if settings.mssql_configured:
        _mssql(event, ip, loc, email, detail, ip_source)


def _sqlite(event, role, ip, loc, email, user_id, detail) -> None:
    try:
        db.execute(
            """INSERT INTO login_audit
                   (at, event, role, ip, country, region, city, timezone,
                    latitude, longitude, user_agent, email, user_id, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), event, role, ip, loc.get("country"), loc.get("region"),
             loc.get("city"), loc.get("timezone"), loc.get("latitude"),
             loc.get("longitude"), loc.get("user_agent"), email, user_id,
             str(detail)[:300] if detail else None))
    except Exception as e:
        log.warning("本機稽核紀錄寫入失敗: %s", e)


def _mssql(event, ip, loc, email, detail, ip_source) -> None:
    from . import mssql, users
    action = _ACTION.get(event)
    if action not in ALLOWED:
        log.warning("未知的稽核事件 %r，不寫入 MSSQL", event)
        return
    try:
        mssql.execute(
            """INSERT INTO dbo.filmax_audit
                   (at, email, action, detail, ip, ip_source, country, region, city,
                    timezone, latitude, longitude, user_agent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mssql.now_dt(), (email or None) and str(email)[:320], action,
             str(detail)[:500] if detail else None,
             str(ip)[:64] if ip else None,
             ip_source if ip_source in ("cloudflare", "lan", "direct", "internal") else None,
             users._country(loc.get("country")),
             _cut(loc.get("region"), 120), _cut(loc.get("city"), 120),
             _cut(loc.get("timezone"), 64),
             _dec(loc.get("latitude")), _dec(loc.get("longitude")),
             _cut(loc.get("user_agent"), 400)))
    except Exception as e:
        log.warning("MSSQL 稽核紀錄寫入失敗: %s", mssql.scrub(e))


def _cut(v: Any, n: int) -> Optional[str]:
    return (str(v)[:n] or None) if v else None


def _dec(v: Any) -> Optional[float]:
    """DECIMAL(9,6)：整數部分只有 3 位，超出範圍會直接讓整句 INSERT 失敗。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 6) if -180 <= f <= 180 else None
