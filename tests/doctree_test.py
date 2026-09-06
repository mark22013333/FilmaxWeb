"""在真的瀏覽器裡驗文件牆的資料夾樹。

原本的文件牆把所有資料夾一次平鋪成標籤。實測庫裡有 452 個資料夾、97% 落在
第 7～8 層，而標籤只顯示路徑最後一層 —— 畫面上就是一排分不出來的「PDF」
「教用」。改成懶載入的樹之後，這一支負責證明它真的會動：

  - 一開始只畫一層，不是把整棵樹攤開
  - 點箭頭只展開、點名字才換內容（兩件事分開）
  - 選中中間層看得到底下所有層的檔案（子樹查詢）
  - 列表顯示的是相對路徑，不是每一列都一樣的最後一層
  - 搜尋跨全庫，不被選中的資料夾綁住

    python tests/doctree_test.py
"""
import asyncio, os, shutil, sys, tempfile, threading, time
from pathlib import Path

# 這一支會印出路徑分隔用的「›」，而 Windows 主控台預設是 cp950 —— 吐不出來
# 會直接讓測試在 print 裡爆掉（看起來像測試失敗，其實是主控台編碼）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-tree-")
PORT = 29322
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "HOST": "127.0.0.1", "PORT": str(PORT),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
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


from app import db                                  # noqa: E402

db.init_db()
now = db.now()

# 刻意做成真實庫的形狀：根底下只有一條路（要被自動穿透掉），中間有一段
# 也是單鏈（要被併成「A / 深」），葉節點的名字故意重複（「教用」「學用」
# 在兩個不同的單元底下各有一份）——重複正是原本那版分不出來的東西。
ROWS = [
    ("/庫/Book/國文/第一單元/教用", "n1.pdf"),
    ("/庫/Book/國文/第一單元/學用", "n2.pdf"),
    ("/庫/Book/國文/第二單元/教用", "n3.pdf"),
    ("/庫/Book/國文/第二單元/學用", "n4.pdf"),
    ("/庫/Book/數學/單鏈/再一層", "m1.pdf"),
    ("/庫/Book/數學/單鏈/再一層", "m2.pdf"),
]
for i, (folder, fn) in enumerate(ROWS, 1):
    db.execute("INSERT INTO document(ftp_path,folder,filename,ext,size,probe_state,"
               "seen_at,added_at) VALUES(?,?,?,'pdf',1024,'ok',?,?)",
               (f"{folder}/{fn}", folder, fn, now, now))

# 相片牆走的是另一套（封面標籤，不是樹）—— 相簿全在同一層而且有縮圖。
# 「外拍/外拍」是真實庫裡就有的父子同名，那是原本兩個標籤長得一模一樣的原因。
PHOTOS = [
    ("/庫/相簿/外拍/外拍", 3),
    ("/庫/相簿/旅行/外拍", 2),
    ("/庫/相簿/一個名字很長很長很長很長很長很長很長很長很長的相簿", 4),
]
for folder, n in PHOTOS:
    for i in range(n):
        db.execute("INSERT INTO photo(ftp_path,folder,filename,ext,size,mtime_ts,sort_ts,"
                   "probe_state,seen_at,added_at) VALUES(?,?,?,'jpg',1024,?,?,'ok',?,?)",
                   (f"{folder}/p{i}.jpg", folder, f"p{i}.jpg",
                    int(now) - i, int(now) - i, now, now))

