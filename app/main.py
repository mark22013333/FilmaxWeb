"""FilmaxWeb — FTP 影片庫網頁串流服務。"""
from __future__ import annotations

from html import escape
import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (audit, auth, db, ftpclient, geo, hls, media, mssql, params,
               paramstore, purge, scanner, users)
from .config import settings
from .routers import api, auth_google, stream

# 日期一定要在時間旁邊。服務會連著開好幾天，而問題往往是隔天才回報的 ——
# 只有 `21:06:39` 的話，沒有人分得出那是今天還是三天前的那一次啟動。
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
log = logging.getLogger("filmax")

STATIC_DIR = Path(__file__).parent / "static"


def no_admin_lines(listed) -> list:
    """「還沒有管理員帳號」該說什麼。

    **「沒設名單」與「設了但還沒登入」是兩件事。**`GOOGLE_ADMIN_EMAILS` 只是
    一份名單，它不會建立帳號 —— 帳號是在那個人**第一次用該 Gmail 登入**時才
    建立並提升為管理員（見 `users.upsert_from_google`）。

    原本兩種情況共用同一句「請在 .env 設 GOOGLE_ADMIN_EMAILS」，
    於是已經設好的人會以為自己設錯了，回頭去改一個本來就對的設定 ——
    **一個把人導向錯誤方向的提示，比沒有提示更糟。**
    """
    out = ["目前還沒有任何管理員帳號，新註冊的人會全部卡在「待審核」而沒有人能核准。"]
    listed = list(listed or [])
    if listed:
        out.append(f"GOOGLE_ADMIN_EMAILS 已經設了 {len(listed)} 個帳號："
                   + "、".join(_mask_email(e) for e in listed))
        out.append("設定本身沒問題，只是還沒有人用名單內的帳號登入過 ——")
        out.append("用其中一個 Gmail 登入一次，那個帳號就會自動成為管理員。")
    else:
        out.append("請在 .env 設 GOOGLE_ADMIN_EMAILS=你的Gmail 後重新啟動，再用該帳號登入一次。")
    return out


