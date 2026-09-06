"""掃描的收錄判斷：大小門檻與它的目錄豁免。

    python tests/scan_filter_test.py

為什麼需要豁免名單：`MIN_FILE_MB` 是個啟發式（「太小的大概是預告片」），
在某些目錄裡就是猜錯。而唯一的替代做法 —— 把全域門檻調低 —— 實測會多收
2,215 個 1～50MB 的影片，其中 2,201 個（99.4%）是手機錄的短片，
真正想收的只有 13 個。門檻本身是對的，要的是「這幾個目錄例外」。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-scanf-")).resolve())
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "MIN_FILE_MB": "50",
    "SCAN_EXCLUDE_EXTS": "iso,ts,m2ts,xci,rar",
    "SCAN_MIN_SIZE_EXEMPT_DIRS": "白上咲花,shorts",
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


from app import scanner            # noqa: E402
from app.config import settings    # noqa: E402

MB = 1024 * 1024


class F:
    """`ftpclient.FtpEntry` 的最小替身 —— `_wanted` 只用到這四個欄位。"""

    def __init__(self, path, size):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.ext = self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""
        self.size = size


head("[O] 設定解析")

check("豁免名單解析成小寫 tuple",
      settings.min_size_exempt_dirs == ("白上咲花", "shorts"),
      settings.min_size_exempt_dirs)
check("跟 SCAN_EXCLUDE_DIR_PREFIXES 共用同一套解析（使用者不必記兩套規則）",
      settings._dir_prefixes("A, b ;C") == ("a", "b", "c"),
      settings._dir_prefixes("A, b ;C"))
check("空字串 = 沒有任何豁免", settings._dir_prefixes("") == ())
check("資料夾名稱可以有空白（不像副檔名那樣拿空白當分隔）",
      settings._dir_prefixes("New Folder,未 分類") == ("new folder", "未 分類"))


head("[O] 大小門檻與豁免")

cases = [
    # (要收嗎, 路徑, 大小, 說明)
    (True,  "/媒體資料庫/電影/big.mp4", 500 * MB, "一般目錄的大檔照收"),
    (False, "/媒體資料庫/電影/small.mp4", 2 * MB, "一般目錄的小檔擋掉"),
    (True,  "/pic/cent/白上咲花/a.mp4", 2 * MB, "豁免目錄裡的小檔要收"),
    (True,  "/pic/cent/白上咲花/sub/b.mp4", 1, "豁免目錄的子目錄也算（含子資料夾）"),
    (True,  "/x/SHORTS/c.mp4", 1 * MB, "比對大小寫不分"),
    (True,  "/x/白上咲花2024/d.mp4", 1 * MB, "比對的是名稱開頭（前綴）"),
    (False, "/x/我的白上咲花/e.mp4", 1 * MB, "前綴是開頭，不是任意位置"),
    (True,  "/shorts/deep/a/b/c/f.mp4", 1, "放在哪一層都有效"),
]
for want, path, size, label in cases:
    got = scanner._wanted(F(path, size))
    check(label, got == want, f"{path} ({size}) → {got}，預期 {want}")


head("[O] 豁免的邊界：只放寬大小，其餘一概不放寬")

# **這是這個功能最重要的一條界線。**大小門檻是啟發式，在某些目錄裡會猜錯；
# 而副檔名排除清單是使用者明講「這種格式我不要」—— 那句話不會因為換個目錄
# 就改變。兩者性質不同，所以豁免只碰大小。
check("豁免目錄裡的 .iso 仍然排除（副檔名清單優先）",
      scanner._wanted(F("/pic/cent/白上咲花/x.iso", 900 * MB)) is False)
check("豁免目錄裡的 .rar 仍然排除",
      scanner._wanted(F("/pic/cent/白上咲花/y.rar", 2 * MB)) is False)
check("豁免目錄裡的非影片副檔名仍然不收",
      scanner._wanted(F("/pic/cent/白上咲花/z.txt", 900 * MB)) is False)

# 只比目錄，不比檔名 —— 否則一個叫「白上咲花.mp4」的檔案放在任何地方都會被豁免
check("只比目錄名，不比檔名",
      scanner._wanted(F("/媒體資料庫/白上咲花.mp4", 2 * MB)) is False,
      "檔名剛好等於豁免名稱時不該被豁免")
check("_size_exempt 本身也不看檔名",
      scanner._size_exempt("/媒體資料庫/白上咲花.mp4") is False)
check("_size_exempt 認得目錄", scanner._size_exempt("/pic/cent/白上咲花/a.mp4") is True)


head("[O] 沒有設定時的行為要跟以前一模一樣")

os.environ["SCAN_MIN_SIZE_EXEMPT_DIRS"] = ""
check("名單空的時候 _size_exempt 一律 False",
      scanner._size_exempt("/pic/cent/白上咲花/a.mp4") is False)
check("名單空的時候小檔照樣被門檻擋掉（不能因為加了這個功能就改變預設行為）",
      scanner._wanted(F("/pic/cent/白上咲花/a.mp4", 2 * MB)) is False)
check("名單空的時候大檔照收",
      scanner._wanted(F("/媒體資料庫/big.mp4", 500 * MB)) is True)
os.environ["SCAN_MIN_SIZE_EXEMPT_DIRS"] = "白上咲花,shorts"


head("[O] 這一項是 HOT 參數（後台改完即時生效，不必重啟）")

from app import params  # noqa: E402
row = params.REGISTRY.get("SCAN_MIN_SIZE_EXEMPT_DIRS")
check("參數頁裡有這一項（不然只能改 .env）", row is not None)
if row:
    check("套用方式是 hot", row.apply == params.APPLY_HOT, row.apply)
    check("歸在「媒體庫」分區", row.section == "媒體庫", row.section)
    check("跟 SCAN_EXCLUDE_DIR_PREFIXES 同一層 Tier（兩個設定長得一樣）",
          row.tier == params.REGISTRY["SCAN_EXCLUDE_DIR_PREFIXES"].tier,
          (row.tier, params.REGISTRY["SCAN_EXCLUDE_DIR_PREFIXES"].tier))
check("設定是即時解析的 property，不是 dataclass 欄位（改了才會生效）",
      "scan_min_size_exempt_dirs" in __import__("app.config", fromlist=["_HOT_ATTRS"])._HOT_ATTRS)

env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
check(".env.example 有寫（env_drift_test 會擋，但這裡也釘一次）",
      "SCAN_MIN_SIZE_EXEMPT_DIRS=" in env_example)


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
