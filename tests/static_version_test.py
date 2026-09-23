"""HTML 引用的 /static/ 網址要帶版本號。

    python tests/static_version_test.py

實際踩到的：服務重啟、伺服器上已經是新版 admin.js，使用者看到的還是舊畫面。
原因是對外走 Cloudflare 時它會替 /static/* 補 `max-age=14400`，瀏覽器四小時內
不回伺服器問。HTML 本身是 no-store，所以由 HTML 在網址上帶 ?v=<修改時間>，
檔案一改網址就變 —— 這支測試盯的就是「檔案改了，網址真的會變」。
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-sv-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


from app import db, main                       # noqa: E402
from starlette.testclient import TestClient    # noqa: E402

db.init_db()
cl = TestClient(main.app, client=("127.0.0.1", 1))
REF = re.compile(r'(?:src|href)="(/static/[^"]+)"')

print("\n[1] 每個頁面的 /static/ 引用都帶版本號")
for path in ("/", "/player", "/reader", "/admin"):
    r = cl.get(path, follow_redirects=False)
    refs = REF.findall(r.text)
    check(f"{path} 有引用 /static/（{len(refs)} 個）", len(refs) > 0, r.status_code)
    check(f"{path} 全部帶 ?v=", refs and all("?v=" in u for u in refs),
          [u for u in refs if "?v=" not in u])
    check(f"{path} 本身仍然是 no-store",
          "no-store" in r.headers.get("cache-control", ""), dict(r.headers))
# 測試環境關了認證，/login 會直接轉走，所以直接呼叫產生登入頁的那個函式
lg = REF.findall(main._login_page().body.decode("utf-8"))
check(f"登入頁的引用也帶版本號（{len(lg)} 個）", lg and all("?v=" in u for u in lg), lg)

print("\n[2] 帶版本號的網址打得開")
refs = REF.findall(cl.get("/admin").text)
for u in refs:
    check(f"{u} → 200", cl.get(u).status_code == 200)

print("\n[3] 檔案一改，網址就變")
js = main.STATIC_DIR / "admin.js"
st = js.stat()
v1 = next(u for u in REF.findall(cl.get("/admin").text) if "admin.js" in u)
try:
    os.utime(js, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    v2 = next(u for u in REF.findall(cl.get("/admin").text) if "admin.js" in u)
    check("admin.js 修改時間變了 → 網址跟著變", v1 != v2, (v1, v2))
finally:
    os.utime(js, ns=(st.st_atime_ns, st.st_mtime_ns))
v3 = next(u for u in REF.findall(cl.get("/admin").text) if "admin.js" in u)
check("沒改就是同一個網址（快取才吃得到）", v1 == v3, (v1, v3))

print("\n[4] 不存在的檔案原樣留著、不讓頁面壞掉")
out = main._versioned('<script src="/static/no-such-file.js"></script>')
check("找不到的檔案不加版本號也不丟錯", out == '<script src="/static/no-such-file.js"></script>', out)

print(f"\n{'=' * 52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