def _mask_email(addr: str) -> str:
    """日誌裡的信箱只留頭尾。日誌會被貼進聊天室、issue、螢幕截圖裡。"""
    addr = (addr or "").strip()
    if "@" not in addr:
        return addr
    name, _, domain = addr.partition("@")
    keep = name[:2] if len(name) > 3 else name[:1]
    return f"{keep}{'*' * max(1, len(name) - len(keep))}@{domain}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings.check_stores()
    except ValueError as e:
        log.error("=" * 78)
        log.error("儲存後端設定有問題：%s", e)
        log.error("=" * 78)
        raise

    # .env 裡的值無效就讓啟動失敗。理由：.env 是使用者明確寫下的指令，
    # 寫錯了還默默用預設值跑起來，使用者會以為設定生效了。
    # （資料庫裡的壞值不一樣 —— 那退回預設就好，不該讓服務開不了機。）
    # 秘密的日誌過濾要盡早掛上 —— 掛得比第一行可能洩漏的日誌晚就沒意義了
    params.install_log_filter()

    bad = paramstore.validate_env()
    if bad:
        log.error("=" * 78)
        log.error(".env 裡有無效的設定值，服務不會啟動：")
        for b in bad:
            log.error("  • %s", b)
        log.error("=" * 78)
        raise ValueError("；".join(bad))

    # 打錯字的設定鍵。TMDB_API_KAY=xxx 這種現在是完全靜默失效的：
    # 程式讀不到那個鍵，使用者以為設好了，而沒有任何一行訊息提到它。
    for key, guess in params.unknown_env_keys():
        if guess:
            log.warning(".env 有一個程式不認得的設定 %s —— 是不是想打 %s？", key, guess)
        else:
            log.warning(".env 有一個程式不認得的設定：%s", key)

    db.init_db()
    log.info("媒體庫資料庫就緒（%s）", settings.media_store)

    # profile key 換過形狀（加了階別維度）→ 舊分段的資料夾名對不上了，清一次。
    # 放在這裡而不是 db._migrate()：刪快取不是資料層的事，而且要看得到訊息。
    try:
        hls.migrate_cache()
    except Exception as e:
        log.warning("HLS 快取版本檢查失敗（不影響啟動）：%s", e)

    # 被環境變數蓋掉的後台設定。**絕不自動刪** —— 使用者可能只是暫時用
    # 環境變數覆蓋（測試、容器編排），刪掉他們在後台設過的東西是不可逆的。
    try:
        d = paramstore.drift()
        if d:
            log.warning("有 %s 項後台設定被環境變數蓋掉（後台會顯示，不會自動刪）：%s",
                        len(d), "、".join(x["key"] for x in d))
    except Exception as e:
        log.debug("漂移檢查失敗：%s", e)

    # 啟動時只**回報**孤兒，刻意不修。
    # 啟動路徑自動刪資料是很糟的預設行為，尤其在剛還原備份、剛換過儲存後端
    # 之後 —— 那時候「看起來像孤兒」的東西特別多，而它們多半只是還沒對上。
    # 要清的話有掃描後的自動清掃，以及後台的按鈕（可以先 dry-run 看清單）。
    try:
        r = purge.sweep("report")
        if r["total"]:
            log.warning("發現 %s 筆孤兒資料：%s", r["total"],
                        "、".join(f"{k}×{v}" for k, v in r["found"].items() if v))
            log.warning("（啟動時不會自動清理。掃描結束後會清，或用後台的清掃功能。）")
    except Exception as e:
        log.warning("孤兒檢查失敗：%s", e)

    # 使用者資料可能在 MSSQL。連不上就大聲講，而且要講「怎麼修」——
    # 這裡不 raise：服務照樣起來，/healthz 與這段日誌才看得到，
    # 直接讓 uvicorn 起不來的話，使用者只會看到一個沒頭沒尾的堆疊。
    try:
        users.init()
        log.info("使用者資料：%s", users.describe())
    except Exception as e:
        log.error("=" * 78)
        log.error("使用者資料庫不可用：%s", e)
        log.error("")
        log.error("在這個狀態下沒有人登得進來（Google 登入會顯示錯誤訊息）。")
        log.error("請依序確認：")
        log.error("  1. 在專案目錄跑 db\\檢查連線.bat，它會逐項告訴你卡在哪一關")
        log.error("  2. 表還沒建的話，先跑 db\\migrate.bat 套用 Flyway 遷移")
        log.error("  3. 不想接 MSSQL 就把 .env 的 MSSQL_HOST 清空，會改用本機 SQLite")
        log.error("=" * 78)
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
        ways = []
        if settings.auth_password:
            ways.append("密碼")
        if settings.google_enabled:
            ways.append("Google 帳號")
        if ways:
            log.info("登入方式：%s（session 有效 %s 天）", " / ".join(ways), settings.session_days)
        else:
            log.error("=" * 78)
            log.error("AUTH_ENABLED=true 但沒有設定任何登入方式 —— 現在沒有人登得進來。")
            log.error("請在 .env 加上 AUTH_PASSWORD=你的密碼，")
            log.error("或設定 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 後重新啟動。")
            log.error("=" * 78)

    if settings.google_enabled:
        uri = settings.redirect_uri()
        if not uri:
            log.error("=" * 78)
            log.error("設了 Google 登入卻沒有 PUBLIC_BASE_URL 或 GOOGLE_REDIRECT_URI，")
            log.error("無法組出回呼網址。請在 .env 設 PUBLIC_BASE_URL=https://你的網域")
            log.error("=" * 78)
        else:
            log.info("Google 登入回呼網址：%s（要跟 Google Cloud Console 登記的完全一致）", uri)
        if not users.has_owner():
            # **「沒設名單」與「設了但還沒登入」是兩件事。**
            # GOOGLE_ADMIN_EMAILS 只是一份名單，它不會建立帳號 ——
            # 帳號是在那個人**第一次用該 Gmail 登入**時才建立並提升為管理員
            # （見 users.upsert_from_google）。原本兩種情況共用同一句
            # 「請在 .env 設 GOOGLE_ADMIN_EMAILS」，於是已經設好的人會以為
            # 自己設錯了，然後回頭改一個本來就對的設定。
            log.warning("=" * 78)
            for line in no_admin_lines(settings.admin_emails):
                log.warning("%s", line)
            log.warning("=" * 78)
    else:
        log.warning("=" * 78)
        log.warning("AUTH_ENABLED=false —— 沒有登入驗證。")
        log.warning("區網內的人可以看到整個媒體庫，並且擁有管理員權限")
        log.warning("（下載原始檔、瀏覽 FTP、觸發掃描、看診斷資訊）。")
        log.warning("")
        log.warning("外部連線會被直接擋掉並回 403，避免接上 Tunnel 或反向代理時整個對外公開。")
        log.warning("要對外開放請在 .env 設定 AUTH_ENABLED=true 與 AUTH_PASSWORD 後重新啟動。")
        log.warning("=" * 78)

    if settings.mssql_configured:
        st = mssql.ping()
        if st["ok"]:
            log.info("MSSQL 連線正常：%s", st["version"] or mssql.safe_target())
        elif st["missing"]:
            log.error("MSSQL 連得上但缺少：%s —— 請跑 db\\migrate.bat",
                      "、".join(st["missing"]))

    if settings.auto_scan_on_start:
        threading.Timer(2.0, lambda: scanner.start(full=False)).start()
    yield
    ftpclient.pool.close_all()
    mssql.close_all()


