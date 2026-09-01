"""FilmaxWeb — FTP 影片庫網頁串流服務。"""
from __future__ import annotations

from html import escape
import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, ftpclient, media, scanner
from .config import settings
from .routers import api, stream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("filmax")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    log.info("資料庫就緒")
    log.info("FTP 目標 %s:%s  根目錄 %s", settings.ftp_host, settings.ftp_port,
             [r.path for r in settings.library_roots])
    tools = media.tool_status()
    for kind, t in tools.items():
        if t["ok"]:
            log.info("%-8s → %s  (%s)", kind, t["resolved"], t["version"])
        else:
            log.error("=" * 78)
            log.error("%s", t["hint"])
            log.error("=" * 78)
    if not tools["ffprobe"]["ok"]:
        log.error("沒有 ffprobe 就無法判斷影片格式，所有檔案都會停在「未分析」而不能播放。")

    if settings.auth_enabled:
        if settings.auth_password:
            log.info("登入驗證：已啟用（session 有效 %s 天）", settings.session_days)
        else:
            log.error("=" * 78)
            log.error("AUTH_ENABLED=true 但沒有設 AUTH_PASSWORD —— 現在沒有人登得進來。")
            log.error("請在 .env 加上 AUTH_PASSWORD=你的密碼 後重新啟動。")
            log.error("=" * 78)
    else:
        log.warning("登入驗證未啟用：任何能連到這個位址的人都能看到你的整個媒體庫。"
                    "要對外開放前請先在 .env 設定 AUTH_ENABLED=true 與 AUTH_PASSWORD。")

    if settings.auto_scan_on_start:
        threading.Timer(2.0, lambda: scanner.start(full=False)).start()
    yield
    ftpclient.pool.close_all()


app = FastAPI(title="FilmaxWeb", version="1.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

class SecurityHeaders:
    """補上基本的安全標頭。

    用純 ASGI 而不是 BaseHTTPMiddleware：後者會把回應內容包起來，
    對 Range 串流和 HLS 分段這種大量輸出不划算。這裡只改標頭，不碰內容。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                have = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS:
                    if k.lower() not in have:
                        headers.append((k, v))
                if settings.cookie_secure and b"strict-transport-security" not in have:
                    headers.append((b"strict-transport-security",
                                    b"max-age=31536000; includeSubDomains"))
            await send(message)

        await self.app(scope, receive, _send)


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),                  # 不給別的網站 iframe 進去做點擊劫持
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
    # 頁面只從自己這裡載資源。幾個 blob: 是必要的：
    #   media-src  —— hls.js 把緩衝好的片段餵給 <video>
    #   worker-src —— hls.js 用 blob 建 Web Worker 做解多工。少了這條它會退回
    #                 主執行緒解，播得動但比較卡，而且主控台每次都噴一行錯誤。
    # 'unsafe-inline' 是因為現有頁面還有 inline 的 style 與 onclick，
    # 之後把那些清乾淨就可以拿掉。
    (b"content-security-policy",
     b"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
     b"script-src 'self' 'unsafe-inline'; worker-src 'self' blob:; "
     b"style-src 'self' 'unsafe-inline'; "
     b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
]

app.add_middleware(auth.AuthMiddleware)
app.add_middleware(SecurityHeaders)

app.include_router(api.router, prefix="/api", tags=["library"])
app.include_router(stream.router, prefix="/api", tags=["stream"])

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/player", include_in_schema=False)
def player():
    return FileResponse(STATIC_DIR / "player.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# ------------------------------- 登入 -------------------------------
def _login_page(error: str = "", nxt: str = "") -> HTMLResponse:
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    # next 是網址列來的，會被填進 HTML 屬性值。沒跳脫的話一個
    # ?next="><script>… 就能在登入頁上跑任意程式碼 —— 而登入頁不需要登入，
    # 等於把釣密碼的舞台直接架在自家網域上。
    # 順便把 next 限制成 base64url 的字元集，不合格式的直接丟掉。
    safe_next = nxt if re.fullmatch(r"[A-Za-z0-9_\-=]{0,512}", nxt or "") else ""
    html = (html.replace("{{ERROR}}", escape(error, quote=True))
                .replace("{{NEXT}}", escape(safe_next, quote=True)))
    return HTMLResponse(html, status_code=401 if error else 200)


@app.get("/login", include_in_schema=False)
def login_form(next: str = ""):
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=303)
    return _login_page(nxt=next)


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    # 不用 FastAPI 的 Form()：那會強制相依 python-multipart。
    # 登入表單是 application/x-www-form-urlencoded，自己解就好，少一個相依。
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size > 8192:
        return _login_page("表單內容異常", "")
    raw = await request.body()
    data = parse_qs(raw.decode("utf-8", "ignore"), keep_blank_values=True)
    password = (data.get("password") or [""])[0]
    next = (data.get("next") or [""])[0]

    ip = auth.client_ip(request.scope)
    if auth.too_many_failures(ip):
        return _login_page("嘗試次數過多，請等 15 分鐘後再試", next)
    if not settings.auth_password and not settings.viewer_password:
        return _login_page("伺服器還沒設定 AUTH_PASSWORD，請先在 .env 設好密碼", next)
    role = auth.role_for_password(password)
    if not role:
        auth.note_failure(ip)
        log.warning("登入失敗，來源 %s", ip)
        return _login_page("密碼不正確", next)

    auth.clear_failures(ip)
    dest = "/"
    if next:
        try:
            import base64
            cand = base64.urlsafe_b64decode(next.encode() + b"==").decode()
            # 只收「單斜線開頭的站內路徑」。//host 和 /\host 都會被瀏覽器
            # 當成外部網址，要一起擋掉；控制字元也不能進 Location 標頭。
            if re.fullmatch(r"/[^/\\\x00-\x1f][^\x00-\x1f]*", cand) or cand == "/":
                dest = cand
        except Exception:
            pass
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_token(role),
                    max_age=settings.session_days * 86400,
                    httponly=True, samesite="lax", path="/",
                    secure=settings.cookie_secure)
    log.info("登入成功（%s），來源 %s", role, ip)
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/session/revoke-all", include_in_schema=False)
def revoke_all(request: Request):
    """把所有裝置上的 session 一次作廢。

    密碼外洩時，光是改 .env 的密碼就已經會讓舊 token 失效（密碼指紋有納入
    簽章）；這個是不想改密碼、但要把所有人踢掉時用的。
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")
    n = auth.bump_session_epoch()
    log.warning("已作廢所有 session（第 %s 次），來源 %s", n, auth.client_ip(request.scope))
    resp = JSONResponse({"ok": True, "epoch": n})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp
