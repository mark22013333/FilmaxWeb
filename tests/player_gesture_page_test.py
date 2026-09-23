"""在真的瀏覽器裡用「真的觸控」操作播放頁：手勢、hit-testing、reduced-motion。

    python tests/player_gesture_page_test.py

`player_gesture_test.js` 用 DOM stub 驗手勢的邏輯，但 stub 裡事件不會冒泡、
也沒有版面 —— 所以它證明不了兩件最容易壞、也最重要的事：

  1. **hit-testing**：回饋圖層（中央圖示、左右快轉提示、2×、重播）真的沒有
     擋到點擊；控制列、進度條、下一集上的觸控真的到不了 #tap。
     少一行 `pointer-events:none`，雙擊的第二下就會打在圖示上而不是畫面上。
  2. **真的觸控事件序列**：Chromium 的觸控會產生 pointer 事件、再補一個
     合成的 click。合成 click 被當成滑鼠點擊的話，每一下觸控都會切兩次播放。

`<video>` 沒有真的片源，所以 play/pause/currentTime 由測試接手（記次數、發事件）
—— 要驗的是「事件進來之後播放器怎麼反應」，跟解碼無關。
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
TMP = tempfile.mkdtemp(prefix="filmax-pg-")
PORT = 29338
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "HOST": "127.0.0.1", "PORT": str(PORT),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
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
    " VALUES('movie','手勢測試','t','movie::g::2020','ok',?,?)", (now, now))
FID = db.execute(
    """INSERT INTO media_file(item_id,ftp_path,filename,ext,size,duration,width,height,
                              video_codec,audio_codec,bitrate,probe_state,play_mode,
                              seen_at,added_at)
       VALUES(?,'/x/g.mkv','g.mkv','mkv',2000000000,3600,1920,804,'h264','aac',
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

# 讓 <video> 表現得像「正在播 10:00」。play/pause 會發真的事件（回饋看的是事件）。
INSTRUMENT = """() => {
  const v = document.getElementById('v');
  const t = window.__t = { play: 0, pause: 0, seeks: [] };
  let paused = false, ct = 600;
  const def = (k, get, set) => Object.defineProperty(v, k, { configurable: true, get, set });
  def('paused', () => paused);
  def('ended', () => false);
  def('readyState', () => 4);
  def('currentTime', () => ct, x => { ct = x; t.seeks.push(x); });
  v.play = () => { t.play++; if (paused) { paused = false; v.dispatchEvent(new Event('play')); }
                   return Promise.resolve(); };
  v.pause = () => { t.pause++; if (!paused) { paused = true; v.dispatchEvent(new Event('pause')); } };
  v.dispatchEvent(new Event('playing'));
}"""
RESET = "() => { Object.assign(window.__t, { play: 0, pause: 0, seeks: [] }); }"


async def open_player(ctx):
    pg = await ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)
    await pg.goto(f"{BASE}/static/player.html?file={FID}", wait_until="networkidle")
    await pg.wait_for_function("() => window.__hls && window.__playerTestHooks", timeout=8000)
    await pg.evaluate(INSTRUMENT)
    return pg, errs