# docs_url / openapi_url 設成 None，改用下面自己掛的管理員限定版本。
# FastAPI 內建那兩條只受登入保護、不分角色，唯讀使用者看得到完整 API 清單。
app = FastAPI(title="FilmaxWeb", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

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
     b"default-src 'self'; "
     # 大頭貼在 Google 的 CDN、TMDB 的搜尋結果縮圖在 image.tmdb.org
     # （「手動指定 TMDB」那個畫面直接引用它，少了這一條整排都是破圖）。
     # 只開這兩個網域，其他外部圖片一律擋掉。
     b"img-src 'self' data: blob: https://lh3.googleusercontent.com "
     b"https://image.tmdb.org; "
     b"media-src 'self' blob:; "
     b"script-src 'self' 'unsafe-inline'; worker-src 'self' blob:; "
     b"style-src 'self' 'unsafe-inline'; "
     b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
]

app.add_middleware(auth.AuthMiddleware)
app.add_middleware(SecurityHeaders)

app.include_router(auth_google.router, prefix="/auth/google", tags=["auth"])
app.include_router(api.router, prefix="/api", tags=["library"])
app.include_router(stream.router, prefix="/api", tags=["stream"])

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# HTML 進入點一律不准快取。
#
# **這是「改了程式卻看不到效果」的根因。**`/static/*` 走 StaticFiles，它會發
# ETag 與 Last-Modified，所以 app.js／style.css 每次都會回伺服器問一下（304
# 很便宜）。但 index.html 這幾支是 FileResponse 直接吐檔案，瀏覽器有可能整份
# 快取起來 —— 那就連「要載哪些 script」都是舊的，新加的 DOM 元素當然不存在。
# 實際發生過：切換鈕與哨兵元素都在伺服器端了，使用者的畫面上卻沒有。
#
# 這幾支是小檔案而且一天不會被要幾次，no-store 的成本可以忽略；
# 而它換到的是「重新整理就一定看得到最新版」。
_NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers=_NO_CACHE)


@app.get("/", include_in_schema=False)
def index():
    return _page("index.html")


@app.get("/player", include_in_schema=False)
def player():
    return _page("player.html")


@app.get("/reader", include_in_schema=False)
def reader():
    """PDF 閱讀器。實際要讀哪一份由網址的 ?doc= 決定，權限在 API 那一層擋。"""
    return _page("reader.html")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    """管理後台。

    **這裡要自己檢查角色。** 中介層只驗「有沒有登入」，不驗「是不是管理員」——
    掛在中介層上的保護對這個頁面來說是不夠的。

    頁面本身（admin.html / admin.js）不含任何敏感資訊，真正的保護在每一個
    API 端點的 admin_only 上；這一關是為了不要讓唯讀使用者看到一個
    到處都是錯誤訊息的空後台。
    """
    if not auth.is_admin(request):
        if not auth.role_of(request):
            return RedirectResponse("/login?next=" + quote("/admin"), status_code=303)
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>需要管理員權限</title>"
            "<style>body{font:15px/1.7 system-ui,sans-serif;max-width:520px;"
            "margin:15vh auto;padding:0 24px;color:#222}</style>"
            "<h2>需要管理員權限</h2><p>你的帳號是唯讀角色，看不到管理後台。</p>"
            "<p><a href=\"/\">回到媒體庫</a></p>", status_code=403)
    return _page("admin.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/api/openapi.json", include_in_schema=False)
