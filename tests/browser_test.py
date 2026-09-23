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
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
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

# 影集詳情的版面要有東西可測：兩季各三集，檔名刻意用真實世界那種長度
# （Reacher.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb.mkv），
# 短檔名測不出「長檔名把版面撐破」那一類問題。
_tv = db.execute(
    "INSERT INTO media_item(kind,title,sort_title,year,guess_key,scrape_state,overview,"
    "added_at,updated_at) VALUES('tv','測試影集','ceshi',2022,'tv::ceshi::2022','ok',?,?,?)",
    ("一段用來把簡介區塞滿的敘述，長度接近真實的 TMDB 簡介。", now, now)).lastrowid
TV_ITEM = _tv
for _s in (1, 2):
    for _e in range(1, 4):
        _ep = db.execute("INSERT INTO episode(item_id,season,episode,title) VALUES(?,?,?,?)",
                         (_tv, _s, _e, f"第{_e}集 一個相當長的集數標題用來測試截斷")).lastrowid
        db.execute(
            "INSERT INTO media_file(item_id,episode_id,ftp_path,filename,ext,size,duration,"
            "width,height,video_codec,audio_codec,bitrate,probe_state,play_mode,seen_at,added_at)"
            " VALUES(?,?,?,?,'mkv',3704000000,3240,1920,1080,'h264','eac3',4000000,"
            "'ok','hls',?,?)",
            (_tv, _ep, f"/tv/S{_s:02d}E{_e:02d}.mkv",
             f"TestShow.S{_s:02d}E{_e:02d}.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb.mkv",
             now, now))
db.execute("INSERT INTO login_audit(at,event,role,ip) VALUES(?,'success','admin','10.0.0.1')",
           (now,))

# L 受限資料夾的 picker 要有東西可測：有深度（展開／收合）、有同名（/Movies/2024 與
# /HomeVideo/2024）、有已受限與被涵蓋、有一個長到會撐破版面的名字。
# 掛在「刮不到的片」底下，不新增條目 —— 海報牆的分頁數不能被這裡影響。
from app import acl, users
users.init()
_holder = db.q1("SELECT id FROM media_item WHERE guess_key='tv::x::0'")["id"]
for _p in ("/HBO/Westworld/Season 01/e1.mkv", "/HBO/Westworld/Season 02/e2.mkv",
           "/HBO/House of the Dragon/h1.mkv", "/Movies/2024/m1.mkv", "/HomeVideo/2024/v1.mkv",
           "/Private/Stuff/p1.mkv", "/Private/Stuff/Deep/p2.mkv",
           "/Long/" + "一個非常非常長的資料夾名稱用來測試手機上會不會把版面撐破" * 2 + "/l1.mkv"):
    db.execute("INSERT INTO media_file(item_id,ftp_path,filename,size,ext,probe_state,seen_at,added_at)"
               " VALUES(?,?,?,1024,'mkv','ok',?,?)", (_holder, _p, _p.rsplit("/", 1)[1], now, now))
ACL_RULE = acl.add_rule("/Private", "測試規則")["id"]


def _mkuser(sub, name, owner=False):
    u = users.upsert_from_google({"sub": sub, "email": f"{sub}@example.com", "name": name},
                                 "127.0.0.1", {})
    users.set_status(u["id"], "approved")
    users.set_role(u["id"], users.OWNER if owner else "viewer")
    return u["id"]


