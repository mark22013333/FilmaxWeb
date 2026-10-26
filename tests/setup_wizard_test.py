"""設定精靈的後端：.env 寫入不能弄壞別的行、整批驗證、白名單、錯誤翻譯。

    python tests/setup_wizard_test.py
"""
import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_d = tempfile.mkdtemp()
os.environ["FILMAX_DATA_DIR"] = _d
os.environ["FILMAX_ENV_FILE"] = str(Path(_d) / ".env")

from dotenv import dotenv_values  # noqa: E402
from app import setup  # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


env = Path(_d) / ".env"

# --- write_env ---
env.write_text("# 註解\nFTP_HOST=1.1.1.1\nOTHER=keep\n", encoding="utf-8")
secret = r'p#ss "w" \ 密'
changed = setup.write_env({"FTP_HOST": "10.0.0.2", "FTP_PASSWORD": secret}, env)
text = env.read_text(encoding="utf-8")
vals = dotenv_values(env)
check("既有鍵原地更新", vals["FTP_HOST"] == "10.0.0.2" and text.count("FTP_HOST=") == 1, text)
check("註解與其他行不動", "# 註解" in text and vals["OTHER"] == "keep")
check("含 # 引號 反斜線的密碼讀得回原值", vals["FTP_PASSWORD"] == secret, vals["FTP_PASSWORD"])
check("回傳有改動的鍵", set(changed) == {"FTP_HOST", "FTP_PASSWORD"}, changed)
check("寫入前備份成 .env.bak", env.with_name(".env.bak").read_text(encoding="utf-8").startswith("# 註解"))
check("沒有變動就不寫", setup.write_env({"FTP_HOST": "10.0.0.2"}, env) == [])
try:
    setup.write_env({"FTP_PASSWORD": "a\nAUTH_ENABLED=false"}, env)
    check("值裡有換行要拒絕（防注入別的鍵）", False)
except ValueError:
    check("值裡有換行要拒絕（防注入別的鍵）", True)
check("CJK 路徑不用加引號", setup._quote("/電影|auto") == "/電影|auto")

# --- build_updates ---
good = {
    "source": {"protocol": "sftp", "host": "h", "user": "u", "password": "pw", "port": 22},
    "roots": [{"path": "/a", "kind": "tv"}, {"path": "b", "kind": "auto"}],
    "access": {"mode": "public", "adminPassword": "12345678", "viewerPassword": "",
               "publicBaseUrl": "https://v.example.com/"},
    "tmdbKey": "a" * 32,
}
upd, err = setup.build_updates(good)
check("合法表單沒有錯誤", not err, err)
check("路徑正規化成 /b", upd["LIBRARY_ROOTS"] == "/a|tv;/b|auto", upd.get("LIBRARY_ROOTS"))
check("https 對外網址自動開 COOKIE_SECURE", upd["COOKIE_SECURE"] == "true")
check("結尾斜線被去掉", upd["PUBLIC_BASE_URL"] == "https://v.example.com")
check("寫出的鍵都在白名單", set(upd) <= set(setup.WIZARD_KEYS), set(upd) - set(setup.WIZARD_KEYS))
check("sftp 時 FTP_TLS 一律 false",
      setup.build_updates({"source": {**good["source"], "tls": True}})[0]["FTP_TLS"] == "false")

bad = {
    "source": {"protocol": "ftp", "host": "ftp://x"},
    "roots": [{"path": "/a;b", "kind": "tv"}],
    "access": {"mode": "public", "adminPassword": "short", "viewerPassword": "short"},
    "tmdbKey": "eyJxxxx",
}
upd, err = setup.build_updates(bad)
check("任何一項錯就整批不寫", upd == {} and {"source", "roots", "adminPassword", "tmdbKey"} <= set(err), err)
_, err = setup.build_updates({"access": {"mode": "public", "adminPassword": "12345678",
                                          "viewerPassword": "12345678"}})
check("兩組密碼相同要擋", "viewerPassword" in err, err)
_, err = setup.build_updates({"source": {"protocol": "ftp", "host": "h", "password": "••••••••"}})
check("遮罩當密碼要擋", "source" in err, err)

# --- 錯誤翻譯 ---
src = setup.Source({"protocol": "ftp", "host": "h", "port": 21})
check("拒絕連線給出處方", setup.explain(ConnectionRefusedError(), src)["code"] == "refused")
r = setup.test_source({"protocol": "ftp", "host": "127.0.0.1", "port": 1})
check("連到沒人聽的埠 → refused 而非例外", not r["ok"] and r["code"] == "refused", r)
check("主機位址含協定 → 輸入錯誤", setup.test_source({"host": "ftp://x"})["code"] == "input")
check("TMDB 權杖格式有專屬提示", "權杖" in setup.test_tmdb("eyJ" + "a" * 100)["message"])

# --- 閘門：沒開驗證時只有本機能動 .env ---
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from app.config import settings  # noqa: E402


def gate(peer):
    req = Request({"type": "http", "client": (peer, 5555), "headers": [], "method": "GET", "path": "/"})
    try:
        setup_router.setup_gate(req)
        return "pass"
    except HTTPException as e:
        return e.status_code


settings.auth_enabled = False
check("沒開驗證 + 本機 → 放行", gate("127.0.0.1") == "pass", gate("127.0.0.1"))
check("沒開驗證 + 區網別台裝置 → 403", gate("192.168.1.50") == 403, gate("192.168.1.50"))

check("頁面與 API 共用同一個判斷（區網裝置被擋）", setup_router.deny_reason(Request(
    {"type": "http", "client": ("192.168.1.50", 1), "headers": [], "method": "GET", "path": "/"})) != "")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
