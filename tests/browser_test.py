"""在真的瀏覽器裡驗換頁捲動與全螢幕。

phase1_test 對 E / F 只做了「原始碼有沒有這段字」的靜態檢查 ——
那只證明改了，不證明會動。這一支開真的 Chromium 去按。

    python tests/browser_test.py
"""
import asyncio, os, shutil, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-br-")
PORT = 29321
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "HOST": "127.0.0.1", "PORT": str(PORT),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    # 故意用環境變數鎖住一項，才測得到後台的「被 .env 鎖住」長什麼樣子
    "MIN_FILE_MB": "0",
})
# 反過來，要驗「可以改」的那一項就不能被本機環境變數蓋住
os.environ.pop("TRANSCODE_CRF", None)
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")


# 塞一些假資料，讓海報牆有兩頁可以翻
from app import db
db.init_db()
now = db.now()
for i in range(1, 130):
    cur = db.execute(
        "INSERT INTO media_item(kind,title,sort_title,year,guess_key,scrape_state,added_at,updated_at)"
        " VALUES('movie',?,?,2020,?,'ok',?,?)",
        (f"測試影片 {i:03d}", f"t{i:03d}", f"movie::t{i}::2020", now - i, now))
    db.execute("INSERT INTO media_file(item_id,ftp_path,filename,size,ext,probe_state,seen_at,added_at)"
               " VALUES(?,?,?,?,'mkv','ok',?,?)",
               (cur.lastrowid, f"/x/{i}.mkv", f"{i}.mkv", 1024, now, now))
for i in range(1, 130):
    db.execute("INSERT INTO photo(ftp_path,folder,filename,ext,size,mtime_ts,sort_ts,"
               "probe_state,seen_at,added_at) VALUES(?,?,?,'jpg',1024,?,?,'ok',?,?)",
               (f"/p/{i}.jpg", "/p", f"{i}.jpg", int(now) - i, int(now) - i, now, now))

# 後台要有東西可看
db.execute("INSERT INTO media_item(kind,title,sort_title,guess_key,scrape_state,added_at,updated_at)"
           " VALUES('tv','刮不到的片','x','tv::x::0','failed',?,?)", (now, now))
db.execute("INSERT INTO login_audit(at,event,role,ip) VALUES(?,'success','admin','10.0.0.1')",
           (now,))

import uvicorn
from app.main import app

_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=_server.run, daemon=True).start()
for _ in range(100):
    if getattr(_server, "started", False):
        break
    time.sleep(0.1)