U_MARK, U_ALICE = _mkuser("u-mark", "Mark"), _mkuser("u-alice", "Alice")
# 「先被授權、後來升管理員」的那一個 —— 別人按儲存時他的授權不能被收回
U_ADMIN = _mkuser("u-john", "John")
acl.set_grants(ACL_RULE, [U_MARK, U_ADMIN])
users.set_role(U_ADMIN, users.OWNER)
acl.invalidate()

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
        # 只測 375px 是不夠的。實測（修正前）有三個寬度會溢出而 375 剛好沒事：
        # 320px（nav 的七顆分類排不下）、768px 與 1024px（brand + nav +
        # 250px 搜尋框 + 四顆按鈕合計約 1053px，「管理後台」整顆在畫面外）。
        # 768／1024 正是 iPad 直向與小筆電，而在那裡被推出去的剛好是管理入口。
        print("\n[版面] 各種寬度都不能橫捲")
        WIDTHS = (320, 375, 390, 414, 430, 560, 768, 1024)
        for path in ("/", "/player?file=1"):
            p2 = await ctx.new_page()
            await p2.goto(BASE + path, wait_until="domcontentloaded")
            await p2.wait_for_timeout(800)
            for w in WIDTHS:
                await p2.set_viewport_size({"width": w, "height": 780})
                await p2.wait_for_timeout(220)
                over = await p2.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth")
                check(f"{path} @{w}px 沒有橫向捲動（溢出 {over}px）", over <= 1, over)
            # 手機橫向：可視高度只剩 3xx px
            await p2.set_viewport_size({"width": 780, "height": 380})
            await p2.wait_for_timeout(220)
            over = await p2.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth")
            check(f"{path} 橫向 780x380 沒有橫向捲動（溢出 {over}px）", over <= 1, over)
            await p2.close()

        # ---------------- 影集詳情彈窗 ----------------
        # 這是使用者在手機上待最久、也最容易跑版的畫面：
        # season → episodes → files → fileRow()，每一列有檔名、資訊、pill
        # 與三顆按鈕。原本 .file-row 是一個沒有 flex-wrap 的 flex row，
        # 於是 375px 以下 `.n` 被壓到幾乎沒有寬度、資訊一個字一行地往下長，
        # 而「重新分析」那顆按鈕的右緣會跑到容器外面（實測 320px：327 > 320）。
        print("\n[版面] 影集詳情彈窗（fileRow）在手機上不能跑版")
        p3 = await ctx.new_page()
        await p3.goto(BASE + "/", wait_until="domcontentloaded")
        await p3.wait_for_timeout(800)
        PROBE = """
        () => {
          const doc = document.documentElement;
          const out = { overflow: doc.scrollWidth - doc.clientWidth,
                        rows: 0, tall: [], escaped: [], small: [] };
          for (const r of document.querySelectorAll('.file-row')) {
            const rb = r.getBoundingClientRect();
            out.rows++;
            // 一列超過 200px 高 = 文字被壓成一個字一行（原本的壞法）
            if (rb.height > 200) out.tall.push(Math.round(rb.height));
            for (const b of r.querySelectorAll('.btn')) {
              const bb = b.getBoundingClientRect();
              if (bb.right > rb.right + 1 || bb.left < rb.left - 1)
                out.escaped.push(b.textContent.trim());
              // 手機上的觸控目標要夠大
              if (window.innerWidth <= 560 && bb.height < 38)
                out.small.push(Math.round(bb.height));
            }
          }
          return out;
        }
        """
        for w in (320, 375, 390, 414, 430, 560, 768):
            await p3.set_viewport_size({"width": w, "height": 780})
            await p3.wait_for_timeout(200)
            await p3.evaluate(f"() => openItem({TV_ITEM})")
            await p3.wait_for_timeout(400)
            r = await p3.evaluate(PROBE)
            check(f"詳情 @{w}px 有畫出集數列（{r['rows']} 列）", r["rows"] > 0, r)
            check(f"詳情 @{w}px 沒有橫向捲動（溢出 {r['overflow']}px）",
                  r["overflow"] <= 1, r)
            check(f"詳情 @{w}px 按鈕沒有跑出容器", not r["escaped"], r["escaped"])
            check(f"詳情 @{w}px 沒有被壓成一個字一行的列", not r["tall"], r["tall"])
            if w <= 560:
                check(f"詳情 @{w}px 按鈕的觸控目標夠大（≥38px）",
                      not r["small"], r["small"])
            await p3.evaluate("() => closeModal()")
            await p3.wait_for_timeout(120)
        await p3.close()

        # ---------------- 管理後台 ----------------
        print("\n[G] 管理後台")
        pg2 = await ctx.new_page()
        pg2.on("pageerror", lambda e: errs.append("admin: " + str(e)))
        pg2.on("console", lambda m: errs.append("admin console: " + m.text)
               if m.type == "error" else None)
        await pg2.goto(BASE + "/admin", wait_until="networkidle")
        await pg2.wait_for_selector(".sidenav button", timeout=8000)
        check("七個分區都在（系統參數獨立成一頁）",
              await pg2.locator(".sidenav button").count() == 7,
              await pg2.locator(".sidenav button").count())
        await pg2.wait_for_selector("#tab-overview .stat", timeout=8000)
        check("總覽有載到數字", await pg2.locator("#tab-overview .stat").count() >= 6,
              await pg2.locator("#tab-overview .stat").count())

        # 每一個分區都要真的畫得出來，不能只是掛在那裡
        for tab, sel in (("library", "#tab-library .box"), ("users", "#tab-users"),
                         ("playback", "#tab-playback .stat"), ("system", "#tab-system .stat"),
                         ("params", "#paramList .param"),
                         ("logs", "#tab-logs table.t, #tab-logs .rowcards")):
            await pg2.click(f".sidenav button[data-tab='{tab}']")
            try:
                await pg2.wait_for_selector(sel, timeout=8000, state="attached")
                ok = True
            except Exception:
                ok = False
            check(f"{tab} 畫得出來", ok,
                  (await pg2.locator("#tab-" + tab).inner_text())[:80])

        # 系統參數（M：獨立分頁 ＋ 二層 nav ＋ 批次儲存 ＋ 搜尋）
        await pg2.click(".sidenav button[data-tab='params']")
        await pg2.wait_for_selector("#paramList .param", timeout=8000)

        nav = await pg2.evaluate("""() => {
            const n = document.querySelector('#paramNav');
            return { hidden: n.hidden,
                     cats: [...n.querySelectorAll('button')].map(b => b.dataset.cat) };
        }""")
        check("二層 nav 展開了", nav and not nav["hidden"], nav)
        check("狀態分類在最前面（可以在這裡改／由 .env 決定）",
              nav["cats"][:2] == ["__free", "__env"], nav["cats"][:2])
        check("分類數 = 狀態 2 + 有項目的 section", len(nav["cats"]) >= 4, nav["cats"])

        n = await pg2.locator("#paramList .param").count()
        check(f"「可以在這裡改」列出來了（{n} 項）", n >= 1, n)
        keys = await pg2.evaluate(
            "() => [...document.querySelectorAll('#paramList .param')].map(e => e.dataset.key)")
        check("Tier 0 / Tier 4 完全沒出現在畫面上",
              not ({"HOST", "AUTH_SECRET", "MSSQL_PASSWORD", "FFMPEG_PATH",
                    "LIBRARY_ROOTS", "TRUST_PROXY"} & set(keys)),
              sorted({"HOST", "AUTH_SECRET", "FFMPEG_PATH"} & set(keys)))

        # 切到「由 .env 決定」：那些列不給輸入框，而且說得出原因
        await pg2.click("#paramNav button[data-cat='__env']")
        await pg2.wait_for_timeout(400)
        envcat = await pg2.evaluate("""() => {
            const rows = [...document.querySelectorAll('#paramList .param')];
            return { rows: rows.length,
                     ro: rows.filter(r => r.classList.contains('ro')).length,
                     inputs: document.querySelectorAll('#paramList .param input,'
                                                     + '#paramList .param select').length,
                     rovals: document.querySelectorAll('#paramList .roval').length,
                     hint: !!document.querySelector('.box.lockhint'),
                     lockNotes: document.querySelectorAll('#paramList .note.lock').length };
        }""")
        if envcat["rows"]:
            check("被 .env 決定的列全部標成唯讀",
                  envcat["ro"] == envcat["rows"], envcat)
            check("唯讀的列不給輸入框（不能是打了字才發現存不進去）",
                  envcat["inputs"] == 0 and envcat["rovals"] == envcat["rows"], envcat)
            check("而且畫面上說得出原因", envcat["hint"] and envcat["lockNotes"], envcat)

        # 搜尋要跨分類
        await pg2.click("#paramNav button[data-cat='__free']")
        await pg2.wait_for_timeout(300)
        await pg2.fill("#pq", "crf")
        await pg2.wait_for_timeout(400)
        found = await pg2.evaluate(
            "() => [...document.querySelectorAll('#paramList .param')].map(e => e.dataset.key)")
        check("搜尋找得到 TRANSCODE_CRF", "TRANSCODE_CRF" in found, found)
        await pg2.fill("#pq", "")
        await pg2.wait_for_timeout(300)

        # 批次儲存：一項合法 + 一項不合法 → 整批拒絕，一個字都不寫
        await pg2.evaluate("""() => {
            const set = (k, v) => {
                const w = document.querySelector(`.param[data-key="${k}"]`);
                if (!w) return;
                const inp = w.querySelector('input,select');
                inp.value = v;
                inp.dispatchEvent(new Event('input', { bubbles: true }));
            };
            set('TRANSCODE_CRF', '23');
            set('NVENC_CQ', '999');
        }""")
        await pg2.wait_for_timeout(300)
        check("改了之後有「未儲存」標記",
              await pg2.locator(".param.dirty").count() == 2,
              await pg2.locator(".param.dirty").count())
        check("底部出現批次儲存列",
              not await pg2.evaluate("() => document.querySelector('#savebar').hidden"))
        await pg2.click("#bSaveAll")
        await pg2.wait_for_timeout(900)
        rej = await pg2.evaluate("""async () => {
            const d = await (await fetch('/api/params')).json();
            const v = k => d.items.find(i => i.key === k).effective;
            return { crf: v('TRANSCODE_CRF'), cq: v('NVENC_CQ'),
                     bad: [...document.querySelectorAll('.param.bad')].map(e => e.dataset.key),
                     dirty: document.querySelectorAll('.param.dirty').length };
        }""")
        check("一項不合法 → 整批不寫（合法那一項也沒有進去）",
              rej["crf"]["source"] != "db", rej["crf"])
        check("錯誤標在出錯的那一列上", rej["bad"] == ["NVENC_CQ"], rej["bad"])
        check("使用者打的東西沒有被錯誤吃掉", rej["dirty"] == 2, rej["dirty"])

        # 修好 → 兩項一起寫進去
        await pg2.evaluate("""() => {
            const w = document.querySelector('.param[data-key="NVENC_CQ"]');
            const inp = w.querySelector('input');
            inp.value = '30';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        await pg2.click("#bSaveAll")
        await pg2.wait_for_timeout(1200)
        okv = await pg2.evaluate("""async () => {
            const d = await (await fetch('/api/params')).json();
            const v = k => d.items.find(i => i.key === k).effective;
            return { crf: v('TRANSCODE_CRF'), cq: v('NVENC_CQ'),
                     bar: document.querySelector('#savebar').hidden,
                     dirty: document.querySelectorAll('.param.dirty').length };
        }""")
        check(f"批次存好之後兩項都生效（CRF={okv['crf']['value']}, CQ={okv['cq']['value']}）",
              okv["crf"]["value"] == 23 and okv["crf"]["source"] == "db"
              and okv["cq"]["value"] == 30 and okv["cq"]["source"] == "db", okv)
        check("存完之後儲存列收起來、沒有殘留的未儲存標記",
              okv["bar"] and okv["dirty"] == 0, okv)

        # 新增的那幾塊要真的畫得出來，而且錯誤態要是「說明」而不是空白
        await pg2.click(".sidenav button[data-tab='library']")
        await pg2.wait_for_selector("#ftpList", timeout=8000)
        await pg2.wait_for_timeout(1200)
        ftp_txt = await pg2.locator("#ftpList").inner_text()
        # 測試環境的 FTP 是連不上的，所以這裡要看到錯誤說明而不是一片空白
        check("FTP 瀏覽器連不上時給的是說明不是空白", len(ftp_txt.strip()) > 0, ftp_txt[:60])
        check("有上一層按鈕", await pg2.locator("#ftpUp").count() == 1)

        # L：受限資料夾。另開一頁跑（會真的建規則、改授權），見 acl_ui()
        await acl_ui(ctx)

        await pg2.click(".sidenav button[data-tab='playback']")
        await pg2.wait_for_selector("#encSel", timeout=8000)
        for sel, label in (("#encSel", "編碼器選單"), ("#bEnc", "編碼器診斷鈕"),
                           ("#benchPick", "轉碼實測的檔案選擇器"), ("#bBench", "轉碼實測鈕"),
                           ("#bGpu", "GPU 查詢鈕"), ("#bProbe", "FTP 併發量測鈕"),
                           ("#bCache", "清快取鈕")):
            check(f"{label}在", await pg2.locator(sel).count() == 1)

        # ---- 檔案選擇器 ----
        # 這一段要守住的不變量是「**不讓人手打 file_id**」。手打的失敗方式很安靜：
        # 打錯一個數字不會報錯，而是對另一支存在的片跑了一次好幾分鐘的實測，
        # 畫面上有數字、有倍速，看起來完全正常，只是答非所問。
        fp = await pg2.evaluate("""() => {
            const r = document.querySelector('#benchPick');
            const inp = r && r.querySelector('.fpick-input');
            return {
              role: inp && inp.getAttribute('role'),
              expanded: inp && inp.getAttribute('aria-expanded'),
              numberInputs: [...document.querySelectorAll('#tab-playback input[type=number]')]
                              .map(i => i.id),
              btnDisabled: document.querySelector('#bBench').disabled,
            };
        }""")
        check("轉碼實測改成 combobox", fp["role"] == "combobox", fp)
        check("播放頁沒有可以手打 file_id 的數字欄位了",
              not fp["numberInputs"], fp["numberInputs"])
        check("還沒選檔案時「開始實測」是停用的（不是按了才被罵）", fp["btnDisabled"], fp)

        # **這是「下拉選單」與「搜尋框」的分界線，不是可有可無的體驗細節。**
        # 第一版只在打字之後才顯示清單 —— 點下去沒有任何反應，於是它看起來
        # 就只是個要你自己打字的輸入框，而那正是這整件事要取代的東西。
        # 什麼都不打、只點一下，就必須有東西掉下來。
        await pg2.evaluate("""() => {
            const i = document.querySelector('#benchPick .fpick-input');
            i.dispatchEvent(new MouseEvent('click', { bubbles: true })); i.focus();
        }""")
        await pg2.wait_for_timeout(900)
        drop = await pg2.evaluate("""() => ({
            open: !document.querySelector('#benchPick .fpick-pop').hidden,
            n: document.querySelectorAll('#benchPick .fpick-opt').length,
            caret: !!document.querySelector('#benchPick .fpick-caret'),
        })""")
        check("什麼都不打、點一下就掉出清單（這才叫下拉選單）",
              drop["open"] and drop["n"] > 0, drop)
        check("有展開箭頭（「這裡有東西可以掉下來」的視覺線索）", drop["caret"], drop)

        await pg2.evaluate("""() => {
            const i = document.querySelector('#benchPick .fpick-input');
            i.value = 'a'; i.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        await pg2.wait_for_timeout(900)     # debounce 300 + 查詢
        res = await pg2.evaluate("""() => {
            const opts = [...document.querySelectorAll('#benchPick .fpick-opt')];
            const inp = document.querySelector('#benchPick .fpick-input');
            return {
              open: !document.querySelector('#benchPick .fpick-pop').hidden,
              n: opts.length,
              expanded: inp.getAttribute('aria-expanded'),
              listRole: document.querySelector('#benchPick .fpick-list').getAttribute('role'),
              optRole: opts[0] && opts[0].getAttribute('role'),
              allHaveFilename: opts.length > 0 && opts.every(
                  o => (o.querySelector('.fpick-file') || {}).textContent),
            };
        }""")
        check(f"打字之後浮層開起來且有結果（{res['n']} 筆）", res["open"] and res["n"] > 0, res)
        # 同一個 item 底下有多個檔案時（實測全庫約 5%）主行完全一樣，
        # 檔名是唯一分得出來的東西 —— 少了它使用者只能用猜的。
        check("每一列都帶檔名（同片多檔時只有檔名分得出來）", res["allHaveFilename"], res)
        check("aria：combobox/listbox/option 三件都在",
              res["expanded"] == "true" and res["listRole"] == "listbox"
              and res["optRole"] == "option", res)

        await pg2.press("#benchPick .fpick-input", "ArrowDown")
        await pg2.wait_for_timeout(150)
        kb = await pg2.evaluate("""() => {
            const inp = document.querySelector('#benchPick .fpick-input');
            const on = document.querySelector('#benchPick .fpick-opt.on');
            return { focusStillInput: document.activeElement === inp,
                     matches: !!on && on.id === inp.getAttribute('aria-activedescendant') };
        }""")
        # aria-activedescendant 模式：焦點留在 input（才能繼續打字），
        # 由屬性告訴輔助技術「現在指著哪一列」。焦點真的跑到列上就打不了字了。
        check("方向鍵移游標時焦點留在輸入框", kb["focusStillInput"], kb)
        check("aria-activedescendant 指到真的那一列", kb["matches"], kb)

        await pg2.press("#benchPick .fpick-input", "Enter")
        await pg2.wait_for_timeout(250)
        picked = await pg2.evaluate("""() => ({
            closed: document.querySelector('#benchPick .fpick-pop').hidden,
            text: document.querySelector('#benchPick .fpick-input').value,
            file: (document.querySelector('#benchPick .fpick-picked') || {}).textContent || '',
            btnOk: !document.querySelector('#bBench').disabled,
        })""")
        check("Enter 選中之後浮層關起來", picked["closed"], picked)
        check("選中之後看得到選了什麼（片名摘要 + 檔名）",
              len(picked["text"]) > 0 and len(picked["file"].strip()) > 0, picked)
        check("選了才能按「開始實測」", picked["btnOk"], picked)

        # 選好了再點開 = 他想換一個。清單要回到完整那一份，不是上一次搜尋
        # 剩下的那幾筆 —— 選單裡只剩自己，看起來像「沒有別的可選」。
        await pg2.evaluate("""() => {
            const i = document.querySelector('#benchPick .fpick-input');
            i.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }""")
        await pg2.wait_for_timeout(900)
        again = await pg2.evaluate("""() => ({
            n: document.querySelectorAll('#benchPick .fpick-opt').length,
            stillSelected: !document.querySelector('#bBench').disabled,
        })""")
        check("選好之後再點開，清單回到完整的那一份（不是只剩自己）",
              again["n"] > 1, again)
        check("而且選擇還在（點開不等於取消）", again["stillSelected"], again)

        await pg2.press("#benchPick .fpick-input", "Escape")
        await pg2.wait_for_timeout(150)
        # Escape 只收浮層。把已經選好的東西一起清掉是資料損失 ——
        # 使用者按 Escape 的意思是「不看清單了」，不是「我不要這個檔案」。
        check("Escape 只收浮層，不清掉已選的檔案",
              not await pg2.evaluate("() => document.querySelector('#bBench').disabled"))

        await pg2.evaluate("""() => {
            const i = document.querySelector('#benchPick .fpick-input');
            i.value = 'zzzz不可能存在的片名zzzz';
            i.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        await pg2.wait_for_timeout(900)
        none = await pg2.evaluate("""() => {
            const pop = document.querySelector('#benchPick .fpick-pop');
            return { open: !pop.hidden,
                     msg: (pop.textContent || '').trim(),
                     opts: document.querySelectorAll('#benchPick .fpick-opt').length };
        }""")
        # 收起來的話使用者分不出「沒找到」與「元件壞了」。
        check("查無結果時浮層開著並說明（不是空白也不是消失）",
              none["open"] and none["opts"] == 0 and len(none["msg"]) > 0, none)

        # 快取那一側用的是同一個元件（兩處都要改，不能只改一邊）
        wsel = await pg2.evaluate("""() => ({
            same: !!document.querySelector('#warmPickSel .fpick-input'),
            numberInputs: [...document.querySelectorAll('#cacheBox input[type=number]')].length,
            warmDisabled: document.querySelector('#bWarmPick').disabled,
            clearDisabled: document.querySelector('#bClearOne').disabled,
        })""")
        check("快取預備用的是同一個元件", wsel["same"], wsel)
        check("快取那一側也沒有手打 id 的欄位了", wsel["numberInputs"] == 0, wsel)
        check("沒選檔案時預備與清除都是停用的",
              wsel["warmDisabled"] and wsel["clearDisabled"], wsel)
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


async def acl_ui(ctx):
    """L：受限資料夾的 picker 與規則卡。

    UI 的關鍵不變量是「**不讓人手打路徑**」—— 手打就會拼錯，而拼錯的規則等於
    沒有保護、畫面上還顯示已設定。挑選介面原本是 <select>（藏不住縮排也修不了篩選），
    後來是永遠攤開的資料夾清單；現在是點了才出現的 picker。
    不管長什麼樣子，以下幾件事不能變：
      - 只能從掃到的資料夾挑，沒有任何可以打路徑的欄位
      - 點資料夾不會直接生效：暫選 →「選擇」→「加入限制」→ confirmBox → POST
      - 已受限／被涵蓋的列得找得到（知道為什麼不能選），但選不了
      - 授權的 checkbox 不會一勾就打 API；存的時候把管理員原有的授權帶回去
    """
    import json
    print("\n[L] 受限資料夾：folder picker 與規則卡")
    pg = await ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    reqs = []
    pg.on("request", lambda r: reqs.append((r.method, r.url, r.post_data))
          if "/api/folders/acl" in r.url and r.method != "GET" else None)
    await pg.set_viewport_size({"width": 1280, "height": 800})
    await pg.goto(BASE + "/admin#library", wait_until="networkidle")
    await pg.wait_for_selector("#aclPick-btn", timeout=8000)
    await pg.wait_for_timeout(300)

    POP = "#aclPick-pop"
    ROWS = """() => [...document.querySelectorAll('#aclPick-tree [role=treeitem]')].map(r => ({
        f: r.dataset.folder, lv: +r.getAttribute('aria-level'),
        exp: r.getAttribute('aria-expanded'), off: r.getAttribute('aria-disabled') === 'true',
        sel: r.getAttribute('aria-selected') === 'true',
        name: r.querySelector('.ftree-name').textContent,
        ctx: (r.querySelector('.ftree-ctx') || {}).textContent || '',
        tag: (r.querySelector('.ftree-tag') || {}).textContent || '',
        mark: !!r.querySelector('mark'), label: r.getAttribute('aria-label') }))"""

    async def rows():
        return await pg.evaluate(ROWS)

    async def row(folder):
        return next((r for r in await rows() if r["f"] == folder), None)

    def rsel(folder):
        return f'#aclPick-tree [role=treeitem][data-folder="{folder}"]'

    async def focused():
        return await pg.evaluate(
            "() => document.activeElement && document.activeElement.dataset.folder")

    async def is_open():
        return not await pg.evaluate("() => document.querySelector('#aclPick-pop').hidden")

    # ---- 關著的時候不佔版面
    closed = await pg.evaluate("""() => {
        const b = document.querySelector('#aclPick-btn');
        const pop = document.querySelector('#aclPick-pop');
        const form = document.querySelector('.acl-form');
        return { hidden: pop.hidden, popH: pop.offsetHeight, formH: form.offsetHeight,
                 expanded: b.getAttribute('aria-expanded'), haspopup: b.getAttribute('aria-haspopup'),
                 name: (b.getAttribute('aria-labelledby') || '').split(' ')
                          .map(id => (document.getElementById(id) || {}).textContent || '')
                          .join(' ').replace(/\\s+/g, ' ').trim(),
                 treeRows: document.querySelectorAll('#aclPick-tree [role=treeitem]').length };
    }""")
    check("picker 關著時資料夾樹不在畫面上（不佔主要頁面高度）",
          closed["hidden"] and closed["popH"] == 0 and closed["treeRows"] == 0, closed)
    check(f"新增限制的整個表單很矮（{closed['formH']}px；原本光清單就 320px）",
          closed["formH"] < 260, closed)
    check("trigger 有 accessible name、aria-haspopup=dialog、aria-expanded=false",
          "資料夾" in closed["name"] and "選擇要限制的資料夾" in closed["name"]
          and closed["haspopup"] == "dialog" and closed["expanded"] == "false", closed)

    # ---- 不能手打路徑
    inputs = await pg.evaluate("""() => [...document.querySelectorAll('#tab-library .acl input')]
        .map(i => ({ id: i.id, type: i.type }))""")
    check("沒有 aclPrefix", not any(i["id"] == "aclPrefix" for i in inputs), inputs)
    check("ACL 區塊的文字欄位只有備註與 picker 內的搜尋（沒有任何可以打路徑的欄位）",
          all(i["type"] == "checkbox" or i["id"] in ("aclNote", "aclPick-q") for i in inputs),
          inputs)
    check("畫面上講明「密碼登入沒有身分、無法授權」",
          "密碼登入沒有帳號身分" in await pg.locator("#tab-library .acl").inner_text())

    # ---- 打開
    await pg.click("#aclPick-btn")
    await pg.wait_for_timeout(250)
    op = await pg.evaluate("""() => {
        const b = document.querySelector('#aclPick-btn'), pop = document.querySelector('#aclPick-pop');
        const br = b.getBoundingClientRect(), pr = pop.getBoundingClientRect();
        return { open: !pop.hidden, expanded: b.getAttribute('aria-expanded'),
                 role: pop.getAttribute('role'), tree: !!pop.querySelector('[role=tree]'),
                 below: pr.top >= br.bottom - 1, wide: pr.width >= br.width - 1, h: pr.height,
                 focus: document.activeElement && document.activeElement.id };
    }""")
    check("點 trigger → 浮層打開、aria-expanded=true、role=dialog 內有 role=tree",
          op["open"] and op["expanded"] == "true" and op["role"] == "dialog" and op["tree"], op)
    check("桌機：浮層在 trigger 下方、至少跟 trigger 一樣寬、不超過 520px 高",
          op["below"] and op["wide"] and op["h"] <= 521, op)
    check("桌機打開時焦點在搜尋框", op["focus"] == "aclPick-q", op)
    r0 = await rows()
    check("初始只顯示最上層", r0 and all(r["lv"] == 1 for r in r0), r0)
    check("最上層有 HBO／Movies／HomeVideo／Private",
          {"/HBO", "/Movies", "/HomeVideo", "/Private"} <= {r["f"] for r in r0},
          [r["f"] for r in r0])
    pr = await row("/Private")
    check("已受限的列有狀態、而且 aria-disabled",
          pr and pr["off"] and "已受限" in pr["tag"] and "已受限" in pr["label"], pr)

    # ---- 展開／收合
    await pg.click(rsel("/HBO") + " [data-toggle]")
    await pg.wait_for_timeout(120)
    after = await rows()
    check("展開 HBO → 子資料夾出現在第 2 層",
          any(r["f"] == "/HBO/Westworld" and r["lv"] == 2 for r in after)
          and (await row("/HBO"))["exp"] == "true", [r["f"] for r in after])
    await pg.click(rsel("/HBO") + " [data-toggle]")
    await pg.wait_for_timeout(120)
    check("再按一次收合", not any(r["f"].startswith("/HBO/") for r in await rows())
          and (await row("/HBO"))["exp"] == "false")

    # ---- 被涵蓋：找得到、選不了
    await pg.click(f"{POP} [data-show=covered]")
    await pg.wait_for_timeout(100)
    # aria-disabled 在 Playwright 眼裡是「不能點」，但滑鼠點得到 —— 要測的正是點了會怎樣
    await pg.click(rsel("/Private"), force=True)   # 不能選的列，點了是展開
    await pg.wait_for_timeout(120)
    cv = await row("/Private/Stuff")
    check("打開「被涵蓋」、點 /Private 展開 → /Private/Stuff 標著「被 /Private 涵蓋」",
          cv and cv["off"] and "/Private" in cv["tag"], cv)
    await pg.click(rsel("/Private/Stuff"), force=True)
    await pg.wait_for_timeout(120)
    check("被涵蓋的列點了不會被選", not (await row("/Private/Stuff"))["sel"]
          and await pg.evaluate("() => document.querySelector('[data-fp-ok]').disabled"))
    await pg.click(f"{POP} [data-show=covered]")
    await pg.wait_for_timeout(100)

    # ---- 同名消歧義
    await pg.click(rsel("/Movies") + " [data-toggle]")
    await pg.click(rsel("/HomeVideo") + " [data-toggle]")
    await pg.wait_for_timeout(120)
    m24, h24 = await row("/Movies/2024"), await row("/HomeVideo/2024")
    check("同名資料夾各自帶上層脈絡（Movies / 2024、HomeVideo / 2024）",
          m24 and h24 and m24["ctx"] == "Movies / 2024" and h24["ctx"] == "HomeVideo / 2024",
          (m24, h24))
    check("完整路徑在 aria-label 裡", m24 and m24["label"].startswith("/Movies/2024"), m24)

    # ---- 搜尋
    await pg.fill("#aclPick-q", "season")
    await pg.wait_for_timeout(150)
    sr = await rows()
    fs_ = [r["f"] for r in sr]
    check("搜深層資料夾：命中的列出現", "/HBO/Westworld/Season 01" in fs_, fs_)
    check("祖先也顯示而且展開", "/HBO" in fs_ and "/HBO/Westworld" in fs_
          and all(r["exp"] == "true" for r in sr if r["f"] in ("/HBO", "/HBO/Westworld")), sr)
    check("命中字有標亮", any(r["mark"] for r in sr if r["f"] == "/HBO/Westworld/Season 01"), sr)
    check("不相干的列不出現", "/Movies" not in fs_, fs_)
    await pg.fill("#aclPick-q", "stuff")
    await pg.wait_for_timeout(150)
    hid = await pg.evaluate("() => document.querySelector('#aclPick-pop .fp-msg').textContent")
    check("命中的只有被藏起來的 → 說「有但沒顯示」而不是「找不到」", "目前沒顯示" in hid, hid)
    await pg.press("#aclPick-q", "Escape")
    await pg.wait_for_timeout(150)
    back = await pg.evaluate("() => document.querySelector('#aclPick-q').value")
    check("搜尋中按 Escape 先清字、不關浮層", back == "" and await is_open(), back)
    fs2 = [r["f"] for r in await rows()]
    check("清掉搜尋 → 回到搜尋前的展開狀態（Movies、HomeVideo 開著、HBO 收著）",
          "/Movies/2024" in fs2 and "/HomeVideo/2024" in fs2 and "/HBO/Westworld" not in fs2, fs2)

    # ---- 暫選 → 確認：點一下不會送出
    await pg.click(rsel("/HBO") + " [data-toggle]")
    await pg.wait_for_timeout(100)
    n0 = len(reqs)
    await pg.click(rsel("/HBO/Westworld"))
    await pg.wait_for_timeout(150)
    st = await pg.evaluate("""() => ({
        staged: document.querySelector('#aclPick-pop .fp-staged').textContent,
        ok: !document.querySelector('[data-fp-ok]').disabled,
        add: !document.querySelector('#aclAdd').disabled })""")
    check("點資料夾只是暫選：浮層還開著、footer 顯示已選、「加入限制」還不能按",
          await is_open() and "/HBO/Westworld" in st["staged"] and st["ok"] and not st["add"], st)
    check("點資料夾不會送出任何請求", len(reqs) == n0, reqs[n0:])
    await pg.click(f"{POP} [data-fp-ok]")
    await pg.wait_for_timeout(150)
    tg = await pg.evaluate("""() => { const b = document.querySelector('#aclPick-btn');
        return { main: (b.querySelector('.folder-pick-main') || {}).textContent || '',
                 sub: (b.querySelector('.folder-pick-sub') || {}).textContent || '',
                 add: !document.querySelector('#aclAdd').disabled,
                 focus: document.activeElement === b, expanded: b.getAttribute('aria-expanded') }; }""")
    check("按「選擇」→ 浮層關起來、焦點回 trigger",
          not await is_open() and tg["focus"] and tg["expanded"] == "false", tg)
    check("trigger 主行是名稱（HBO / Westworld）、次行是完整路徑",
          "Westworld" in tg["main"] and tg["sub"].strip() == "/HBO/Westworld", tg)
    check("這時才能按「加入限制」，而且還沒送出", tg["add"] and len(reqs) == n0, tg)

    # ---- Escape 關閉不丟掉已確認的選擇；點外面也會關
    await pg.click("#aclPick-btn")
    await pg.wait_for_timeout(150)
    check("重開時已確認的那一列是暫選狀態", (await row("/HBO/Westworld") or {}).get("sel"),
          await row("/HBO/Westworld"))
    await pg.keyboard.press("Escape")
    await pg.wait_for_timeout(150)
    es = await pg.evaluate("""() => ({
        sub: (document.querySelector('#aclPick-btn .folder-pick-sub') || {}).textContent || '',
        focus: document.activeElement && document.activeElement.id })""")
    check("Escape 關閉、焦點回 trigger、已選的還在",
          not await is_open() and es["focus"] == "aclPick-btn" and "/HBO/Westworld" in es["sub"], es)
    await pg.click("#aclPick-btn")
    await pg.wait_for_timeout(150)
    await pg.mouse.click(1270, 20)
    await pg.wait_for_timeout(150)
    check("點外面會關閉", not await is_open())

    # ---- 全程鍵盤：從 trigger 選到 /Movies/2024
    await pg.focus("#aclPick-btn")
    await pg.keyboard.press("ArrowDown")
    await pg.wait_for_timeout(150)
    check("trigger 上按 ArrowDown 打開", await is_open())
    await pg.keyboard.press("ArrowDown")          # 搜尋框 → 樹
    await pg.wait_for_timeout(80)
    role = await pg.evaluate("() => document.activeElement.getAttribute('role')")
    check("搜尋框按 ArrowDown 進到樹裡（焦點在 treeitem 上）", role == "treeitem", role)
    await pg.keyboard.press("Home")
    for _ in range(20):
        if await focused() == "/Movies":
            break
        await pg.keyboard.press("ArrowDown")
    check("↓ 可以一列一列移動", await focused() == "/Movies", await focused())
    if (await row("/Movies"))["exp"] == "true":
        await pg.keyboard.press("ArrowLeft")
        await pg.wait_for_timeout(60)
    check("← 收合", (await row("/Movies"))["exp"] == "false", await row("/Movies"))
    await pg.keyboard.press("ArrowRight")
    await pg.wait_for_timeout(60)
    check("→ 展開", (await row("/Movies"))["exp"] == "true", await row("/Movies"))
    await pg.keyboard.press("ArrowRight")
    await pg.wait_for_timeout(60)
    check("已展開時再按 → 移到子資料夾", await focused() == "/Movies/2024", await focused())
    await pg.keyboard.press("ArrowLeft")
    await pg.wait_for_timeout(60)
    check("子資料夾上按 ← 回到父層", await focused() == "/Movies", await focused())
    await pg.keyboard.press("ArrowDown")
    await pg.keyboard.press("Enter")
    await pg.wait_for_timeout(80)
    check("Enter 暫選（不送出）", (await row("/Movies/2024"))["sel"] and len(reqs) == n0,
          await row("/Movies/2024"))
    await pg.keyboard.press("Enter")              # 同一列再 Enter = 確認
    await pg.wait_for_timeout(150)
    sub = await pg.evaluate(
        "() => (document.querySelector('#aclPick-btn .folder-pick-sub') || {}).textContent || ''")
    check("再按一次 Enter 確認：浮層關閉、選到 /Movies/2024（全程只用鍵盤）",
          not await is_open() and sub.strip() == "/Movies/2024", sub)

    # ---- 加入限制：confirm 之後才 POST
    await pg.fill("#aclNote", "瀏覽器測試")
    await pg.click("#aclAdd")
    await pg.wait_for_timeout(200)
    check("按「加入限制」先跳確認框、還沒送出",
          not await pg.evaluate("() => document.querySelector('#modal').hidden") and len(reqs) == n0)
    await pg.click("#modal [data-no]")
    await pg.wait_for_timeout(200)
    check("確認框按取消 → 什麼都沒送", len(reqs) == n0, reqs[n0:])
    await pg.click("#aclAdd")
    await pg.wait_for_timeout(200)
    await pg.click("#modal [data-yes]")
    await pg.wait_for_timeout(1200)
    posts = [r for r in reqs[n0:] if r[0] == "POST"]
    body = json.loads(posts[0][2]) if posts else {}
    check("確認之後才 POST，prefix 是從清單挑的那一個",
          len(posts) == 1 and body.get("prefix") == "/Movies/2024"
          and body.get("note") == "瀏覽器測試", (posts, body))
    await pg.wait_for_selector(".acl-rule", timeout=8000)
    await pg.wait_for_timeout(300)

    # ---- 規則卡：摘要
    cards = await pg.evaluate("""() => [...document.querySelectorAll('.acl-rule')].map(c => ({
        path: c.querySelector('.acl-rule-path').textContent, who: c.querySelector('.acl-who').textContent,
        warn: !!c.querySelector('.acl-warn'), text: c.textContent,
        editHidden: c.querySelector('.acl-edit').hidden,
        visibleChecks: [...c.querySelectorAll('input[type=checkbox]')].filter(i => i.offsetParent).length,
        rm: c.querySelector('[data-aclrm]').className }))""")
    cp = next((c for c in cards if c["path"] == "/Private"), None)
    cn = next((c for c in cards if c["path"] == "/Movies/2024"), None)
    check("規則卡預設收合：看不到任何 checkbox",
          cards and all(c["editHidden"] and c["visibleChecks"] == 0 for c in cards), cards)
    check("/Private 顯示「1 人可看」（管理員不算在裡面）", cp and cp["who"] == "1 人可看", cp)
    check("剛建的規則顯示「尚未授權任何一般帳號／管理員仍可存取」",
          cn and cn["warn"] and "尚未授權任何一般帳號" in cn["text"]
          and "管理員仍可存取" in cn["text"], cn)
    check("「移除限制」是次要的危險樣式", cp and "subtle-danger" in cp["rm"], cp)
    check("涵蓋的子資料夾預設不攤開", await pg.evaluate(
        "() => [...document.querySelectorAll('.acl-covers')].every(x => x.hidden)"))
    await pg.click("#aclPick-btn")
    await pg.click(rsel("/Movies") + " [data-toggle]")
    await pg.wait_for_timeout(120)
    dup = await row("/Movies/2024")
    check("建好之後 /Movies/2024 變成已受限、不能再選一次",
          dup and dup["off"] and "已受限" in dup["tag"], dup)
    await pg.keyboard.press("Escape")
    await pg.wait_for_timeout(100)

    # ---- 授權：dirty state、儲存、管理員授權保留
    card = '.acl-rule[data-rule="%d"]' % ACL_RULE
    await pg.click(card + " [data-acledit]")
    await pg.wait_for_timeout(120)
    ed = await pg.evaluate("""(sel) => { const c = document.querySelector(sel);
        return { open: !c.querySelector('.acl-edit').hidden,
                 exp: c.querySelector('[data-acledit]').getAttribute('aria-expanded'),
                 ids: [...c.querySelectorAll('[data-aclu]')].map(i => +i.dataset.aclu),
                 saveDisabled: c.querySelector('[data-aclsave]').disabled,
                 dirtyHidden: c.querySelector('.acl-dirty').hidden }; }""", card)
    check("按「管理授權」才展開 checkbox（aria-expanded=true）", ed["open"] and ed["exp"] == "true", ed)
    check("管理員不在勾選清單裡", U_ADMIN not in ed["ids"] and U_MARK in ed["ids"], ed)
    check("沒改之前「儲存授權」是停用的、沒有未儲存標記", ed["saveDisabled"] and ed["dirtyHidden"], ed)
    n1 = len(reqs)
    alice = f'{card} [data-aclu="{U_ALICE}"]'
    await pg.click(alice)
    await pg.wait_for_timeout(120)
    dt = await pg.evaluate("""(sel) => { const c = document.querySelector(sel);
        return { dirty: c.classList.contains('is-dirty') && !c.querySelector('.acl-dirty').hidden,
                 save: !c.querySelector('[data-aclsave]').disabled }; }""", card)
    check("勾一個 → 卡片顯示「尚未儲存」、儲存變成可按", dt["dirty"] and dt["save"], dt)
    check("勾選不會立刻打 API", len(reqs) == n1, reqs[n1:])
    await pg.click(alice)
    await pg.wait_for_timeout(80)
    check("勾回原狀 → 不再是未儲存", await pg.evaluate(
        "(sel) => document.querySelector(sel + ' [data-aclsave]').disabled", card))
    await pg.click(alice)
    await pg.click(card + " [data-aclcancel]")
    await pg.wait_for_timeout(100)
    cc = await pg.evaluate("""([sel, id]) => { const c = document.querySelector(sel);
        return { checked: c.querySelector(`[data-aclu="${id}"]`).checked,
                 closed: c.querySelector('.acl-edit').hidden,
                 dirty: c.classList.contains('is-dirty') }; }""", [card, U_ALICE])
    check("取消 → 恢復原本的勾選、收起來、沒有未儲存",
          not cc["checked"] and cc["closed"] and not cc["dirty"] and len(reqs) == n1, cc)
    await pg.click(card + " [data-acledit]")
    await pg.click(alice)
    await pg.click(card + " [data-aclsave]")
    await pg.wait_for_timeout(1200)
    puts = [r for r in reqs[n1:] if r[0] == "PUT"]
    sent = sorted(json.loads(puts[0][2])["user_ids"]) if puts else []
    check("儲存才 PUT，一次送整份名單",
          len(puts) == 1 and f"/folders/acl/{ACL_RULE}/grants" in puts[0][1], puts)
    check("送出的名單包含先前授權、後來升管理員的那個 id（不會被安靜地收回）",
          sent == sorted([U_MARK, U_ALICE, U_ADMIN]), sent)
    kept = await pg.evaluate("""async (rid) => (await (await fetch('/api/folders/acl')).json())
        .rules.find(r => r.id === rid).user_ids""", ACL_RULE)
    check("存完之後後端的授權裡仍有那位管理員", U_ADMIN in kept and U_ALICE in kept, kept)

    # ---- 移除限制：仍然要 confirm，而且警告還在
    await pg.wait_for_selector(card, timeout=8000)
    await pg.wait_for_timeout(200)
    await pg.click(card + " [data-aclrm]")
    await pg.wait_for_timeout(200)
    mt = await pg.evaluate("() => document.querySelector('#modal').textContent")
    check("移除限制要確認，並警告「所有登入者都看得到」", "所有登入者都看得到" in mt, mt[:80])
    await pg.click("#modal [data-no]")
    await pg.wait_for_timeout(150)
    check("取消就什麼都沒刪", not any(r[0] == "DELETE" for r in reqs), reqs)

    # ---- 手機：bottom sheet
    for w in (390, 320):
        await pg.set_viewport_size({"width": w, "height": 780})
        await pg.wait_for_timeout(250)
        await pg.evaluate("() => document.querySelector('#aclPick-btn').scrollIntoView()")
        await pg.click("#aclPick-btn")
        await pg.wait_for_timeout(300)
        await pg.fill("#aclPick-q", "一個非常")
        await pg.wait_for_timeout(150)
        mb = await pg.evaluate("""() => {
            const pop = document.querySelector('#aclPick-pop');
            const r = pop.getBoundingClientRect();
            const ok = pop.querySelector('[data-fp-ok]').getBoundingClientRect();
            const bd = document.querySelector('.folder-pick-backdrop');
            const doc = document.documentElement;
            const rowsOver = [...pop.querySelectorAll('[role=treeitem]')]
                .some(x => x.getBoundingClientRect().right > innerWidth + 1);
            return { pos: getComputedStyle(pop).position, left: r.left, right: r.right,
                     bottom: r.bottom, top: r.top, vw: innerWidth, vh: innerHeight,
                     okBottom: ok.bottom, okVisible: ok.height > 0,
                     backdrop: !bd.hidden && getComputedStyle(bd).display !== 'none',
                     over: doc.scrollWidth - doc.clientWidth, popOver: pop.scrollWidth - pop.clientWidth,
                     rowsOver, n: pop.querySelectorAll('[role=treeitem]').length,
                     modal: pop.getAttribute('aria-modal') };
        }""")
        check(f"@{w}px picker 是貼底的 bottom sheet（position:fixed、貼齊左右與底部）",
              mb["pos"] == "fixed" and abs(mb["bottom"] - mb["vh"]) <= 1
              and mb["left"] <= 0.5 and abs(mb["right"] - mb["vw"]) <= 1, mb)
        check(f"@{w}px sheet 不超過 85% 高、有 backdrop、aria-modal",
              mb["top"] >= mb["vh"] * 0.15 - 1 and mb["backdrop"] and mb["modal"] == "true", mb)
        check(f"@{w}px「選擇」按鈕在畫面內", mb["okVisible"] and mb["okBottom"] <= mb["vh"] + 0.5, mb)
        check(f"@{w}px 長路徑不會造成水平爆版（{mb['n']} 列）",
              mb["n"] > 0 and mb["over"] <= 1 and mb["popOver"] <= 1 and not mb["rowsOver"], mb)
        await pg.click(".folder-pick-backdrop", position={"x": 10, "y": 10})
        await pg.wait_for_timeout(200)
        check(f"@{w}px 點 backdrop 關閉", not await is_open())
    over = await pg.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"手機上整個 /admin#library 不橫捲（溢出 {over}px）", over <= 1, over)
    check("ACL 頁沒有 JS 錯誤", not errs, errs[:3])
    await pg.close()


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
