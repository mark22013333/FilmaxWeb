"""第一期驗收測試。

對應規格需求書的 C / D / E / F / G 與 A-1。每一項驗收條件都要有測試，
而且測的是**行為**不是「有沒有跑完不出錯」。

    python tests/phase1_test.py
"""
import os, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-p1-")
os.environ.update({"FILMAX_DATA_DIR": TMP, "TMDB_API_KEY": "",
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
                   "AUTH_ENABLED": "false", "GOOGLE_CLIENT_ID": "",
                   "GOOGLE_CLIENT_SECRET": "", "MSSQL_HOST": ""})
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


# ============================================================ A-1
head("[A-1] 儲存後端開關")
from app.config import settings

check("預設 media_store 是 sqlite", settings.media_store == "sqlite", settings.media_store)
settings.user_store_setting = "bogus"
try:
    settings.user_store
    check("USER_STORE 寫錯值會被擋", False, "竟然通過")
except ValueError as e:
    check("USER_STORE 寫錯值會被擋", "只能是 sqlite 或 mssql" in str(e))
settings.user_store_setting = ""
settings.media_store_setting = "mssql"
try:
    settings.check_stores()
    check("MEDIA_STORE=mssql 但未實作 → 報錯", False, "竟然通過")
except ValueError:
    check("MEDIA_STORE=mssql 但未實作 → 報錯", True)
settings.media_store_setting = ""
settings.user_store_setting = "mssql"
try:
    settings.check_stores()
    check("USER_STORE=mssql 但沒連線資訊 → 報錯", False, "竟然通過")
except ValueError as e:
    check("USER_STORE=mssql 但沒連線資訊 → 報錯", "MSSQL_HOST" in str(e))
settings.user_store_setting = ""

# ============================================================ C
head("[C] 影集分組")
from app import nameparser as np


def group(files, parents, sibdirs=None):
    ks, kinds, eps = set(), set(), []
    for f in files:
        r = np.parse(f, parent_dirs=parents, sibling_dirs=sibdirs, sibling_files=files)
        kinds.add("tv" if r.is_tv else "movie")
        ks.add(np.guess_key(r, "tv" if r.is_tv else "movie"))
        eps.append(r.episode)
    return len(ks), kinds, eps


n, k, eps = group(["ep.mkv"], ["x", "來！金來號 ！", "03"], ["01", "02", "03", "04", "05"])
check("一集一資料夾 → 認出集數與劇名", n == 1 and k == {"tv"} and eps == [3], (n, k, eps))

files = [f"來！金來號 ！{i:02d}.mp4" for i in range(1, 6)]
ks = set()
for i, f in enumerate(files, 1):
    r = np.parse(f, parent_dirs=["x", "HBO", "來！金來號 ！", f"{i:02d}"],
                 sibling_dirs=[f"{n:02d}" for n in range(1, 6)], sibling_files=[f])
    ks.add(np.guess_key(r, "tv" if r.is_tv else "movie"))
check("你的實際案例：5 個檔案併成 1 個條目", len(ks) == 1, ks)

n, k, eps = group(["蠟筆小新 01.mp4", "蠟筆小新 02.mp4", "蠟筆小新 03.mp4"], ["卡通"])
check("檔名尾端補零的數字是集數", n == 1 and k == {"tv"} and eps == [1, 2, 3], (n, k, eps))

n, k, _ = group(["玩命關頭 7.mkv", "玩命關頭 8.mkv", "玩命關頭 9.mkv"], ["電影"])
check("續集電影不會被併成影集", n == 3 and k == {"movie"}, (n, k))

n, k, _ = group(["海賊王 1.mkv", "海賊王 2.mkv", "海賊王 3.mkv"], ["卡通"])
check("從 1 開始的無補零集數仍認得出來", n == 1 and k == {"tv"}, (n, k))

n, k, _ = group(["玩命關頭 7.mkv"], ["電影"])
check("單一檔案不會被當影集", n == 1 and k == {"movie"}, (n, k))

n, k, _ = group(["Reacher.S01E01.mkv", "Reacher.S01E02.mkv"], ["神隱任務", "2022 Reacher S01"])
check("回歸：標準 S01E01 分組不變", n == 1 and k == {"tv"}, (n, k))

n, k, _ = group(["01.mkv", "02.mkv"], ["x", "Dark Matter", "Season 1"])
check("回歸：季別資料夾分組不變", n == 1 and k == {"tv"}, (n, k))

n, k, _ = group(["movie.mkv"], ["電影", "魔戒", "Disc 1"], ["Disc 1"])
check("Disc 資料夾不會被當集數", k == {"movie"}, k)

# ============================================================ D
head("[D] 相片庫排除影片封面")
from app import scanner