def openapi_json(request: Request):
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")
    return app.openapi()


@app.get("/api/docs", include_in_schema=False)
def api_docs(request: Request):
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="FilmaxWeb API")


# ------------------------------- 登入 -------------------------------
def _record_login(event: str, role, ip: str, loc: dict,
                  scope=None) -> None:
    audit.record(event, role=role, ip=ip, loc=loc,
                 ip_source=geo.ip_source(scope) if scope is not None else None)


def _login_page(error: str = "", nxt: str = "") -> HTMLResponse:
    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    # next 是網址列來的，會被填進 HTML 屬性值。沒跳脫的話一個
    # ?next="><script>… 就能在登入頁上跑任意程式碼 —— 而登入頁不需要登入，
    # 等於把釣密碼的舞台直接架在自家網域上。
    # 順便把 next 限制成 base64url 的字元集，不合格式的直接丟掉。
    safe_next = nxt if re.fullmatch(r"[A-Za-z0-9_\-=]{0,512}", nxt or "") else ""
    google = ""
    if settings.google_enabled:
        href = "/auth/google/start" + (f"?next={escape(safe_next, quote=True)}" if safe_next else "")
        google = (f'<a class="gbtn" href="{href}">'
                  '<svg viewBox="0 0 18 18" width="17" height="17" aria-hidden="true">'
                  '<path fill="#4285F4" d="M17.6 9.2c0-.6-.1-1.3-.2-1.8H9v3.5h4.8a4.1 4.1 0 0 1-1.8 2.7v2.2h2.9c1.7-1.6 2.7-3.9 2.7-6.6z"/>'
                  '<path fill="#34A853" d="M9 18c2.4 0 4.5-.8 6-2.2l-2.9-2.2c-.8.5-1.8.9-3.1.9-2.4 0-4.4-1.6-5.1-3.8H.9v2.3A9 9 0 0 0 9 18z"/>'
                  '<path fill="#FBBC05" d="M3.9 10.7a5.4 5.4 0 0 1 0-3.4V5H.9a9 9 0 0 0 0 8l3-2.3z"/>'
                  '<path fill="#EA4335" d="M9 3.6c1.3 0 2.5.5 3.4 1.3l2.6-2.6A9 9 0 0 0 .9 5l3 2.3C4.6 5.2 6.6 3.6 9 3.6z"/>'
                  '</svg>使用 Google 帳號登入</a>')
        if not (settings.auth_password or settings.viewer_password):
            # 沒設任何密碼時，密碼欄位只會讓人白填一次，直接收起來
            google += '<style>.pwpart{display:none}</style>'
    html = (html.replace("{{ERROR}}", escape(error, quote=True))
                .replace("{{NEXT}}", escape(safe_next, quote=True))
                .replace("{{GOOGLE}}", google))
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
    loc = geo.from_scope(request.scope)
    if auth.too_many_failures(ip):
        _record_login("locked", None, ip, loc, request.scope)
        return _login_page("嘗試次數過多，請等 15 分鐘後再試", next)
    if not settings.auth_password and not settings.viewer_password:
        msg = ("這個服務只開放 Google 帳號登入" if settings.google_enabled
               else "伺服器還沒設定 AUTH_PASSWORD，請先在 .env 設好密碼")
        return _login_page(msg, next)
    role = auth.role_for_password(password)
    if not role:
        auth.note_failure(ip)
        _record_login("failed", None, ip, loc, request.scope)
        log.warning("登入失敗，來源 %s（%s）", ip, geo.describe(loc))
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
    _record_login("success", role, ip, loc, request.scope)
    log.info("登入成功（%s），來源 %s（%s）", role, ip, geo.describe(loc))
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
