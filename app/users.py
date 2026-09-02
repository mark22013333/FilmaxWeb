"""使用者帳號。

儲存有兩個後端，由設定決定用哪一個：
    設了 MSSQL_HOST/DATABASE/USER  → users_mssql（dbo.filmax_users）
    沒設                            → users_sqlite（data/library.db 的 app_user）

**刻意沒有「連不上 MSSQL 就自動退回 SQLite」**。乍看貼心，實際上是把權限判斷
變成兩個各說各話的來源：在 MSSQL 核准的人在 SQLite 不存在，反過來也一樣，
而且沒有人會發現自己正在用哪一份。設定了就是要用它，連不上就明講連不上。

角色有兩套名字：
    資料庫    owner / viewer     （V1 的 CHECK 限制就是這兩個值）
    程式內    admin / viewer     （auth.ADMIN / auth.VIEWER）
轉換只在 role_to_auth() 一個地方發生。

狀態：pending（待審核）/ approved（可用）/ rejected / disabled，只有 approved 進得來。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .config import settings

log = logging.getLogger("filmax.users")

OWNER, VIEWER = "owner", "viewer"
DB_ROLES = (OWNER, VIEWER)
STATUSES = ("pending", "approved", "rejected", "disabled")

# 欄位長度以 dbo.filmax_users 為準（見 db/sql/V1__create_users.sql）。
# 在這裡就截斷，兩個後端才會存到一樣的東西 —— 只在 MSSQL 那邊截的話，
# 本機測起來正常、上了正式環境才因為超長噴錯。
_LIMITS = {"email": 320, "google_sub": 64, "display_name": 200, "picture_url": 500,
           "ip": 64, "country": 2, "city": 120, "user_agent": 400,
           "approved_by": 200, "note": 500}


def _cut(v: Optional[str], key: str) -> Optional[str]:
    if v is None:
        return None
    v = str(v)[:_LIMITS[key]]
    return v or None


def _backend():
    from . import users_mssql, users_sqlite
    return users_mssql if settings.user_store == "mssql" else users_sqlite


def store_name() -> str:
    return _backend().NAME


def describe() -> str:
    return _backend().describe()


def init() -> None:
    _backend().init()


def role_to_auth(db_role: str) -> str:
    from . import auth
    return auth.ADMIN if db_role == OWNER else auth.VIEWER


# ---------------------------------------------------------------- 查詢
def by_id(uid: int) -> Optional[Dict[str, Any]]:
    return _backend().by_id(int(uid))


def by_email(email: str) -> Optional[Dict[str, Any]]:
    return _backend().by_email(email)


def by_sub(sub: str) -> Optional[Dict[str, Any]]:
    return _backend().by_sub(sub)


def listing(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    if status not in STATUSES:
        status = None
    return _backend().listing(status, max(1, min(int(limit), 1000)))


def counts() -> Dict[str, int]:
    got = _backend().counts()
    out = {s: int(got.get(s, 0)) for s in STATUSES}
    out["total"] = sum(out[s] for s in STATUSES)
    return out


def has_owner() -> bool:
    return _backend().owner_count() > 0


def owner_count() -> int:
    return _backend().owner_count()


# ---------------------------------------------------------------- 寫入
def upsert_from_google(claims: Dict[str, Any], ip: str, loc: Dict[str, Any]) -> Dict[str, Any]:
    """第一次登入就建帳號，之後只更新名字與頭像。

    比對優先用 google_sub（Google 帳號的永久 ID），沒有再用 email。
    email 是會變的（改網域、改別名），sub 不會。反過來用 email 當主鍵的話，
    對方換了顯示用的 email 就會多一個帳號，權限也跟著掉。
    """
    sub = _cut(str(claims.get("sub") or ""), "google_sub") or ""
    email = _cut(str(claims.get("email") or "").strip(), "email") or ""
    name = _cut(claims.get("name") or email.split("@")[0], "display_name")
    picture = _cut(claims.get("picture") or "", "picture_url")
    is_admin_email = email.lower() in settings.admin_emails

    b = _backend()
    rec = b.by_sub(sub) or b.by_email(email)
    if rec:
        b.touch_profile(
            rec["id"], name, picture,
            sub=sub if (sub and not rec.get("google_sub")) else None,
            email=email if (email and (rec.get("email") or "").lower() != email.lower()) else None)
        if is_admin_email and (rec.get("role") != OWNER or rec.get("status") != "approved"):
            # GOOGLE_ADMIN_EMAILS 是最後的逃生門。
            # 沒有這一段會有一個解不開的死結：某人先登入一次變成待審核，
            # 管理員這時才把他的信箱寫進 .env —— 但他已經有帳號了，
            # 新帳號那條路不會跑到，於是所有人永遠卡在待審核、沒有人能核准。
            b.promote(rec["id"], OWNER, "approved", "system")
            log.warning("%s 在 GOOGLE_ADMIN_EMAILS 名單內，已提升為管理員", email)
        invalidate(rec["id"])
        return b.by_id(rec["id"])

    role = OWNER if is_admin_email else VIEWER
    status = "approved" if (is_admin_email or settings.google_auto_approve) else "pending"
    uid = b.create(email, sub, name, picture, role, status,
                   _cut(ip, "ip"), _loc(loc),
                   _cut("system", "approved_by") if status == "approved" else None)
    log.info("新帳號 %s（%s / %s），來源 %s", email, role, status, ip)
    invalidate(uid)
    return b.by_id(uid)


def _country(v: Any) -> Optional[str]:
    """國碼欄位是 CHAR(2)，但截斷是錯的做法。

    把 "TAIWAN" 截成 "TA" 會存進一個看起來合法、實際上不存在的國碼，
    之後所有依國家做的統計都被汙染而且查不出來。Cloudflare 只會送
    ISO 3166-1 alpha-2，不是那個格式就代表來源不對，寧可留空。
    """
    v = (str(v or "")).strip()
    return v.upper() if len(v) == 2 and v.isalpha() else None


def _loc(loc: Dict[str, Any]) -> Dict[str, Any]:
    return {"country": _country(loc.get("country")),
            "city": _cut(loc.get("city"), "city"),
            "user_agent": _cut(loc.get("user_agent"), "user_agent")}


def record_login(uid: int, ip: str, loc: Dict[str, Any]) -> None:
    _backend().record_login(int(uid), _cut(ip, "ip"), _loc(loc))
    invalidate(uid)


def set_status(uid: int, status: str, by: str = "") -> Optional[Dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError("狀態不合法")
    _backend().set_status(int(uid), status, _cut(by, "approved_by"))
    invalidate(uid)
    return by_id(uid)


def set_role(uid: int, db_role: str) -> Optional[Dict[str, Any]]:
    if db_role not in DB_ROLES:
        raise ValueError("角色不合法")
    # sess_ver +1 由後端負責：降權要立刻生效，不然對方手上那張寫著 owner 的
    # token 還能用到過期為止，「拔掉權限」就成了空話。
    _backend().set_role(int(uid), db_role)
    invalidate(uid)
    return by_id(uid)


def set_note(uid: int, note: str) -> Optional[Dict[str, Any]]:
    _backend().set_note(int(uid), _cut(note, "note"))
    invalidate(uid)
    return by_id(uid)


def delete(uid: int) -> None:
    _backend().delete(int(uid))
    invalidate(uid)


# ---------------------------------------------------------------- session 用的快取
# 每一個請求（包含每一個 HLS 分段）都要確認這個人還在、還是這個角色。
# 播放時每秒好幾發，直接查資料庫太浪費 —— 接上 MSSQL 之後更是每次都要跨網路。
# 很短的 TTL 加上異動時主動清除：停權幾乎立即生效，又不會讓串流一直敲資料庫。
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
    """回傳 {id, email, role, status, sess_ver}，查不到就 None。

    資料庫連不上時回 None（等於「這張 token 不算數」），不會讓例外竄到
    中介層變成 500。使用者看到的是「請重新登入」，日誌裡有真正的原因。
    """
    uid = int(uid)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(uid)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    try:
        val = _backend().state(uid)
    except Exception as e:
        log.warning("讀取帳號狀態失敗（uid=%s）：%s", uid, e)
        return None
    with _cache_lock:
        _cache[uid] = (now, val)
        if len(_cache) > 2048:          # 別讓它無限長大
            _cache.clear()
    return val