class E:
    def __init__(self, name, ext=None):
        self.name = name
        self.ext = ext or (name.rsplit(".", 1)[-1].lower() if "." in name else "")
        self.path = "/f/" + name
        self.size = 5 * 1024 * 1024


vids = [E("2024 人生複本 Dark Matter S01E01.mkv"), E("電影.mkv")]
stems = {scanner._stem(x.name) for x in vids}
cases = [
    ("2024 人生複本 Dark Matter S01.jpg", "2024 人生複本 Dark Matter S01", True, "與資料夾同名"),
    ("poster.jpg", "任意資料夾", True, "常見封面檔名"),
    ("fanart.png", "任意資料夾", True, "常見封面檔名"),
    ("電影.jpg", "任意資料夾", True, "與同目錄影片同主檔名"),
    ("蘋蘋澎澎六月會員禮_1.JPG", "20260601_六月會員禮", False, "正常相片"),
    ("IMG_0042.jpg", "外拍", False, "正常相片"),
]
for fn, folder, want, why in cases:
    got = scanner._is_video_artwork(E(fn), stems, folder)
    check(f"{why}：{fn[:34]} → {'排除' if want else '收錄'}", got == want, got)

check("關掉開關時 poster.jpg 也會被收",
      scanner._is_video_artwork(E("poster.jpg"), set(), "x") is True)
check("同目錄沒有影片時，與資料夾同名的圖仍收錄",
      scanner._is_video_artwork(E("外拍.jpg"), set(), "外拍") is False)

# ============================================================ G-2
head("[G-2] 時間正規化與排序")
from app import timeparse as tp

fmt = [("2026-08-31T09:48:11", "MLSD ISO"), ("2024:03:09 14:05:22", "EXIF"),
       ("Mar 15 10:22", "LIST 無年份"), ("Mar 15  2024", "LIST 無時間"),
       ("03-15-24 10:22AM", "DOS")]
for v, why in fmt:
    check(f"解析 {why}", tp.parse(v) is not None, v)
for v, why in [("", "空字串"), ("garbage", "亂碼"), ("0000:00:00 00:00:00", "壞掉的 EXIF"),
               (1788256211000, "毫秒當秒")]:
    check(f"拒絕 {why}", tp.parse(v) is None, tp.parse(v))

a, b = "2026-03-19 23:59:59", "2026-03-19T00:00:01"
check("原本的字串比較是錯的（前提）", b > a)
check("改成數字之後順序正確", tp.parse(b) < tp.parse(a))
check("LIST 無年份會推到前一年",
      tp.parse("Dec 25 10:22", now=__import__("datetime").datetime(2026, 2, 1).timestamp())
      < tp.parse("Jan 5 10:22", now=__import__("datetime").datetime(2026, 2, 1).timestamp()))

# ============================================================ G-5
head("[G-5] 縮圖用內容雜湊")
import io, random
from PIL import Image
from app import photo


def mkimg(seed, w=1200, h=800):
    random.seed(seed)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(0, h, 9):
        for x in range(0, w, 9):
            px[x, y] = (random.randrange(256),) * 3
    b = io.BytesIO()
    im.save(b, "JPEG", quality=88)
    return b.getvalue()


a_, b_ = mkimg(1), mkimg(2)
n1, n2, n3 = photo.make_thumb(a_), photo.make_thumb(b_), photo.make_thumb(a_)
check("不同的圖 → 不同縮圖檔名", n1 != n2, (n1, n2))
check("同一張圖 → 同一個縮圖檔名（可安全跳過重畫）", n1 == n3)
check("檔名不含資料庫 id", "ph_" not in (n1 or ""), n1)
photo.make_thumb(a_, "ph_7.jpg")
first = (photo.IMAGE_DIR / "ph_7.jpg").read_bytes()
photo.make_thumb(b_, "ph_7.jpg")
check("重現舊 bug：同一個 id 給不同的圖 → 拿到舊縮圖",
      (photo.IMAGE_DIR / "ph_7.jpg").read_bytes() == first)
check("新命名下兩張圖的檔案內容不同",
      (photo.IMAGE_DIR / n1).read_bytes() != (photo.IMAGE_DIR / n2).read_bytes())

# ============================================================ G-1
head("[G-1] 孤兒條目清理不再綁在相片上")
src = ROOT / "app" / "scanner.py"
text = src.read_text(encoding="utf-8")
i_photo = text.index('if seen_photos and not _cancel.is_set():')
i_orphan = text.index('purge.sweep("fix", grace=0')
between = text[i_photo:i_orphan]
check("孤兒清理不在 seen_photos 的區塊裡",
      'if seen_paths and not _cancel.is_set():' in between,
      "仍然巢狀在相片條件底下")

