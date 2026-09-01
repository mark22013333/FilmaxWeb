"""登入驗證：單一密碼 + 簽章 cookie。

刻意不引入任何額外套件 —— 只用 hmac / hashlib / secrets。

三種放行情形：
1. 未啟用 (AUTH_ENABLED=false) → 全部放行
2. 帶著有效的簽章 cookie
3. 內部呼叫：ffmpeg 從 127.0.0.1 讀 /api/stream 時帶著本次啟動產生的隨機 token
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from typing import Optional, Tuple
from urllib.parse import parse_qs

from .config import DATA_DIR, settings

log = logging.getLogger("filmax.auth")

COOKIE = "filmax_session"
SECRET_FILE = DATA_DIR / "secret.key"

# 每次啟動產生，只給本機的 ffmpeg 用
INTERNAL_TOKEN = secrets.token_urlsafe(24)
INTERNAL_HEADER = b"x-filmax-internal"


def _secret() -> bytes:
    if settings.auth_secret:
        return settings.auth_secret.encode()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes().strip()
    key = secrets.token_urlsafe(48).encode()
    SECRET_FILE.write_bytes(key)
    try:                       # 盡量把權限收緊（Windows 上不一定有效果）
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    log.info("已產生新的 session 簽章金鑰：%s", SECRET_FILE)
    return key


# ---------------------------------------------------------------- 角色
ADMIN = "admin"        # 完整權限
VIEWER = "viewer"      # 只能看與播
ROLES = (ADMIN, VIEWER)

_TOKEN_VER = "1"       # token 格式版本，改格式時 +1 就能讓舊的全部失效


def password_for(role: str) -> str:
    return settings.auth_password if role == ADMIN else settings.viewer_password


def _session_epoch() -> int:
    """全部登出的計數器。加一就讓所有已發出的 token 立刻失效。"""
    from . import db      # 延後 import，避免循環相依
    try:
        return int(db.kv_get("session_epoch", 0) or 0)
    except Exception:
        return 0


def bump_session_epoch() -> int:
    from . import db
    n = _session_epoch() + 1
    db.kv_set("session_epoch", n)
    return n


def _sign(role: str, exp: int) -> str:
    """簽章的內容包含角色、到期時間、全域計數器，以及該角色密碼的指紋。

    密碼指紋放進簽章是刻意的：這樣改了 .env 裡的密碼，用舊密碼登入拿到的
    token 會自動失效。原本的設計只簽到期時間，改密碼對既有 session 毫無作用 ——
    密碼外洩時「改密碼」擋不住已經登入的人，那等於沒有補救手段。
    """
    pw = password_for(role) or ""
    fp = hmac.new(_secret(), f"pw:{role}:{pw}".encode(), hashlib.sha256).hexdigest()[:16]
    payload = f"{_TOKEN_VER}|{role}|{exp}|{_session_epoch()}|{fp}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def make_token(role: str = ADMIN, days: Optional[int] = None) -> str:
    if role not in ROLES:
        role = VIEWER
    exp = int(time.time()) + int((days if days is not None else settings.session_days) * 86400)
    return f"{role}.{exp}.{_sign(role, exp)}"


def read_token(token: Optional[str]) -> Optional[str]:
    """驗證 session cookie，通過就回傳角色，否則 None。

    這個函式直接吃使用者送來的原始 cookie，所以任何畸形輸入都只能回 None，
    絕對不能讓例外竄出去 —— 竄出去就是一個不用登入就能打的 500：
      * 5000 位數的數字：Python 3.11 起 int() 超過 4300 位會丟 ValueError
      * 簽章含非 ASCII：hmac.compare_digest 對非 ASCII 字串會丟 TypeError
    """
    try:
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        role, exp_s, sig_s = parts
        if role not in ROLES:
            return None
        # 位數先擋掉，int() 才不會踩到轉換上限
        if not exp_s.isdigit() or len(exp_s) > 12:
            return None
        if int(exp_s) < time.time():
            return None
        if not sig_s.isascii():
            return None
        if not hmac.compare_digest(sig_s, _sign(role, int(exp_s))):
            return None
        # 密碼被清空的角色不該還能登入
        if not password_for(role):
            return None
        return role
    except Exception:
        return None


def check_token(token: Optional[str]) -> bool:
    return read_token(token) is not None


def role_for_password(pw: str) -> Optional[str]:
    """輸入的密碼對應哪個角色。比對兩組都走 compare_digest，避免時序差異。"""
    matched = None
    for role in ROLES:
        real = password_for(role)
        if real and hmac.compare_digest(pw.encode(), real.encode()):
            matched = matched or role
    return matched


def check_password(pw: str) -> bool:
    return role_for_password(pw) is not None


# ---------------------------------------------------------------- 來源判斷
def peer_ip(scope) -> str:
    """真正連進來的那個 socket 的位址。這個值偽造不了，安全判斷一律用它。"""
    client = scope.get("client")
    return client[0] if client else ""


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode(errors="ignore").strip()
    return ""


def _peer_is_proxy(scope) -> bool:
    """連進來的是不是我們自己前面那台 proxy。

    cloudflared / nginx 這類反向代理跟本服務同機或同內網，所以 peer 會是
    loopback 或私有位址。如果 peer 是公網位址，那就是有人直接連進來，
    他送的任何 X-Forwarded-For 都只是他自己編的，一個字都不能信。
    """
    ip = peer_ip(scope)
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or a.is_private or a.is_link_local


def client_ip(scope) -> str:
    """使用者「看起來」的來源位址，用於顯示與記錄。

    X-Forwarded-For 最左邊那一段是**用戶端自己送的**（proxy 只會往後 append），
    拿它做判斷等於直接採信攻擊者輸入 —— 送個 X-Forwarded-For: 192.168.1.5
    就能假裝自己在內網。所以這裡：
      1. peer 不是我們前面的 proxy，就完全不看標頭；
      2. Cloudflare 自己填的 CF-Connecting-IP 優先；
      3. 退而求其次取 XFF 最右邊那段（最靠近我們、由我們的 proxy 寫入的）。
    """
    if not settings.trust_proxy or not _peer_is_proxy(scope):
        return peer_ip(scope)
    cf = _header(scope, b"cf-connecting-ip")
    if cf:
        return cf
    xff = _header(scope, b"x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return peer_ip(scope)


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


_extra_nets: Optional[list] = None


def _local_networks() -> list:
    global _extra_nets
    if _extra_nets is None:
        _extra_nets = []
        for chunk in (settings.extra_local_networks or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                _extra_nets.append(ipaddress.ip_network(chunk, strict=False))
            except ValueError:
                log.warning("LOCAL_NETWORKS 裡的 %r 不是合法網段，已忽略", chunk)
    return _extra_nets


def is_local_network(ip: str) -> bool:
    """區網（含 loopback）視為本地，其他一律當成遠端。

    注意 Tailscale / CGNAT 的 100.64.0.0/10 在 Python 眼中不是 private，
    預設會被當成遠端（自動降畫質）。想讓它算本地就在 .env 的
    LOCAL_NETWORKS 加上 100.64.0.0/10。
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_private or a.is_link_local:
        return True
    return any(a in net for net in _local_networks())


