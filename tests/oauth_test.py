"""Google 登入的端對端測試。

執行方式（在專案根目錄）：python tests/oauth_test.py

會用一個暫存資料夾當資料庫，跑完就刪掉，不會動到 data/ 底下的東西。

不連 Google：把 oauth.exchange 換掉，自己組一個 id_token 回去。
id_token 的簽章本來就不驗（見 oauth.py 的說明），所以組一個 payload 就夠真實。
測的是我們自己那一段 —— state / nonce / aud / 狀態 / 權限。
"""
import base64, json, os, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 同一套測試跑兩種儲存後端：
#   python tests/oauth_test.py            → SQLite
#   python tests/oauth_test.py --mssql    → MSSQL 那條程式碼路徑
# --mssql 用 tests/fake_pyodbc.py 頂替驅動（沙箱連不到真的 SQL Server）。
# 它驗得到的是 SQL 欄位對齊、參數順序與時間轉換；驗不到定序與權限那類方言問題，
# 那些要用 app/dbcheck.py 在真的資料庫上跑。
USE_MSSQL = "--mssql" in sys.argv
# 全新的資料目錄，不要碰到你正在用的 data/library.db。
# FILMAX_DATA_DIR 必須在 import app 之前設好（config.py 在載入時就決定路徑了）。
TMP = tempfile.mkdtemp(prefix="filmax-oauth-test-")
if USE_MSSQL:
    os.environ.update({"MSSQL_HOST": "fake-server", "MSSQL_DATABASE": "filmax",
                       "MSSQL_USER": "filmax_app", "MSSQL_PASSWORD": "x"})
os.environ.update({
    "FILMAX_DATA_DIR": TMP,
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "true",
    "AUTH_PASSWORD": "", "VIEWER_PASSWORD": "",
    "AUTH_SECRET": "test-secret-do-not-use",
    "GOOGLE_CLIENT_ID": "cid.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "csecret",
    "PUBLIC_BASE_URL": "https://video.example.com",
    "GOOGLE_ADMIN_EMAILS": "boss@example.com",
    "GOOGLE_ALLOWED_DOMAINS": "", "GOOGLE_AUTO_APPROVE": "false",
    "TMDB_API_KEY": "", "AUTO_SCAN_ON_START": "false",
})
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tests"))
from starlette.testclient import TestClient
from app import auth, db, oauth, users
from app.config import settings
from app.main import app

if USE_MSSQL:
    import fake_pyodbc
    fake_pyodbc.install(os.path.join(TMP, "fake_mssql.db"))
print(f"儲存後端：{users.store_name()}")
assert users.store_name() == ("mssql" if USE_MSSQL else "sqlite")

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  {extra}")

def b64(o): return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

def fake_id_token(nonce, email="user@example.com", sub=None, aud=None,
                  iss="https://accounts.google.com", verified=True, exp=None, name="測試"):
    payload = {"iss": iss, "aud": aud or settings.google_client_id,
               "sub": sub or ("sub-" + email), "email": email,
               "email_verified": verified, "nonce": nonce, "name": name,
               "picture": "https://lh3.example/p.jpg",
               "exp": exp if exp is not None else int(time.time()) + 3600}
    return "hdr." + b64(payload) + ".sig"

_next_token = {}
def patched_exchange(code, verifier, redirect_uri):
    if code == "bad-code":
        raise oauth.OAuthError("Google 拒絕了這次登入，請再試一次")
    return {"id_token": _next_token["t"]}
oauth.exchange = patched_exchange

def flow_nonce(cookie_val):
    raw = oauth._b64d(cookie_val.split(".", 1)[0])
    return json.loads(raw.decode())

def login(c, **kw):
    """跑一次完整的 start → callback，回傳 callback 的回應。"""
    c.cookies.clear()
    r = c.get("/auth/google/start" + kw.pop("q", ""), follow_redirects=False)
    assert r.status_code == 303, r.status_code
    sc = r.cookies.get(oauth.STATE_COOKIE)
    p = flow_nonce(sc)
    nonce = kw.pop("nonce", p["n"])
    state = kw.pop("state", p["s"])
    _next_token["t"] = fake_id_token(nonce, **kw)
    c.cookies.set(oauth.STATE_COOKIE, sc, path="/auth/google")
    return c.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)

