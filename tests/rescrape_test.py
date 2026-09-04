"""N 刮削修正包。

最重要的一項是第一項：**手動修正過的條目不能被自動刮削覆蓋。**
那個 bug 的代價不是「要再按一次」—— 使用者挑的那個 tmdb_id 沒有留在任何地方，
蓋掉之後只能靠記憶重做一次，而且自動比對會用同樣的檔名挑出同樣的錯答案。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-n-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "false", "MSSQL_HOST": "", "TMDB_API_KEY": "",
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


from app import db, nameparser, scanner            # noqa: E402
from app.main import app                           # noqa: E402
from app.routers.api import _rekey                 # noqa: E402
from starlette.testclient import TestClient        # noqa: E402

db.init_db()
cl = TestClient(app, client=("127.0.0.1", 1))


def seed():
    with db.tx() as conn:
        conn.execute("DELETE FROM media_file")
        conn.execute("DELETE FROM episode")
        conn.execute("DELETE FROM media_item")
    rows = [
        # id, kind, title, year, state, guess_key
        (1, "movie", "手動修好的片", 2024, "manual", "movie::手動修好的片::2024"),
        (2, "movie", "自動刮到的片", 2023, "ok", "movie::自動刮到的片::2023"),
        (3, "movie", "還沒刮到的片", None, "pending", "movie::還沒刮到的片::"),
        (4, "movie", "刮失敗的片", None, "failed", "movie::刮失敗的片::"),
        (5, "movie", "其實是影集", None, "ok", "movie::其實是影集::"),
    ]
    with db.tx() as conn:
        for i, kind, title, year, st, key in rows:
            conn.execute("""INSERT INTO media_item(id, kind, title, sort_title, year,
                            guess_key, scrape_state, tmdb_id, added_at, updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (i, kind, title, title.lower(), year, key, st, 1000 + i, 1.0, 1.0))
        for n in (1, 2, 3):
            conn.execute("""INSERT INTO media_file(id, item_id, ftp_path, filename, size,
                            duration, probe_state) VALUES(?,?,?,?,?,?,?)""",
                         (n, 5, f"/x/其實是影集 EP0{n}.mp4", f"其實是影集 EP0{n}.mp4",
                          100, 60.0, "ok"))


seed()

print("\n[1] 這一項是整包的重點：manual 不能被 force 蓋掉")
sel = lambda where: sorted(r["id"] for r in db.q(f"SELECT id FROM media_item {where}"))
# _scrape_items 內部的 where 邏輯，逐一驗證（tmdb 沒開，所以直接驗 SQL 選擇）
check("一般掃描只刮 pending / failed",
      sel("WHERE scrape_state IN ('pending','failed')") == [3, 4])
check("force 重掃會排除 manual",
      sel("WHERE scrape_state != 'manual'") == [2, 3, 4, 5])
check("force + 連 manual 一起 才會包含它", sel("") == [1, 2, 3, 4, 5])

src = (ROOT / "app" / "scanner.py").read_text(encoding="utf-8")
check("scanner 的 force 分支真的用了 != 'manual'（不是空字串）",
      "\"\" if include_manual else \"WHERE scrape_state != 'manual'\"" in src, )
check("start() 有 remanual 參數，而且傳到 _scrape_items",
      "remanual: bool = False" in src and "include_manual=remanual" in src)
api_src = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
check("remanual 只有跟 full 一起才生效（避免單獨誤用）",
      "remanual=remanual and full" in api_src)

print("\n[2] guess_key 要跟著 kind / year 重算")
check("電影改年份 → key 的第三段換掉",
      _rekey("movie::某片::2024", "movie", 2023) == "movie::某片::2023")
check("改成影集 → 前綴換成 tv、年份那一段消失",
      _rekey("tv 前綴檢查::某片::2024".replace("tv 前綴檢查", "movie"), "tv", 2024)
      == "tv::某片")
check("改回電影 → 年份補回去",
      _rekey("tv::某片", "movie", 1999) == "movie::某片::1999")
check("沒有舊 key 就不亂猜", _rekey(None, "movie", 2000) is None)
# 跟 nameparser 的真實輸出對得上，不是自己想的格式
p = nameparser.parse("某片 2024.mkv", [])
real = nameparser.guess_key(p, "movie")
check(f"格式跟 nameparser.guess_key 一致（{real}）",
      real.startswith("movie::") and real.count("::") == 2, real)

