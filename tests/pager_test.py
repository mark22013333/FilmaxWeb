"""分頁器與排序穩定性（P 章第 1 期）。

    python tests/pager_test.py

兩部分：
  1. `pagerPages()` 的省略號版面 —— 用 node 跑，因為它是 app.js 裡的純函式。
     找不到 node 就跳過那一段（不讓沒裝 node 的機器紅燈）。
  2. 排序的決定性收尾 —— 這一項是無限捲動（P-2）的正確性前提，用 SQL 驗。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-pager-")).resolve())
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
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


# ============================================================ 版面
head("[P-1] 頁碼的省略號版面")

APP_JS = ROOT / "app" / "static" / "app.js"
src = APP_JS.read_text(encoding="utf-8")

node = shutil.which("node")
if not node:
    print("  (跳過：這台機器上找不到 node)")
else:
    # app.js 依賴 DOM，不能整個 require —— 只抽這個純函式出來跑
    start = src.index("function pagerPages")
    end = src.index("\n}", start) + 2
    fn = src[start:end]
    cases = [
        ([1, 1, 2], [1], "只有 1 頁"),
        ([1, 5, 2], [1, 2, 3, 4, 5], "5 頁全部列出，沒有省略號"),
        ([1, 335, 2], [1, 2, 3, "…", 335], "第 1 頁"),
        ([8, 335, 2], [1, "…", 6, 7, 8, 9, 10, "…", 335], "第 8 頁（前後各 2）"),
        ([335, 335, 2], [1, "…", 333, 334, 335], "最後一頁"),
        ([3, 335, 2], [1, 2, 3, 4, 5, "…", 335], "第 3 頁不該有前省略號"),
        ([4, 335, 2], [1, 2, 3, 4, 5, 6, "…", 335],
         "只跳過一頁時直接列出那一頁（省略號跟數字一樣寬，藏一個 2 沒意義）"),
        ([8, 335, 1], [1, "…", 7, 8, 9, "…", 335], "手機 window=1"),
    ]
    script = fn + "\nconst out=[];\n" + "".join(
        f"out.push(pagerPages({a},{b},{c}));\n" for (a, b, c), _, _ in cases
    ) + "console.log(JSON.stringify(out));"
    # 明確指定 utf-8：省略號「…」在 Windows 的預設 cp950 解碼下會炸
    r = subprocess.run([node, "-e", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        check("node 跑得起來", False, r.stderr[:200])
    else:
        got = json.loads(r.stdout)
        for (args, want, label), g in zip(cases, got):
            check(label, g == want, f"got={g} want={want}")

check("三個列表都改用共用的 paintPager（不要各寫一份）",
      src.count("paintPager('#") == 3, src.count("paintPager('#"))
check("三個分頁器都記進 _pagerLast（resize 時才重畫得出來）",
      "_pagerLast.set(sel" in src)
check("視窗寬度變了要重畫（手機橫轉直時 window 從 2 變 1）",
      "addEventListener('resize'" in src and "_pagerResizeTimer" in src)
check("頁碼超出範圍會夾住（停在最後一頁時刪相片，總數變少而 page 沒變）",
      "Math.min(Math.max(1, page), pages)" in src)
check("舊的全域 window.go 已經移除（行內 onclick 需要它，那是最難追的耦合）",
      "window.go =" not in src)
check("分頁用事件委派，不是逐一綁 onclick",
      "data-pg" in src and "closest('[data-pg]')" in src)
check("目前頁有 aria-current", 'aria-current="page"' in src)
check("超過 20 頁才給跳頁輸入框", "PAGER_JUMP_MIN = 20" in src)


# ============================================================ 開關動畫
head("[P-3] Modal 開關動畫")

CSS = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
ov = CSS[CSS.index(".overlay{"):CSS.index(".hero{")]
check("`.overlay` 不再用 display 切換（display 不能做 transition）",
      "display:none" not in ov, ov[:160])
check("關掉時 visibility:hidden（只用 opacity 的話那一層還會擋滑鼠事件）",
      "visibility:hidden" in ov)
check("visibility 的 transition 有延遲（等淡出跑完才真的隱藏）",
      "visibility 0s linear .18s" in ov, ov[:300])
check("開啟時 visibility 立刻生效（0s，沒有延遲）",
      "visibility 0s;" in ov or "visibility 0s\n" in ov)
check("Modal 本身有位移動畫", ".overlay.show .modal{transform:none}" in CSS)
check("開著時鎖住背景捲動", "body.modal-open{overflow:hidden}" in CSS)
check("JS 有跟著加／移除 modal-open",
      "classList.add('modal-open')" in src and "classList.remove('modal-open')" in src)
check("開啟集中在 showOverlay()（三個開啟點各寫一次遲早漏一個）",
      src.count("showOverlay()") >= 4, src.count("showOverlay()"))

rm = CSS[CSS.index("@media(prefers-reduced-motion:reduce){"):]
for sel in (".overlay", ".modal", ".pager .pg"):
    check(f"prefers-reduced-motion 有涵蓋 {sel}", sel in rm, rm[:400])


# ============================================================ HTML 不准快取
head("[P] HTML 進入點不准快取")

# **這是「改了程式卻看不到效果」的根因。**/static/* 走 StaticFiles，它會發
# ETag／Last-Modified 所以每次都會回來問；但 index.html 這幾支是 FileResponse
# 直接吐檔案，瀏覽器可能整份快取 —— 那就連「要載哪些 script」都是舊的，
# 新加的 DOM 元素當然不存在。實際發生過：切換鈕與哨兵都在伺服器端了，
# 使用者的畫面上卻沒有。
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("HTML 進入點帶 no-store", '"Cache-Control": "no-store, must-revalidate"' in MAIN)
check("四支 HTML 路由都走同一個 helper（各寫一次遲早漏一個）",
      MAIN.count('return _page("') == 4, MAIN.count('return _page("'))
# 唯一允許的 FileResponse(STATIC_DIR / ...) 是 _page() 自己；
# 路由裡再出現一次就代表那一支繞過了 no-store。
check("沒有路由繞過 _page() 直接吐檔案（繞過的那一支就會被快取）",
      MAIN.count("FileResponse(STATIC_DIR /") == 1,
      MAIN.count("FileResponse(STATIC_DIR /"))
check("index.html 也在裡面", '_page("index.html")' in MAIN)


# ============================================================ 瀑布流
head("[P-2] 相片瀑布流＋無限捲動")

check("預設維持方格（既有行為，不強迫所有人適應新版面）",
      "mode: 'grid'" in src, src[src.find("const pstate"):src.find("const pstate") + 200])
check("模式存 localStorage", "localStorage.setItem('photoMode'" in src)
_i = src.find("localStorage.getItem('photoMode')")
check("讀 localStorage 有 try/catch（隱私模式會丟例外）",
      _i > 0 and "try {" in src[max(0, _i - 60):_i]
      and "catch" in src[_i:_i + 220], src[max(0, _i - 60):_i + 220])

# **這一項是整個瀑布流最容易錯的地方**：openPhoto(i) 與 PhotoSwipe 的
# thumbEl(n) 都是拿 data-i 對回 pstate.items。覆蓋而不是 append 的話索引
# 就會錯位，使用者點第 80 張會開到第 20 張。
check("下一頁是 append 不是覆蓋", "pstate.items.push(...d.items)" in src)
check("data-i 用累計索引（base + k），不是頁內索引",
      "photoTile(x, base + k)" in src, "索引錯位會讓 PhotoSwipe 開錯張")
check("photoTile 的 data-i 直接來自參數 i", 'data-i="${i}"' in src)

check("有載入上限（20,033 張全塞進 DOM 會吃掉記憶體）", "FLOW_MAX" in src)
check("上限可以被使用者按「繼續載入」推高（上限是防呆不是禁令）",
      "pstate.flowCap += FLOW_MAX" in src)
check("重新載入時上限歸位", "pstate.flowCap = FLOW_MAX" in src)
check("撞到上限與真的到底要分開講（不然使用者以為相片只有這些）",
      "已載入" in src and "已經到底了" in src)

check("同時只跑一個載入（loading 旗標）",
      "if (pstate.loading || pstate.done" in src)
check("換相簿／排序／模式時把累積的清掉（不清的話會翻到上一個相簿的相片）",
      "pstate.items = []; pstate.done = false" in src)
check("載入失敗不清空已看到的、也不無限重試",
      "pstate.done = true;" in src and "updateFlowFooter(d._err)" in src)
check("用 IntersectionObserver 而不是 scroll 事件",
      "new IntersectionObserver" in src)
check("提早載（rootMargin），滑到底時通常已經接上",
      "rootMargin: '600px 0px'" in src)
check("切回方格時停止觀察", "flowObserver.disconnect()" in src)
check("瀑布流沒有「第幾頁」的概念 → 分頁器收起來",
      "$('#ppager').innerHTML = '';" in src)

head("[P-2] 回到頂端")

check("有這顆按鈕", 'id="pTop"' in (ROOT / "app" / "static" / "index.html")
      .read_text(encoding="utf-8"))
# **不能用 scrollToGrid('#pgrid')**：那是給換頁用的（捲到格線頂端、跳過篩選列），
# 而相簿標籤那一列在真實片庫有 110 個標籤、高 2,974px —— 於是「回頂端」
# 會停在 3,095px，完全不是頂端。實機量到才發現的。
check("點下去是真的捲到 0，不是捲到格線頂端",
      "window.scrollTo({ top: 0" in src, "用 scrollToGrid 會停在篩選列底下")
check("尊重 prefers-reduced-motion",
      src.count("prefers-reduced-motion") >= 2)
check("只在瀑布流出現（方格有分頁器，換頁本來就會捲回去）",
      "pstate.mode === 'flow'" in src and "TOP_SHOW_AT" in src)
check("相片牆沒開的時候不出現", "!$('#photoView').hidden" in src)
# 這個環境的 scroll 事件不會觸發（實測 scrollY 到 900、事件 0 次），
# 只靠 scroll 監聽的話按鈕永遠不會出現。視覺元素不該只靠一個訊號源。
check("除了 scroll 事件還有輪詢兜底（有些環境收不到 window 的 scroll）",
      "setInterval(" in src and "_lastY" in src)
check("值沒變就不碰 DOM（輪詢每 300ms 叫一次）",
      "if (b.hidden !== want)" in src)
TOP = CSS[CSS.index(".to-top{"):CSS.index(".pmode{")]
check("位置避開掃描面板（右下）與 toast（正下方置中）",
      "left:" in TOP and "bottom:" in TOP, TOP[:120])
check("z-index 比掃描面板(80)低", "z-index:70" in TOP)
check("手機上只留箭頭", ".to-top span{display:none}" in CSS)
check("prefers-reduced-motion 有涵蓋", ".to-top" in rm, rm[:400])


FLOW = CSS[CSS.index(".pgrid.flow{"):CSS.index(".flow-end{")]
check("瀑布流用 CSS columns，不引進 masonry 函式庫",
      "column-count" in FLOW and "break-inside:avoid" in FLOW, FLOW[:200])
check("不裁切（aspect-ratio 放掉、object-fit 改 contain）",
      "aspect-ratio:auto" in FLOW and "object-fit:contain" in FLOW)
check("<img> 帶 width/height（圖載入前就知道要留多高，版面不會跳）",
      'width="${x.width}" height="${x.height}"' in src)
check("欄數隨寬度縮（手機不能還是 5 欄）",
      CSS.count(".pgrid.flow{column-count") >= 4,
      CSS.count(".pgrid.flow{column-count"))


# ============================================================ 排序穩定性
head("[P-4-3] 排序要有決定性的收尾（無限捲動的前提）")

from app import db  # noqa: E402
db.init_db()

API = (ROOT / "app" / "routers" / "api.py").read_text(encoding="utf-8")
# 三支端點的 order 白名單裡，每一條都要以 id 收尾
import re  # noqa: E402
blocks = re.findall(r'order = \{(.*?)\}\.get\(', API, re.S)
check("找得到三支端點的排序白名單", len(blocks) == 3, len(blocks))
for i, b in enumerate(blocks):
    exprs = re.findall(r'"[a-z]+":\s*"([^"]+)"', b)
    bad = [e for e in exprs if not re.search(r'\bid\b\s*(ASC|DESC)?\s*$', e)]
    check(f"第 {i + 1} 支端點的每一種排序都以 id 收尾", not bad, bad)

# 真的逐頁掃一次，確認不重不漏
now = db.now_i()
for i in range(150):
    db.execute("""INSERT INTO photo(ftp_path, folder, filename, ext, size, mtime,
                                    probe_state, seen_at, added_at)
                  VALUES(?,?,?,?,?,?,'ok',?,?)""",
               (f"/p/{i}.jpg", "/p", "same.jpg", "jpg", 4096, "x", now, now))

seen, page_size = [], 20
while True:
    rows = db.q("""SELECT id FROM photo WHERE probe_state='ok'
                   ORDER BY filename COLLATE NOCASE, id DESC
                   LIMIT ? OFFSET ?""", (page_size, len(seen)))
    if not rows:
        break
    seen += [r["id"] for r in rows]
check("150 筆同檔名的相片逐頁取完：不重複",
      len(seen) == len(set(seen)), f"{len(seen)} 筆但只有 {len(set(seen))} 個不同")
check("150 筆同檔名的相片逐頁取完：不漏掉", len(set(seen)) == 150, len(set(seen)))


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
