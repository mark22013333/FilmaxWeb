"""在真的瀏覽器裡開播放頁，確認它真的載得起來、右上角真的講對畫質。

    python tests/player_page_test.py

`player_wiring_test.js` 用 DOM stub 測接線，那證明得了邏輯，但證明不了
「這個頁面在真的瀏覽器裡打得開」—— stub 少實作一個 API、CSP 擋掉新的
script、播放清單的載入順序寫反，這幾種在 stub 裡全都看不出來。這一支補那一半。

不需要真的影片：`play()` 走到 hls.js 才需要片子，而這裡要驗的是它之前那一段
（頁面載入、狀態初始化、右上角渲染）。假的媒體列 ＋ 假的 hls.js 就夠了。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-pp-")
PORT = 29337
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "HOST": "127.0.0.1", "PORT": str(PORT),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "REMOTE_MAX_HEIGHT": "720", "REMOTE_BITRATE_KBPS": "2800",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import db
db.init_db()
now = db.now()
cur = db.execute(
    "INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
    " VALUES('movie','測試片','t','movie::t::2020','ok',?,?)", (now, now))
FID = db.execute(
    """INSERT INTO media_file(item_id,ftp_path,filename,ext,size,duration,width,height,
                              video_codec,audio_codec,bitrate,probe_state,play_mode,
                              seen_at,added_at)
       VALUES(?,'/x/a.mkv','a.mkv','mkv',2000000000,3600,1920,804,'h264','aac',
              4000000,'ok','hls',?,?)""", (cur.lastrowid, now, now)).lastrowid

import uvicorn
from app.main import app
_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=_server.run, daemon=True).start()
for _ in range(100):
    if getattr(_server, "started", False):
        break
    time.sleep(0.1)
BASE = f"http://127.0.0.1:{PORT}"

# 假的 hls.js，在真的那一支載入之前先蓋掉。播放頁不需要真的解碼器 ——
# 要驗的是「事件進來之後畫面對不對」，而那跟解碼無關。
FAKE_HLS = """
window.Hls = class {
  constructor(cfg) { this.config = cfg; this._h = {}; this.levels = [];
    this.currentLevel = -1; this.autoLevelCapping = -1; this.autoLevelEnabled = true;
    this.bandwidthEstimate = 0; window.__hls = this; }
  on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); }
  emit(ev, d) { (this._h[ev] || []).forEach(fn => fn(ev, d)); }
  loadSource() {} attachMedia() {} startLoad() {} stopLoad() {} detachMedia() {}
  destroy() {} recoverMediaError() {} swapAudioCodec() {}
};
window.Hls.isSupported = () => true;
window.Hls.ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' };
window.Hls.Events = { MANIFEST_PARSED: 'hlsManifestParsed',
  LEVEL_SWITCHING: 'hlsLevelSwitching', LEVEL_SWITCHED: 'hlsLevelSwitched',
  FRAG_LOADING: 'hlsFragLoading', FRAG_LOADED: 'hlsFragLoaded',
  FRAG_BUFFERED: 'hlsFragBuffered', ERROR: 'hlsError' };