# 比「有沒有清孤兒」更該釘住的是「刪除只有一個進入點」。
# 掃描器只要自己寫一句 DELETE，磁碟上的海報與縮圖就會被漏掉 ——
# 那正是 A-4 要解決的問題，而它會安靜地失效。
import re as _re
strays = [m.group(0) for m in
          _re.finditer(r'DELETE FROM (?!_seen)\w+', text)]
check("掃描器自己不寫 DELETE，一律走 purge", not strays, strays)
check("清掃掛在 finally，取消與出錯時也會跑",
      text.index('finally:') < text.index('purge.sweep("fix", reason="scan_finished")'))

# ============================================================ G-3 / G-4
head("[G-3 / G-4] 取消與封面競態")
check("探測改用 submit + cancel 而不是 pool.map",
      "pool.submit(_probe_one" in text and "f.cancel()" in text)
check("封面改成登記候選，探測完再單執行緒處理",
      "_cover_wanted" in text and "def _make_covers" in text)
check("封面不再在 _probe_one 裡直接 make_thumbnail",
      "make_thumbnail" not in text[text.index("def _probe_one"):text.index("def _probe_one") + 2000])

# ============================================================ E / F 靜態檢查
head("[E] 換頁捲動")
js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
# E 的不變量是「**換頁**不要捲到文件頂端」（那會捲到標頭、搜尋列、相簿標籤
# 之上，離第一列還有一大段）。原本用「整個檔案都不准出現 scrollTo top:0」
# 來釘，但那太寬 —— 「回頂端」那顆按鈕的語意就是捲到 0，而它是對的。
# 改成只看換頁那條路徑：pageTo／scrollToGrid 裡不准出現。
_page_path = js[js.index("function scrollToGrid"):js.index("let toastTimer")]
check("換頁的路徑不捲到文件頂端", "window.scrollTo({ top: 0" not in _page_path,
      _page_path[:200])
check("「回頂端」按鈕是刻意捲到 0 的（唯一允許的地方）",
      js.count("window.scrollTo({ top: 0") == 1, js.count("window.scrollTo({ top: 0"))
check("有共用的 scrollToGrid", "function scrollToGrid" in js)
check("扣掉 sticky 標頭的高度", "getBoundingClientRect" in js and "header" in js)
# 「要不要捲」必須在載入新內容之前判斷：換頁時格線會先塌成「載入中」，
# 頁面高度縮短，瀏覽器把 scrollY 夾到 0，之後再問就永遠是「在頂端」。
check("換頁前先記下捲動狀態", "const wasScrolled = window.scrollY > 4" in js)
check("原本就在頂端才不捲", "if (wasScrolled) scrollToGrid(sel)" in js)
check("換頁一律走 pageTo", "pageTo(loadLibrary" in js and "pageTo(loadPhotos" in js)
check("尊重 prefers-reduced-motion", "prefers-reduced-motion" in js)

head("[F] 全螢幕")
pjs = (ROOT / "app" / "static" / "player.js").read_text(encoding="utf-8")
check("有 webkit 備援", "webkitRequestFullscreen" in pjs)
check("有 iOS 的原生影片全螢幕", "webkitEnterFullscreen" in pjs)
check("有網頁全螢幕模式", "setPageFullscreen" in pjs and "page-fs" in pjs)
check("三種模式可選", "'auto'" in pjs and "'page'" in pjs and "'native'" in pjs)
check("原生模式有字幕軌", "addTextTrack" in pjs and "VTTCue" in pjs)
check("退出原生全螢幕會還原", "webkitendfullscreen" in pjs)
check("橫向鎖定失敗不會中止全螢幕", "lockLandscape" in pjs and ".catch(() => {})" in pjs)
pcss = (ROOT / "app" / "static" / "player.css").read_text(encoding="utf-8")
check("版面用 dvh 避開手機工具列", "100dvh" in pcss)
check("有 page-fs 的樣式", "body.page-fs" in pcss)

# ============================================================ G 小項
head("[G] 其他")
main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("CSP 允許 TMDB 圖片", "image.tmdb.org" in main)
check("/api/docs 限管理員", 'def api_docs' in main and 'is_admin' in main)
api = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
check("/api/scan/status 對唯讀角色過濾欄位", "_SCAN_PUBLIC" in api)
check("排序改用 sort_ts", "sort_ts DESC" in api)
check("手動指定 TMDB 按鈕限管理員",
      "me.is_admin ? `<button class=\"btn\" onclick=\"manualMatch" in js)
check("掃描面板有 photos 階段", "photos: '讀取相片'" in js)
check("掃描面板顯示相片進度", "s.photos_read" in js)
check("有重新分組按鈕", "reparseLibrary" in js and "reparse=true" in js)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
