"""第三期：系統參數（B+）與管理後台（B）。

    python tests/phase3_test.py
"""
import ast, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-p3-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "true", "AUTH_PASSWORD": "adminpw12345",
    "VIEWER_PASSWORD": "viewerpw12345", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
})
# 這些一定要在 import app 之前清掉，不然本機環境的值會讓測試看到不同的鎖定狀態
for k in ("TRANSCODE_CRF", "MIN_PHOTO_KB", "X264_PRESET", "HDR_TONEMAP",
          "SCAN_EXCLUDE_EXTS", "TMDB_LANGUAGE", "PROBE_CONCURRENCY",
          "SESSION_DAYS", "REMOTE_MAX_HEIGHT", "MIN_FILE_MB",
          "HLS_SEGMENT_SECONDS", "FTP_USER", "FTP_PASSWORD", "FTP_ENCODING",
          "LIBRARY_ROOTS", "TRUST_PROXY", "LOCAL_NETWORKS", "PORT",
          "REMOTE_BITRATE_KBPS"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import db, params, paramstore as ps
from app.config import settings
db.init_db()


# ============================================================ 解析順序
head("[B+] 解析順序：env > DB > 預設值")

check("沒設任何東西時是預設值", ps.resolve("TRANSCODE_CRF") == (21, "default"),
      ps.resolve("TRANSCODE_CRF"))
ps.set_value("TRANSCODE_CRF", 18, actor="test")
check("存進 DB 之後由 DB 決定", ps.resolve("TRANSCODE_CRF") == (18, "db"),
      ps.resolve("TRANSCODE_CRF"))
os.environ["TRANSCODE_CRF"] = "26"
ps.invalidate()
check("環境變數贏過 DB", ps.resolve("TRANSCODE_CRF") == (26, "env"),
      ps.resolve("TRANSCODE_CRF"))

d = ps.describe("TRANSCODE_CRF")
check("三個候選值都回得出來",
      d["candidates"] == {"env": 26, "db": 18, "default": 21}, d["candidates"])
check("locked 標出「這一項後台改不動」", d["locked"] is True)
check("shadowed 標出「DB 有值但被蓋掉」", d["shadowed"] is True)
os.environ.pop("TRANSCODE_CRF")
ps.invalidate()
check("拿掉環境變數就回到 DB 的值", ps.resolve("TRANSCODE_CRF") == (18, "db"))
check("清掉 DB 的值就回到預設",
      ps.clear("TRANSCODE_CRF", actor="test")["effective"]["value"] == 21)

# DB 只存跟預設不同的
ps.set_value("MIN_PHOTO_KB", 40, actor="test")       # 40 就是預設值
check("存跟預設一樣的值時不會留下資料列",
      db.q1("SELECT COUNT(*) c FROM config_param WHERE k='MIN_PHOTO_KB'")["c"] == 0)
ps.set_value("MIN_PHOTO_KB", 55, actor="test")
check("存不一樣的值才留列",
      db.q1("SELECT COUNT(*) c FROM config_param WHERE k='MIN_PHOTO_KB'")["c"] == 1)
ps.set_value("MIN_PHOTO_KB", 40, actor="test")
check("再改回預設值時那一列會被刪掉（不然改預設值時會變成隱形覆蓋）",
      db.q1("SELECT COUNT(*) c FROM config_param WHERE k='MIN_PHOTO_KB'")["c"] == 0)


# ============================================================ 分層
head("[B+] 分層")

for key, tier in (("HOST", params.ENV_ONLY), ("AUTH_SECRET", params.ENV_ONLY),
                  ("MSSQL_HOST", params.ENV_ONLY), ("USER_STORE", params.ENV_ONLY),
                  ("MEDIA_STORE", params.ENV_ONLY)):
    check(f"{key} 是 Tier 0（改錯會進不了後台／讀它時 DB 還沒連上）",
          params.REGISTRY[key].tier == tier, params.REGISTRY[key].tier)
for key in ("FFMPEG_PATH", "FFPROBE_PATH", "LIBRARY_ROOTS", "TRUST_PROXY", "LOCAL_NETWORKS"):
    check(f"{key} 是 Tier 4（會被當命令或路徑執行，不放 UI）",
          params.REGISTRY[key].tier == params.NO_UI, params.REGISTRY[key].tier)
for key in ("TMDB_API_KEY", "FTP_PASSWORD", "GOOGLE_CLIENT_SECRET"):
    check(f"{key} 是秘密", params.REGISTRY[key].secret)

check("Tier 0 / Tier 4 完全不出現在 UI 清單裡",
      not [i for i in ps.describe_all()
           if params.REGISTRY[i["key"]].tier in (params.ENV_ONLY, params.NO_UI)])

for key in ("HOST", "FFMPEG_PATH", "LIBRARY_ROOTS", "AUTH_SECRET"):
    try:
        ps.set_value(key, "x")
        blocked = False
    except ps.NotEditable:
        blocked = True
    check(f"{key} 存不進 DB（不是只有前端擋）", blocked)

# 秘密與分層是獨立的兩件事
check("AUTH_PASSWORD 是 Tier 0 但同時是秘密（分層講「在哪設」，秘密講「能不能讀」）",
      params.REGISTRY["AUTH_PASSWORD"].tier == params.ENV_ONLY
      and params.REGISTRY["AUTH_PASSWORD"].secret)
check("FTP_HOST 是敏感但不是秘密（管理員要看得到才編輯得了）",
      params.REGISTRY["FTP_HOST"].sensitive and not params.REGISTRY["FTP_HOST"].secret)


# ============================================================ hot 不能退化
head("[B+] hot 真的即時生效，而且沒有退化成 restart")

before = settings.crf
ps.set_value("TRANSCODE_CRF", 14, actor="test")
check(f"後台改完 settings.crf 立刻變（{before} → {settings.crf}），不用重啟",
      settings.crf == 14, settings.crf)
ps.set_value("PHOTO_EXCLUDE_VIDEO_ARTWORK", False, actor="test")
check("布林也是即時的", settings.photo_exclude_artwork is False)
ps.set_value("SCAN_EXCLUDE_EXTS", "iso,ts", actor="test")
check("衍生屬性跟著變（exclude_exts 是從字串算出來的）",
      settings.exclude_exts == {"iso", "ts"}, settings.exclude_exts)
ps.clear("TRANSCODE_CRF", actor="test")
ps.clear("PHOTO_EXCLUDE_VIDEO_ARTWORK", actor="test")
ps.clear("SCAN_EXCLUDE_EXTS", actor="test")

# 規格點名「最容易悄悄漂移」的那一項：有人在模組頂層寫 CRF = settings.crf，
# 那一項就從 hot 退化成 restart，而 UI 仍然顯示「已生效」。
# 這種退步不會出任何錯，只是沒有作用 —— 只能靠靜態檢查釘住。
HOT_ATTRS = set(__import__("app.config", fromlist=["_HOT_ATTRS"])._HOT_ATTRS)
frozen = []
for f in sorted((ROOT / "app").glob("*.py")) + sorted((ROOT / "app" / "routers").glob("*.py")):
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in tree.body:                      # 只看模組頂層
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for sub in ast.walk(node.value if node.value else node):
            if (isinstance(sub, ast.Attribute) and sub.attr in HOT_ATTRS
                    and isinstance(sub.value, ast.Name) and sub.value.id == "settings"):
                frozen.append(f"{f.name}: settings.{sub.attr}")
check("沒有任何模組在頂層把即時設定凍成常數", not frozen, frozen)

check("每一項的 applyMode 都是三態之一",
      all(i["applyMode"] in ("hot", "reload", "restart") for i in ps.describe_all()))


# ============================================================ 驗證
head("[B+] 驗證嚴格度：.env 無效就啟動失敗，DB 無效退回預設")

os.environ["TRANSCODE_CRF"] = "高畫質"
check(".env 的無效值會被 validate_env 抓出來",
      any("TRANSCODE_CRF" in b for b in ps.validate_env()), ps.validate_env())
os.environ.pop("TRANSCODE_CRF")

db.execute("INSERT INTO config_param(k,v,updated_at) VALUES('TRANSCODE_CRF','壞掉的值',0) "
           "ON CONFLICT(k) DO UPDATE SET v=excluded.v")
ps.invalidate()
check("DB 的無效值退回預設值，不讓服務起不來",
      ps.resolve("TRANSCODE_CRF") == (21, "default"), ps.resolve("TRANSCODE_CRF"))
check("但後台看得到那是壞的", "error" in ps.describe("TRANSCODE_CRF"))
db.execute("DELETE FROM config_param WHERE k='TRANSCODE_CRF'")
ps.invalidate()

for bad, why in ((99, "超過上限"), (-1, "小於下限")):
    try:
        ps.set_value("TRANSCODE_CRF", bad, actor="t"); blocked = False
    except ValueError:
        blocked = True
    check(f"{why}的值存不進去（{bad}）", blocked)
try:
    ps.set_value("X264_PRESET", "超快", actor="t"); blocked = False
except ValueError:
    blocked = True
check("不在選項裡的值存不進去", blocked)

# 大小寫不是「設錯了」，是同一個值的另一種寫法。
# 舊的讀法是 _s(...).lower()，所以 HDR_TONEMAP=AUTO 一直都能用；
# 新的驗證要是判它無效，使用者升級之後服務會開不起來，而他什麼都沒改。
for key, raw, want in (("HDR_TONEMAP", "AUTO", "auto"),
                       ("X264_PRESET", "VeryFast", "veryfast"),
                       ("FFMPEG_HWACCEL", "NVENC", "nvenc")):
    got = params.parse(params.REGISTRY[key], raw)
    check(f"{key}={raw} 收成 {want}（大小寫不該讓服務開不起來）", got == want, got)

# 編碼名稱不能列白名單：Python 認得一百多種，big5 / cp950 / ms950 是同一個東西。
# 列白名單的結果是某個人的 NAS 用 cp950 用得好好的，升級之後就開不起來。
for enc in ("cp950", "big5", "Big5", "UTF-8", "gb18030", "ms950"):
    try:
        params.parse(params.REGISTRY["FTP_ENCODING"], enc); ok_enc = True
    except ValueError:
        ok_enc = False
    check(f"FTP_ENCODING={enc} 收得下", ok_enc)
try:
    params.parse(params.REGISTRY["FTP_ENCODING"], "火星文"); ok_enc = True
except ValueError:
    ok_enc = False
check("但真的不存在的編碼還是要擋", not ok_enc)


# ============================================================ 錯字
head("[B+] .env 的錯字")

fake = {"TMDB_API_KAY": "x", "FTP_HSOT": "y", "TOTALLY_MADE_UP": "z", "FTP_HOST": "1.1.1.1"}
found = dict(params.unknown_env_keys(fake))
check("TMDB_API_KAY 被抓出來，而且猜得到是 TMDB_API_KEY",
      found.get("TMDB_API_KAY") == "TMDB_API_KEY", found)
check("FTP_HSOT 猜得到是 FTP_HOST", found.get("FTP_HSOT") == "FTP_HOST", found)
check("完全沒關係的鍵也會報，只是猜不出來",
      "TOTALLY_MADE_UP" in found and not found["TOTALLY_MADE_UP"], found)
check("拼對的鍵不會被誤報", "FTP_HOST" not in found)


# ============================================================ 秘密
head("[B+] 秘密")

ps.set_value("TMDB_API_KEY", "sk-super-secret-value", actor="test")
d = ps.describe("TMDB_API_KEY")
blob = db.q1("SELECT v FROM config_param WHERE k='TMDB_API_KEY'")["v"]
check("API 回的是遮罩，不是明文", d["effective"]["value"] == params.MASK, d["effective"])
check("三個候選值也都是遮罩，沒有一個是明文",
      "sk-super-secret-value" not in str(d), "有明文外洩")
check("回得出「有沒有設定」", d["isSet"] is True)
check("回得出最後更新時間", bool(d["updatedAt"]))
check("存進資料庫的是密文", blob.startswith("enc:v1:") and "sk-super" not in blob, blob[:40])
check("解得回來", params.decrypt(blob) == "sk-super-secret-value")
check("設定真的生效了", settings.tmdb_api_key == "sk-super-secret-value")

ps.set_value("TMDB_API_KEY", params.MASK, actor="test")
check("把遮罩原封不動送回來 = 沒有修改，不會把金鑰洗成「••••••••」",
      settings.tmdb_api_key == "sk-super-secret-value")

rows = db.q("SELECT * FROM config_audit WHERE k='TMDB_API_KEY'")
check("稽核有記到這一項改過", len(rows) >= 1)
check("稽核不記秘密的值，連遮罩後的長度都不記",
      all("sk-super" not in str(r["old_value"]) + str(r["new_value"]) for r in rows))
check("稽核記的是遮罩",
      all(r["new_value"] == params.MASK for r in rows), [dict(r) for r in rows])

# XXX_FILE
sec = os.path.join(TMP, "tmdb.key")
open(sec, "w").write("from-a-file-123\n")
os.environ["TMDB_API_KEY_FILE"] = sec
ps.invalidate()
check("支援 XXX_FILE 從檔案讀（docker secrets 的慣例）",
      ps.resolve("TMDB_API_KEY") == ("from-a-file-123", "env"), ps.resolve("TMDB_API_KEY"))
os.environ.pop("TMDB_API_KEY_FILE")
ps.invalidate()
ps.clear("TMDB_API_KEY", actor="test")


# ============================================================ 漂移與重啟
head("[B+] 漂移掃描與重啟橫幅")

ps.set_value("REMOTE_MAX_HEIGHT", 480, actor="test")
os.environ["REMOTE_MAX_HEIGHT"] = "1080"
ps.invalidate()
dr = [x["key"] for x in ps.drift()]
check("被環境變數蓋掉的 DB 值會出現在漂移清單", "REMOTE_MAX_HEIGHT" in dr, dr)
check("**絕不自動刪** DB 的值",
      db.q1("SELECT COUNT(*) c FROM config_param WHERE k='REMOTE_MAX_HEIGHT'")["c"] == 1)
os.environ.pop("REMOTE_MAX_HEIGHT")
ps.invalidate()
ps.clear("REMOTE_MAX_HEIGHT", actor="test")

ps.STARTED_AT = 1
ps.set_value("PROBE_CONCURRENCY", 6, actor="test")     # applyMode=restart
check("restart 類的設定改過之後會出現在「需重啟」清單",
      "PROBE_CONCURRENCY" in ps.needs_restart(), ps.needs_restart())
import time as _t
ps.STARTED_AT = _t.time() + 5          # 假裝剛重開過
check("重開之後橫幅自己消失（比對啟動時間與最後變更時間）",
      "PROBE_CONCURRENCY" not in ps.needs_restart(), ps.needs_restart())
ps.clear("PROBE_CONCURRENCY", actor="test")
ps.STARTED_AT = _t.time()


# ============================================================ 日誌過濾
head("[B+] 秘密不會漏進日誌")

import logging, io
ps.set_value("TMDB_API_KEY", "leaky-key-abcdef", actor="test")
buf = io.StringIO()
h = logging.StreamHandler(buf)
h.setFormatter(logging.Formatter("%(message)s"))
lg = logging.getLogger("filmax.test.leak")
lg.handlers = [h]
lg.propagate = False
lg.setLevel(logging.INFO)
h.addFilter(params.SecretFilter())
lg.info("請求失敗，用的金鑰是 %s", "leaky-key-abcdef")
out = buf.getvalue()
check("日誌裡的秘密被換成遮罩", "leaky-key-abcdef" not in out and params.MASK in out, out.strip())
ps.clear("TMDB_API_KEY", actor="test")

# ============================================================ 後台 API 與權限
head("[B] 管理後台的權限")

from starlette.testclient import TestClient
from app.main import app

with TestClient(app) as c:
    check("沒登入就打管理 API → 擋下", c.get("/api/params").status_code in (401, 403),
          c.get("/api/params").status_code)
    r = c.get("/admin", follow_redirects=False)
    check("沒登入開 /admin → 導去登入頁", r.status_code == 303 and "/login" in r.headers.get("location", ""),
          (r.status_code, r.headers.get("location")))

    # 唯讀角色
    c.post("/login", data={"password": "viewerpw12345"}, follow_redirects=False)
    check("唯讀角色打管理 API → 403", c.get("/api/params").status_code == 403)
    r = c.get("/admin")
    check("唯讀角色開 /admin → 403（不是給一個到處是錯誤的空後台）",
          r.status_code == 403 and "管理員權限" in r.text, r.status_code)
    for path in ("/api/admin/overview", "/api/admin/problems", "/api/params/audit",
                 "/api/maintenance/purge-log"):
        check(f"唯讀角色打 {path} → 403", c.get(path).status_code == 403)
    check("唯讀角色不能改設定",
          c.put("/api/params/TRANSCODE_CRF", json={"value": 1}).status_code == 403)
    c.get("/logout", follow_redirects=False)

with TestClient(app) as c:
    c.post("/login", data={"password": "adminpw12345"}, follow_redirects=False)
    check("管理員開得了 /admin", c.get("/admin").status_code == 200)

    d = c.get("/api/params").json()
    check("清單有分區", bool(d["sections"]), d.get("sections"))
    keys = {i["key"] for i in d["items"]}
    check("清單裡沒有 Tier 0 的東西",
          not (keys & {"HOST", "PORT", "AUTH_SECRET", "MSSQL_PASSWORD", "USER_STORE"}),
          keys & {"HOST", "AUTH_SECRET"})
    check("清單裡沒有 Tier 4 的東西",
          not (keys & {"FFMPEG_PATH", "LIBRARY_ROOTS", "TRUST_PROXY"}))

    r = c.put("/api/params/TRANSCODE_CRF", json={"value": 20})
    check("改得動 Tier 2", r.status_code == 200 and r.json()["effective"]["value"] == 20,
          r.status_code)
    check("Tier 0 從 API 改 → 403（前端的停用是提示，不是防線）",
          c.put("/api/params/HOST", json={"value": "1.2.3.4"}).status_code == 403)
    check("Tier 4 從 API 改 → 403",
          c.put("/api/params/FFMPEG_PATH", json={"value": "/bin/sh"}).status_code == 403)
    r = c.put("/api/params/TRANSCODE_CRF", json={"value": 99})
    check("超出範圍 → 400，而且訊息說得出為什麼",
          r.status_code == 400 and "51" in r.json()["detail"], r.text[:120])
    check("沒登記的鍵 → 404",
          c.put("/api/params/NOT_A_REAL_KEY", json={"value": 1}).status_code == 404)
    c.delete("/api/params/TRANSCODE_CRF")

    ov = c.get("/api/admin/overview").json()
    for k in ("uptime", "counts", "disk", "orphans", "recent", "configDrift", "needsRestart"):
        check(f"總覽有 {k}", k in ov, list(ov))
    check("總覽有磁碟剩餘空間", "free" in ov["disk"] or "error" in ov["disk"], ov["disk"])
    check("總覽的最近活動最多 10 筆", len(ov["recent"]) <= 10)

    lg = c.get("/api/audit/logins?limit=2").json()
    check("登入稽核是伺服器端分頁（有 total / limit / offset）",
          {"total", "limit", "offset"} <= set(lg), list(lg))
    check("limit 真的有作用", len(lg["items"]) <= 2, len(lg["items"]))
    check("預設一頁 25 筆", c.get("/api/audit/logins").json()["limit"] == 25)

    pr = c.get("/api/admin/problems").json()
    check("問題清單有未刮削與失敗兩塊", {"unscraped", "failed"} <= set(pr), list(pr))


# ============================================================ 前端資產
head("[B] 前端")

admin_js = (ROOT / "app" / "static" / "admin.js").read_text(encoding="utf-8")
admin_css = (ROOT / "app" / "static" / "admin.css").read_text(encoding="utf-8")
admin_html = (ROOT / "app" / "static" / "admin.html").read_text(encoding="utf-8")

# admin.js 在 /static/ 底下，不需登入就下載得到
import re as _re
leaks = [w for w in ("adminpw12345", "viewerpw12345", "AUTH_PASSWORD=", "MSSQL_PASSWORD")
         if w in admin_js]
check("admin.js 裡沒有寫死任何敏感資訊（它不需登入就下載得到）", not leaks, leaks)

check("有六個分區",
      len(_re.findall(r'data-tab="(\w+)"', admin_html)) == 6,
      _re.findall(r'data-tab="(\w+)"', admin_html))
for tab in ("overview", "library", "users", "playback", "system", "logs"):
    check(f"{tab} 有對應的實作", f"TABS.{tab} =" in admin_js)

check("斷點是 768px", "max-width:768px" in admin_css)
check("手機把表格換成卡片，不是用 CSS 藏欄位",
      "table.t{display:none}" in admin_css and ".rowcards{display:block}" in admin_css)
check("有 .rowcard 這個替代呈現", ".rowcard{" in admin_css)
check("觸控目標 ≥44px", "min-height:44px" in admin_css)
check("寬內容包在自己的 overflow-x", ".scrollx{overflow-x:auto" in admin_css)
check("抽屜點連結會自動關閉", "closeDrawer()" in admin_js and "function show(" in admin_js)
check("iOS 不做邊緣滑動開啟（會跟系統返回手勢打架）",
      "touchstart" not in admin_js and "邊緣滑動" in admin_html)
check("危險操作有二次確認", "function confirmBox" in admin_js)
check("確認文案會重述對象名稱", "nameOf(id)" in admin_js)
check("被鎖的欄位會停用並說明原因",
      "disabled" in admin_js and "由環境變數" in admin_js)
check("被鎖且 shadowed 的有「清除這裡存的值」按鈕", 'data-do="clear"' in admin_js)
check("有「重設為預設值」與「重設為上次儲存值」",
      'data-do="default"' in admin_js and 'data-do="revert"' in admin_js)
check("有逐欄位的「未儲存」標記", ".param.dirty" in admin_css and "classList.add('dirty')" in admin_js)
check("會鎖死自己的設定要二次確認，而且給 CLI 復原指令",
      "item.lockout" in admin_js and "app.paramstore unset" in admin_js)
check("每張表都有空狀態", admin_js.count("empty\">") >= 4 or admin_js.count('class="empty"') >= 4)
# 規格的分區表列了每一區「最少要有」什麼。少一項不會壞掉，只是那個功能
# 從此不存在 —— 而沒有人會發現，所以逐項釘住。
MUST_HAVE = {
    "總覽": ["/admin/overview", "磁碟剩餘", "孤兒", "最近活動", "待審核"],
    "媒體庫": ["/scan", "/ftp/browse", "還沒刮到資料", "分析失敗", "上一層"],
    "使用者": ["/users", "核准", "拒絕", "停權", "刪除", "備註", "最後登入"],
    "播放與轉碼": ["/diagnostics/encoder/", "/diagnostics/bench/", "/diagnostics/gpu",
                   "/admin/ftp-probe", "/cache/clear"],
    "系統": ["/ftp/test", "/params", "資料庫", "TMDB"],
    "紀錄": ["/audit/logins", "/params/audit", "掃描日誌"],
}
for sec, needs in MUST_HAVE.items():
    missing = [n for n in needs if n not in admin_js]
    check(f"{sec} 分區該有的都有了", not missing, missing)

check("沿用既有的 CSS 元件類別",
      all(k in admin_js for k in ("class=\"btn", "class=\"st ", "class=\"chip")))


print(f"\n{'=' * 54}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