"""

LEVELS = [{"width": 640, "height": 268, "bitrate": 700000},
          {"width": 854, "height": 358, "bitrate": 1260000},
          {"width": 1280, "height": 536, "bitrate": 2800000}]


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1280, "height": 800})
        # 蓋掉 hls.js。**不能只用 add_init_script** —— 那只保證在頁面腳本之前跑，
        # 而真的 hls.min.js 之後才載入，會把假的覆蓋回去。改成攔截那個請求，
        # 直接用假的內容回應它（順帶也證明了 player.html 真的有去要那支檔案）。
        await ctx.route("**/static/hls.min.js",
                        lambda route: route.fulfill(
                            status=200, content_type="application/javascript",
                            body=FAKE_HLS))
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text)
              if m.type == "error" else None)

        head("[J] 播放頁在真的瀏覽器裡載得起來")
        await pg.goto(f"{BASE}/static/player.html?file={FID}", wait_until="networkidle")
        await pg.wait_for_function("() => window.__hls", timeout=8000)
        check("頁面載入沒有 JS 錯誤（CSP、載入順序、少實作的 API 都會在這裡現形）",
              not errs, errs[:3])
        check("playback-state.js 真的被載進來了",
              await pg.evaluate("() => !!window.PlaybackState"))
        check("player.js 掛得起來（有 __playerTestHooks 代表整支跑完了）",
              await pg.evaluate("() => !!window.__playerTestHooks"))

        head("[A] 開播當下不要拿片源的畫質冒充「正在播的」")
        tags = await pg.eval_on_selector("#tags", "e => e.textContent")
        check("片源是 1920×804（1080p），但還沒切好階 → 不能顯示 1080p",
              "1080p" not in tags, tags)
        check("只講「自動」", "自動" in tags, tags)

        head("[D][E] LEVEL_SWITCHED 之後右上角要立刻變")
        await pg.evaluate("""(levels) => {
            const h = window.__hls;
            h.levels = levels;
            h.emit('hlsManifestParsed', { levels });
            h.currentLevel = 2;
            h.emit('hlsLevelSwitched', { level: 2 });
        }""", LEVELS)
        tags = await pg.eval_on_selector("#tags", "e => e.textContent")
        check("切到 720p → 「自動 · 720p · 1280×536」",
              "自動 · 720p · 1280×536" in tags, tags)

        await pg.evaluate("""() => {
            const h = window.__hls;
            h.currentLevel = 1;
            h.emit('hlsLevelSwitched', { level: 1 });
        }""")
        tags = await pg.eval_on_selector("#tags", "e => e.textContent")
        check("[E] 降到 480p → 立刻變成 480p", "480p · 854×358" in tags, tags)
        check("[E] 舊的 720p 不留在畫面上", "720p" not in tags, tags)

        await pg.evaluate("""() => {
            const h = window.__hls;
            h.currentLevel = 2;
            h.emit('hlsLevelSwitched', { level: 2 });
        }""")
        tags = await pg.eval_on_selector("#tags", "e => e.textContent")
        check("[E] 升回 720p，UI 同步更新", "720p · 1280×536" in tags, tags)

        head("[J] 手機上那顆標籤不可以被 CSS 藏掉")
        # 原本的規則是 .tg:not(.hdr):not(.warn) 在 720px 以下 display:none
        # —— 手機遠端降階正是最需要看到它的情境，藏掉等於把這次的修正藏掉。
        await pg.set_viewport_size({"width": 390, "height": 844})
        await pg.wait_for_timeout(200)
        vis = await pg.eval_on_selector(
            "#tags .tg.live",
            "e => { const s = getComputedStyle(e);"
            "  return { d: s.display, w: e.getBoundingClientRect().width }; }")
        check("390px 寬的手機上「目前畫質」那顆仍然看得見",
              vis["d"] != "none" and vis["w"] > 0, vis)

        head("[8] debug 疊圖預設不出現，帶 ?debugPlayer=1 才出現")
        n = await pg.eval_on_selector_all("#dbg", "e => e.length")
        check("正式模式下畫面上沒有整排 debug 資訊", n == 0, n)

        pg2 = await ctx.new_page()
        errs2 = []
        pg2.on("pageerror", lambda e: errs2.append(str(e)))
        await pg2.goto(f"{BASE}/static/player.html?file={FID}&debugPlayer=1",
                       wait_until="networkidle")
        await pg2.wait_for_function("() => window.__hls", timeout=8000)
        await pg2.wait_for_selector("#dbg", timeout=5000)
        txt = await pg2.eval_on_selector("#dbg", "e => e.textContent")
        check("?debugPlayer=1 時疊圖出現，而且列得出診斷欄位",
              "Bandwidth est" in txt and "Buffer" in txt and "Mobile" in txt, txt[:200])
        check("debug 模式也沒有 JS 錯誤", not errs2, errs2[:3])

        head("[F] 面板要把「選的上限」與「目前實際」分開列")
        await pg2.evaluate("""(levels) => {
            const h = window.__hls;
            h.levels = levels;
            h.emit('hlsManifestParsed', { levels });
            h.currentLevel = 1;                     // 實際只播得動 480p
            h.emit('hlsLevelSwitched', { level: 1 });
            window.__playerTestHooks.cfg.quality = 1080;   // 使用者選 1080p 上限
            window.__playerTestHooks.updatePlaybackState({ selectedQuality: 1080 });
        }""", LEVELS)
        tags = await pg2.eval_on_selector("#tags", "e => e.textContent")
        check("右上角同時講「上限 1080p」與「目前 480p」",
              "上限 1080p" in tags and "目前 480p" in tags, tags)

        await pg2.click("#btnSettings")
        await pg2.wait_for_selector("#playbackRows", timeout=5000)
        rows = await pg2.eval_on_selector("#playbackRows", "e => e.textContent")
        for label in ("片源", "選擇的畫質", "目前實際畫質", "實際解析度",
                      "HLS level", "Variant bitrate", "估計頻寬", "Buffer 秒數",
                      "播放模式"):
            check(f"面板有「{label}」這一列", label in rows, rows[:200])
        check("「片源」講 1080p（片源是 1920×804）", "1080p" in rows, rows[:300])
        check("而且「目前實際畫質」講 480p —— 兩者刻意不混在一起",
              "480p" in rows, rows[:300])

        await b.close()


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
