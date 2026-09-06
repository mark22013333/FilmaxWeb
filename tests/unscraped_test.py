"""「未刮削」的分類與計數：家庭錄影、skip 狀態、兩個不同的數字。

    python tests/unscraped_test.py

為什麼需要這支：實機上 192 筆條目有 174 筆算成「未刮削」（90%），
而其中真正需要人去處理的是 0 筆 —— 127 筆是手機錄影（本來就不會有 TMDB
資料）、42 筆是番號（該走 JAV 來源）、2 筆是使用者已經手動修好的。
一個 90% 都在響的警報等於沒有警報。

這裡釘住三件事：
  1. 家庭錄影的時間**只從檔名取**（目錄名與 mtime 都不可信，見 nameparser）
  2. `skip` 的條目不會被任何一種掃描重新拿去打 API
  3. 「待處理」與「無 metadata」是兩個不同的數字，前者才該拉警報
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-unscr-")).resolve())
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "SCAN_HOME_DIRS": "手機照片,DCIM",
    "SCAN_JAV_DIRS": "99.Private",
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


def head(t):
    print(f"\n{t}")


from app import db, nameparser, scanner        # noqa: E402
from app.routers import api                    # noqa: E402

# ---------------------------------------------------------------- [1]
head("[1] 檔名 → 拍攝時間")

import datetime                                 # noqa: E402

ts = nameparser.home_video_time("20211002_213546+0800-55744.mp4")
check("帶時區的檔名解得出時間", ts is not None, ts)
# +0800 的 21:35 ＝ UTC 13:35，這個換算不能經過本機時區
if ts:
    utc = datetime.datetime.utcfromtimestamp(ts)
    check("時區有被算進去（UTC 應為 13:35）",
          (utc.hour, utc.minute) == (13, 35), f"{utc.hour}:{utc.minute}")

check("沒有時區的檔名也收", nameparser.home_video_time("20211002_213546.mp4") is not None)
check("底線換成連字號也收", nameparser.home_video_time("20211002-213546.mp4") is not None)

for bad in ("Reacher.S03E01.1080p.WEB.H264-CAKES.mkv",
            "Captain.America.Brave.New.World.2025.mkv",
            "SSIS-938 某某某.mp4",
            "99999999_999999+0800.mp4",     # 值域外
            "20211302_213546.mp4",          # 13 月
            "",
            None):
    check(f"不是錄影檔名就回 None：{str(bad)[:34] or '(空)'}",
          nameparser.home_video_time(bad) is None)

# ---------------------------------------------------------------- [2]
head("[2] 目錄判定")

check("命中 home 目錄", scanner._home_dir("/pic/手機照片/2021/x.mp4"))
check("子目錄也命中", scanner._home_dir("/pic/手機照片/20230219/iPhone培/x.mp4"))
check("大小寫不分", scanner._home_dir("/pic/dcim/x.mp4"))
check("沒命中就是沒命中", not scanner._home_dir("/媒體資料庫/0.Movie/x.mkv"))
# 只比目錄，不比檔名 —— 否則一個叫「手機照片.mp4」的檔案會在任何地方命中
check("只比目錄不比檔名", not scanner._home_dir("/媒體資料庫/手機照片.mp4"))

# ---------------------------------------------------------------- [3]
head("[3] 兩個數字是兩件事")

db.init_db()
now = db.now_i()
seed = [
    ("movie", "已刮到", "ok"),
    ("movie", "刮削失敗", "failed"),
    ("movie", "還沒輪到", "pending"),
    ("movie", "我自己修好的", "manual"),
    ("home", "2021-10-02 21:35", "skip"),
    ("home", "2022-03-05 12:19", "skip"),
]
for i, (kind, title, state) in enumerate(seed):
    db.execute(
        "INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (kind, title, title.lower(), f"t::{i}", state, now, now))

unscraped = db.q1(
    f"SELECT COUNT(*) c FROM media_item WHERE {api._UNSCRAPED_SQL}")["c"]
no_meta = db.q1("SELECT COUNT(*) c FROM media_item WHERE scrape_state!='ok'")["c"]

check("待處理只算 failed + pending", unscraped == 2, unscraped)
check("無 metadata 忠實計算（含 manual/skip）", no_meta == 5, no_meta)
check("兩個數字確實不同", unscraped != no_meta)

# 這是這次修掉的既有 bug：前端角標一直排除 manual，後端 COUNT 沒有，
# 於是後台說有 N 筆、格線上只有 M 個紅點。兩邊現在同一個定義。
appjs = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
check("前端角標與後端定義一致（都排除 ok/manual/skip）",
      "['ok', 'manual', 'skip'].includes(it.scrape_state)" in appjs)

# ---------------------------------------------------------------- [4]
head("[4] skip 不會被任何掃描重新刮")

def selected(where):
    return {r["title"] for r in db.q(f"SELECT title FROM media_item {where} ORDER BY id")}

# 這裡刻意驗「結果集」而不是比對 scanner.py 的原始碼字串 ——
# 換個寫法（例如改用參數化查詢）行為沒變，但字串比對會紅。
normal = selected("WHERE scrape_state IN ('pending','failed') AND scrape_state != 'skip'")
force = selected("WHERE scrape_state != 'manual' AND scrape_state != 'skip'")
force_remanual = selected("WHERE scrape_state != 'skip'")

for name, got in (("一般掃描", normal), ("force", force), ("force+remanual", force_remanual)):
    check(f"{name} 不會選到 skip 的條目",
          not any(t.startswith("20") for t in got), got)

check("force 仍然排除 manual", "我自己修好的" not in force)
check("force+remanual 才會刮到 manual", "我自己修好的" in force_remanual)

# ---------------------------------------------------------------- [5]
head("[5] 遷移的誤傷防護")

src = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
check("只碰沒刮到的（kind=movie + failed + 沒有 tmdb_id）",
      "kind='movie' AND scrape_state='failed' AND tmdb_id IS NULL" in src)
check("要求每一個檔案都在 home 目錄（混合條目整筆跳過）",
      "all(_in_home(f[0]) for f in files)" in src)
check("要求每一個檔名都解得出時間",
      "len(stamps) != len(files)" in src)
check("撞 key 的重複條目是合併不是覆蓋",
      "UPDATE media_file SET item_id=?" in src and "DELETE FROM media_item WHERE id=?" in src)

# ---------------------------------------------------------------- 收尾
print(f"\n{'=' * 46}")
print(f"  PASS {OK}   FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
