"""改了 SCAN_JAV_DIRS / SCAN_HOME_DIRS 之後，一般掃描就要把既有檔案重新歸類。

    python tests/jav_reclassify_test.py

實際發生過：先掃過片庫、之後才設 SCAN_JAV_DIRS=vv，結果 vv 底下 44 支影片
全部維持 kind=movie —— 大小沒變的檔案在 _index_file 一進門就早退，
目錄設定改了也輪不到重新判斷。JAV 一筆都沒有，三支還被 TMDB 配到錯的片。
不連網：用一份只有 extract_number 的假 OpenAver。
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="filmax-javre-")).resolve()
FAKE = TMP / "OpenAver"
(FAKE / "core" / "scrapers").mkdir(parents=True)
(FAKE / "core" / "__init__.py").write_text("", encoding="utf-8")
(FAKE / "core" / "scraper.py").write_text("def search_jav(n, **kw):\n    return None\n",
                                          encoding="utf-8")
(FAKE / "core" / "scrapers" / "__init__.py").write_text("", encoding="utf-8")
(FAKE / "core" / "scrapers" / "utils.py").write_text(
    "import re\n"
    "def extract_number(name):\n"
    "    m = re.search(r'([A-Z]{2,5}-\\d{3,5})', name)\n"
    "    return m.group(1) if m else None\n", encoding="utf-8")

os.environ.update({
    "FILMAX_DATA_DIR": str(TMP / "data"),
    "FILMAX_ENV_FILE": str(TMP / "no-such.env"),
    "OPENAVER_PATH": str(FAKE),
    "SCAN_JAV_DIRS": "", "SCAN_HOME_DIRS": "",
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
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


def head(t):
    print(f"\n{t}")


from app import db, ftpclient, scanner  # noqa: E402
from app.config import LibraryRoot      # noqa: E402

db.init_db()
LIB = LibraryRoot("/媒體櫃", "auto")


def scan(*paths):
    """跑一次「一般掃描」的索引步驟：同樣的路徑、同樣的大小、不帶 reparse。"""
    scanner._load_registry()
    with db.batch() as batch:
        for p in paths:
            scanner._index_file(ftpclient.FtpEntry(p.rsplit("/", 1)[-1], p, False, 100),
                                LIB, reparse=False, batch=batch)


def item_of(path):
    return db.q1("SELECT i.kind, i.guess_key, i.scrape_state FROM media_file f "
                 "JOIN media_item i ON i.id=f.item_id WHERE f.ftp_path=?", (path,))


JAV = "/媒體櫃/100.private/vv/REBD-1067 Emika3.mp4"
TRAILER = "/媒體櫃/100.private/vv/trailer.mp4"
HOME = "/媒體櫃/家庭/20211002_213546+0800.mp4"


head("[R] 設定之前掃過：照一般影片歸類")
scan(JAV, TRAILER, HOME)
check("番號檔是 movie", item_of(JAV)["kind"] == "movie", dict(item_of(JAV)))
check("錄影檔是 movie", item_of(HOME)["kind"] == "movie", dict(item_of(HOME)))


head("[R] 之後才設目錄：一般掃描就重新歸類，不必按「重新分組」")
os.environ["SCAN_JAV_DIRS"] = "vv"
os.environ["SCAN_HOME_DIRS"] = "家庭"
scan(JAV, TRAILER, HOME)
r = item_of(JAV)
check("番號檔變成 jav", r["kind"] == "jav", dict(r))
check("用番號當 key", r["guess_key"] == "jav::REBD-1067", dict(r))
check("等著被 JAV 來源配對", r["scrape_state"] == "pending", dict(r))
check("同目錄裡沒番號的檔案不受影響", item_of(TRAILER)["kind"] == "movie",
      dict(item_of(TRAILER)))
check("錄影檔變成 home", item_of(HOME)["kind"] == "home", dict(item_of(HOME)))


head("[R] 已經歸類好的再掃一次不會變")
scan(JAV, TRAILER, HOME)
check("還是 jav", item_of(JAV)["guess_key"] == "jav::REBD-1067", dict(item_of(JAV)))
check("還是 home", item_of(HOME)["kind"] == "home", dict(item_of(HOME)))


head("[R] 把目錄從設定拿掉：歸類跟著退回")
os.environ["SCAN_JAV_DIRS"] = ""
os.environ["SCAN_HOME_DIRS"] = ""
scan(JAV, TRAILER, HOME)
check("番號檔回到 movie", item_of(JAV)["kind"] == "movie", dict(item_of(JAV)))
check("錄影檔回到 movie", item_of(HOME)["kind"] == "movie", dict(item_of(HOME)))


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