BASE = f"http://127.0.0.1:{PORT}"


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1280, "height": 800})
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)

        # ---------------- E 換頁捲動 ----------------
        print("\n[E] 換頁捲動")
        await pg.goto(BASE + "/", wait_until="networkidle")
        await pg.wait_for_selector(".card", timeout=8000)
        await pg.wait_for_selector("#pager button", timeout=8000)

        # Playwright 的 click() 會先把元素捲進視窗才按 —— 分頁鈕在第一屏
        # 之外，於是每次點擊當下的 scrollY 都是它捲過去的位置，不是我們設定
        # 的那個。這樣量到的是 Playwright 的行為，不是 app 的。改用 JS 觸發
        # 真正的 click 事件（不捲動），才測得到「按下去的當下捲動位置是多少」。
        async def click_pager(pager, label="下一頁"):
            await pg.evaluate(
                """([sel, txt]) => {
                    const b = [...document.querySelectorAll(sel + ' button')]
                        .find(x => x.textContent.includes(txt));
                    if (!b) throw new Error('找不到分頁鈕 ' + sel + ' ' + txt);
                    b.click();
                }""", [pager, label])

        # 先捲到中段，再按下一頁
        await pg.evaluate("window.scrollTo(0, 900)")
        await pg.wait_for_timeout(200)
        before = await pg.evaluate("window.scrollY")
        await click_pager("#pager")
        await pg.wait_for_timeout(900)
        after = await pg.evaluate("window.scrollY")
        geom = await pg.evaluate("""() => {
            const g = document.querySelector('#grid').getBoundingClientRect();
            const h = document.querySelector('header').getBoundingClientRect();
            return { gridTop: g.top, headerBottom: h.bottom, y: window.scrollY };
        }""")
        check(f"從 {before:.0f} 捲到 {after:.0f}，不是回到 0", after > 0, after)
        # 格線頂端應該剛好落在標頭下方（容許幾像素誤差）
        gap = geom["gridTop"] - geom["headerBottom"]
        check(f"格線頂端落在標頭正下方（相距 {gap:.0f}px）", -4 <= gap <= 40, gap)

        # 已經在頂端時按下一頁不該跳動。
        # 上一次是平滑捲動，要等它真的停下來才算數，否則量到的是殘留動畫。
        await pg.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        await pg.wait_for_function(
            "() => window.scrollY === 0 && (window.__last === 0 || (window.__last = 0, false))",
            timeout=4000)
        await pg.wait_for_timeout(700)
        await click_pager("#pager")
        await pg.wait_for_timeout(900)
        check("在頁面頂端按下一頁不會跳動",
              await pg.evaluate("window.scrollY") == 0,
              await pg.evaluate("window.scrollY"))

        # 相片分頁同樣行為
        await pg.click("nav button[data-kind='photo']")
        await pg.wait_for_selector("#pgrid img", timeout=8000)
        await pg.evaluate("window.scrollTo(0, 900)")
        await pg.wait_for_timeout(200)
        await click_pager("#ppager")
        await pg.wait_for_timeout(900)
        pgeom = await pg.evaluate("""() => {
            const g = document.querySelector('#pgrid').getBoundingClientRect();
            const h = document.querySelector('header').getBoundingClientRect();
            return { gap: g.top - h.bottom, y: window.scrollY };
        }""")
        check(f"相片換頁也捲到格線頂端（相距 {pgeom['gap']:.0f}px）",
              -4 <= pgeom["gap"] <= 40 and pgeom["y"] > 0, pgeom)

        # ---------------- F 全螢幕 ----------------
        print("\n[F] 全螢幕")
        await pg.goto(BASE + "/player?file=1", wait_until="domcontentloaded")
        await pg.wait_for_selector("#btnFull", timeout=8000)
        await pg.wait_for_timeout(600)

        modes = await pg.evaluate("""() => ({
            hasToggle: typeof toggleFull === 'function',
            hasPage: typeof setPageFullscreen === 'function',
            hasNativeTrackFn: typeof showNativeTrack === 'function',
            canElement: canElementFullscreen,
            mode: fsMode(),
        })""")
        check("三個函式都掛上了",
              modes["hasToggle"] and modes["hasPage"] and modes["hasNativeTrackFn"], modes)
        check("桌面 Chromium 會選 element 模式",
              modes["canElement"] and modes["mode"] == "element", modes)

        # 強制 page 模式（模擬 iPhone 的處境）
        r = await pg.evaluate("""() => {
            cfg.fullscreen = 'page';
            const m = fsMode();
            toggleFull();
            const on = document.body.classList.contains('page-fs');
            const rect = document.querySelector('.pl').getBoundingClientRect();
            const overflow = getComputedStyle(document.documentElement).overflow;
            toggleFull();
            return { m, on, w: rect.width, h: rect.height,
                     vw: innerWidth, vh: innerHeight, overflow,
                     off: document.body.classList.contains('page-fs') };
        }""")
        check("選了 page 就走網頁全螢幕", r["m"] == "page", r["m"])
        check("開啟後 body 有 page-fs", r["on"] is True)
        check(f"播放器撐滿可視區域（{r['w']:.0f}x{r['h']:.0f} vs {r['vw']}x{r['vh']}）",
              abs(r["w"] - r["vw"]) < 2 and abs(r["h"] - r["vh"]) < 2, r)
        check("開啟時鎖住背景捲動", r["overflow"] == "hidden", r["overflow"])
        check("再按一次會關閉", r["off"] is False)

        # 原生模式在沒有 webkitEnterFullscreen 的瀏覽器要退回網頁全螢幕
        r2 = await pg.evaluate("""() => {
            cfg.fullscreen = 'native';
            const m = fsMode();               // 沒有 webkitEnterFullscreen → 應為 page
            toggleFull();
            const on = document.body.classList.contains('page-fs');
            toggleFull();
            cfg.fullscreen = 'auto';
            return { m, on };
        }""")
        check("桌面沒有原生影片全螢幕時會退回網頁全螢幕",
              r2["m"] == "page" and r2["on"] is True, r2)

        # 原生字幕軌
        r3 = await pg.evaluate("""() => {
            resetNativeTrack('測試', 'zh');
            pushNativeCue({ start: 1, end: 2, html: '<b>你好</b>世界' });
            pushNativeCue({ start: 3, end: 4, html: '第二句' });
            const t = nativeTrack;
            const before = t ? t.mode : null;
            showNativeTrack(true);
            const during = { mode: t.mode, subsHidden: document.querySelector('#subs').style.visibility };
            showNativeTrack(false);
            const cues = t ? t.cues : null;
            return { n: cues ? cues.length : -1, before, during,
                     after: t.mode, text: cues && cues[0] ? cues[0].text : null,
                     subsBack: document.querySelector('#subs').style.visibility };
        }""")
        check("原生字幕軌收到兩句", r3["n"] == 2, r3["n"])
        check("平常是關著的（hidden：有在跑但不顯示，這樣 cues 才清得掉）",
              r3["before"] == "hidden", r3["before"])
        check("進原生全螢幕時打開，同時藏起自繪字幕",
              r3["during"]["mode"] == "showing" and r3["during"]["subsHidden"] == "hidden",
              r3["during"])
        check("退出後關掉並還原自繪字幕",
              r3["after"] == "hidden" and r3["subsBack"] != "hidden", r3)
        check("字幕內容有去掉 HTML 標籤", r3["text"] == "你好世界", r3["text"])

        r4 = await pg.evaluate("""() => {
            resetNativeTrack('第一軌', 'zh');
            pushNativeCue({ start: 1, end: 2, html: '中文第一句' });
            pushNativeCue({ start: 3, end: 4, html: '中文第二句' });
            const a = nativeTrack.cues.length;
            resetNativeTrack('第二軌', 'en');      // 換字幕軌
            const b = nativeTrack.cues.length;
            pushNativeCue({ start: 1, end: 2, html: 'English one' });
            return { a, b, c: nativeTrack.cues.length,
                     first: nativeTrack.cues[0] ? nativeTrack.cues[0].text : null };
        }""")
        check("換字幕軌會清掉舊的句子（不會中英文疊在一起）",
              r4["a"] == 2 and r4["b"] == 0 and r4["c"] == 1 and r4["first"] == "English one",
              r4)

        # ---------------- 手機視窗 ----------------
        print("\n[版面] 375px 寬不能橫捲")
        for path in ("/", "/player?file=1"):
            p2 = await ctx.new_page()
            await p2.goto(BASE + path, wait_until="domcontentloaded")
            await p2.set_viewport_size({"width": 375, "height": 780})
            await p2.wait_for_timeout(800)
            over = await p2.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"{path} 沒有橫向捲動（溢出 {over}px）", over <= 1, over)
            await p2.close()

        # ---------------- 管理後台 ----------------
        print("\n[G] 管理後台")
        pg2 = await ctx.new_page()
        pg2.on("pageerror", lambda e: errs.append("admin: " + str(e)))
        pg2.on("console", lambda m: errs.append("admin console: " + m.text)
               if m.type == "error" else None)
        await pg2.goto(BASE + "/admin", wait_until="networkidle")
        await pg2.wait_for_selector(".sidenav button", timeout=8000)
        check("六個分區都在", await pg2.locator(".sidenav button").count() == 6,
              await pg2.locator(".sidenav button").count())
        await pg2.wait_for_selector("#tab-overview .stat", timeout=8000)
        check("總覽有載到數字", await pg2.locator("#tab-overview .stat").count() >= 6,
              await pg2.locator("#tab-overview .stat").count())

        # 每一個分區都要真的畫得出來，不能只是掛在那裡
        for tab, sel in (("library", "#tab-library .box"), ("users", "#tab-users"),
                         ("playback", "#tab-playback .stat"), ("system", "#paramList .param"),
                         ("logs", "#tab-logs table.t, #tab-logs .rowcards")):
            await pg2.click(f".sidenav button[data-tab='{tab}']")
            try:
                await pg2.wait_for_selector(sel, timeout=8000, state="attached")
                ok = True
            except Exception:
                ok = False
            check(f"{tab} 畫得出來", ok,
                  (await pg2.locator("#tab-" + tab).inner_text())[:80])

        # 系統參數
        await pg2.click(".sidenav button[data-tab='system']")
        await pg2.wait_for_selector("#paramList .param", timeout=8000)
        n = await pg2.locator("#paramList .param").count()
        check(f"參數列出來了（{n} 項）", n >= 20, n)
        keys = await pg2.evaluate(
            "() => [...document.querySelectorAll('#paramList .param')].map(e => e.dataset.key)")
        check("Tier 0 / Tier 4 完全沒出現在畫面上",
              not ({"HOST", "AUTH_SECRET", "MSSQL_PASSWORD", "FFMPEG_PATH",
                    "LIBRARY_ROOTS", "TRUST_PROXY"} & set(keys)),
              sorted({"HOST", "AUTH_SECRET", "FFMPEG_PATH"} & set(keys)))

        # 被 .env 鎖住的那一項：輸入框要真的是停用狀態
        locked = await pg2.evaluate("""() => {
            const el = document.querySelector('.param[data-key="MIN_FILE_MB"]');
            if (!el) return null;
            const inp = el.querySelector('input,select');
            return { disabled: inp.disabled, hasLock: !!el.querySelector('.note.lock'),
                     hasSave: !!el.querySelector('[data-do="save"]') };
        }""")
        check("被環境變數決定的欄位是停用的", locked and locked["disabled"], locked)
        check("而且畫面上說得出原因", locked and locked["hasLock"], locked)
        check("停用的欄位不給儲存鈕（不能是送出後才在後端報錯）",
              locked and not locked["hasSave"], locked)

        # 沒被鎖的那一項：改值 → 出現未儲存 → 存檔 → 真的生效
        free_key = await pg2.evaluate("""() => {
            const el = [...document.querySelectorAll('.param')].find(e =>
                e.dataset.key === 'TRANSCODE_CRF');
            return el ? el.dataset.key : null;
        }""")
        check("找得到一個沒被鎖的參數來改", free_key == "TRANSCODE_CRF", free_key)
        if free_key:
            sel = ".param[data-key='TRANSCODE_CRF'] input"
            await pg2.fill(sel, "23")
            await pg2.dispatch_event(sel, "input")
            check("改了之後有「未儲存」標記",
                  await pg2.locator(".param[data-key='TRANSCODE_CRF'].dirty").count() == 1)
            await pg2.click(".param[data-key='TRANSCODE_CRF'] [data-do='save']")
            await pg2.wait_for_timeout(900)
            got = await pg2.evaluate(
                "async () => (await (await fetch('/api/params')).json())"
                ".items.find(i => i.key === 'TRANSCODE_CRF').effective")
            check(f"存完之後真的變成 23（來源 {got['source']}）",
                  got["value"] == 23 and got["source"] == "db", got)

        # 新增的那幾塊要真的畫得出來，而且錯誤態要是「說明」而不是空白
        await pg2.click(".sidenav button[data-tab='library']")
        await pg2.wait_for_selector("#ftpList", timeout=8000)
        await pg2.wait_for_timeout(1200)
        ftp_txt = await pg2.locator("#ftpList").inner_text()
        # 測試環境的 FTP 是連不上的，所以這裡要看到錯誤說明而不是一片空白
        check("FTP 瀏覽器連不上時給的是說明不是空白", len(ftp_txt.strip()) > 0, ftp_txt[:60])
        check("有上一層按鈕", await pg2.locator("#ftpUp").count() == 1)

        await pg2.click(".sidenav button[data-tab='playback']")
        await pg2.wait_for_selector("#encSel", timeout=8000)
        for sel, label in (("#encSel", "編碼器選單"), ("#bEnc", "編碼器診斷鈕"),
                           ("#benchId", "轉碼實測輸入"), ("#bBench", "轉碼實測鈕"),
                           ("#bGpu", "GPU 查詢鈕"), ("#bProbe", "FTP 併發量測鈕"),
                           ("#bCache", "清快取鈕")):
            check(f"{label}在", await pg2.locator(sel).count() == 1)
        await pg2.click("#bGpu")
        await pg2.wait_for_timeout(1500)
        check("GPU 查詢有回應（沒有 GPU 也要說話）",
              len((await pg2.locator("#gpuOut").inner_text()).strip()) > 0)

        await pg2.click(".sidenav button[data-tab='system']")
        await pg2.wait_for_selector("#bFtpTest", timeout=8000)
        await pg2.click("#bFtpTest")
        await pg2.wait_for_timeout(2500)
        sys_txt = await pg2.locator("#sysOut").inner_text()
        check("FTP 測試會回報結果（連不上時說連不上）",
              "連" in sys_txt or len(sys_txt.strip()) > 0, sys_txt[:60])

        await pg2.click(".sidenav button[data-tab='logs']")
        await pg2.wait_for_selector("#scanLog", timeout=8000)
        await pg2.wait_for_timeout(900)
        check("掃描日誌區塊有內容（沒掃過就說沒掃過）",
              len((await pg2.locator("#scanLog").inner_text()).strip()) > 0)

        # 手機：表格換成卡片，不是把欄位藏起來
        await pg2.set_viewport_size({"width": 375, "height": 780})
        await pg2.wait_for_timeout(300)
        # 窄螢幕的側欄收在抽屜裡，要先打開才點得到 —— 這本來就是預期行為
        await pg2.click("#menuBtn")
        await pg2.wait_for_timeout(400)
        await pg2.click(".sidenav button[data-tab='logs']")
        await pg2.wait_for_timeout(700)
        m = await pg2.evaluate("""() => {
            const t = document.querySelector('#tab-logs table.t');
            const c = document.querySelector('#tab-logs .rowcards');
            const g = n => n ? getComputedStyle(n).display : null;
            return { table: g(t), cards: g(c),
                     cardCount: c ? c.children.length : 0,
                     over: document.documentElement.scrollWidth - document.documentElement.clientWidth };
        }""")
        check("手機上表格收起來", m["table"] == "none", m)
        check("同一批資料用卡片呈現（不是消失）",
              m["cards"] == "block" and m["cardCount"] > 0, m)
        check(f"後台在 375px 不橫捲（溢出 {m['over']}px）", m["over"] <= 1, m)

        # 抽屜
        await pg2.click("#menuBtn")
        await pg2.wait_for_timeout(400)
        check("漢堡鍵打得開抽屜",
              await pg2.evaluate("document.querySelector('#side').classList.contains('open')"))
        await pg2.click(".sidenav button[data-tab='overview']")
        await pg2.wait_for_timeout(400)
        check("點分區之後抽屜自己關掉",
              not await pg2.evaluate("document.querySelector('#side').classList.contains('open')"))
        await pg2.close()

        # 測試資料沒有真的影片檔與海報，所以 /api/image、/api/stream 的 404 是預期的
        real = [e for e in errs if "Failed to load resource" not in e and "ERR_" not in e]
        check("沒有 JS 錯誤", not real, real[:3])
        await b.close()

asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