# ---------------------------------------------------------------- 登入次數限制
_fails: "OrderedDict[str, list[float]]" = OrderedDict()
_FAILS_MAX = 4096          # key 是來源位址，來源可以無限多，一定要有上限


def _prune_fails(now: float) -> None:
    for ip in [k for k, v in _fails.items() if not v or now - v[-1] > 900]:
        _fails.pop(ip, None)
    while len(_fails) > _FAILS_MAX:
        _fails.popitem(last=False)      # 丟掉最久沒動的


def too_many_failures(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < 900]
    if hits:
        _fails[ip] = hits
        _fails.move_to_end(ip)
    else:
        _fails.pop(ip, None)
    return len(hits) >= 8


def note_failure(ip: str) -> None:
    now = time.time()
    _fails.setdefault(ip, []).append(now)
    _fails.move_to_end(ip)
    _prune_fails(now)


def clear_failures(ip: str) -> None:
    _fails.pop(ip, None)


# ---------------------------------------------------------------- ASGI 中介層
# 不用 BaseHTTPMiddleware：它會包住 response body，對 Range 串流和 HLS 分段
# 這種大量串流輸出不理想。純 ASGI 中介層只看 request，完全不碰回應內容。
PUBLIC_PATHS = ("/login", "/logout", "/healthz", "/static/", "/favicon.ico")

# 中介層驗證完把角色掛在 scope 上，路由再從這裡讀
ROLE_KEY = "filmax_role"


def role_of(request) -> str:
    """目前這個請求的角色。沒開驗證時一律當管理員。"""
    scope = getattr(request, "scope", request)
    return scope.get(ROLE_KEY) or (ADMIN if not settings.auth_enabled else VIEWER)


def is_admin(request) -> bool:
    return role_of(request) == ADMIN


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not settings.auth_enabled:
            # 沒開驗證時大家都是管理員，行為跟以前一樣
            scope[ROLE_KEY] = ADMIN
            return await self.app(scope, receive, send)

        # CORS preflight 不帶 cookie，放行讓 CORS 中介層處理
        if scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path.startswith(PUBLIC_PATHS):
            return await self.app(scope, receive, send)

        # 本機的 ffmpeg 拿內部 token 來讀來源檔。
        # 兩個地方要特別小心：
        #  1. token 走標頭不走網址。放在網址裡的話，ffmpeg 失敗時會把整串 URL
        #     印進 stderr，那段訊息又會被存進 probe_error 並回給前端 —— 等於
        #     把一把萬能鑰匙交出去。
        #  2. loopback 判斷只認真正的 socket 位址。用 client_ip() 的話，
        #     只要送一個 X-Forwarded-For: 127.0.0.1 就能假裝自己是本機。
        tok = _header(scope, INTERNAL_HEADER)
        if tok and is_loopback(peer_ip(scope)) and hmac.compare_digest(tok, INTERNAL_TOKEN):
            scope[ROLE_KEY] = ADMIN
            return await self.app(scope, receive, send)

        cookies = ""
        for k, v in scope.get("headers") or []:
            if k == b"cookie":
                cookies = v.decode(errors="ignore")
                break
        token = ""
        for part in cookies.split(";"):
            name, _, val = part.strip().partition("=")
            if name == COOKIE:
                token = val
                break

        role = read_token(token)
        if role:
            scope[ROLE_KEY] = role
            return await self.app(scope, receive, send)

        await self._deny(scope, send, path)

    async def _deny(self, scope, send, path: str) -> None:
        wants_html = path.startswith(("/player", "/")) and not path.startswith("/api/")
        if wants_html:
            nxt = path
            q = (scope.get("query_string") or b"").decode(errors="ignore")
            if q:
                nxt += "?" + q
            location = "/login?next=" + base64.urlsafe_b64encode(nxt.encode()).decode()
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", location.encode()),
                                    (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
        else:
            body = b'{"detail":"need_login"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
