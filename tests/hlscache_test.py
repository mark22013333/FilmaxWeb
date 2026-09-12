"""後台的 HLS 快取盤點與預備。

這支測試的重點不是「按鈕會不會動」，是兩件更容易在改版時悄悄壞掉的事：

1. **盤點照磁碟講實話。**統計數字不可以被分頁截斷（後台顯示的「總共佔用
   4.2 GB」隨著翻頁變動的話，那個數字沒有人敢信），而版本殘留要認得出來。
2. **清除與預備都是 admin-only。**它們會刪東西、會吃滿 CPU，唯讀使用者
   碰不得。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-hlscache-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "true", "AUTH_PASSWORD": "adminpw12345",
    "VIEWER_PASSWORD": "viewerpw12345",
    "MSSQL_HOST": "", "TMDB_API_KEY": "", "AUTO_SCAN_ON_START": "false",
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
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
        print(f"  FAIL  {name}  -> {extra}")


def head(t):
    print(f"\n{t}")


from app import auth, db, hls, hlscache          # noqa: E402
from app.config import CACHE_DIR                 # noqa: E402
from app.main import app                         # noqa: E402
from starlette.testclient import TestClient      # noqa: E402

db.init_db()

V = hls.HLS_CACHE_FORMAT_VERSION


def seed(file_id: int, dirname: str, segs: int, size: int = 1000) -> None:
    d = CACHE_DIR / str(file_id) / dirname
    d.mkdir(parents=True, exist_ok=True)
    for i in range(segs):
        (d / f"seg-{i}.ts").write_bytes(b"x" * size)


head("[快取盤點] 照磁碟上實際有什麼回答")

# 目前版本的兩個 profile、一個舊版本殘留、一個空資料夾
seed(101, f"v{V}_h720_ad_b2800_c2_m0", 3, 1000)
seed(101, f"v{V}_h0_ad_b0_c2_m1", 2, 2000)
seed(102, f"v{V - 1}_h720_ad_b2800_c2_m0", 4, 500)
(CACHE_DIR / "103" / f"v{V}_h360_ad_b0_c2_m0").mkdir(parents=True, exist_ok=True)

s = hlscache.survey()
check("總量把所有檔案加起來", s["totalBytes"] == 3 * 1000 + 2 * 2000 + 4 * 500,
      s["totalBytes"])
check("分段總數也是", s["totalSegments"] == 3 + 2 + 4, s["totalSegments"])
check("有快取的檔案數（空資料夾的那個不算）", s["fileCount"] == 2, s["fileCount"])
check("舊版本的殘留認得出來", s["staleBytes"] == 4 * 500 and s["staleDirs"] == 1,
      (s["staleBytes"], s["staleDirs"]))
check("空資料夾數得出來", s["emptyDirs"] == 1, s["emptyDirs"])
check("回報的是目前的格式版本", s["version"] == V, s["version"])

f101 = next(f for f in s["files"] if f["file_id"] == 101)
check("同一個檔案的多個 profile 會聚在一起", len(f101["profiles"]) == 2, f101)
check("檔案依佔用大小排序（大的在前）",
      [f["file_id"] for f in s["files"]] == [101, 102], s["files"])

# **統計不可以被分頁截斷。**後台那四個數字是拿來做決定的（要不要清、清哪個），
# 它們隨著翻頁變動的話就沒有意義了。
s1 = hlscache.survey(limit=1)
check("limit 只限制列出的筆數，不影響統計",
      s1["totalBytes"] == s["totalBytes"] and s1["fileCount"] == s["fileCount"],
      (s1["totalBytes"], s1["fileCount"]))
check("列出的筆數真的被限制了", len(s1["files"]) == 1, len(s1["files"]))
check("被截斷時要講", s1["truncated"] is True, s1)
check("沒截斷時不要亂講", s["truncated"] is False, s)


head("[快取盤點] 沒有版本前綴的舊資料夾也要算進殘留")

seed(104, "h1080_ad_b0", 2, 300)          # v3 以前的格式，連版本前綴都沒有
s2 = hlscache.survey()
old = next((p for f in s2["files"] if f["file_id"] == 104 for p in f["profiles"]), None)
check("認不得版本就當成 v0（不要猜）", old and old["version"] == 0, old)
check("而且一定是 stale（路徑帶版本之後它讀不到了）", old and old["stale"] is True, old)


head("[清殘留] 只刪不是目前版本的，目前版本的一段都不能少")

before = hlscache.survey()
r = hlscache.clear_stale()
after = hlscache.survey()
check("回報刪了幾個資料夾", r["dirs"] == 2, r)          # 102 的 v(V-1) + 104 的無前綴
check("目前版本的分段一段都沒少",
      after["totalSegments"] == before["totalSegments"] - 4 - 2,
      (before["totalSegments"], after["totalSegments"]))
check("殘留歸零", after["staleBytes"] == 0 and after["staleDirs"] == 0, after)
check("101 的兩個 profile 都還在",
      len(next(f for f in after["files"] if f["file_id"] == 101)["profiles"]) == 2)


head("[預備] 參數檢查（不真的去轉，那需要片源）")

ok, msg = hlscache.start(999999, "", 0)
check("找不到檔案就拒絕", ok is False and "找不到" in msg, msg)

db.execute("INSERT INTO media_item(id, kind, title) VALUES(1,'movie','T')")
db.execute("""INSERT INTO media_file(id, item_id, ftp_path, filename, duration)
              VALUES(9001, 1, '/x/a.mkv', 'a.mkv', 0)""")
ok, msg = hlscache.start(9001, "", 0)
check("沒有長度就拒絕（算不出要轉幾段）", ok is False and "長度" in msg, msg)

check("閒著的時候 running 是 false", hlscache.is_running() is False)

# **問選項不可以在磁碟上留下東西。**hls.seg_dir() 會順手 mkdir，直接拿來數
# 段數的話，光是點開預備對話框（就算按取消）就會生出一個空資料夾，然後出現
# 在下一次盤點的列表裡。實測踩過，所以這條是回歸測試。
db.execute("""INSERT INTO media_file(id, item_id, ftp_path, filename, duration)
              VALUES(9002, 1, '/x/b.mkv', 'b.mkv', 600)""")
before_dirs = {p.name for p in (CACHE_DIR / "9002").iterdir()} if (CACHE_DIR / "9002").is_dir() else set()
hlscache.warm_options(9002)
after_dirs = {p.name for p in (CACHE_DIR / "9002").iterdir()} if (CACHE_DIR / "9002").is_dir() else set()
check("問「可以預備哪些階」不會建出空資料夾", before_dirs == after_dirs,
      after_dirs - before_dirs)
st = hlscache.status.dict()
check("狀態欄位齊全（前端要靠它畫進度）",
      all(k in st for k in ("running", "done", "skipped", "failed", "total",
                            "phase", "eta", "elapsed")), st)


head("[權限] 會刪東西、會吃 CPU 的端點都要是 admin-only")

admin = TestClient(app, client=("127.0.0.1", 1))
admin.cookies.set(auth.COOKIE, auth.make_token(auth.ADMIN))
viewer = TestClient(app, client=("127.0.0.1", 1))
viewer.cookies.set(auth.COOKIE, auth.make_token(auth.VIEWER))

for path, method in (("/api/cache/survey", "get"),
                     ("/api/cache/clear-stale", "post"),
                     ("/api/cache/warm", "get"),
                     ("/api/cache/warm/options?file_id=9001", "get"),
                     ("/api/cache/warm?file_id=9001", "post"),
                     ("/api/cache/warm/cancel", "post"),
                     ("/api/cache/clear", "post")):
    r = getattr(viewer, method)(path)
    check(f"唯讀使用者不能碰 {method.upper()} {path.split('?')[0]}",
          r.status_code == 403, r.status_code)

r = admin.get("/api/cache/survey")
check("管理員拿得到盤點", r.status_code == 200 and "totalBytes" in r.json(),
      r.status_code)
check("盤點會帶上快取上限（前端要算百分比）", "limitMb" in r.json(), r.json().keys())

r = admin.get("/api/cache/warm/options?file_id=9001")
check("沒長度的檔案問選項會被擋下來，而且說得出原因",
      r.status_code == 200 and r.json()["ok"] is False, r.text[:120])

r = admin.post("/api/cache/warm?file_id=9001")
check("排不進去要回 409（前端才分得出「已經有一支在跑」）",
      r.status_code == 409, r.status_code)

head("[檔案選擇器] 後端的搜尋端點")

db.execute("INSERT INTO media_item(id, kind, title) VALUES(9, 'tv', '幸運女神')")
db.execute("INSERT INTO episode(id, item_id, season, episode) VALUES(9, 9, 1, 2)")
db.execute("""INSERT INTO media_file(id, item_id, episode_id, ftp_path, filename,
              duration, height, probe_state, added_at)
              VALUES(9101, 9, 9, '/x/L.mkv', 'Lucky.S01E02.1080p.mkv', 2880, 1080, 'ok', 100)""")
db.execute("""INSERT INTO media_file(id, item_id, ftp_path, filename, added_at)
              VALUES(9102, 9, '/x/pct.mkv', '100%_off_special.mkv', 200)""")

r = admin.get("/api/cache/survey")     # 先確認 admin client 還活著
r = admin.get("/api/admin/files?q=lucky")
items = r.json()["items"]
check("關鍵字找得到（檔名比對）", any(i["id"] == 9101 for i in items), r.text[:200])
check("帶得出片名與季集（前端要組顯示字串）",
      any(i["title"] == "幸運女神" and i["season"] == 1 and i["episode"] == 2
          for i in items), items[:2])

# **純數字要置頂。**舊習慣是從詳情頁抄 file_id，那條路不能因為改了 UI 就斷掉。
r = admin.get("/api/admin/files?q=9101")
items = r.json()["items"]
check("打純數字時那個 id 排第一", items and items[0]["id"] == 9101,
      [i["id"] for i in items[:3]])

# **% 要逸出。**沒逸出的話 '%' 會比對到全部，而選單只給 30 筆 ——
# 使用者看到的是「我打的字明明對，選單裡卻是別支片」。
r = admin.get("/api/admin/files?q=%25")        # URL 編碼的 %
ids = [i["id"] for i in r.json()["items"]]
check("% 被當成字面而不是萬用字元", ids == [9102], ids)

r = admin.get("/api/admin/files?q=&limit=2")
body = r.json()
check("沒有關鍵字時回最近加入的（不是空清單）", len(body["items"]) == 2, body)
check("撈得到更多時要講（前端才顯示「再打幾個字」）", body["more"] is True, body)

r = viewer.get("/api/admin/files?q=a")
check("唯讀使用者不能用檔案搜尋", r.status_code == 403, r.status_code)

print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