async def rect(pg, sel):
    return await pg.eval_on_selector(sel, "e => { const r = e.getBoundingClientRect();"
                                          " return { x: r.left, y: r.top, w: r.width, h: r.height }; }")


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()

        # ------------------------------------------------ 手機（橫向）
        ctx = await b.new_context(viewport={"width": 844, "height": 390}, has_touch=True,
                                  is_mobile=True, device_scale_factor=2)
        await ctx.route("**/static/hls.min.js", lambda r: r.fulfill(
            status=200, content_type="application/javascript", body=FAKE_HLS))
        pg, errs = await open_player(ctx)
        tap = await rect(pg, "#tap")
        cy = tap["y"] + tap["h"] * 0.45
        right_x = tap["x"] + tap["w"] * 0.82
        center_x = tap["x"] + tap["w"] * 0.5

        head("[9] 回饋圖層不可以擋到點擊（pointer-events:none ＋ 實際 hit-test）")
        for sel in ("#flash", "#seekL", "#seekR", "#holdFb", "#replayHint"):
            pe = await pg.eval_on_selector(sel, "e => getComputedStyle(e).pointerEvents")
            check(f"{sel} 是 pointer-events:none", pe == "none", pe)
        # 讓它們全部「看得見」再 hit-test —— 看不見的時候擋不擋本來就看不出來
        hits = await pg.evaluate("""() => {
            const pl = document.getElementById('pl');
            pl.classList.add('ended');
            for (const id of ['flash', 'seekL', 'seekR', 'holdFb']) document.getElementById(id).classList.add('show');
            const out = {};
            for (const id of ['flash', 'seekL', 'seekR', 'holdFb', 'replayHint']) {
              const r = document.getElementById(id).getBoundingClientRect();
              const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
              out[id] = hit ? (hit.id || hit.tagName) : null;
            }
            pl.classList.remove('ended');
            for (const id of ['flash', 'seekL', 'seekR', 'holdFb']) document.getElementById(id).classList.remove('show');
            return out;
        }""")
        for k, hit in hits.items():
            check(f"點在 #{k} 上 → 實際打到的是 #tap", hit == "tap", hit)

        head("[9] 控制元件上的觸控到不了 #tap")
        hits = await pg.evaluate("""() => {
            document.getElementById('pl').classList.remove('idle');
            const ne = document.getElementById('nextEp'); ne.hidden = false;   // 只為了 hit-test
            const out = {};
            for (const id of ['barWrap', 'btnPlay', 'btnBack', 'btnFwd', 'btnSubs', 'btnSettings',
                              'btnPip', 'btnFull', 'nextEp']) {
              const el = document.getElementById(id), r = el.getBoundingClientRect();
              const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
              out[id] = { ok: !!hit && el.contains(hit), tap: hit && hit.id === 'tap', w: r.width };
            }
            ne.hidden = true;
            return out;
        }""")
        for k, h in hits.items():
            check(f"#{k} 的中心點打到它自己，不是 #tap", h["ok"] and not h["tap"] and h["w"] > 0, h)

        head("[3][7] 真的觸控：右側雙擊 ×3 → UI 立刻 30 秒，seek 只有一次")
        await pg.evaluate("() => document.getElementById('pl').classList.add('idle')")
        await pg.evaluate(RESET)
        for _ in range(3):
            await pg.touchscreen.tap(right_x, cy)
            await pg.touchscreen.tap(right_x, cy)
            await pg.wait_for_timeout(120)
        txt = await pg.eval_on_selector("#seekRText", "e => e.textContent")
        shown = await pg.eval_on_selector("#seekR", "e => e.classList.contains('show')")
        t = await pg.evaluate("() => window.__t")
        check("回饋出現在右邊，寫「30 秒」", shown and txt == "30 秒", txt)
        check("三次雙擊期間還沒有任何 seek", t["seeks"] == [], t)
        await pg.wait_for_timeout(700)
        t = await pg.evaluate("() => window.__t")
        check("最後只有一次 seek：600 → 630", t["seeks"] == [630], t["seeks"])
        check("觸控（含合成 click）沒有切到播放/暫停", t["play"] == 0 and t["pause"] == 0, t)
        idle = await pg.eval_on_selector("#pl", "e => e.classList.contains('idle')")
        check("雙擊快轉沒有把控制列叫出來（畫面保持乾淨）", idle)
        await pg.evaluate("() => document.getElementById('v').dispatchEvent(new Event('seeked'))")

        head("[4] 真的觸控：單擊不跳秒")
        await pg.evaluate(RESET)
        await pg.touchscreen.tap(right_x, cy)
        await pg.wait_for_timeout(500)
        t = await pg.evaluate("() => window.__t")
        idle = await pg.eval_on_selector("#pl", "e => e.classList.contains('idle')")
        check("控制列藏著時單擊右側：不 seek、不暫停", t["seeks"] == [] and t["pause"] == 0, t)
        check("而是叫出控制列", not idle)
        await pg.touchscreen.tap(center_x, cy)
        await pg.wait_for_timeout(500)
        t = await pg.evaluate("() => window.__t")
        kind = await pg.eval_on_selector("#flash", "e => e.dataset.kind")
        check("控制列在時單擊中央 → 暫停一次（不是兩次）", t["pause"] == 1 and t["play"] == 0, t)
        check("中央閃 ❚❚", kind == "pause", kind)
        await pg.touchscreen.tap(center_x, cy)
        await pg.wait_for_timeout(500)

        head("[9] 真的觸控：點進度條不會被畫面手勢接管")
        await pg.evaluate("() => document.getElementById('pl').classList.remove('idle')")
        await pg.evaluate(RESET)
        bar = await rect(pg, "#barWrap")
        await pg.touchscreen.tap(bar["x"] + bar["w"] * 0.5, bar["y"] + bar["h"] / 2)
        await pg.wait_for_timeout(600)
        t = await pg.evaluate("() => window.__t")
        check("進度條：一次 seek，落在約 50%（1800 秒）",
              len(t["seeks"]) == 1 and abs(t["seeks"][0] - 1800) < 30, t["seeks"])
        paused = await pg.evaluate("() => document.getElementById('v').paused")
        check("進度條：沒有暫停", t["pause"] == 0 and not paused, t)
        await pg.evaluate("() => document.getElementById('v').dispatchEvent(new Event('seeked'))")

        async def tap_el(sel):
            await pg.evaluate("() => document.getElementById('pl').classList.remove('idle')")
            r = await rect(pg, sel)
            await pg.touchscreen.tap(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
            await pg.wait_for_timeout(120)

        await pg.evaluate(RESET)
        await tap_el("#btnSettings")
        panel = await pg.eval_on_selector("#panel", "e => e.classList.contains('show')")
        check("設定鍵真的打開了面板", panel)
        await pg.touchscreen.tap(center_x, cy)            # 點畫面關面板
        await pg.wait_for_timeout(500)
        panel = await pg.eval_on_selector("#panel", "e => e.classList.contains('show')")
        check("點畫面關掉面板", not panel)
        await tap_el("#btnFull")
        fs = await pg.evaluate("() => !!document.fullscreenElement"
                               " || document.body.classList.contains('page-fs')")
        check("全螢幕鍵真的進了全螢幕", fs)
        await tap_el("#btnFull")
        await pg.wait_for_timeout(300)
        t = await pg.evaluate("() => window.__t")
        check("設定／關面板／全螢幕進出：都沒有播放/暫停、沒有手勢 seek",
              t["play"] == 0 and t["pause"] == 0 and t["seeks"] == [], t)

        head("[8] 真的觸控：長按右側 2×，放開回到 1.25×")
        cdp = await ctx.new_cdp_session(pg)
        await pg.evaluate("() => { const v = document.getElementById('v');"
                          " if (v.paused) v.play(); v.playbackRate = 1.25; }")
        await pg.evaluate(RESET)
        await cdp.send("Input.dispatchTouchEvent",
                       {"type": "touchStart", "touchPoints": [{"x": right_x, "y": cy}]})
        await pg.wait_for_timeout(650)
        rate = await pg.evaluate("() => document.getElementById('v').playbackRate")
        hold = await pg.eval_on_selector("#holdFb", "e => getComputedStyle(e).opacity")
        check("按住 → 2×", rate == 2, rate)
        check("2× 提示看得見", float(hold) > 0.5, hold)
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        await pg.wait_for_timeout(100)
        rate = await pg.evaluate("() => document.getElementById('v').playbackRate")
        check("放開 → 回到 1.25×", rate == 1.25, rate)
        t = await pg.evaluate("() => window.__t")
        check("長按放開不算點擊", t["pause"] == 0, t)

        head("[1][11] reduced-motion：回饋還在，但不做縮放")
        await pg.evaluate("() => window.__playerTestHooks.togglePlay()")
        anim = await pg.eval_on_selector("#flash", "e => getComputedStyle(e).animationName")
        check("一般模式：中央回饋用 flash-pop（有縮放）", anim == "flash-pop", anim)
        await pg.emulate_media(reduced_motion="reduce")
        await pg.evaluate("() => window.__playerTestHooks.togglePlay()")
        anim = await pg.eval_on_selector("#flash", "e => getComputedStyle(e).animationName")
        check("reduced-motion：改用只有透明度的 flash-fade", anim == "flash-fade", anim)
        pressed = await pg.evaluate("""() => { const b = document.getElementById('btnPlay');
            b.classList.add('pressed'); const a = getComputedStyle(b).animationName;
            b.classList.remove('pressed'); return a; }""")
        check("reduced-motion：按鈕按壓不做縮放動畫", pressed == "none", pressed)
        await pg.emulate_media(reduced_motion="no-preference")

        check("手機整段操作沒有 JS 錯誤", not errs, errs[:3])
        await ctx.close()

        # ------------------------------------------------ 桌機（滑鼠）
        head("[4] 桌機滑鼠：click = 播放/暫停、dblclick = 全螢幕，照舊")
        ctx = await b.new_context(viewport={"width": 1280, "height": 800})
        await ctx.route("**/static/hls.min.js", lambda r: r.fulfill(
            status=200, content_type="application/javascript", body=FAKE_HLS))
        pg, errs = await open_player(ctx)
        tap = await rect(pg, "#tap")
        cx, cy = tap["x"] + tap["w"] * 0.5, tap["y"] + tap["h"] * 0.45
        await pg.evaluate(RESET)
        await pg.mouse.click(cx, cy)
        await pg.wait_for_timeout(50)
        t = await pg.evaluate("() => window.__t")
        kind = await pg.eval_on_selector("#flash", "e => e.dataset.kind")
        check("單擊 → 立刻暫停（滑鼠不用等雙擊窗口）", t["pause"] == 1, t)
        check("中央閃 ❚❚", kind == "pause", kind)
        await pg.evaluate(RESET)
        await pg.mouse.dblclick(tap["x"] + tap["w"] * 0.85, cy)
        await pg.wait_for_timeout(300)
        fs = await pg.evaluate("() => !!document.fullscreenElement"
                               " || document.body.classList.contains('page-fs')")
        t = await pg.evaluate("() => window.__t")
        check("雙擊 → 全螢幕", fs)
        check("滑鼠雙擊右側不會變成快轉", t["seeks"] == [], t)
        await pg.keyboard.press("ArrowRight")
        t = await pg.evaluate("() => window.__t")
        check("→ 鍵照舊 +10", t["seeks"] == [610], t["seeks"])
        check("桌機沒有 JS 錯誤", not errs, errs[:3])

        await b.close()


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
