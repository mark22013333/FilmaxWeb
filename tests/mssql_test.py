"""MSSQL 後端的針對性測試。

oauth_test.py --mssql 已經把整個登入流程在 MSSQL 那條路上跑過一遍；
這裡補的是那套流程「看不出對錯」的幾件事：時區、欄位長度、TOP 的參數順序、
連不上資料庫時的行為、密碼會不會漏進日誌。

一樣不需要真的 SQL Server（見 tests/fake_pyodbc.py 的說明）。
"""
import os, shutil, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-mssql-test-")
os.environ.update({
    "FILMAX_DATA_DIR": TMP,
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "MSSQL_HOST": "fake-server", "MSSQL_DATABASE": "filmax",
    "MSSQL_USER": "filmax_app", "MSSQL_PASSWORD": "sup3r-s3cret-pw",
    "AUTH_ENABLED": "true", "AUTH_SECRET": "t", "TMDB_API_KEY": "",
    "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "",
})
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

import fake_pyodbc
from app import mssql, users, users_mssql
from app.config import settings

fake_pyodbc.install(os.path.join(TMP, "m.db"))
users.init()

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")

LOC = {"country": "TW", "city": "Taipei", "user_agent": "UA/1.0"}

print("\n[1] 時區：DATETIME2 存的是 UTC，讀回來不能差 8 小時")
uid = users_mssql.create("a@example.com", "sub-a", "A", None, "viewer", "approved",
                         "1.2.3.4", LOC, "system")
before = time.time()
users_mssql.record_login(uid, "1.2.3.4", LOC)
rec = users_mssql.by_id(uid)
drift = abs(rec["last_login_at"] - before)
check(f"last_login_at 與現在相差 {drift:.1f} 秒", drift < 60, f"{drift:.0f} 秒（8 小時 = 28800）")
check("created_at 也是 epoch float", isinstance(rec["created_at"], float), type(rec["created_at"]))
# 直接驗轉換函式本身：帶時區與不帶時區都要當成 UTC
naive = datetime(2026, 1, 2, 3, 4, 5)
check("to_epoch 把無時區當成 UTC",
      mssql.to_epoch(naive) == naive.replace(tzinfo=timezone.utc).timestamp())
check("from_epoch 來回一致",
      abs(mssql.to_epoch(mssql.from_epoch(1788256211.0)) - 1788256211.0) < 1)

print("\n[2] 欄位長度：超過 dbo.filmax_users 的定義要先截掉")
claims = {"sub": "s" * 200, "email": "b@example.com", "name": "N" * 500,
          "picture": "https://x/" + "p" * 900}
rec = users.upsert_from_google(claims, "9" * 200, {"country": "TAIWAN", "city": "C" * 400,
                                                   "user_agent": "U" * 900})
check(f"google_sub ≤64（實得 {len(rec['google_sub'])}）", len(rec["google_sub"]) <= 64)
check(f"display_name ≤200（實得 {len(rec['display_name'])}）", len(rec["display_name"]) <= 200)
check(f"picture_url ≤500（實得 {len(rec['picture_url'])}）", len(rec["picture_url"]) <= 500)
check(f"registered_ip ≤64（實得 {len(rec['registered_ip'])}）", len(rec["registered_ip"]) <= 64)
# 不合格式的國碼要留空，不是截成兩個字母 —— "TAIWAN" 截成 "TA" 會變成
# 一個看起來合法卻不存在的國碼，統計從此被汙染而且查不出來
check(f"不合法的國碼留空而不是截斷（實得 {rec['registered_country']!r}）",
      rec["registered_country"] is None)
check("合法國碼照樣存得進去",
      users.upsert_from_google({"sub": "s-tw", "email": "tw@example.com", "name": "T"},
                               "1.1.1.1", {"country": "tw"})["registered_country"] == "TW")
users.record_login(rec["id"], "1.2.3.4", {"country": "TW", "city": "x", "user_agent": "U" * 900})
check("last_login_ua ≤400", len(users.by_id(rec["id"])["last_login_ua"]) <= 400)

print("\n[3] CHAR(2) 讀回來的補白要清掉")
uid3 = users_mssql.create("c@example.com", "sub-c", "C", None, "viewer", "pending",
                          "1.1.1.1", {"country": "T ", "city": None}, None)
check("country 沒有殘留空白", users_mssql.by_id(uid3)["registered_country"] == "T")

print("\n[4] TOP (?) 的參數順序")
for i in range(6):
    users_mssql.create(f"bulk{i}@example.com", f"sub-b{i}", f"B{i}", None, "viewer",
                       "approved", "1.1.1.1", LOC, "system")
check("limit=3 真的只回 3 筆", len(users_mssql.listing(None, 3)) == 3,
      len(users_mssql.listing(None, 3)))
check("帶 status 時參數沒有錯位",
      all(u["status"] == "approved" for u in users_mssql.listing("approved", 50)))
check("待審核的排在最前面", users_mssql.listing(None, 50)[0]["status"] == "pending",
      users_mssql.listing(None, 50)[0]["status"])
check("counts 總數對得起來", users.counts()["total"] == len(users_mssql.listing(None, 999)))

print("\n[5] 連不上資料庫時不能變成 500")
saved = settings.mssql_host
settings.mssql_host = "unreachable"          # fake driver 看到就丟連線錯誤
mssql.close_all(); users.invalidate()
check("session_state 回 None 而不是丟例外", users.session_state(uid) is None)
try:
    users.listing()
    check("listing 連不上時會丟 MssqlUnavailable", False, "竟然成功了")
