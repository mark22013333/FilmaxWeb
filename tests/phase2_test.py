"""第二期：批次寫入、時間欄位、單一刪除進入點。

    python tests/phase2_test.py
"""
import os, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-p2-")
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

from app import db, media, purge, timeparse
from app.config import IMAGE_DIR
db.init_db()


# ============================================================ G-3 子行程終止
head("[G-3] 停止掃描要能真的殺掉 ffprobe")

# 子行程每 0.1 秒寫一次心跳檔。**用心跳而不是 pgrep**：
# 這一套測試也會在 Windows 上跑，那裡沒有 pgrep —— 原本那一句在 Windows 上
# 是「內層 python 找不到 pgrep → 例外 → stdout 空的 → 判定沒有殘留」，
# 也就是**因為錯誤的理由而通過**，真的漏掉一隻子行程時它一樣會綠。
# 心跳檔測的是同一件事，而且不依賴任何平台工具。
HB = os.path.join(TMP, "heartbeat.txt")
CHILD = ("import sys, time\n"
         "while True:\n"
         "    open(sys.argv[1], 'w').write(str(time.time()))\n"
         "    time.sleep(0.1)\n")

ev = threading.Event()
threading.Timer(0.8, ev.set).start()
t0 = time.monotonic()
err = None
try:
    media._run([sys.executable, "-c", CHILD, HB], timeout=60, cancel=ev)
except Exception as e:
    err = e
dt = time.monotonic() - t0
check("取消時丟 Cancelled 而不是等滿逾時", isinstance(err, media.Cancelled), type(err).__name__)
check(f"0.8 秒設旗標，{dt:.1f} 秒內回來（原本要等 60 秒）", dt < 3, dt)

check("子行程有真的跑起來（不然下面那一項會因為錯的理由通過）",
      os.path.exists(HB), HB)
before = os.path.getmtime(HB) if os.path.exists(HB) else 0
time.sleep(1.0)
after = os.path.getmtime(HB) if os.path.exists(HB) else 0
check("子行程真的死了，不是只設了旗標（心跳停了）", after == before, (before, after))

t0 = time.monotonic()
code, out, _ = media._run([sys.executable, "-c", "print('hi')"], timeout=10)
check("沒傳 cancel 的舊路徑不受影響", code == 0 and b"hi" in out, (code, out))

try:
    media._run([sys.executable, "-c", "import time; time.sleep(30)"],
               timeout=1, cancel=threading.Event())
    kind = "沒丟例外"
except Exception as e:
    kind = type(e).__name__
check("逾時仍然是 TimeoutExpired，不會跟取消搞混", kind == "TimeoutExpired", kind)


# ============================================================ A-3 時間
head("[A-3] 時間欄位")

check("EXIF 有時區就照它算",
      timeparse.parse("2024:03:09 14:05:22", tz_offset="Z")
      - timeparse.parse("2024:03:09 14:05:22", tz_offset="+09:00") == 9 * 3600)
check("Z 等於 +00:00", timeparse.offset_seconds("Z") == 0)
check("-0330 這種沒有冒號的也認得", timeparse.offset_seconds("-0330") == -12600)
check("認不出來的時區回 None 而不是當成 0",
      timeparse.offset_seconds("台北") is None and timeparse.offset_seconds("") is None)
check("時區認不出來就退回本機時區，不是整個放棄",
      timeparse.parse("2024:03:09 14:05:22", tz_offset="台北") is not None)

now = db.now()
def add_photo(path, **kw):
    cols = dict(ftp_path=path, folder="/p", filename=path.rsplit("/", 1)[-1],
                ext="jpg", size=1024, probe_state="ok", seen_at=now, added_at=now)
    cols.update(kw)
    k = ",".join(cols); q = ",".join("?" * len(cols))
    return db.execute(f"INSERT INTO photo({k}) VALUES({q})", tuple(cols.values())).lastrowid

blocked = []
for bad, label in ((1700000000 * 1000, "毫秒當秒"), (-5, "負數"), (0, "epoch 0"),
                   (99999999999, "遠未來")):
    try:
        add_photo(f"/bad{bad}.jpg", sort_ts=bad)
        blocked.append(label)
    except Exception:
        pass
check("亂七八糟的時間值寫不進去（毫秒／負數／0／遠未來）", not blocked, blocked)

good = add_photo("/ok.jpg", sort_ts=1700000000, mtime_ts=1700000000)
check("正常值寫得進去", good > 0)
try:
    db.execute("UPDATE photo SET sort_ts=? WHERE id=?", (1700000000 * 1000, good))
    upd_blocked = False
except Exception:
    upd_blocked = True
check("UPDATE 也擋（不是只擋 INSERT）", upd_blocked)
check("NULL 可以（沒有拍攝時間是正常的）", add_photo("/null.jpg") > 0)


# ============================================================ A-4 purge
head("[A-4] purge 是唯一的刪除進入點")

try:
    purge.purge(file_ids=[1], reason="")
    no_reason = False
except ValueError:
    no_reason = True
check("沒給 reason 就拒絕", no_reason)