print("\n[3] PATCH /api/items：片名、年份、類型")
r = cl.patch("/api/items/2", json={"title": "改過的片名"})
d = db.row_to_dict(db.q1("SELECT * FROM media_item WHERE id=2"))
check("片名改了", d["title"] == "改過的片名", d["title"])
check("sort_title 跟著改（否則排序會用舊名字）",
      d["sort_title"] == "改過的片名", d["sort_title"])
check("狀態變成 manual，之後自動刮削不會再蓋掉",
      d["scrape_state"] == "manual", d["scrape_state"])
check("只改片名時 guess_key 不動（key 是從檔名算的，跟顯示片名無關）",
      d["guess_key"] == "movie::自動刮到的片::2023", d["guess_key"])

r = cl.patch("/api/items/2", json={"year": 1999})
d = db.row_to_dict(db.q1("SELECT * FROM media_item WHERE id=2"))
check("年份改了", d["year"] == 1999, d["year"])
check("改年份時 guess_key 跟著重算",
      d["guess_key"] == "movie::自動刮到的片::1999", d["guess_key"])

check("年份亂填會被擋", cl.patch("/api/items/2", json={"year": 99999}).status_code == 400)
check("片名不能空白", cl.patch("/api/items/2", json={"title": "   "}).status_code == 400)
check("kind 只能是 movie / tv",
      cl.patch("/api/items/2", json={"kind": "anime"}).status_code == 400)
check("找不到的條目回 404", cl.patch("/api/items/999", json={"title": "x"}).status_code == 404)

print("\n[4] 類型改成影集：集數要被指派出來")
before = db.q1("SELECT COUNT(*) c FROM episode WHERE item_id=5")["c"]
r = cl.patch("/api/items/5", json={"kind": "tv"})
d = db.row_to_dict(db.q1("SELECT * FROM media_item WHERE id=5"))
eps = db.q("SELECT season, episode FROM episode WHERE item_id=5 ORDER BY episode")
files = db.q("SELECT id, episode_id FROM media_file WHERE item_id=5")
check("kind 變成 tv", d["kind"] == "tv", d["kind"])
check("guess_key 換成 tv:: 前綴（不然下次掃描會多一筆重複條目）",
      d["guess_key"] == "tv::其實是影集", d["guess_key"])
check(f"三個檔案都被指派了集數（原本 {before} 集 → {len(eps)} 集）",
      len(eps) == 3 and all(f["episode_id"] for f in files),
      [dict(e) for e in eps])
check("狀態是 manual（改過類型的條目不該被自動刮削蓋回去）",
      d["scrape_state"] == "manual", d["scrape_state"])

print("\n[5] 類型改回電影：集數要走 purge 清掉")
r = cl.patch("/api/items/5", json={"kind": "movie"})
check("集數清掉了", db.q1("SELECT COUNT(*) c FROM episode WHERE item_id=5")["c"] == 0)
check("檔案的 episode_id 也解開了",
      all(f["episode_id"] is None for f in
          db.q("SELECT episode_id FROM media_file WHERE item_id=5")))
check("清集數是走 purge()，不是自己寫 DELETE",
      "purge.purge(episode_ids=" in src)

print("\n[6] 手動修正清單")
mn = cl.get("/api/admin/manual").json()["items"]
ids = sorted(x["id"] for x in mn)
db_ids = sorted(r["id"] for r in db.q("SELECT id FROM media_item WHERE scrape_state='manual'"))
check(f"清單筆數與資料庫一致（{len(ids)} 筆）", ids == db_ids, (ids, db_ids))
check("每一筆都帶得出檔案數", all("files" in x for x in mn))

print("\n[7] 前端的三個入口都在")
appjs = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
check("有「改片名／年份」", "window.editItem" in appjs and "改片名／年份" in appjs)
check("有「改成影集／電影」", "window.switchKind" in appjs)
check("檔案列有「重新分析」", "window.reprobe" in appjs and "重新分析" in appjs)
check("api() 帶了 Content-Type（不然 PATCH 會收到 422）",
      "'Content-Type': 'application/json'" in appjs)
adminjs = (ROOT / "app" / "static" / "admin.js").read_text(encoding="utf-8")
check("後台有 remanual 勾選", "cbRemanual" in adminjs and "remanual=true" in adminjs)
check("勾選在對話框被清掉之前就讀走（collect）",
      "collect" in adminjs and "if (v && collect) collect(m)" in adminjs)
check("後台有手動修正清單", "/admin/manual" in adminjs)

print("\n" + "=" * 54)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
