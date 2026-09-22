"""「繼續觀看」的影集去重 ＋ 「下一集」的判斷。

    python tests/continue_next_test.py

這兩件事放同一支測試，因為它們共用同一個假片庫，而且真正要保護的是同一件
事實：**「現在在看哪一集」與「下一集是哪一集」只有一份答案**。兩邊各自對、
合起來不一致（播放器跳到 S02E03、繼續觀看還顯示 S02E02）沒有錯誤訊息，
只會讓使用者覺得系統在亂跳。

除了功能，這裡也把 ACL 當成一級公民測：`next_episode` 回的是 file_id、
season、集名與劇照網址 —— 每一個欄位都可以拿來推斷「那一集存在」，
所以它的可見性條件不能比 `/api/play` 本身更寬鬆（見 app/episodes.py）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-cn-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
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
        print(f"  FAIL  {name}  → {extra}")


def head(t):
    print(f"\n{t}")


from app import acl, auth, db, episodes, users        # noqa: E402
from app.main import app                             # noqa: E402
from starlette.testclient import TestClient          # noqa: E402

db.init_db()
users.init()

OPEN = "/媒體資料庫/公開"
SECRET = "/媒體資料庫/私人"

# 假片庫的形狀：
#   item 1  影集 A（公開）   S01E01..E03, S02E01..E02
#   item 2  影集 B（公開）   S01E01
#   item 3  電影（公開）     兩個檔案（不同版本），刻意不分集
#   item 4  影集 C（受限）   S01E01..E02
# 影集 A 的 S01E02 刻意**有 episode 沒有檔案**，用來驗「缺一集要跳過」。
# 影集 A 的 S02E02 刻意**有兩個檔案**，用來驗「同一集的另一版本不是下一集」。
FILES: dict = {}


def seed():
    with db.tx() as conn:
        for t in ("media_file", "episode", "media_item", "play_state"):
            conn.execute(f"DELETE FROM {t}")

        def item(iid, title, kind):
            conn.execute(
                "INSERT INTO media_item(id,title,kind,sort_title,genres,scrape_state,added_at)"
                " VALUES(?,?,?,?,'[]','ok',1.0)", (iid, title, kind, title))

        def ep(eid, iid, s, e, title):
            conn.execute("INSERT INTO episode(id,item_id,season,episode,title)"
                         " VALUES(?,?,?,?,?)", (eid, iid, s, e, title))

        def mf(fid, iid, eid, folder, name):
            conn.execute(
                "INSERT INTO media_file(id,item_id,episode_id,ftp_path,filename,size,"
                "duration,probe_state,play_mode) VALUES(?,?,?,?,?,1000,3600.0,'ok','direct')",
                (fid, iid, eid, f"{folder}/{name}", name))

        item(1, "影集A", "tv")
        item(2, "影集B", "tv")
        item(3, "電影", "movie")
        item(4, "影集C", "tv")

        # 影集 A
        ep(11, 1, 1, 1, "一之一")
        ep(12, 1, 1, 2, "一之二（沒有檔案）")
        ep(13, 1, 1, 3, "一之三")
        ep(14, 1, 2, 1, "二之一")
        ep(15, 1, 2, 2, "二之二")
        mf(101, 1, 11, OPEN, "A.S01E01.mkv")
        # 12 故意沒有檔案
        mf(103, 1, 13, OPEN, "A.S01E03.mkv")
        mf(104, 1, 14, OPEN, "A.S02E01.mkv")
        # S02E02 有兩個版本：filename 排序上 1080p 在 720p 前面
        mf(105, 1, 15, OPEN, "A.S02E02.1080p.mkv")
        mf(106, 1, 15, OPEN, "A.S02E02.720p.mkv")

        # 影集 B
        ep(21, 2, 1, 1, "B 第一集")
        mf(201, 2, 21, OPEN, "B.S01E01.mkv")

        # 電影：兩個檔案，沒有 episode
        mf(301, 3, None, OPEN, "movie.1080p.mkv")
        mf(302, 3, None, OPEN, "movie.720p.mkv")

        # 影集 C（受限資料夾）
        ep(41, 4, 1, 1, "C 第一集")
        ep(42, 4, 1, 2, "C 第二集")
        mf(401, 4, 41, SECRET, "C.S01E01.mkv")
        mf(402, 4, 42, SECRET, "C.S01E02.mkv")
    acl.invalidate()


def progress(file_id, position, updated_at, finished=0, duration=3600.0):
    db.execute(
        "INSERT INTO play_state(file_id,position,duration,finished,updated_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET position=excluded.position,"
        " duration=excluded.duration, finished=excluded.finished,"
        " updated_at=excluded.updated_at",
        (file_id, position, duration, finished, updated_at))


seed()

u_ok = users.upsert_from_google({"sub": "c1", "email": "ok@example.com",
                                 "name": "有權限"}, "127.0.0.1", {})
u_no = users.upsert_from_google({"sub": "c2", "email": "no@example.com",
                                 "name": "沒權限"}, "127.0.0.1", {})
for u in (u_ok, u_no):
    users.set_status(u["id"], "approved")
    users.set_role(u["id"], "viewer")
rule = acl.add_rule(SECRET, "測試")
acl.set_grants(rule["id"], [u_ok["id"]])

admin = TestClient(app, client=("127.0.0.1", 1))
admin.cookies.set(auth.COOKIE, auth.make_token(auth.ADMIN))
granted = TestClient(app, client=("127.0.0.1", 1))
granted.cookies.set(auth.COOKIE, auth.make_user_token(u_ok["id"]))
denied = TestClient(app, client=("127.0.0.1", 1))
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))


def cont(cl, **kw):
    qs = "&".join(f"{k}={v}" for k, v in kw.items())
    return cl.get("/api/continue" + (("?" + qs) if qs else "")).json()["items"]


# ---------------------------------------------------------------- 去重
head("[1] 影集去重：同一部影集最多一筆")
db.execute("DELETE FROM play_state")
progress(104, 600, 1000)        # A S02E01，先看
progress(105, 600, 2000)        # A S02E02，後看
items = cont(admin)
check("同一部影集只回一筆", len([i for i in items if i["item_id"] == 1]) == 1,
      [(i["file_id"], i["season"], i["episode"]) for i in items])
check("留的是最近觀看的那一集（S02E02，不是先看的 S02E01）",
      items[0]["file_id"] == 105, items[0])

head("[2] 回頭看舊集：以 updated_at 為準，不是季集數字最大的")
progress(101, 600, 3000)        # 回頭看 A S01E01，成為最新
items = cont(admin)
a = [i for i in items if i["item_id"] == 1]
check("同一部影集仍然只有一筆", len(a) == 1, a)
check("顯示的是 S01E01（最新觀看），不是 S02E02（數字最大）",
      a[0]["file_id"] == 101 and (a[0]["season"], a[0]["episode"]) == (1, 1), a[0])

head("[3] 兩部不同影集各自存在")
progress(201, 600, 2500)        # 影集 B
items = cont(admin)
check("影集 A 與影集 B 都在", sorted(i["item_id"] for i in items) == [1, 2],
      [(i["item_id"], i["file_id"]) for i in items])
check("排序仍然以最新觀看為主（A 的 3000 > B 的 2500）",
      [i["item_id"] for i in items] == [1, 2], [i["item_id"] for i in items])

head("[4] 電影不受影集去重誤傷")
db.execute("DELETE FROM play_state")
progress(301, 600, 1000)
progress(302, 600, 1100)        # 同一個 media_item 的另一個版本
items = cont(admin)
check("同一部電影的兩個檔案都還在（它們不是集數）",
      sorted(i["file_id"] for i in items) == [301, 302],
      [i["file_id"] for i in items])

head("[5] limit 作用在去重之後")
db.execute("DELETE FROM play_state")
# 影集 A 的五集全部有進度，加上影集 B 一集
for n, (fid, at) in enumerate([(101, 1000), (103, 1100), (104, 1200),
                               (105, 1300), (106, 1400)]):
    progress(fid, 600, at)
progress(201, 600, 900)
items = cont(admin, limit=2)
check("limit=2 拿到 2 筆（不是先 LIMIT 再去重而只剩 1 筆）",
      len(items) == 2, [(i["item_id"], i["file_id"]) for i in items])
check("兩筆分別是影集 A 與影集 B",
      sorted(i["item_id"] for i in items) == [1, 2],
      [i["item_id"] for i in items])
items = cont(admin, limit=1)
check("limit=1 只回最近看的那一部（影集 A）",
      len(items) == 1 and items[0]["item_id"] == 1, items)

head("[6] 看完的集數不出現")
db.execute("DELETE FROM play_state")
progress(104, 600, 1000, finished=1)       # A S02E01 已看完
progress(105, 600, 900)                    # A S02E02 未看完、時間較舊
items = cont(admin)
check("看完的那一集不在清單上",
      [i["file_id"] for i in items] == [105], [i["file_id"] for i in items])
db.execute("DELETE FROM play_state")
progress(104, 600, 1000, finished=1)
items = cont(admin)
check("整部影集只剩看完的紀錄時，這部影集完全不出現",
      [i["item_id"] for i in items] == [], items)

head("[7] 去重不會讓 ACL 失效")
db.execute("DELETE FROM play_state")
progress(401, 600, 5000)       # 受限影集 C
progress(402, 600, 5100)
progress(101, 600, 1000)       # 公開影集 A
check("沒權限的人看不到受限影集（一筆都不行）",
      [i["item_id"] for i in cont(denied)] == [1],
      [(i["item_id"], i["file_id"]) for i in cont(denied)])
check("沒權限的人拿不到受限影集的片名",
      all("影集C" != i.get("title") for i in cont(denied)), cont(denied))
# 一般入口不再把受限的東西混進來，連被授權的人也一樣 ——
# 「繼續看」那一排就掛在首頁上，旁邊的人會看到。
check("被授權的人在一般入口也看不到受限影集",
      all(i["item_id"] != 4 for i in cont(granted)),
      [(i["item_id"], i["file_id"]) for i in cont(granted)])
# 去重仍然要驗，只是改在保險庫入口。
gi = cont(granted, scope="vault")
check("被授權的人在保險庫看得到，而且同樣去重成一筆",
      len([i for i in gi if i["item_id"] == 4]) == 1,
      [(i["item_id"], i["file_id"]) for i in gi])
check("被授權的人看到的是最近觀看的 C S01E02",
      [i["file_id"] for i in gi if i["item_id"] == 4] == [402], gi)

head("[8] tie-break 是 deterministic 的")
db.execute("DELETE FROM play_state")
progress(301, 600, 7000)
progress(302, 600, 7000)       # 同一秒，不同檔案
runs = {tuple(i["file_id"] for i in cont(admin)) for _ in range(5)}
check("updated_at 完全相同時，多次查詢的順序一致", len(runs) == 1, runs)

# ---------------------------------------------------------------- 下一集
head("[9] 同季的下一集")
db.execute("DELETE FROM play_state")
n = admin.get("/api/play/101").json()["next_episode"]
check("A S01E01 的下一集跳過沒有檔案的 S01E02，落在 S01E03",
      n and (n["season"], n["episode"], n["file_id"]) == (1, 3, 103), n)
check("附上前端需要的欄位（item_id / 集名 / label / url）",
      n and n["item_id"] == 1 and n["title"] == "一之三"
      and n["label"] == "S01E03" and n["url"] == "/player?file=103", n)

head("[10] 跨季")
n = admin.get("/api/play/103").json()["next_episode"]
check("同季最後一集 A S01E03 → 下一季第一集 S02E01",
      n and (n["season"], n["episode"], n["file_id"]) == (2, 1, 104), n)

head("[11] 最後一集回 null")
n = admin.get("/api/play/105").json()["next_episode"]
check("A S02E02 是最後一集 → next_episode 是 null", n is None, n)

head("[12] 同一集的多個檔案不能被當成「下一集」")
n = admin.get("/api/play/104").json()["next_episode"]
check("A S02E01 的下一集是 S02E02 這一集（不是它的第二個版本）",
      n and (n["season"], n["episode"]) == (2, 2), n)
check("挑的是 deterministic 的那一個檔案（filename 排序的第一個 = 1080p）",
      n and n["file_id"] == 105, n)
# 反向確認：106（同一集的另一版本）的下一集也是 null，不是 105
n106 = admin.get("/api/play/106").json()["next_episode"]
check("同一集的另一個版本也不會把「同集的另一檔」當下一集", n106 is None, n106)

head("[13] 電影沒有下一集")
for fid in (301, 302):
    n = admin.get(f"/api/play/{fid}").json()["next_episode"]
    check(f"電影檔 {fid} 的 next_episode 是 null", n is None, n)

head("[14] 下一集必須套用 ACL（不能洩漏受限內容）")
r = denied.get("/api/play/401")
check("受限影集連 /api/play 都進不去（404，不是 403）", r.status_code == 404, r.status_code)
n = granted.get("/api/play/401").json()["next_episode"]
check("被授權的人看得到 C 的下一集", n and n["file_id"] == 402, n)

# 關鍵的一項：公開資料夾的集數，但下一集落在受限資料夾裡。
# 這是 next_episode 最容易洩漏的形狀 —— 目前的片子看得到，下一集看不到。
with db.tx() as conn:
    conn.execute("INSERT INTO episode(id,item_id,season,episode,title)"
                 " VALUES(31,2,1,2,'B 第二集（受限）')")
    conn.execute(
        "INSERT INTO media_file(id,item_id,episode_id,ftp_path,filename,size,duration,"
        "probe_state,play_mode) VALUES(202,2,31,?,'B.S01E02.mkv',1000,3600.0,'ok','direct')",
        (f"{SECRET}/B.S01E02.mkv",))
acl.invalidate()
n_admin = admin.get("/api/play/201").json()["next_episode"]
n_denied = denied.get("/api/play/201").json()["next_episode"]
check("管理員看得到那一集當下一集", n_admin and n_admin["file_id"] == 202, n_admin)
check("沒權限的人的 next_episode 是 null（不是給一個點了 404 的 file_id）",
      n_denied is None, n_denied)
check("而且完全拿不到那一集的集名／季集編號",
      n_denied is None or ("受限" not in str(n_denied)), n_denied)

# 再往後補一集公開的，驗「跳過不可讀的，找下一個真的播得到的」
with db.tx() as conn:
    conn.execute("INSERT INTO episode(id,item_id,season,episode,title)"
                 " VALUES(32,2,1,3,'B 第三集')")
    conn.execute(
        "INSERT INTO media_file(id,item_id,episode_id,ftp_path,filename,size,duration,"
        "probe_state,play_mode) VALUES(203,2,32,?,'B.S01E03.mkv',1000,3600.0,'ok','direct')",
        (f"{OPEN}/B.S01E03.mkv",))
acl.invalidate()
n_denied = denied.get("/api/play/201").json()["next_episode"]
check("受限的那一集被跳過，落在後面第一個真的播得到的 S01E03",
      n_denied and n_denied["file_id"] == 203, n_denied)
n_admin = admin.get("/api/play/201").json()["next_episode"]
check("管理員的答案不受影響（仍然是緊接著的 S01E02）",
      n_admin and n_admin["file_id"] == 202, n_admin)

head("[15] 共用的判斷函式只有一份")
# episodes.next_episode 是唯一的實作 —— api.py 不能自己再寫一套排序。
import inspect                                              # noqa: E402
from app.routers import api as _api                          # noqa: E402
src = inspect.getsource(_api.play_info)
check("/api/play 是呼叫 episodes.next_episode，不是自己排序",
      "episodes.next_episode" in src, src[-400:])
check("api.py 裡沒有第二份 (season, episode) 比較邏輯",
      inspect.getsource(_api).count("e.season > ?") == 0)

head("[16] 接近片尾的進度會被正規化成看完")
db.execute("DELETE FROM play_state")
# 剩 5 秒就離開頁面：ended 沒觸發，但這一筆不該留在繼續觀看
admin.post("/api/progress", json={"file_id": 101, "position": 3595,
                                  "duration": 3600, "finished": False})
row = db.q1("SELECT finished FROM play_state WHERE file_id=101")
check("剩 5 秒的進度被正規化成 finished", row["finished"] == 1, dict(row))
check("所以它不出現在繼續觀看", [i["file_id"] for i in cont(admin)] == [],
      cont(admin))
# 而「下一集按鈕的門檻」（剩 30～90 秒）絕對不能被當成看完
admin.post("/api/progress", json={"file_id": 101, "position": 3540,
                                  "duration": 3600, "finished": False})
row = db.q1("SELECT finished FROM play_state WHERE file_id=101")
check("剩 60 秒（下一集按鈕已經浮出來的位置）不算看完",
      row["finished"] == 0, dict(row))
check("而且仍然在繼續觀看上", [i["file_id"] for i in cont(admin)] == [101],
      cont(admin))

print(f"\n{'='*60}\n通過 {OK}、失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
