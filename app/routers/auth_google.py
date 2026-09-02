"""Google 登入的兩個端點：/auth/google/start 與 /auth/google/callback。

流程：
  start     產生 state / nonce / PKCE，把它們簽章後放進短效 cookie，導向 Google
  callback  驗 state → 用 code 換 id_token → 驗 claims → 建立或更新帳號
            → 已核准就發 session cookie，未核准就顯示「等待審核」

未核准的人**不會**拿到 session cookie。這點很重要：狀態檢查只做在發放的那一刻
是不夠的，所以 auth 那邊每次驗 token 也會再確認一次 status（見 _read_user_token）。
"""
from __future__ import annotations

import base64
import html
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import audit, auth, db, geo, mssql, oauth, users
from ..config import settings

log = logging.getLogger("filmax.oauth")
router = APIRouter()

# next 是從網址列來的，只收 base64url 的字元集
_NEXT_OK = re.compile(r"[A-Za-z0-9_\-=]{0,512}")


def _safe_next(raw: str) -> str:
    return raw if raw and _NEXT_OK.fullmatch(raw) else ""


def _dest(next_b64: str) -> str:
    """把 base64url 的 next 還原成站內路徑。只收單斜線開頭的相對路徑。

    //host 和 /\\host 瀏覽器都會當成外部網址，放行的話就是一個開放轉址漏洞：
    攻擊者寄一個看起來完全正常的自家網域連結，登入後把人送到釣魚站。
    """
    if not next_b64:
        return "/"
    try:
        cand = base64.urlsafe_b64decode(next_b64.encode() + b"==").decode()
    except Exception:
        return "/"
    if cand == "/" or re.fullmatch(r"/[^/\\\x00-\x1f][^\x00-\x1f]*", cand):
        return cand
    return "/"


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=zh-Hant><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} — FilmaxWeb</title>"
        "<style>body{font:15px/1.75 system-ui,-apple-system,'Noto Sans TC',sans-serif;"
        "background:#141414;color:#e8e8e8;display:grid;place-items:center;min-height:100vh;"
        "margin:0;padding:24px}.box{max-width:460px;background:#1d1d1d;border:1px solid #333;"
        "border-radius:16px;padding:32px 28px}h2{margin:0 0 14px;font-size:19px}"
        "p{margin:0 0 12px;color:#b9b9b9}a{color:#e5b23c}"
        "code{background:#2a2a2a;padding:2px 6px;border-radius:4px;font-size:13px}</style>"
        f"<div class=box><h2>{html.escape(title)}</h2>{body}"
        "<p style='margin-top:20px'><a href='/login'>回到登入頁</a></p></div>",
        status_code=status)


@router.get("/start", include_in_schema=False)
def start(request: Request, next: str = ""):
    if not settings.google_enabled:
        return _page("尚未啟用 Google 登入",
                     "<p>請在 <code>.env</code> 設定 <code>GOOGLE_CLIENT_ID</code> 與 "
                     "<code>GOOGLE_CLIENT_SECRET</code> 後重新啟動。</p>", 404)
    try:
        url, cookie_val, _ = oauth.new_flow(_safe_next(next))
    except oauth.OAuthError as e:
        return _page("無法開始登入", f"<p>{html.escape(str(e))}</p>", 500)

    resp = RedirectResponse(url, status_code=303)
    # path 收在 /auth/google：這個 cookie 只有回呼那一支需要，
    # 沒必要跟著每一個影片分段請求一起送出去。
    resp.set_cookie(oauth.STATE_COOKIE, cookie_val, max_age=oauth.STATE_TTL,
                    httponly=True, samesite="lax", path="/auth/google",
                    secure=settings.cookie_secure)
    return resp