def mkitem(title, key):
    return db.execute(
        "INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
        " VALUES('tv',?,?,?,'ok',?,?)", (title, title, key, now, now)).lastrowid
def mkfile(item_id, path, **kw):
    cols = dict(item_id=item_id, ftp_path=path, filename=path.rsplit("/", 1)[-1],
                size=1024, ext="mkv", probe_state="ok", seen_at=now, added_at=now)
    cols.update(kw)
    k = ",".join(cols); q = ",".join("?" * len(cols))
    return db.execute(f"INSERT INTO media_file({k}) VALUES({q})", tuple(cols.values())).lastrowid

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
def mkimg(name):
    (IMAGE_DIR / name).write_bytes(b"x" * 10)
    return name

it = mkitem("測試影集", "tv::test::2020")
db.execute("UPDATE media_item SET poster=?, backdrop=? WHERE id=?",
           (mkimg("poster_a.jpg"), mkimg("backdrop_a.jpg"), it))
ep = db.execute("INSERT INTO episode(item_id,season,episode,title,still) VALUES(?,1,1,'第一集',?)",
                (it, mkimg("still_a.jpg"))).lastrowid
f1 = mkfile(it, "/tv/1.mkv", episode_id=ep, thumb=mkimg("thumb_1.jpg"))
f2 = mkfile(it, "/tv/2.mkv")
db.execute("INSERT INTO play_state(file_id,position,duration,updated_at) VALUES(?,?,?,?)",
           (f1, 120.5, 1400.0, now))

r = purge.purge(item_ids=[it], reason="test_cascade", dry_run=True)
check(f"dry_run 說會刪 1 條目 / 1 集數 / 2 檔案（實際 {r})",
      (r["item"], r["episode"], r["file"]) == (1, 1, 2), r)
check("dry_run 真的沒刪", db.q1("SELECT COUNT(*) c FROM media_file WHERE item_id=?", (it,))["c"] == 2)
check("dry_run 也沒刪磁碟檔", (IMAGE_DIR / "poster_a.jpg").is_file())

r = purge.purge(item_ids=[it], reason="test_cascade")
check(f"刪條目連帶刪掉集數與檔案（{r}）",
      (r["item"], r["episode"], r["file"], r["play_state"]) == (1, 1, 2, 1), r)
check("繼續觀看的紀錄也跟著走",
      db.q1("SELECT COUNT(*) c FROM play_state WHERE file_id=?", (f1,))["c"] == 0)
gone = [n for n in ("poster_a.jpg", "backdrop_a.jpg", "still_a.jpg", "thumb_1.jpg")
        if not (IMAGE_DIR / n).is_file()]
check(f"海報、底圖、劇照、縮圖四個磁碟檔都刪掉了（{r['image']} 個）", len(gone) == 4, gone)

# 內容雜湊命名 → 兩張一樣的照片共用同一個縮圖檔
shared = mkimg("p480_sharedhash.jpg")
p1 = add_photo("/dup/a.jpg", thumb=shared)
p2 = add_photo("/dup/b.jpg", thumb=shared)
purge.purge(photo_ids=[p1], reason="test_shared_thumb")
check("刪掉共用縮圖的其中一張，檔案不能被刪（不然另一張變破圖）",
      (IMAGE_DIR / shared).is_file())
purge.purge(photo_ids=[p2], reason="test_shared_thumb")
check("最後一張刪掉時，檔案才真的清掉", not (IMAGE_DIR / shared).is_file())

log = db.q("SELECT * FROM purge_log ORDER BY id DESC LIMIT 5")
check("刪除有留紀錄，而且記得住 reason",
      any(r["reason"] == "test_cascade" for r in log), [r["reason"] for r in log])


# ============================================================ A-4 sweep
head("[A-4] sweep 孤兒清掃")

orphan_item = mkitem("剛建好還沒寫檔案", "tv::fresh::2020")
r = purge.sweep("report")
check("剛建好的空條目在寬限期內不算孤兒",
      orphan_item not in [] and r["found"]["item_nofile"] == 0, r["found"])

# 把 added_at 往回撥兩小時，模擬「一小時前就在那裡了」
db.execute("UPDATE media_item SET added_at=? WHERE id=?", (now - 7200, orphan_item))
r = purge.sweep("report")
check("超過寬限期的空條目才算孤兒", r["found"]["item_nofile"] == 1, r["found"])
check("report 模式不會動手", db.q1("SELECT COUNT(*) c FROM media_item WHERE id=?",
                                   (orphan_item,))["c"] == 1)

# 前四類孤兒（指到不存在的東西）在 SQLite 開著外鍵時根本寫不進去。
# 它們是為了兩種情境存在的：規格 A-5 的 MSSQL 媒體庫刻意不做外鍵，
# 以及匯入／還原來的資料庫（外鍵是連線層級的設定，不是資料的性質）。
# 所以測試要**刻意**造出那個狀態，把外鍵關掉再寫。
dangling = mkfile(None, "/tv/dangling.mkv")
conn = db.get_conn()
conn.execute("PRAGMA foreign_keys=OFF")
try:
    conn.execute("UPDATE media_file SET item_id=? WHERE id=?", (999999, dangling))
    conn.execute("INSERT INTO play_state(file_id,position,duration,updated_at)"
                 " VALUES(999999,1,2,?)", (now,))
    conn.commit()
