"""Google 登入（OpenID Connect authorization code + PKCE）。

刻意不引入 authlib / PyJWT：整個流程需要的東西標準函式庫都有，
只有那一次 HTTPS 交換要用 httpx（專案本來就有）。

三個安全重點：

1. **CSRF**：授權請求帶一個隨機 state，同時把 state、nonce、PKCE verifier
   簽章後放進一個短效 cookie。回呼時兩邊要對得起來，否則就是別人塞給你的
   回呼網址，直接丟掉。

2. **id_token 的簽章沒有另外驗**。這不是偷懶：token 是我們自己用
   client_secret 直接向 Google 的 token endpoint 換來的，走的是 TLS 且
   對方經過憑證驗證，OIDC Core 3.1.3.7 明文說這種情形可以不驗簽。
   （會需要驗簽的是 implicit flow —— token 經由瀏覽器轉手，中間有人可以動手腳。）
   但 iss / aud / exp / nonce / email_verified 一項都不能少，那些不是簽章能取代的。

3. **redirect_uri 一定要事先在 Google Cloud Console 登記**，而且必須是 https
   （只有 http://localhost 例外，純 IP 位址一律不接受）。所以這裡用設定檔裡
   寫死的那一個，不從請求的 Host 推導 —— Host 是使用者可以塞的。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from .config import settings

log = logging.getLogger("filmax.oauth")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

STATE_COOKIE = "filmax_oauth"
STATE_TTL = 600          # 10 分鐘內要走完，逾時就重來


class OAuthError(Exception):
    """流程失敗。訊息會直接顯示給使用者，所以不要放內部細節。"""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode() + b"=" * (-len(s) % 4))


def _secret() -> bytes:
    from .auth import _secret as auth_secret
    return auth_secret()


# ---------------------------------------------------------------- state cookie
def new_flow(next_b64: str = "") -> Tuple[str, str, str]:
    """產生一次授權流程需要的東西。

    回傳 (授權網址, state cookie 的值, redirect_uri)。
    """
    redirect_uri = settings.redirect_uri()
    if not redirect_uri:
        raise OAuthError("伺服器沒有設定 PUBLIC_BASE_URL 或 GOOGLE_REDIRECT_URI")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64e(hashlib.sha256(verifier.encode()).digest())

    payload = {"s": state, "n": nonce, "v": verifier,
               "x": next_b64 or "", "e": int(time.time()) + STATE_TTL}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()
    cookie_val = f"{_b64e(raw)}.{_b64e(sig)}"

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # consent 畫面每次都問「用哪個帳號」，避免瀏覽器有多個 Google 帳號時
        # 直接沿用上一個，使用者以為登錯人
        "prompt": "select_account",
        "access_type": "online",
    }
    from urllib.parse import urlencode
    return f"{AUTH_ENDPOINT}?{urlencode(params)}", cookie_val, redirect_uri


def read_flow(cookie_val: Optional[str], state: str) -> Dict[str, Any]:
    """驗證回呼帶回來的 state。任何一項不合就丟 OAuthError。"""
    if not cookie_val or not state:
        raise OAuthError("登入流程已逾時，請再試一次")
    try:
        raw_b64, sig_b64 = cookie_val.split(".", 1)
        raw = _b64d(raw_b64)
        sig = _b64d(sig_b64)
    except Exception:
        raise OAuthError("登入流程資料異常，請再試一次")
    if not hmac.compare_digest(sig, hmac.new(_secret(), raw, hashlib.sha256).digest()):
        raise OAuthError("登入流程驗證失敗，請再試一次")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise OAuthError("登入流程資料異常，請再試一次")
    if int(payload.get("e") or 0) < time.time():
        raise OAuthError("登入流程已逾時，請再試一次")
    if not hmac.compare_digest(str(payload.get("s") or ""), state):
        raise OAuthError("登入流程驗證失敗，請再試一次")
    return payload


# ---------------------------------------------------------------- 換 token
def exchange(code: str, verifier: str, redirect_uri: str) -> Dict[str, Any]:
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    try:
        r = httpx.post(TOKEN_ENDPOINT, data=data, timeout=15)
    except httpx.HTTPError as e:
        log.warning("向 Google 換 token 失敗：%s", e)
        raise OAuthError("連不上 Google，請稍後再試")
    if r.status_code != 200:
        # Google 的錯誤內容可能含 client_secret 相關線索，只寫進日誌不回前端
        log.warning("Google token endpoint 回 %s：%s", r.status_code, r.text[:400])
        raise OAuthError("Google 拒絕了這次登入，請再試一次")
    try:
        return r.json()
    except Exception:
        raise OAuthError("Google 回應格式異常")


def claims_from(token_response: Dict[str, Any], nonce: str) -> Dict[str, Any]:
    """從 id_token 取出身分，並檢查每一項該檢查的。"""
    id_token = token_response.get("id_token")
    if not id_token:
        raise OAuthError("Google 沒有回傳身分資訊")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OAuthError("身分資訊格式異常")
    try:
        claims = json.loads(_b64d(parts[1]).decode("utf-8"))
    except Exception:
        raise OAuthError("身分資訊格式異常")

    if claims.get("iss") not in ISSUERS:
        raise OAuthError("身分資訊來源不正確")
    aud = claims.get("aud")
    if isinstance(aud, list):
        ok_aud = settings.google_client_id in aud
    else:
        ok_aud = aud == settings.google_client_id
    if not ok_aud:
        raise OAuthError("身分資訊不是發給這個服務的")
    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp < time.time() - 60:          # 給 60 秒的時鐘誤差
        raise OAuthError("身分資訊已過期，請再試一次")
    got_nonce = str(claims.get("nonce") or "")
    if not got_nonce or not hmac.compare_digest(got_nonce, nonce):
        raise OAuthError("登入流程驗證失敗，請再試一次")

    email = str(claims.get("email") or "").strip()
    if not email or "@" not in email:
        raise OAuthError("這個 Google 帳號沒有 email，無法登入")
    # email_verified=false 代表 Google 自己都不確定這個信箱屬於他。
    # 放行的話，別人只要註冊一個宣稱是你信箱的帳號就能冒充你。
    if claims.get("email_verified") not in (True, "true"):
        raise OAuthError("這個 Google 帳號的 email 尚未驗證")

    domains = settings.allowed_domains
    if domains and email.rsplit("@", 1)[-1].lower() not in domains:
        raise OAuthError("這個網域的帳號不在允許名單內")

    return {"sub": str(claims.get("sub") or ""), "email": email,
            "name": claims.get("name") or "", "picture": claims.get("picture") or "",
            "hd": claims.get("hd") or ""}