@router.get("/callback", include_in_schema=False)
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    ip = auth.client_ip(request.scope)
    loc = geo.from_scope(request.scope)
    src = geo.ip_source(request.scope)

    def fail(msg: str, status: int = 400, event: str = "failed", email: str = ""):
        auth.note_failure(ip)
        audit.record(event, ip=ip, loc=loc, email=email or None,
                     detail=msg, ip_source=src)
        resp = _page("登入失敗", f"<p>{html.escape(msg)}</p>", status)
        resp.delete_cookie(oauth.STATE_COOKIE, path="/auth/google")
        return resp

    if not settings.google_enabled:
        return fail("伺服器沒有啟用 Google 登入", 404)
    if auth.too_many_failures(ip):
        return fail("嘗試次數過多，請等 15 分鐘後再試", 429, event="locked")
    if error:
        # 使用者在 Google 那邊按了取消，這不是錯誤，不要記成登入失敗
        return _page("已取消登入", "<p>你在 Google 的授權畫面選擇了取消。</p>", 200)
    if not code:
        return fail("回呼缺少必要參數")

    try:
        flow = oauth.read_flow(request.cookies.get(oauth.STATE_COOKIE), state)
        tokens = oauth.exchange(code, flow["v"], settings.redirect_uri())
        claims = oauth.claims_from(tokens, flow["n"])
    except oauth.OAuthError as e:
        return fail(str(e))
    except Exception:
        log.exception("Google 登入流程發生未預期的錯誤")
        return fail("登入過程發生錯誤，請再試一次", 500)

    email = claims["email"]
    try:
        # 判斷是不是新帳號要跟 upsert 用同一組條件（sub 優先、再 email），
        # 只看 email 的話，換過信箱的人會被誤記成「新註冊」。
        existed = (users.by_sub(claims.get("sub") or "") or users.by_email(email)) is not None
        rec = users.upsert_from_google(claims, ip, loc)
    except mssql.MssqlUnavailable as e:
        # 資料庫連不上跟「你沒有權限」是完全不同的兩件事，訊息要分開，
        # 不然使用者會一直重試一個永遠不會成功的登入。
        log.error("使用者資料庫不可用：%s", e)
        return fail("伺服器的使用者資料庫目前連不上，請聯絡管理員", 503, email=email)
    except Exception:
        log.exception("寫入帳號失敗")
        return fail("無法建立帳號，請聯絡管理員", 500, email=email)

    if not existed:
        # 註冊事件要在狀態檢查「之前」記 —— 新帳號預設是待審核，
        # 記在後面的話，最需要被看到的那一群（剛申請、還沒過）反而沒有註冊紀錄。
        audit.record("register", role=None, ip=ip, loc=loc, email=email,
                     user_id=rec["id"], detail=f"status={rec['status']}", ip_source=src)

    if rec["status"] != "approved":
        audit.record("pending", ip=ip, loc=loc, email=email, user_id=rec["id"],
                     detail=f"status={rec['status']}", ip_source=src)
        log.info("帳號 %s 狀態為 %s，未放行（來源 %s / %s）",
                 email, rec["status"], ip, geo.describe(loc))
        if rec["status"] == "pending":
            body = ("<p>已收到 <b>" + html.escape(email) + "</b> 的申請。</p>"
                    "<p>這個服務採審核制，管理員核准之後你就能直接用同一個 Google "
                    "帳號登入，不需要再申請一次。</p>")
            title = "等待管理員核准"
        else:
            body = ("<p><b>" + html.escape(email) + "</b> 目前無法登入"
                    "（狀態：" + html.escape(rec["status"]) + "）。</p>"
                    "<p>如果你認為這是誤判，請聯絡管理員。</p>")
            title = "這個帳號無法登入"
        resp = _page(title, body, 403)
        resp.delete_cookie(oauth.STATE_COOKIE, path="/auth/google")
        return resp

    users.record_login(rec["id"], ip, loc)
    auth.clear_failures(ip)
    role = users.role_to_auth(rec["role"])
    audit.record("success", role=role, ip=ip, loc=loc, email=email,
                 user_id=rec["id"], detail="google", ip_source=src)
    log.info("Google 登入成功：%s（%s），來源 %s（%s）",
             email, role, ip, geo.describe(loc))

    resp = RedirectResponse(_dest(flow.get("x") or ""), status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_user_token(rec["id"]),
                    max_age=settings.session_days * 86400,
                    httponly=True, samesite="lax", path="/",
                    secure=settings.cookie_secure)
    resp.delete_cookie(oauth.STATE_COOKIE, path="/auth/google")
    return resp