import uvicorn                                      # noqa: E402
from app.main import app                            # noqa: E402

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
        ctx = await b.new_context(viewport={"width": 1280, "height": 900})
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" else None)

        await pg.goto(BASE + "/", wait_until="networkidle")
        await pg.click("nav button[data-kind='doc']")
        await pg.wait_for_selector("#docTreeBody .tnode", timeout=8000)

        print("\n[1] 一開始只畫一層")
        top = await pg.eval_on_selector_all(
            "#docTreeBody > .tnode-wrap > .tnode .tn", "els => els.map(e => e.textContent)")
        # 根是 /庫 → Book，兩層都只有一條路，應該被自動跳過，直接看到分岔。
        # 「數學」底下也只有一條路，所以它自己就被併成「數學 / 單鏈」——
        # 併到 CHAIN_MAX(2) 段就停，不會一路併成「數學 / 單鏈 / 再一層」。
        check(f"根的單鏈被跳過，第一層就是分岔（{top}）",
              sorted(top) == ["國文", "數學 / 單鏈"], top)
        allnodes = await pg.eval_on_selector_all("#docTreeBody .tnode", "els => els.length")
        check(f"沒有把整棵樹攤開（畫面上 {allnodes} 個節點）", allnodes == 2, allnodes)

        print("\n[2] 單鏈穿透有上限，併不完的留給下一次展開")
        await pg.click("#docTreeBody .tnode:has(.tn:text-is('數學 / 單鏈')) .tw")
        await pg.wait_for_timeout(500)
        kids = await pg.eval_on_selector_all(
            "[data-kids='/庫/Book/數學/單鏈'] .tn", "els => els.map(e => e.textContent)")
        check(f"併到上限就停，剩下的在下一層（{kids}）", kids == ["再一層"], kids)

        print("\n[3] 點箭頭只展開，點名字才換內容")
        title_before = await pg.text_content("#docCount")
        await pg.click("#docTreeBody .tnode:has(.tn:text-is('國文')) .tw")
        await pg.wait_for_timeout(500)
        check("點箭頭不會換右邊的內容",
              await pg.text_content("#docCount") == title_before,
              (title_before, await pg.text_content("#docCount")))
        check("點箭頭有展開子節點",
              await pg.eval_on_selector_all(
                  "[data-kids='/庫/Book/國文'] .tnode", "els => els.length") == 2)

        print("\n[4] 選中中間層 → 看得到底下所有層的檔案")
        await pg.click("#docTreeBody .tnode:has(.tn:text-is('國文')) .tn")
        await pg.wait_for_timeout(700)
        n = await pg.eval_on_selector_all("#dgrid .doc", "els => els.length")
        check(f"選「國文」看得到底下 4 份（子樹查詢；不是 0）", n == 4, n)
        check("選中的節點有標示出來",
              await pg.eval_on_selector_all("#docTreeBody .tnode.on", "e => e.length") == 1)

        print("\n[5] 列表顯示相對路徑，不是每列都一樣")
        subs = await pg.eval_on_selector_all("#dgrid .doc .dn span",
                                             "els => els.map(e => e.textContent)")
        check(f"每列顯示相對於選中節點的路徑（{subs}）",
              sorted(subs) == ["第一單元 › 學用", "第一單元 › 教用",
                               "第二單元 › 學用", "第二單元 › 教用"], subs)
        check("重複的末層名字不再無法分辨（4 列有 4 種顯示）", len(set(subs)) == 4, subs)

        # 選到最底層時剩餘路徑是空的，不該再顯示冗餘的自己
        await pg.click("#docTreeBody .tnode:has(.tn:text-is('第一單元')) .tw")
        await pg.wait_for_timeout(400)
        await pg.click("[data-kids='/庫/Book/國文/第一單元'] .tnode:has(.tn:text-is('教用')) .tn")
        await pg.wait_for_timeout(700)
        leaf = await pg.eval_on_selector_all("#dgrid .doc .dn span",
                                             "els => els.map(e => e.textContent)")
        check(f"選到最底層就不再顯示冗餘路徑（{leaf}）", leaf == [""], leaf)

        print("\n[6] 麵包屑")
        crumbs = await pg.eval_on_selector_all("#docCrumbs button, #docCrumbs .cur",
                                               "els => els.map(e => e.textContent)")
        check(f"麵包屑跟著選取走（{crumbs}）",
              crumbs and crumbs[0] == "全部" and crumbs[-1] == "教用", crumbs)
        await pg.click("#docCrumbs button:text-is('國文')")
        await pg.wait_for_timeout(700)
        check("點麵包屑跳回上層（又看到 4 份）",
              await pg.eval_on_selector_all("#dgrid .doc", "e => e.length") == 4)

        print("\n[7] 搜尋跨全庫，不被選中的資料夾綁住")
        await pg.fill("#q", "m1")
        await pg.wait_for_timeout(900)
        hits = await pg.eval_on_selector_all("#dgrid .doc .dn b",
                                             "els => els.map(e => e.textContent)")
        check(f"選著「國文」也搜得到數學底下的檔案（{hits}）", hits == ["m1.pdf"], hits)
        check("有告知這是全庫搜尋",
              "全庫" in (await pg.text_content("#docCount") or ""),
              await pg.text_content("#docCount"))
        await pg.fill("#q", "")
        await pg.wait_for_timeout(900)

        print("\n[8] 篩選資料夾")
        await pg.fill("#docFolderQ", "第二")
        await pg.wait_for_timeout(700)
        found = await pg.eval_on_selector_all("#docTreeBody .tnode .tn",
                                              "els => els.map(e => e.textContent)")
        check(f"篩選直接跳到符合的資料夾（{found}）",
              found and all("第二單元" in f for f in found), found)

        print("\n[9] 排序在文件牆可以用")
        await pg.fill("#docFolderQ", "")
        await pg.wait_for_timeout(600)
        opts = await pg.eval_on_selector_all("#sort option", "els => els.map(e => e.value)")
        check(f"排序選項換成文件的那組（{opts}）", "size" in opts and "year" not in opts, opts)
        check("排序沒有被整條 toolbar 一起藏掉",
              await pg.is_visible("#sort"))
        check("影片的類型 chips 有藏起來", not await pg.is_visible("#genres"))

        print("\n[10] 相片牆：封面標籤（不是樹）")
        await pg.click("nav button[data-kind='photo']")
        await pg.wait_for_selector("#folderBar .fchip", timeout=8000)
        labels = await pg.eval_on_selector_all("#folderBar .fchip .fc-n",
                                               "els => els.map(e => e.textContent)")
        # 「/庫/相簿/外拍/外拍」是父子同名：補上 segs[-2] 只會得到「外拍 / 外拍」，
        # 所以要再往上找到第一個不一樣的祖先（相簿）才補得出有意義的名字。
        check(f"父子同名的相簿分得出來（{labels}）",
              "相簿 / 外拍" in labels and "旅行 / 外拍" in labels, labels)
        check("沒有兩個標籤長得一模一樣", len(set(labels)) == len(labels), labels)
        check("有「全部」而且帶總張數",
              await pg.eval_on_selector("#folderBar .fchip[data-folder='']",
                                        "e => e.textContent.includes('9')"))
        # 長名字要被截斷，不然標籤列會擠成好幾行把相片推出第一屏
        wide = await pg.eval_on_selector_all(
            "#folderBar .fchip", "els => els.map(e => e.getBoundingClientRect().width)")
        check(f"超長的相簿名有限寬（最寬 {max(wide):.0f}px）", max(wide) <= 261, max(wide))

        print("\n[11] 相片牆的篩選與排序")
        await pg.fill("#photoFolderQ", "旅行")
        await pg.wait_for_timeout(500)
        left = await pg.eval_on_selector_all("#folderBar .fchip .fc-n",
                                             "els => els.map(e => e.textContent)")
        check(f"篩選只留下符合的相簿（{left}）", left == ["旅行 / 外拍"], left)
        await pg.fill("#photoFolderQ", "")
        await pg.wait_for_timeout(500)
        popts = await pg.eval_on_selector_all("#photoSort option", "els => els.map(e => e.value)")
        check(f"相片牆有自己的排序（{popts}）", "taken" in popts and "size" in popts, popts)
        check("相片牆的排序看得到", await pg.is_visible("#photoSort"))

        print("\n[12] 相片牆搜尋也跨全庫")
        await pg.click("#folderBar .fchip:has(.fc-n:text-is('旅行 / 外拍'))")
        await pg.wait_for_timeout(700)
        check("選相簿只看那一本（2 張）",
              await pg.eval_on_selector_all("#pgrid .ph", "e => e.length") == 2)
        await pg.fill("#q", "p3")
        await pg.wait_for_timeout(900)
        # p3.jpg 只存在於「名字很長」那本（4 張才有 p3），選著「旅行」也要搜得到
        check("選著某本相簿也搜得到別本的相片",
              await pg.eval_on_selector_all("#pgrid .ph", "e => e.length") == 1)
        check("有告知這是全庫搜尋",
              "全庫" in (await pg.text_content("#photoCount") or ""),
              await pg.text_content("#photoCount"))
        check("搜尋時相簿標籤不再亮著（畫面上不是那本相簿）",
              await pg.eval_on_selector_all("#folderBar .fchip.on", "e => e.length") == 1
              and await pg.eval_on_selector(
                  "#folderBar .fchip.on", "e => e.dataset.folder === ''"))
        await pg.fill("#q", "")
        await pg.wait_for_timeout(900)

        print("\n[13] 窄螢幕")
        pg2 = await ctx.new_page()
        # 先把視窗縮小再切分頁 —— 抽屜的初始狀態是切過去的當下決定的
        await pg2.set_viewport_size({"width": 375, "height": 720})
        await pg2.goto(BASE + "/", wait_until="networkidle")
        await pg2.click("nav button[data-kind='doc']")
        # 抽屜預設是收起來的，所以節點存在但不可見 —— 等 attached 而不是 visible
        await pg2.wait_for_selector("#docTreeBody .tnode", state="attached", timeout=8000)
        check("窄螢幕出現抽屜開關", await pg2.is_visible("#docTreeToggle"))
        check("窄螢幕的抽屜預設收起來（不然一進來滿螢幕都是資料夾）",
              not await pg2.is_visible("#docTreeBody"))
        over = await pg2.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(f"文件牆在 375px 不橫捲（溢出 {over}px）", over <= 1, over)
        await pg2.click("#docTreeToggle")
        await pg2.wait_for_timeout(400)
        check("抽屜打得開", await pg2.is_visible("#docTreeBody"))
        await pg2.click("#docTreeBody .tnode:has(.tn:text-is('國文')) .tn")
        await pg2.wait_for_timeout(600)
        check("選完之後抽屜自己收起來", not await pg2.is_visible("#docTreeBody"))

        # 相片牆的長相簿名在窄螢幕最容易撐破版面（限寬是 260px，螢幕才 375px）
        await pg2.click("nav button[data-kind='photo']")
        await pg2.wait_for_selector("#folderBar .fchip", timeout=8000)
        pover = await pg2.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(f"相片牆在 375px 不橫捲（溢出 {pover}px）", pover <= 1, pover)
        await pg2.close()

        real = [e for e in errs if "Failed to load resource" not in e and "ERR_" not in e]
        check("沒有 JS 錯誤", not real, real[:3])
        await b.close()


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*52}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
