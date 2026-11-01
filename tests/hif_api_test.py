"""HIF 轉檔 API 的閘門（規格 S-1）：誰能呼叫、沒開開關時怎麼回。

    python tests/hif_api_test.py
"""
import os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-hifapi-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "true", "AUTH_PASSWORD": "adminpw12345", "VIEWER_PASSWORD": "viewerpw12345",
    "MSSQL_HOST": "", "TMDB_API_KEY": "", "AUTO_SCAN_ON_START": "false",
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1", "FTP_TIMEOUT": "2",
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


from app import auth, db, hifconvert, users   # noqa: E402
from app.config import settings                # noqa: E402
from app.main import app                       # noqa: E402
from starlette.testclient import TestClient    # noqa: E402

db.init_db()
users.init()
with db.tx() as c:
    c.execute("INSERT INTO photo(ftp_path, folder, filename, ext, size, probe_state, added_at, seen_at)"
              " VALUES('/p/A.HIF','/p','A.HIF','hif',100,'ok',1,1)")


def client(role):
    c = TestClient(app, client=("127.0.0.1", 1))
    c.cookies.set(auth.COOKIE, auth.make_token(role))
    return c


admin, viewer = client(auth.ADMIN), client(auth.VIEWER)
BODY = {"folder": "/p"}

print("[1] 非管理員")
check("info 403", viewer.get("/api/photos/convert/info?folder=/p").status_code == 403)
check("status 403", viewer.get("/api/photos/convert/status").status_code == 403)
check("probe 403", viewer.post("/api/photos/convert/probe", json=BODY).status_code == 403)
check("轉檔 403", viewer.post("/api/photos/convert", json=BODY).status_code == 403)
check("取消 403", viewer.post("/api/photos/convert/cancel").status_code == 403)
check("/api/me 沒有轉檔旗標", viewer.get("/api/me").json()["photo_convert"] is False)

print("\n[2] 管理員，但 .env 沒開 PHOTO_CONVERT_ENABLED")
check("設定預設是關的", settings.photo_convert_enabled is False)
i = admin.get("/api/photos/convert/info?folder=/p").json()
check("info 說沒開、不洩漏數量", i["enabled"] is False and i["hif_count"] == 0, i)
r = admin.post("/api/photos/convert", json=BODY)
check("轉檔被拒並說明怎麼開", r.status_code == 403 and "PHOTO_CONVERT_ENABLED" in r.text, (r.status_code, r.text))
check("probe 也被拒", admin.post("/api/photos/convert/probe", json=BODY).status_code == 403)
check("/api/me 旗標是 false", admin.get("/api/me").json()["photo_convert"] is False)

print("\n[3] 開了之後")
settings.photo_convert_enabled = True
check("/api/me 對管理員是 true、對 viewer 仍是 false",
      admin.get("/api/me").json()["photo_convert"] is True and viewer.get("/api/me").json()["photo_convert"] is False)
check("info 數得出 HIF", admin.get("/api/photos/convert/info?folder=/p").json()["hif_count"] == 1)
r = admin.post("/api/photos/convert", json={"folder": "/nothing"})
check("沒有 HIF 的資料夾 → 404", r.status_code == 404, r.text)
r = admin.post("/api/photos/convert", json={"folder": "/wrong", "photo_ids": [1]})
check("photo_ids 與 folder 對不上 → 404（不會轉到別的資料夾）", r.status_code == 404, r.text)
check("viewer 即使開了也不行", viewer.post("/api/photos/convert", json=BODY).status_code == 403)

print("\n[4] 來源連不上：記成失敗、不崩潰、原因看得懂")
r = admin.post("/api/photos/convert", json=BODY)
check("工作建立", r.status_code == 200 and r.json()["total"] == 1, r.text)
for _ in range(100):
    s = admin.get("/api/photos/convert/status").json()
    if not s["running"]:
        break
    time.sleep(0.1)
check("結束後 failed=1", s["job"]["counts"]["failed"] == 1 and s["job"]["state"] == "done", s)
check("失敗原因有寫", bool(s["job"]["items"][0]["reason"]), s["job"]["items"])
check("相片庫那一列沒被動", db.q1("SELECT COUNT(*) c FROM photo WHERE filename='A.HIF'")["c"] == 1)

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