finally:
    conn.execute("PRAGMA foreign_keys=ON")
r = purge.sweep("report")
check("指到不存在條目的檔案算孤兒", r["found"]["file_item"] == 1, r["found"])
check("指到不存在檔案的播放進度算孤兒", r["found"]["play_file"] == 1, r["found"])

stale = mkimg("nobody_refs_me.jpg")
os.utime(IMAGE_DIR / stale, (time.time() - 7200, time.time() - 7200))
fresh = mkimg("just_written.jpg")
r = purge.sweep("report")
check("沒人參照又放超過一小時的圖片算孤兒", r["found"]["image_unused"] >= 1, r["found"])

r = purge.sweep("fix")
check(f"fix 模式清掉了（{r.get('fixed')}）", r["total"] > 0, r)
check("空條目被刪掉", db.q1("SELECT COUNT(*) c FROM media_item WHERE id=?",
                            (orphan_item,))["c"] == 0)
check("指到不存在條目的檔案改成 item_id=NULL，而不是把檔案刪掉",
      db.q1("SELECT item_id FROM media_file WHERE id=?", (dangling,))["item_id"] is None)
check("孤兒播放進度被刪掉",
      db.q1("SELECT COUNT(*) c FROM play_state WHERE file_id=999999")["c"] == 0)
check("放很久沒人用的圖片被刪掉", not (IMAGE_DIR / stale).is_file())
check("剛寫進去的圖片不能刪（可能正在被寫入資料庫的路上）",
      (IMAGE_DIR / fresh).is_file())

r2 = purge.sweep("report")
check("清完之後就沒有孤兒了", r2["total"] == 0, r2["found"])

# ============================================================ A-2 批次寫入
head("[A-2] 批次寫入")

commits = {"n": 0}
_real_tx = db.tx
from contextlib import contextmanager
@contextmanager
def counting_tx():
    commits["n"] += 1
    with _real_tx() as c:
        yield c
db.tx = counting_tx

def n_files():
    return db.q1("SELECT COUNT(*) c FROM media_file")["c"]

base = n_files()
commits["n"] = 0
with db.batch(size=500) as b:
    for i in range(300):
        b.upsert_file(item_id=None, episode_id=None, path=f"/batch/{i}.mkv",
                      name=f"{i}.mkv", size=100 + i, mtime="", mtime_ts=None,
                      ext="mkv", at=now)
    mid = n_files()
check("還沒滿之前不寫出去（緩衝中）", mid == base, (base, mid))
check("離開 with 就強制寫出", n_files() == base + 300, n_files() - base)
check(f"300 筆只用了 {commits['n']} 個交易，不是 300 個", commits["n"] == 1, commits["n"])

base = n_files()
commits["n"] = 0
with db.batch(size=100) as b:
    for i in range(250):
        b.upsert_file(item_id=None, episode_id=None, path=f"/batch2/{i}.mkv",
                      name=f"{i}.mkv", size=1, mtime="", mtime_ts=None, ext="mkv", at=now)
check("滿了就自動寫出（250 筆 / 每 100 筆 = 3 次）", commits["n"] == 3, commits["n"])
check("全部都寫進去了", n_files() == base + 250, n_files() - base)

# upsert：同一個路徑寫兩次是更新不是重複
base = n_files()
with db.batch() as b:
    b.upsert_file(item_id=None, episode_id=None, path="/batch/0.mkv", name="0.mkv",
                  size=99999, mtime="", mtime_ts=None, ext="mkv", at=now)
check("同一個路徑再寫一次是更新，不會變成兩列", n_files() == base, n_files() - base)
check("欄位真的被更新了",
      db.q1("SELECT size FROM media_file WHERE ftp_path='/batch/0.mkv'")["size"] == 99999)

# 出例外要丟掉緩衝，不能寫半套
base = n_files()
try:
    with db.batch(size=1000) as b:
        for i in range(10):
            b.upsert_file(item_id=None, episode_id=None, path=f"/oops/{i}.mkv",
                          name=f"{i}.mkv", size=1, mtime="", mtime_ts=None, ext="mkv", at=now)
        raise RuntimeError("掃到一半爆了")
except RuntimeError:
    pass
check("出例外時丟掉還沒寫出的部分", n_files() == base, n_files() - base)

# 順序：不同語句交錯時不能被重排
b = db.Batch(size=1000)
seq = []
b._add("A", (1,)); b._add("A", (2,)); b._add("B", (3,)); b._add("A", (4,))
runs, cur = [], None
for sql, _ in b._buf:
    if sql != cur:
        runs.append(sql); cur = sql
check("連續同語句才合併，順序不重排", runs == ["A", "B", "A"], runs)
b._buf.clear()

db.tx = _real_tx

print(f"\n{'=' * 52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
