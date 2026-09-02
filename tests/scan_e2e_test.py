"""掃描流程的端對端測試：架一台真的 FTP 伺服器，放進真的檔案，跑真的掃描。

前面的 phase1_test 有一半是「原始碼有沒有這段字」的靜態檢查 ——
那只證明改了，不證明會動。這一支證明會動：

  C   一集一資料夾的影集併成一個條目
  D   影片封面不會進相片庫，正常相片會
  G-1 沒有相片的片庫也會清掉孤兒條目
  G-2 時間欄位存成 epoch，排序用 sort_ts

    python tests/scan_e2e_test.py
"""
import io, os, random, shutil, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-e2e-")
FTPROOT = os.path.join(TMP, "ftp")
PORT = 2187

os.makedirs(FTPROOT)
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": str(PORT),
    "FTP_USER": "u", "FTP_PASSWORD": "p", "FTP_TLS": "false",
    "LIBRARY_ROOTS": "/|auto", "TMDB_API_KEY": "",
    "MIN_FILE_MB": "0", "MIN_PHOTO_KB": "1",
    "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")


# ---------------------------------------------------------------- 造素材
def vid(path, mb=1):
    """假的影片檔。ffprobe 會分析失敗，但索引與分組不需要它成功。"""
    full = os.path.join(FTPROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(os.urandom(mb * 1024 * 1024))


def img(path, seed=1, w=400, h=300):
    from PIL import Image
    full = os.path.join(FTPROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    random.seed(seed)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(0, h, 5):
        for x in range(0, w, 5):
            px[x, y] = (random.randrange(256),) * 3
    im.save(full, "JPEG", quality=85)


# C：一集一資料夾（你的實際結構）
for i in range(1, 6):
    vid(f"HBO/來！金來號 ！/{i:02d}/來！金來號 ！{i:02d}.mp4")
# C：標準 SxxExx（回歸）
for i in range(1, 4):
    vid(f"神隱任務/2022 神隱任務 Reacher S01/Reacher.S01E{i:02d}.1080p.mkv")
# C：續集電影，不該被併
for i in (7, 8, 9):
    vid(f"0.電影/玩命關頭 {i}.mkv")

# D：影片資料夾裡的封面圖（該排除）+ 純相片資料夾（該收）
img("神隱任務/2022 神隱任務 Reacher S01/2022 神隱任務 Reacher S01.jpg", 11)
img("HBO/來！金來號 ！/poster.jpg", 12)
for i in range(1, 5):
    img(f"寫真/外拍/IMG_{i:04d}.jpg", 20 + i)

# ---------------------------------------------------------------- FTP
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

_auth = DummyAuthorizer()
_auth.add_user("u", "p", FTPROOT, perm="elr")
FTPHandler.authorizer = _auth
_srv = FTPServer(("127.0.0.1", PORT), FTPHandler)
_srv.max_cons = 50
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(1)

from app import db, scanner
db.init_db()

print("[1] 第一次掃描")
scanner.start()
for _ in range(600):
    if not scanner.status.running:
        break
    time.sleep(0.1)
check("掃描有跑完", not scanner.status.running and scanner.status.phase in ("done", "photos"),
      scanner.status.phase)

# ---------------------------------------------------------------- C
print("\n[C] 影集分組")
rows = db.q("""SELECT i.id, i.kind, i.title, COUNT(f.id) n
               FROM media_item i LEFT JOIN media_file f ON f.item_id=i.id
               GROUP BY i.id ORDER BY i.title""")
for r in rows:
    print(f"      {r['kind']:5s} {r['title'][:34]:34s} {r['n']} 個檔案")
jin = [r for r in rows if "金來號" in r["title"]]
check("來！金來號 併成一個條目", len(jin) == 1, [r["title"] for r in jin])
check("而且是影集、有 5 個檔案", bool(jin) and jin[0]["kind"] == "tv" and jin[0]["n"] == 5,
      (jin[0]["kind"], jin[0]["n"]) if jin else None)
eps = db.q("SELECT season, episode FROM episode e JOIN media_item i ON i.id=e.item_id "
           "WHERE i.title LIKE '%金來號%' ORDER BY episode")
check("集數 1~5 都建立了", [e["episode"] for e in eps] == [1, 2, 3, 4, 5],
      [e["episode"] for e in eps])

rea = [r for r in rows if "Reacher" in r["title"] or "神隱" in r["title"]]
check("回歸：S01E01 那組仍是一個條目、3 個檔案",
      len(rea) == 1 and rea[0]["n"] == 3, [(r["title"], r["n"]) for r in rea])

fast = [r for r in rows if "玩命關頭" in r["title"]]
check("續集電影仍是 3 個獨立條目", len(fast) == 3, [r["title"] for r in fast])
check("而且是 movie 不是 tv", all(r["kind"] == "movie" for r in fast),
      [r["kind"] for r in fast])

# ---------------------------------------------------------------- D
print("\n[D] 相片庫")
photos = db.q("SELECT filename, folder FROM photo ORDER BY filename")
for p_ in photos:
    print(f"      {p_['filename'][:40]:40s} {p_['folder'][:34]}")
names = {p_["filename"] for p_ in photos}
check("影集封面沒有進相片庫", "2022 神隱任務 Reacher S01.jpg" not in names, names)
check("poster.jpg 沒有進相片庫", "poster.jpg" not in names)
check("正常相片 4 張都進來了",
      len([n for n in names if n.startswith("IMG_")]) == 4, names)
check("排除數有記錄", scanner.status.artwork_skipped == 2, scanner.status.artwork_skipped)

# ---------------------------------------------------------------- G-2
print("\n[G-2] 時間欄位")
r = db.q1("SELECT COUNT(*) n, COUNT(mtime_ts) m, COUNT(sort_ts) s FROM photo")
check("每張相片都有 mtime_ts 與 sort_ts", r["n"] == r["m"] == r["s"], dict(r))
r = db.q1("SELECT COUNT(*) n, COUNT(mtime_ts) m FROM media_file")
check("每個影片檔都有 mtime_ts", r["n"] == r["m"], dict(r))
bad = db.q1("SELECT COUNT(*) c FROM photo WHERE sort_ts < 631152000 OR sort_ts > 4102444800")
check("沒有超出合理範圍的時間值", bad["c"] == 0, bad["c"])

# ---------------------------------------------------------------- G-1
print("\n[G-1] 沒有相片時也要清孤兒條目")
db.execute("INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
           " VALUES('movie','孤兒條目','孤兒條目','orphan::test','pending',?,?)",
           (db.now(), db.now()))
oid = db.q1("SELECT id FROM media_item WHERE guess_key='orphan::test'")["id"]
db.execute("INSERT INTO episode(item_id,season,episode) VALUES(?,1,1)", (oid,))
# 把所有相片刪掉，模擬「純影片的片庫」
db.execute("DELETE FROM photo")
scanner.start()
for _ in range(600):
    if not scanner.status.running:
        break
    time.sleep(0.1)
gone = db.q1("SELECT COUNT(*) c FROM media_item WHERE guess_key='orphan::test'")["c"]
check("片庫裡沒有相片時，孤兒條目仍然被清掉", gone == 0, f"還剩 {gone} 筆")
orph_ep = db.q1("SELECT COUNT(*) c FROM episode WHERE item_id NOT IN (SELECT id FROM media_item)")
check("孤兒集數也被清掉", orph_ep["c"] == 0, orph_ep["c"])

# ---------------------------------------------------------------- reparse
print("\n[C] 重新分組")
# 模擬「舊版解析器留下的錯誤狀態」：把 5 集拆成 5 個獨立的電影條目，
# 就像修好之前那樣。這才是使用者真正會遇到的情況 ——
# 他的資料庫裡已經有 5 張卡了，改好程式之後要能把它們併回去。
files = db.q("SELECT id, filename FROM media_file WHERE filename LIKE '%金來號%' ORDER BY id")
check("前提：5 個檔案都在", len(files) == 5, len(files))
# 順序很重要：media_file.item_id 有 ON DELETE CASCADE，
# 先刪 media_item 會把底下的檔案一起帶走，模擬出來的就不是「錯誤分組」
# 而是「空片庫」。要先把檔案改指到新條目，最後才刪舊條目。
old_ids = [r["id"] for r in db.q("SELECT id FROM media_item WHERE title LIKE '%金來號%'")]
for i, f in enumerate(files, 1):
    title = f"來!金來號 !{i:02d}"
    cur = db.execute(
        "INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
        " VALUES('movie',?,?,?,'failed',?,?)",
        (title, title.lower(), f"movie::{title}::", db.now(), db.now()))
    db.execute("UPDATE media_file SET item_id=?, episode_id=NULL WHERE id=?",
               (cur.lastrowid, f["id"]))
for oid in old_ids:            # 檔案都改指走了，這時刪舊條目才安全
    db.execute("DELETE FROM episode WHERE item_id=?", (oid,))
    db.execute("DELETE FROM media_item WHERE id=?", (oid,))
split = db.q1("SELECT COUNT(*) c FROM media_item WHERE title LIKE '%金來號%'")["c"]
kept = db.q1("SELECT COUNT(*) c FROM media_file WHERE filename LIKE '%金來號%'")["c"]
check("模擬出舊版的錯誤狀態：5 個獨立條目", split == 5, split)
check("而且 5 個檔案都還在（沒有被 cascade 帶走）", kept == 5, kept)

scanner.start()          # 一般掃描：檔案大小沒變 → 早退，不會重新分組
for _ in range(600):
    if not scanner.status.running: break
    time.sleep(0.1)
still = db.q1("SELECT COUNT(*) c FROM media_item WHERE title LIKE '%金來號%'")["c"]
check("一般掃描修不好（早退仍然有效，這是預期行為）", still == 5, still)

scanner.start(reparse=True)
for _ in range(600):
    if not scanner.status.running: break
    time.sleep(0.1)
merged = db.q("""SELECT i.id, i.kind, i.title, COUNT(f.id) n
                 FROM media_item i LEFT JOIN media_file f ON f.item_id=i.id
                 WHERE i.title LIKE '%金來號%' GROUP BY i.id""")
check("重新分組把 5 個條目併回 1 個", len(merged) == 1,
      [(r["title"], r["n"]) for r in merged])
check("併回去之後是影集、5 個檔案",
      bool(merged) and merged[0]["kind"] == "tv" and merged[0]["n"] == 5,
      (merged[0]["kind"], merged[0]["n"]) if merged else None)
eps2 = db.q("SELECT episode FROM episode e JOIN media_item i ON i.id=e.item_id "
            "WHERE i.title LIKE '%金來號%' ORDER BY episode")
check("集數也重建了", [e["episode"] for e in eps2] == [1, 2, 3, 4, 5],
      [e["episode"] for e in eps2])

# ============================================================ A-2 批次寫入
print("\n[A-2] 重掃時的交易數")

# 為什麼要釘這個數字：批次寫入很容易「還在，但沒有生效」——
# 例如有人在迴圈裡多加一句 db.execute()，測試照樣全綠，
# 只是每個檔案又變回一次 fsync。那種退步除了量交易數以外看不出來。
from contextlib import contextmanager as _cm
_commits = {"n": 0}
_real_tx = db.tx
@_cm
def _counting():
    _commits["n"] += 1
    with _real_tx() as c:
        yield c
db.tx = _counting

rows = (db.q1("SELECT COUNT(*) c FROM media_file")["c"]
        + db.q1("SELECT COUNT(*) c FROM photo")["c"])
_commits["n"] = 0
scanner._scan_files(reparse=False)      # 什麼都沒變的重掃
db.tx = _real_tx
check(f"{rows} 列的重掃只用 {_commits['n']} 個交易（沒批次的話會是 {rows} 個）",
      _commits["n"] <= 12, _commits["n"])

try:
    _srv.close_all()
except Exception:
    pass
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
