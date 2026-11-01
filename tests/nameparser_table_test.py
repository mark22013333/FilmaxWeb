"""解析器表格測試：用真實的發行命名釘住 `nameparser.parse()` 的輸出（issue #14）。

每一列是 (檔名, 上層資料夾, 預期)。預期只寫有意義的欄位，沒寫的不檢查。
另外一段測 `TmdbClient._best` / `search` 的信心門檻 —— 不打網路，`_get` 換成假的。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-np-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "",
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


from app import nameparser                          # noqa: E402
from app.scraper import TmdbClient                  # noqa: E402

CASES = [
    # --- 高：括號裡的年份（Plex 標準命名） ---
    ("Inception (2010).mkv", [], dict(title="Inception", year=2010, is_tv=False)),
    ("The Matrix (1999) [1080p].mkv", [], dict(title="The Matrix", year=1999, is_tv=False)),
    ("Inception (2010) {imdb-tt1375666}.mkv", [], dict(title="Inception", year=2010)),
    ("[Group] Inception (2010) [1080p].mkv", [], dict(title="Inception", year=2010)),
    ("movie.mkv", ["Inception (2010)"], dict(title="Inception", year=2010)),
    # --- 高：連字號片名不是壓制組 ---
    ("Spider-Man.2002.mkv", [], dict(title="Spider-Man", year=2002, is_tv=False)),
    ("X-Men.2000.1080p.BluRay.x264.mkv", [], dict(title="X-Men", year=2000)),
    ("Spider-Man.2002.1080p.BluRay.x264-SPARKS.mkv", [], dict(title="Spider-Man", year=2002)),
    ("X-Men.mkv", [], dict(title="X-Men", year=None, is_tv=False)),
    ("Spider-Man.No.Way.Home.2021.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX.mkv", [],
     dict(title="Spider-Man No Way Home", year=2021)),
    # 真正的壓制組還是要拿掉
    ("The.Matrix.1080p.BluRay.x264-SPARKS.mkv", [], dict(title="The Matrix")),
    ("Show.S01E02.720p.HDTV.x264-KILLERS.mkv", [],
     dict(title="Show", season=1, episode=2, is_tv=True)),
    # --- 高：WALL-E 不是第 1080 集 ---
    ("WALL-E.1080p.BluRay.mkv", [], dict(title="WALL-E", is_tv=False, episode=None)),
    ("WALL-E.2008.1080p.BluRay.mkv", [], dict(title="WALL-E", year=2008, is_tv=False)),
    # 集數判斷別誤傷正常的寫法
    ("Show.E05.mkv", [], dict(title="Show", episode=5, is_tv=True)),
    ("Show EP12.mkv", [], dict(title="Show", episode=12, is_tv=True)),
    ("Show.Episode.3.mkv", [], dict(title="Show", episode=3, is_tv=True)),
    ("Breaking.Bad.S05E14.1080p.WEB-DL.mkv", [],
     dict(title="Breaking Bad", season=5, episode=14, is_tv=True)),
    # --- 中：片名就是年份 ---
    ("1917.2019.1080p.WEB-DL.mkv", [], dict(title="1917", year=2019, is_tv=False)),
    ("Blade.Runner.2049.2017.1080p.BluRay.mkv", [], dict(title="Blade Runner 2049", year=2017)),
    ("2012.mkv", [], dict(title="2012", year=None)),
    ("2012.2009.1080p.BluRay.mkv", [], dict(title="2012", year=2009)),
    ("Inception.2010.1080p.BluRay.x264.mkv", [], dict(title="Inception", year=2010)),
    # --- 既有行為：中英並列、季資料夾 ---
    ("盜夢空間.Inception.2010.1080p.mkv", [], dict(title="盜夢空間", alt_title="Inception", year=2010)),
    ("01.mkv", ["Doctor Who (2005)", "Season 1"], dict(title="Doctor Who", season=1, episode=1)),
]


print("\n[解析器表格]")
for fname, dirs, want in CASES:
    p = nameparser.parse(fname, dirs)
    got = {k: getattr(p, k) for k in want}
    check(f"{'/'.join(dirs + [fname])}", got == want, f"預期 {want}，實際 {got}")


# --------------------------------------------------------------------------
# TMDB 比對信心門檻
# --------------------------------------------------------------------------
def r(id, title, date, orig=None, pop=10.0, votes=100):
    return {"id": id, "title": title, "original_title": orig or title,
            "release_date": date, "popularity": pop, "vote_count": votes}


print("\n[_best 信心]")
hit, sure = TmdbClient._best([r(1, "Inception", "2010-07-15")], "Inception", 2010, "movie")
check("片名、年份都對 → 有把握", hit["id"] == 1 and sure, (hit, sure))
hit, sure = TmdbClient._best([r(1, "全面啟動", "2010-07-15", "Inception")], "Inception", 2010, "movie")
check("比對原文片名 → 有把握", sure, (hit, sure))
hit, sure = TmdbClient._best([r(1, "Spider-Man", "2002-05-01")], "spider man", 2002, "movie")
check("標點不同也算同名", sure, (hit, sure))
hit, sure = TmdbClient._best([r(7, "Totally Different", "1985-01-01", pop=90)],
                             "Inception", 2010, "movie")
check("片名、年份都不像 → 不確定（但仍回最接近的）", hit["id"] == 7 and not sure, (hit, sure))
hit, sure = TmdbClient._best([r(7, "Totally Different", "2010-01-01")], "Inception", None, "movie")
check("沒年份、片名不像 → 不確定", not sure, (hit, sure))
hit, sure = TmdbClient._best([r(3, "Inception: The Cobol Job", "2003-01-01")], "Inception", 2010, "movie")
check("片名只是部分相符、年份差很多 → 不確定", not sure, (hit, sure))
hit, sure = TmdbClient._best([r(5, "全面啟動", "2010-07-15", "Inception")], "盜夢空間", 2010, "movie")
check("片名對不上但唯一結果年份完全相符 → 有把握", sure, (hit, sure))
hit, sure = TmdbClient._best([r(5, "Something", "2010-07-15", pop=1)], "", 2010, "movie")
check("空片名不白拿片名分（仍可靠年份）", hit["id"] == 5, (hit, sure))


print("\n[search 不在第一個有結果的查詢就停]")


class FakeTmdb(TmdbClient):
    def __init__(self, table):
        super().__init__(api_key="x", language="zh-TW", fallback="en-US")
        self.table, self.calls = table, []

    def _get(self, path, **params):
        self.calls.append((params["query"], params.get("year"), params["language"]))
        return {"results": self.table.get((params["query"], params.get("year"), params["language"]), [])}


fake = FakeTmdb({
    # 中文查詢只撈到不相干的片
    ("盜夢空間", 2010, "zh-TW"): [r(99, "盜夢偵探", "2006-01-01")],
    ("Inception", 2010, "zh-TW"): [r(27205, "全面啟動", "2010-07-15", "Inception")],
})
hit, sure = fake.match("movie", "盜夢空間", 2010, "Inception")
check("第一個查詢不確定時繼續試英文片名", hit and hit["id"] == 27205 and sure, (hit, sure, fake.calls))
check("search() 仍回傳 dict（相容舊呼叫）",
      (fake.search("movie", "盜夢空間", 2010, "Inception") or {}).get("id") == 27205)

fake = FakeTmdb({("Inception", 2010, "zh-TW"): [r(27205, "全面啟動", "2010-07-15", "Inception")]})
fake.match("movie", "Inception", 2010)
check("第一個查詢就有把握時不多打 API", len(fake.calls) == 1, fake.calls)

fake = FakeTmdb({("Nope", 2010, "zh-TW"): [r(1, "Other", "1990-01-01")]})
hit, sure = fake.match("movie", "Nope", 2010)
check("全部都不像 → 回最接近的 + 不確定", hit and hit["id"] == 1 and not sure, (hit, sure))

fake = FakeTmdb({})
check("完全沒結果 → (None, False)", fake.match("movie", "Nope", 2010) == (None, False))

print(f"\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