with TestClient(app, base_url="https://video.example.com") as c:
    print("\n[1] 登入頁")
    r = c.get("/login")
    check("登入頁有 Google 按鈕", "/auth/google/start" in r.text)
    check("沒有把 client_secret 洩到頁面上", "csecret" not in r.text)

    print("\n[2] 授權導向")
    c.cookies.clear()
    r = c.get("/auth/google/start", follow_redirects=False)
    loc = r.headers["location"]
    check("導向 Google", loc.startswith(oauth.AUTH_ENDPOINT))
    check("帶 PKCE S256", "code_challenge_method=S256" in loc)
    check("redirect_uri 用設定值", "video.example.com%2Fauth%2Fgoogle%2Fcallback" in loc)
    check("state cookie 只在 /auth/google 底下",
          'Path=/auth/google' in r.headers.get("set-cookie", ""))

    print("\n[3] 管理員第一次登入")
    r = login(c, email="boss@example.com")
    check("導回站內", r.status_code == 303 and r.headers["location"] == "/", r.status_code)
    check("拿到 session cookie", auth.COOKIE in r.cookies or auth.COOKIE in c.cookies)
    me = c.get("/api/me").json()
    check("角色是 admin", me["role"] == "admin", me)
    check("me 帶出 email", me["email"] == "boss@example.com", me)
    boss_cookies = dict(c.cookies)

    print("\n[4] 一般人：待審核")
    r = login(c, email="user@example.com")
    check("回 403", r.status_code == 403, r.status_code)
    check("顯示等待核准", "等待管理員核准" in r.text)
    check("沒有發 session cookie", auth.COOKIE not in r.cookies)
    check("待審核的人進不去", c.get("/api/library", follow_redirects=False).status_code == 401)

    print("\n[5] 管理員審核")
    c.cookies.clear(); c.cookies.update(boss_cookies)
    lst = c.get("/api/users").json()
    check("清單看得到兩個人", lst["counts"]["total"] == 2, lst["counts"])
    uid = [u for u in lst["items"] if u["email"] == "user@example.com"][0]["id"]
    check("註冊 IP 有記到", bool([u for u in lst["items"] if u["id"] == uid][0]["registered_ip"]))
    r = c.patch(f"/api/users/{uid}", json={"status": "approved"})
    check("核准成功", r.status_code == 200 and r.json()["status"] == "approved", r.text[:200])

    print("\n[6] 核准後可以登入")
    r = login(c, email="user@example.com")
    check("導回站內", r.status_code == 303, r.status_code)
    me = c.get("/api/me").json()
    check("角色是 viewer", me["role"] == "viewer", me)
    check("唯讀不能列帳號", c.get("/api/users").status_code == 403)
    check("唯讀不能下載", c.get("/api/stream/1/download", follow_redirects=False).status_code in (403, 404))
    user_cookies = dict(c.cookies)
    check("登入次數有累加", (users.by_id(uid) or {}).get("login_count") == 1)
    check("最近登入 IP 有記到", bool((users.by_id(uid) or {}).get("last_login_ip")))

    print("\n[7] 停權立刻生效（手上的 token 直接失效）")
    c.cookies.clear(); c.cookies.update(boss_cookies)
    c.patch(f"/api/users/{uid}", json={"status": "disabled"})
    c.cookies.clear(); c.cookies.update(user_cookies)
    check("舊 token 不能用", c.get("/api/library", follow_redirects=False).status_code == 401)
    r = login(c, email="user@example.com")
    check("重登也擋住", r.status_code == 403 and "無法登入" in r.text)

    print("\n[8] 升級為管理員後，舊的 viewer token 也要換掉")
    c.cookies.clear(); c.cookies.update(boss_cookies)
    c.patch(f"/api/users/{uid}", json={"status": "approved", "role": "owner"})
    c.cookies.clear(); c.cookies.update(user_cookies)
    check("角色變動讓舊 token 失效", c.get("/api/library", follow_redirects=False).status_code == 401)
    r = login(c, email="user@example.com")
    check("重登拿到 admin", c.get("/api/me").json()["role"] == "admin")

    print("\n[9] 最後一個管理員不能自廢武功")
    c.cookies.clear(); c.cookies.update(boss_cookies)
    c.patch(f"/api/users/{uid}", json={"role": "viewer"})     # 只剩 boss 一個 owner
    boss_id = [u for u in c.get("/api/users").json()["items"] if u["email"] == "boss@example.com"][0]["id"]
    check("不能停掉最後一個管理員",
          c.patch(f"/api/users/{boss_id}", json={"status": "disabled"}).status_code == 400)
    check("不能降級最後一個管理員",
          c.patch(f"/api/users/{boss_id}", json={"role": "viewer"}).status_code == 400)
    check("不能刪掉最後一個管理員", c.delete(f"/api/users/{boss_id}").status_code == 400)
    check("管理員還在", c.get("/api/me").json()["role"] == "admin")

    print("\n[10] 攻擊面")
    # state 不符
    c.cookies.clear()
    r0 = c.get("/auth/google/start", follow_redirects=False)
    sc = r0.cookies.get(oauth.STATE_COOKIE); p = flow_nonce(sc)
    _next_token["t"] = fake_id_token(p["n"], email="boss@example.com")
    c.cookies.set(oauth.STATE_COOKIE, sc, path="/auth/google")
    r = c.get("/auth/google/callback?code=abc&state=someone-elses", follow_redirects=False)
    check("state 不符 → 擋下", r.status_code == 400 and auth.COOKIE not in r.cookies)

    # 完全沒有 state cookie（別人塞給你的回呼網址）
    c.cookies.clear()
    r = c.get(f"/auth/google/callback?code=abc&state={p['s']}", follow_redirects=False)
    check("沒有 state cookie → 擋下", r.status_code == 400 and auth.COOKIE not in r.cookies)

    # 竄改 state cookie
    c.cookies.clear()
    r0 = c.get("/auth/google/start", follow_redirects=False)
    sc = r0.cookies.get(oauth.STATE_COOKIE); p = flow_nonce(sc)
    forged = oauth._b64e(json.dumps({"s": "x", "n": "x", "v": "x", "x": "",
                                     "e": int(time.time()) + 300}).encode()) + "." + sc.split(".")[1]
    _next_token["t"] = fake_id_token("x", email="boss@example.com")
    c.cookies.set(oauth.STATE_COOKIE, forged, path="/auth/google")
    r = c.get("/auth/google/callback?code=abc&state=x", follow_redirects=False)
    check("竄改 state cookie → 簽章對不起來", r.status_code == 400 and auth.COOKIE not in r.cookies)

    auth._fails.clear()
    for name, kw, why in [
        ("nonce 不符（重放別人的 id_token）", {"nonce": "not-the-one"}, None),
        ("aud 不是我們", {"aud": "other.apps.googleusercontent.com"}, None),
        ("iss 不是 Google", {"iss": "https://evil.example"}, None),
        ("id_token 已過期", {"exp": int(time.time()) - 7200}, None),
        ("email 未經驗證", {"verified": False}, None),
    ]:
        r = login(c, email="boss@example.com", **kw)
        check(name + " → 擋下", r.status_code == 400 and auth.COOKIE not in r.cookies, r.status_code)
        auth._fails.clear()

    # 網域白名單
    os.environ["GOOGLE_ALLOWED_DOMAINS"] = "example.com"   # 設定現在是即時解析的
    r = login(c, email="boss@example.com"); check("白名單內放行", r.status_code == 303)
    auth._fails.clear()
    r = login(c, email="outsider@other.com"); check("白名單外擋下", r.status_code == 400)
    os.environ.pop("GOOGLE_ALLOWED_DOMAINS", None)
    auth._fails.clear()

    # 開放轉址
    print("\n[11] 轉址目標")
    for raw, want in [("//evil.example/", "/"), ("/\\evil.example", "/"),
                      ("https://evil.example", "/"), ("/player?id=3", "/player?id=3")]:
        nb = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        r = login(c, q=f"?next={nb}", email="boss@example.com")
        check(f"next={raw!r} → {want!r}", r.headers.get("location") == want,
              r.headers.get("location"))

    print("\n[12] 亂七八糟的輸入不能變成 500")
    for path in ["/auth/google/callback",
                 "/auth/google/callback?code=abc",
                 "/auth/google/callback?state=abc",
                 "/auth/google/callback?error=access_denied&state=x",
                 "/auth/google/callback?code=" + "A" * 5000 + "&state=x",
                 "/auth/google/start?next=" + "%2F" * 900,
                 "/auth/google/start?next=<script>alert(1)</script>"]:
        auth._fails.clear()
        r = c.get(path, follow_redirects=False)
        check(f"{path[:52]:52s} → {r.status_code}", r.status_code < 500, r.status_code)
    check("start 不把 next 原樣塞進網址",
          "<script>" not in c.get("/auth/google/start?next=<script>alert(1)</script>",
                                  follow_redirects=False).headers.get("location", ""))

    print("\n[13] 稽核紀錄")
    c.cookies.clear(); c.cookies.update(boss_cookies)
    rows = c.get("/api/audit/logins").json()["items"]
    evs = {r["event"] for r in rows}
    check("有成功紀錄", "success" in evs, evs)
    check("有待審核紀錄", "pending" in evs, evs)
    check("有失敗紀錄", "failed" in evs, evs)
    check("紀錄帶 email", any(r.get("email") for r in rows))
    check("稽核不含 client_secret", "csecret" not in json.dumps(rows))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*50}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