except mssql.MssqlUnavailable as e:
    check("listing 連不上時丟 MssqlUnavailable", True)
except Exception as e:
    check("listing 連不上時丟 MssqlUnavailable", False, repr(e))
settings.mssql_host = saved
mssql.close_all(); users.invalidate()
check("恢復後又讀得到", users.by_id(uid) is not None)

print("\n[6] 密碼絕對不能出現在任何訊息裡")
PW = "sup3r-s3cret-pw"
check("conn_str 裡確實有密碼（前提成立）", PW in mssql.conn_str())
check("scrub 洗掉連線字串裡的密碼", PW not in mssql.scrub(mssql.conn_str()))
check("safe_target 不含密碼", PW not in mssql.safe_target())
import json as _j
check("ping 的輸出不含密碼", PW not in _j.dumps(mssql.ping(), default=str))
settings.mssql_host = "unreachable"
mssql.close_all()
err = mssql.ping().get("error", "")
check("連線失敗訊息不含密碼", PW not in err, err[:80])
settings.mssql_host = saved
mssql.close_all()

print("\n[7] ping 要講清楚缺什麼")
st = mssql.ping()
check("表齊全時 ok=True", st["ok"] and not st["missing"], st)
import sqlite3
c = sqlite3.connect(os.path.join(TMP, "m.db"))
c.execute("DROP TABLE filmax_audit"); c.commit(); c.close()
# ============================================================ A-2 批次寫入
print("\n[A-2] 批次寫入與交易")

mssql.execute("DELETE FROM dbo.filmax_users WHERE email LIKE 'batch%'")

def n_batch():
    return mssql.scalar("SELECT COUNT(*) FROM dbo.filmax_users WHERE email LIKE 'batch%'")

SQL = ("INSERT INTO dbo.filmax_users(email, display_name, role, status, created_at, note)"
       " VALUES(?,?,?,?,?,?)")
now_dt = mssql.now_dt()
def rows(n, name="n"):
    return [(f"batch{i}@x.com", name, "viewer", "approved", now_dt, "")
            for i in range(n)]

# 沒給 sizes 就不能開 fast_executemany —— 那是會默默截斷字串的組合
try:
    with mssql.tx() as c:
        cur = c.cursor()
        cur.fast_executemany = True
        cur.executemany(SQL, rows(2))
    guarded = False
except Exception as e:
    guarded = "setinputsizes" in str(e)
check("fast_executemany 沒給欄寬就報錯（真的 pyodbc 會默默截斷）", guarded)

# 欄寬照 dbo.filmax_users 的 schema，不讓 pyodbc 自己猜
SIZES = [(0, 255, 0), (0, 120, 0), (0, 20, 0), (0, 20, 0), None, (0, 400, 0)]
n = mssql.executemany(SQL, rows(50), sizes=SIZES)
check(f"50 列一次寫入（回報 {n}）", n == 50 and n_batch() == 50, (n, n_batch()))

# 欄寬超過要被擋，不是默默截斷
mssql.execute("DELETE FROM dbo.filmax_users WHERE email LIKE 'batch%'")
try:
    mssql.executemany(SQL, rows(3, "很長" * 200), sizes=SIZES)
    truncated = True
except Exception:
    truncated = False
check("超過欄寬的字串被擋下來，不是默默截斷", not truncated)
check("被擋下來的整批都沒寫進去（不是寫一半）", n_batch() == 0, n_batch())

# 對帳：宣告的筆數跟資料庫實際筆數不符要整批退回
try:
    mssql.executemany(SQL, rows(10), sizes=SIZES,
                      verify="SELECT COUNT(*) FROM dbo.filmax_users WHERE email LIKE 'nomatch%'")
    verified = False
except RuntimeError as e:
    verified = "對帳不符" in str(e)
check("對帳對不上就整批退回（fast_executemany 不會為被擋的列拋例外）", verified)
check("退回之後資料庫是乾淨的", n_batch() == 0, n_batch())

# tx() 出例外要 rollback
try:
    with mssql.tx() as c:
        c.cursor().execute(SQL, ("batch_tx@x.com", "n", "viewer", "approved", now_dt, ""))
        raise RuntimeError("中途爆了")
except RuntimeError:
    pass
check("tx 出例外會 rollback", n_batch() == 0, n_batch())
check("tx 結束後 autocommit 有還原", mssql.get_conn().autocommit is True)

with mssql.tx() as c:
    c.cursor().execute(SQL, ("batch_ok@x.com", "n", "viewer", "approved", now_dt, ""))
check("tx 正常結束會 commit", n_batch() == 1, n_batch())
mssql.execute("DELETE FROM dbo.filmax_users WHERE email LIKE 'batch%'")

fake_pyodbc._TABLES.discard("filmax_audit")
mssql.close_all()
st = mssql.ping()
check("少了 filmax_audit 會被指出來", "filmax_audit" in st["missing"], st["missing"])
fake_pyodbc._COLUMNS["dbo.filmax_users"].remove("sess_ver")
mssql.close_all()
check("少了 sess_ver 欄位也會被指出來",
      "filmax_users.sess_ver" in mssql.ping()["missing"], mssql.ping()["missing"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*50}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
