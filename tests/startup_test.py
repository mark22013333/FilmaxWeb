"""啟動時的日誌與提示，以及時間點欄位的整數化。

    python tests/startup_test.py

規格：A-3（時間點一律 epoch 整數秒）與 B 章的啟動診斷訊息。
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-start-")).resolve())
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import db, main


# ============================================================ 日誌時間戳
head("[B] 日誌要有時間點")

check("格式裡有 asctime", "%(asctime)s" in main.LOG_FORMAT, main.LOG_FORMAT)
check("日期也要在（服務會連開好幾天，只有時分秒分不出是哪一天）",
      "%Y" in main.LOG_DATEFMT and "%H" in main.LOG_DATEFMT, main.LOG_DATEFMT)

spec = importlib.util.spec_from_file_location("runmod", ROOT / "run.py")
runmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runmod)
cfg = runmod._log_config()
check("uvicorn 也吃同一份格式（basicConfig 管不到它）",
      cfg["formatters"]["std"]["format"] == main.LOG_FORMAT
      and cfg["formatters"]["std"]["datefmt"] == main.LOG_DATEFMT, cfg["formatters"]["std"])
check("access log 也有時間戳",
      "%(asctime)s" in cfg["formatters"]["access"]["fmt"], cfg["formatters"]["access"])
check("三個 uvicorn logger 都被接管",
      set(cfg["loggers"]) == {"uvicorn", "uvicorn.error", "uvicorn.access"}, list(cfg["loggers"]))
import logging.config
logging.config.dictConfig(cfg)          # 設定本身要是合法的
check("這份設定 dictConfig 吃得下", True)


# ============================================================ 管理員提示
head("[B] 「還沒有管理員」的提示要分兩種情況")

with_list = main.no_admin_lines(["me@example.com", "you@example.com"])
without = main.no_admin_lines([])
joined = "\n".join(with_list)
check("名單有值時不要再叫人去設 .env（他已經設了）",
      "請在 .env 設" not in joined, joined)
check("要講出真正的下一步：用那個帳號登入一次",
      "登入一次" in joined, joined)
check("要說出設了幾個", "2 個帳號" in joined, joined)
check("信箱要遮罩（日誌會被貼進聊天室與截圖）",
      "me@example.com" not in joined and "@example.com" in joined, joined)
check("名單空的時候才叫人去設 .env",
      "請在 .env 設" in "\n".join(without), without)
check("兩種情況都要先講後果（大家會卡在待審核）",
      all("待審核" in x[0] for x in (with_list, without)))
check("遮罩保留網域（判斷是不是設錯公司網域時要看得到）",
      main._mask_email("markcheng@intumit.com").endswith("@intumit.com"))
check("不是信箱就原樣回", main._mask_email("notanemail") == "notanemail")


# ============================================================ A-3 時間點整數化
head("[A-3] 時間點欄位一律整數秒")

db.init_db()
fid = db.execute(
    """INSERT INTO media_file(ftp_path, filename, ext, size, duration,
                              probe_state, seen_at, added_at)
       VALUES('/x/a.mkv','a.mkv','mkv',1,123.456,'ok',?,?)""",
    (1788486217.209, 1788486217.777)).lastrowid
db.execute("""INSERT INTO play_state(file_id, position, duration, finished, updated_at)
              VALUES(?,?,?,0,?)""", (fid, 61.25, 123.456, 1788486217.5))
db.execute("DELETE FROM kv WHERE k='time_points_rounded'")

db._round_time_points(db.get_conn())
r = db.q1("SELECT seen_at, added_at, duration FROM media_file WHERE id=?", (fid,))
check("seen_at 取整", r["seen_at"] == int(r["seen_at"]), r["seen_at"])
check("added_at 取整", r["added_at"] == int(r["added_at"]), r["added_at"])
check("**duration 不准動**（時間長度不是時間點）", r["duration"] == 123.456, r["duration"])
ps = db.q1("SELECT position, updated_at FROM play_state WHERE file_id=?", (fid,))
check("play_state.updated_at 取整", ps["updated_at"] == int(ps["updated_at"]), ps["updated_at"])
check("**position 不准動**（取整會讓續播每次往前跳一秒）", ps["position"] == 61.25, ps["position"])

check("做完會記一筆，不會每次啟動都掃一遍",
      db.kv_get("time_points_rounded") == 1, db.kv_get("time_points_rounded"))
db.execute("UPDATE media_file SET seen_at=1788486217.209 WHERE id=?", (fid,))
db._round_time_points(db.get_conn())
again = db.q1("SELECT seen_at FROM media_file WHERE id=?", (fid,))["seen_at"]
check("第二次啟動不會再全表掃一遍（記號已經在了，值原封不動）",
      again == 1788486217.209, again)

# 寫入端也要統一：不能再有人用浮點的 now()
#
# **用 Python 掃，不要 subprocess 呼叫 grep。**這一套測試是在 Windows 上跑的，
# 那裡沒有 grep —— 第一版就是這樣掛掉的（FileNotFoundError: WinError 2），
# 而且掛在最後一項，前面全綠。測試本身不可以有平台假設。
hits = []
for f in sorted((ROOT / "app").rglob("*.py")):
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if "db.now()" in line:
            hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
check("app/ 裡不再有 db.now() 寫時間點（都改成 now_i）", not hits, hits[:5])
check("db.now() 本身留著（給還沒轉的地方與測試用）", callable(db.now))


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
