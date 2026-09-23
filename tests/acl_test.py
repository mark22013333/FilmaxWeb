"""L 目錄權限。

這支測試有兩個工作，第二個比第一個重要：

1. 受限資料夾對沒權限的人**真的看不到**（清單、搜尋、統計、猜 id、實際位元組）。
2. **走過 route table**，確認每一條非管理員路由都經過閘門。

第 2 項是這一整套設計能不能撐下去的關鍵。二十個端點各寫一次判斷，遲早會漏，
而漏掉的表現形式是安靜的外洩，不是錯誤訊息 —— 沒有人會在畫面上看到任何異常。
所以新增端點時，要嘛接上閘門，要嘛明確寫進下面的 EXEMPT 並附理由。
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-acl-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "true", "AUTH_PASSWORD": "adminpw12345",
    # 密碼登入的 viewer 也要測得到，而 read_token 會擋掉「密碼被清空的角色」
    "VIEWER_PASSWORD": "viewerpw12345",
    "MSSQL_HOST": "", "TMDB_API_KEY": "", "AUTO_SCAN_ON_START": "false",
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


from app import acl, auth, db, users            # noqa: E402
from app.main import app                        # noqa: E402
from starlette.testclient import TestClient     # noqa: E402

db.init_db()
users.init()

OPEN = "/媒體資料庫/公開"
SECRET = "/媒體資料庫/私人"
# 前綴比對的陷阱：這個資料夾的名字以受限的那個為開頭，但它是另一個資料夾。
# 不做正規化（前綴補 /）的話它會被一起擋掉，而使用者只會看到「有些片不見了」。
NEAR = "/媒體資料庫/私人物品"


def seed():
    with db.tx() as conn:
        conn.execute("DELETE FROM media_file")
        conn.execute("DELETE FROM media_item")
        conn.execute("DELETE FROM photo")
        conn.execute("DELETE FROM document")
        conn.execute("DELETE FROM play_state")
        for n, folder in ((1, OPEN), (2, SECRET), (3, NEAR)):
            conn.execute("INSERT INTO media_item(id, title, kind, sort_title, genres,"
                         " scrape_state, added_at) VALUES(?,?,?,?,?,?,?)",
                         (n, f"片{n}", "movie", f"片{n}", '["劇情"]', "ok", 1.0))
            conn.execute("INSERT INTO media_file(id, item_id, ftp_path, filename, size,"
                         " duration, probe_state, play_mode) VALUES(?,?,?,?,?,?,?,?)",
                         (n, n, f"{folder}/片{n}.mp4", f"片{n}.mp4", 100, 60.0, "ok", "direct"))
            conn.execute("INSERT INTO photo(id, ftp_path, folder, filename, ext, size,"
                         " probe_state) VALUES(?,?,?,?,?,?,?)",
                         (n, f"{folder}/p{n}.jpg", folder, f"p{n}.jpg", "jpg", 10, "ok"))
            conn.execute("INSERT INTO document(id, ftp_path, folder, filename, ext, size,"
                         " probe_state) VALUES(?,?,?,?,?,?,?)",
                         (n, f"{folder}/d{n}.pdf", folder, f"d{n}.pdf", "pdf", 10, "ok"))
        # 「繼續看」是最容易漏的洩漏點：它不走 /library
        conn.execute("INSERT INTO play_state(file_id, position, duration, finished,"
                     " updated_at) VALUES(2, 100, 600, 0, 1.0)")
    acl.invalidate()


seed()

# 一個 approved 的帳號（有權限）與另一個（沒權限）
u_ok = users.upsert_from_google({"sub": "s1", "email": "ok@example.com",
                                 "name": "有權限"}, "127.0.0.1", {})
u_no = users.upsert_from_google({"sub": "s2", "email": "no@example.com",
                                 "name": "沒權限"}, "127.0.0.1", {})
users.set_status(u_ok["id"], "approved")
users.set_status(u_no["id"], "approved")
users.set_role(u_ok["id"], "viewer")
users.set_role(u_no["id"], "viewer")

rule = acl.add_rule(SECRET, "測試")
acl.set_grants(rule["id"], [u_ok["id"]])

print("\n[1] 三種身分看到的東西")
admin = TestClient(app, client=("127.0.0.1", 1))
admin.cookies.set(auth.COOKIE, auth.make_token(auth.ADMIN))
granted = TestClient(app, client=("127.0.0.1", 1))
granted.cookies.set(auth.COOKIE, auth.make_user_token(u_ok["id"]))
denied = TestClient(app, client=("127.0.0.1", 1))
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
# 密碼登入：token 裡沒有 uid
pw = TestClient(app, client=("127.0.0.1", 1))
pw.cookies.set(auth.COOKIE, auth.make_token(auth.VIEWER))


def titles(cl):
    return sorted(i["title"] for i in cl.get("/api/library").json()["items"])


# 一般入口（shared）：受限資料夾對**所有人**都不出現，管理員與被授權的人也一樣。
# 這是刻意的 —— 受限資料夾只在保險庫入口裡，見 acl.py 開頭「另一個入口」那一段。
check("管理員在一般入口只看得到 2 部（受限的不混進來）",
      titles(admin) == ["片1", "片3"], titles(admin))
check("被授權的帳號在一般入口也只看得到 2 部",
      titles(granted) == ["片1", "片3"], titles(granted))
check("沒被授權的帳號只看得到 2 部", titles(denied) == ["片1", "片3"], titles(denied))
check("密碼登入的 viewer 也只看得到 2 部（沒有身分可以判斷）",
      titles(pw) == ["片1", "片3"], titles(pw))
check("名字相近的資料夾沒有被一起擋掉（前綴要正規化）",
      "片3" in titles(denied), titles(denied))

print("\n[2] 其他清單與數字")
check("相片列表濾掉了",
      sorted(i["filename"] for i in denied.get("/api/photos").json()["items"])
      == ["p1.jpg", "p3.jpg"],
      [i["filename"] for i in denied.get("/api/photos").json()["items"]])
check("文件列表濾掉了",
      sorted(i["filename"] for i in denied.get("/api/documents").json()["items"])
      == ["d1.pdf", "d3.pdf"])
check("相片資料夾清單濾掉了",
      SECRET not in [f["folder"] for f in denied.get("/api/photos/folders").json()["items"]])
check("文件資料夾清單濾掉了（扁平相容形狀）",
      SECRET not in [f["folder"] for f in
                     denied.get("/api/documents/folders?flat=true").json()["items"]])
st = denied.get("/api/stats").json()
check(f"統計數字跟著縮（電影 {st['movies']}、檔案 {st['files']}）",
      st["movies"] == 2 and st["files"] == 2, st)
check("相片統計跟著縮", denied.get("/api/photos/stats").json()["total"] == 2)
sg = granted.get("/api/stats").json()
check(f"被授權的人在一般入口，統計數字也不含受限的（電影 {sg['movies']}）",
      sg["movies"] == 2 and sg["files"] == 2, sg)
check("「繼續看」不會漏出受限的片",
      [x["file_id"] for x in denied.get("/api/continue").json()["items"]] == [],
      denied.get("/api/continue").json()["items"])
# 改掉的行為：被授權的人在**一般入口**的「繼續看」也不該看到受限的片 ——
# 那一排就掛在首頁上，旁邊的人會看到。要看得到就進保險庫。
check("被授權的人在一般入口的「繼續看」也看不到受限的片",
      [x["file_id"] for x in granted.get("/api/continue").json()["items"]] == [],
      granted.get("/api/continue").json()["items"])
check("被授權的人在保險庫的「繼續看」裡看得到",
      [x["file_id"] for x in
       granted.get("/api/continue?scope=vault").json()["items"]] == [2],
      granted.get("/api/continue?scope=vault").json()["items"])
check("搜尋也搜不到", denied.get("/api/library?q=片2").json()["total"] == 0)
check("被授權的人在一般入口也搜不到受限的片",
      granted.get("/api/library?q=片2").json()["total"] == 0)

print("\n[2b] 保險庫入口")


def vtitles(cl):
    return sorted(i["title"] for i in
                  cl.get("/api/library?scope=vault").json()["items"])


check("保險庫裡只有受限的那一部（不是第二個完整片庫）",
      vtitles(granted) == ["片2"], vtitles(granted))
check("管理員的保險庫裡也只有受限的那一部", vtitles(admin) == ["片2"], vtitles(admin))
# 最容易寫錯、而且錯了不會有錯誤訊息的一行：沒有授權時 vault 條件若回
# 「不必過濾」，這裡就會變成整個片庫。
check("沒被授權的人硬帶 scope=vault 也是空的（不是整個片庫）",
      vtitles(denied) == [], vtitles(denied))
check("密碼登入硬帶 scope=vault 也是空的", vtitles(pw) == [], vtitles(pw))
check("保險庫的統計數字只算受限的",
      granted.get("/api/stats?scope=vault").json()["movies"] == 1,
      granted.get("/api/stats?scope=vault").json())

print("\n[2c] /api/vault：有沒有入口")
check("被授權的人有保險庫", granted.get("/api/vault").json()["has_vault"] is True)
gv = granted.get("/api/vault").json()
# 三種媒體分開算：只算影片的話，只有相片或 PDF 的受限資料夾會顯示 0
check("摘要分三種媒體算（影片 1、相片 1、文件 1）",
      gv.get("counts") == {"videos": 1, "photos": 1, "documents": 1}, gv)
check("total 是三者的和", gv.get("total") == 3, gv)
check("舊欄位 items 仍在（= counts.videos，給舊的前端快取）", gv.get("items") == 1, gv)
check("folders 只列資料夾名稱（最後一層）", gv.get("folders") == ["私人"], gv)
vd = denied.get("/api/vault").json()
check("沒被授權的人沒有保險庫", vd["has_vault"] is False, vd)
check("而且只有 has_vault 一個欄位（存在本身就是資訊）", vd == {"has_vault": False}, vd)
check("密碼登入沒有保險庫", pw.get("/api/vault").json() == {"has_vault": False},
      pw.get("/api/vault").json())
# 前端在保險庫裡會對所有 GET 帶 scope=vault；這一支帶了也要是同一個答案
check("帶 scope=vault 問 /api/vault 也一樣（沒授權的人不會因此多知道什麼）",
      denied.get("/api/vault?scope=vault").json() == {"has_vault": False})
check("被授權的人帶 scope=vault 問，摘要不變",
      granted.get("/api/vault?scope=vault").json().get("counts") == gv.get("counts"))

print("\n[2d] 單筆讀取不看入口（否則保險庫點得到卻播不出來）")
check("被授權的人打得開受限的條目詳情",
      granted.get("/api/items/2").status_code == 200,
      granted.get("/api/items/2").status_code)
g2 = granted.get("/api/items/2").json()
check("而且檔案清單不是空的（詳情的檔案也要用 any，否則畫面只剩「沒有檔案」）",
      len(g2.get("files") or []) > 0, g2.get("files"))
check("沒被授權的人條目詳情還是 404", denied.get("/api/items/2").status_code == 404)

print("\n[3] 直接猜 id：要 404，不能 403")
for path in ("/api/items/2", "/api/play/2", "/api/photos/2", "/api/documents/2",
             "/api/stream/2", "/api/photo/2/full",
             "/api/photo/2/preview.jpg", "/api/photo/2/thumb.jpg",
             "/api/document/2/file.pdf", "/api/hls/2/master.m3u8",
             "/api/hls/2/index.m3u8", "/api/subtitle/2/embedded/2.vtt"):
    r = denied.get(path)
    check(f"{path} → {r.status_code}", r.status_code == 404, r.status_code)
# /download 例外：它對**任何**唯讀角色都是 403（不能把原始檔搬走），
# 受限與不受限的檔案回一樣的碼，所以 403 不洩漏任何東西。
# 閘門仍然掛在上面 —— 哪天下載政策放寬，那道判斷已經在了。
check("/api/download/2 → 403（下載本來就只給管理員，不是 ACL 洩漏）",
      denied.get("/api/download/2").status_code == 403,
      denied.get("/api/download/2").status_code)

print("\n[4] 條目層：只要還有一個看得到的檔案就看得到，看不到的檔案要濾掉")
with db.tx() as conn:
    conn.execute("INSERT INTO media_file(id, item_id, ftp_path, filename, size, duration,"
                 " probe_state, play_mode) VALUES(9, 1, ?, '外流.mp4', 100, 60.0, 'ok', 'direct')",
                 (f"{SECRET}/外流.mp4",))
d = denied.get("/api/items/1").json()
check("條目還看得到", d.get("id") == 1, d.get("detail"))
check("但受限的那個檔案被濾掉了",
      [f["filename"] for f in d["files"]] == ["片1.mp4"], [f["filename"] for f in d["files"]])

print("\n[5] 授權改動即時生效，不用重新登入")
# 受限資料夾只在保險庫裡，所以「有沒有授權」要從保險庫入口看 ——
# 一般清單對有沒有授權的人都長一樣，用它判斷不出授權有沒有生效。
acl.set_grants(rule["id"], [u_ok["id"], u_no["id"]])
# 片1 在 [4] 被塞了一個受限資料夾裡的檔案，所以它也算「保險庫裡有東西的條目」——
# 條目只要還有一個看得到的檔案就看得到，這是既有規則。
check("加了授權立刻看得到（保險庫）",
      vtitles(denied) == ["片1", "片2"], vtitles(denied))
check("但一般入口還是看不到（授權不等於混進共用清單）",
      titles(denied) == ["片1", "片3"], titles(denied))
acl.set_grants(rule["id"], [u_ok["id"]])
check("收回授權立刻看不到（保險庫）", vtitles(denied) == [], vtitles(denied))

print("\n[6] 走過 route table：每一條非管理員路由都要經過閘門")
# 明列的例外，每一條都要有理由。新增端點時如果沒有接閘門，
# 這支測試會失敗，而不是安靜地放行。
EXEMPT = {
    "/api/me": "只回目前這個請求自己的角色與帳號",
    "/api/prefs": "使用者自己的介面偏好，跟媒體無關",
    "/api/progress": "寫入播放進度；file_id 猜得到但寫進去也讀不出來（/continue 有濾）",
    "/api/scan/status": "只回掃描進度的數字，不含路徑",
    "/api/image/{name}": "封面圖，內容雜湊命名。刻意接受這道縫，見 L 章",
    "/api/openapi.json": "整支端點是 admin-only（內嵌 auth.is_admin 檢查）",
    "/api/docs": "整支端點是 admin-only（內嵌 auth.is_admin 檢查）",
}
# 閘門只認**真的會過濾或擋下**的函式：
#   filter_sql（含 filter_sql_any）  回一段 SQL 條件，沒有授權時是排除／恆假
#   assert_can_read                  不通過就 404
#   visible_item                     條目層的 ANY 判斷
#   admin_only                       整支端點限管理員
# acl.has_vault **不算**：它只回一個布林值，端點拿到 True 之後查什麼都不受它約束 ——
# 把它列進來的話，「先問 has_vault、然後回一份沒過濾的清單」會被當成有閘門。
# /api/vault 靠的是它裡面的 filter_sql(..., scope=VAULT)。
GATE = ("acl.filter_sql", "acl.assert_can_read", "acl.visible_item", "admin_only")

from app.routers import api as api_mod, stream as stream_mod   # noqa: E402

missing = []
for route in app.routes:
    path = getattr(route, "path", "")
    if not path.startswith("/api/") or "GET" not in (getattr(route, "methods", None) or set()):
        continue
    if path in EXEMPT:
        continue
    fn = getattr(route, "endpoint", None)
    if fn is None:
        continue
    try:
        src = inspect.getsource(fn)
    except OSError:
        continue
    # 端點自己有閘門，或它呼叫的 helper 有（_file_or_404 / _photo_row 內建閘門）
    helpers = re.findall(r"\b(_file_or_404|_photo_row)\b", src)
    src_all = src + "".join(
        inspect.getsource(getattr(stream_mod, h)) for h in set(helpers)
        if hasattr(stream_mod, h))
    if not any(g in src_all for g in GATE):
        missing.append(path)

check(f"每一條 GET 路由都經過閘門或明列例外（檢查了 route table）",
      not missing, missing)
check("例外清單裡的每一條都有寫理由", all(EXEMPT.values()))
# 靜態檢查把 _file_or_404／_photo_row 的原始碼併進來當成「有閘門」——
# 那只在它們自己真的呼叫 assert_can_read 時才成立，這裡把前提釘住。
check("被當成閘門的 helper 自己真的有呼叫 assert_can_read",
      all("acl.assert_can_read" in inspect.getsource(getattr(stream_mod, h))
          for h in ("_file_or_404", "_photo_row")))
stale_exempt = [p for p in EXEMPT if not any(getattr(r, "path", "") == p for r in app.routes)]
check("例外清單裡沒有已經不存在的路由（過期的例外等於沒人看守的洞）",
      not stale_exempt, stale_exempt)

print("\n[6.5] 管理員的存取來自角色，不是來自授權")

# 一個管理員帳號
u_adm = users.upsert_from_google({"sub": "s3", "email": "adm@example.com",
                                  "name": "管理員甲"}, "127.0.0.1", {})
users.set_status(u_adm["id"], "approved")
users.set_role(u_adm["id"], users.OWNER)
adm_cl = TestClient(app, client=("127.0.0.1", 1))
adm_cl.cookies.set(auth.COOKIE, auth.make_user_token(u_adm["id"]))

acl.set_grants(rule["id"], [u_ok["id"]])        # 管理員刻意不給授權
check("管理員沒有任何授權也進得了保險庫（角色就夠了）",
      vtitles(adm_cl) == ["片1", "片2"], vtitles(adm_cl))

payload = admin.get("/api/folders/acl").json()
who = {u["name"]: u.get("is_admin") for u in payload["users"]}
check("授權清單有回 is_admin，前端才分得出誰是管理員",
      who.get("管理員甲") is True and who.get("有權限") is False, who)
check("**不要讓前端自己去比對 DB 的字彙**（那裡是 owner，畫面上是管理員）",
      all("is_admin" in u for u in payload["users"]))

# 「先被授權、後來升管理員」的那一筆授權不能消失 —— 它是降級之後唯一的依據
acl.set_grants(rule["id"], [u_ok["id"], u_adm["id"]])
users.set_role(u_adm["id"], users.OWNER)
still = [r for r in acl.rules() if r.id == rule["id"]][0].user_ids
check("升管理員不會動到既有授權", u_adm["id"] in still, still)

users.set_role(u_adm["id"], "viewer")
acl.invalidate()
# 改角色會把 sess_ver +1，手上那張舊 token 立刻失效 —— 這是刻意的：
# 「拔掉權限」如果要等 token 過期才生效，那就是一句空話。
check("改角色會讓舊 token 立刻失效（降權不能等過期）",
      adm_cl.get("/api/library").status_code == 401)
adm_cl.cookies.set(auth.COOKIE, auth.make_user_token(u_adm["id"]))
check("降回一般帳號之後，靠那筆授權仍然進得了保險庫",
      vtitles(adm_cl) == ["片1", "片2"], vtitles(adm_cl))

users.set_role(u_no["id"], users.OWNER)
acl.invalidate()
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
check("沒有授權的人升成管理員 → 立刻進得了保險庫（不需要補授權）",
      vtitles(denied) == ["片1", "片2"], vtitles(denied))
users.set_role(u_no["id"], "viewer")
acl.invalidate()
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
check("降回來就看不到了（權限不會殘留）", vtitles(denied) == [], vtitles(denied))
acl.set_grants(rule["id"], [u_ok["id"]])


print("\n[6.8] 資料夾樹：遞迴計數與子樹查詢都不能把受限的東西算進來")
# 樹的節點份數是**遞迴總數**（含所有子孫），所以受限資料夾就算不出現在
# 節點清單裡，也可能從父節點的數字洩漏出去 —— 這一段就是在防那個。
# 前面的 seed 只有單層資料夾，樹要有深度才測得出穿透與遞迴，所以自己補。
with db.tx() as conn:
    for i, (folder, fn) in enumerate((
            (OPEN + "/A/深", "o1.pdf"), (OPEN + "/A/深", "o2.pdf"), (OPEN + "/B", "o3.pdf"),
            (SECRET + "/X/深", "s1.pdf"), (SECRET + "/X/深", "s2.pdf"),
            (NEAR + "/C", "n1.pdf")), start=100):
        conn.execute("INSERT INTO document(id, ftp_path, folder, filename, ext, size,"
                     " probe_state) VALUES(?,?,?,?,?,?,'ok')",
                     (i, f"{folder}/{fn}", folder, fn, "pdf", 10))


def tree(cl, prefix=None):
    p = {} if prefix is None else {"prefix": prefix}
    return cl.get("/api/documents/folders", params=p).json()["items"]


t_adm, t_deny = tree(admin), tree(denied)
deny_names = [i["name"] for i in t_deny]
check("受限資料夾不出現在樹的節點裡",
      not any(n == "私人" or n.startswith("私人/") for n in deny_names), deny_names)
check("名字相近的資料夾沒有被一起擋掉（樹也要正規化前綴）",
      any("私人物品" in n for n in deny_names), deny_names)
# 一般入口對管理員與沒權限的人現在是同一份（受限的都不算進來），
# 所以這裡比的是「一般入口」與「保險庫」：受限的份數要落在保險庫那邊。
sum_adm = sum(i["c"] for i in t_adm)
sum_deny = sum(i["c"] for i in t_deny)
check(f"一般入口的遞迴份數不含受限的（管理員 {sum_adm}、沒權限 {sum_deny}）",
      sum_adm == sum_deny, (sum_adm, sum_deny))
t_vault = admin.get("/api/documents/folders", params={"scope": "vault"}).json()["items"]
sum_vault = sum(i["c"] for i in t_vault)
check(f"受限的份數落在保險庫裡（{sum_vault} 份）", sum_vault > 0, sum_vault)
check("單鏈穿透：只有一條路可走的層會併成一個節點（A / 深）",
      any(i["name"] == "A / 深" for i in tree(denied, OPEN)),
      [i["name"] for i in tree(denied, OPEN)])
sub = denied.get("/api/documents", params={"folder": "/媒體資料庫", "subtree": "true"}).json()
check("子樹查詢濾掉受限的檔案",
      not any(i["filename"].startswith("s") for i in sub["items"]),
      [i["filename"] for i in sub["items"]])
check("直接指定受限的子樹 → 0 筆",
      denied.get("/api/documents",
                 params={"folder": SECRET, "subtree": "true"}).json()["total"] == 0)
# NEAR 底下有 seed 的 d3.pdf（本層）與這一段補的 n1.pdf（子層），子樹要兩個都拿到 ——
# 本層那筆正是 subtree_sql 裡「folder 剛好等於前綴」那個 OR 條件在守的。
check("指定名字相近的子樹 → 本層與子層都拿得到（沒被誤擋）",
      denied.get("/api/documents",
                 params={"folder": NEAR, "subtree": "true"}).json()["total"] == 2,
      denied.get("/api/documents",
                 params={"folder": NEAR, "subtree": "true"}).json()["total"])
with db.tx() as conn:
    conn.execute("DELETE FROM document WHERE id >= 100")


# ============================================================================
# 以下是 Vault V2 的矩陣。前面的段落各自在資料上動過手腳（[4] 塞了一個跨資料夾
# 的檔案），這裡重新 seed 一次，讓每一格的期望值只取決於規則與授權。
#
# 第二個受限資料夾 OTHER **只有相片與文件、沒有影片**：
#   * 只算影片的保險庫摘要會把它算成 0（「保險庫是空的」）
#   * 被授權 SECRET 的人不能在保險庫裡看到 OTHER —— 保險庫不是「所有受限的東西」
# ============================================================================
OTHER = "/媒體資料庫/另一個"
seed()
with db.tx() as conn:
    conn.execute("INSERT INTO photo(id, ftp_path, folder, filename, ext, size, probe_state)"
                 " VALUES(4, ?, ?, 'p4.jpg', 'jpg', 10, 'ok')", (f"{OTHER}/p4.jpg", OTHER))
    conn.execute("INSERT INTO document(id, ftp_path, folder, filename, ext, size, probe_state)"
                 " VALUES(4, ?, ?, 'd4.pdf', 'pdf', 10, 'ok')", (f"{OTHER}/d4.pdf", OTHER))
rule2 = acl.add_rule(OTHER, "只有相片與文件")
acl.set_grants(rule["id"], [u_ok["id"]])        # SECRET → 有權限；OTHER → 沒有人（只有管理員）

LIST = {
    # 媒體 → (清單端點, 取出名稱的欄位)
    "video": ("/api/library", "title"),
    "photo": ("/api/photos", "filename"),
    "document": ("/api/documents", "filename"),
}


def names(cl, media, scope=None):
    url, key = LIST[media]
    r = cl.get(url, params={"scope": scope} if scope else {})
    return sorted(i[key] for i in r.json()["items"])


SHARED_EXPECT = {"video": ["片1", "片3"], "photo": ["p1.jpg", "p3.jpg"],
                 "document": ["d1.pdf", "d3.pdf"]}

print("\n[8] 矩陣：shared／vault × video／photo／document × 三種身分")
for media in LIST:
    # 1. 沒授權的 viewer
    check(f"沒授權 · shared {media}：只看到公開",
          names(denied, media) == SHARED_EXPECT[media], names(denied, media))
    check(f"沒授權 · vault {media}：0 筆",
          names(denied, media, "vault") == [], names(denied, media, "vault"))
    check(f"密碼登入 · vault {media}：0 筆", names(pw, media, "vault") == [])
    # 2. 已授權的 viewer
    check(f"已授權 · shared {media}：仍然只看到公開（授權不等於混進共用）",
          names(granted, media) == SHARED_EXPECT[media], names(granted, media))
    want = {"video": ["片2"], "photo": ["p2.jpg"], "document": ["d2.pdf"]}[media]
    check(f"已授權 · vault {media}：只有被授權的那個資料夾（沒有 OTHER）",
          names(granted, media, "vault") == want, names(granted, media, "vault"))
    # 3. 管理員
    check(f"管理員 · shared {media}：仍然排除所有受限",
          names(admin, media) == SHARED_EXPECT[media], names(admin, media))
    want = {"video": ["片2"], "photo": ["p2.jpg", "p4.jpg"],
            "document": ["d2.pdf", "d4.pdf"]}[media]
    check(f"管理員 · vault {media}：所有受限的都看得到",
          names(admin, media, "vault") == want, names(admin, media, "vault"))

# 相簿與資料夾清單也是清單：保險庫裡的相片要點得進相簿、文件要走得到資料夾
check("已授權 · vault 相簿清單只有 SECRET",
      [f["folder"] for f in granted.get("/api/photos/folders?scope=vault").json()["items"]]
      == [SECRET])
check("沒授權 · vault 相簿清單是空的",
      denied.get("/api/photos/folders?scope=vault").json()["items"] == [])
check("已授權 · vault 文件資料夾（扁平）只有 SECRET",
      [f["folder"] for f in
       granted.get("/api/documents/folders?flat=true&scope=vault").json()["items"]] == [SECRET])
check("沒授權 · vault 文件資料夾樹是空的",
      denied.get("/api/documents/folders?scope=vault").json()["items"] == [])
check("沒授權 · vault 相片統計是 0",
      denied.get("/api/photos/stats?scope=vault").json()["total"] == 0)

adm_v = admin.get("/api/vault").json()
check("管理員的保險庫摘要含 OTHER 的相片與文件（影片 1、相片 2、文件 2）",
      adm_v.get("counts") == {"videos": 1, "photos": 2, "documents": 2}, adm_v)
check("摘要的數字跟保險庫清單實際列得出的筆數一致",
      adm_v["counts"]["photos"] == admin.get("/api/photos?scope=vault").json()["total"]
      and adm_v["counts"]["documents"]
      == admin.get("/api/documents?scope=vault").json()["total"]
      and adm_v["counts"]["videos"] == admin.get("/api/library?scope=vault").json()["total"])
# 同一部片有兩個檔案都在受限資料夾裡：條目只算一次（跟 /library 同一個單位）
with db.tx() as conn:
    conn.execute("INSERT INTO media_file(id, item_id, ftp_path, filename, size, duration,"
                 " probe_state, play_mode) VALUES(20, 2, ?, '片2.720p.mp4', 50, 60.0, 'ok',"
                 " 'direct')", (f"{SECRET}/片2.720p.mp4",))
check("同一個條目有多個檔案，摘要的影片數不重複算",
      granted.get("/api/vault").json()["counts"]["videos"] == 1,
      granted.get("/api/vault").json())
with db.tx() as conn:
    conn.execute("DELETE FROM media_file WHERE id=20")

# 只有相片／文件的受限資料夾：被授權的人要有保險庫、而且數字不是 0
acl.set_grants(rule2["id"], [u_no["id"]])
nv = denied.get("/api/vault").json()
check("只被授權「只有相片與文件」的資料夾 → 有保險庫",
      nv.get("has_vault") is True, nv)
check("而且摘要是影片 0、相片 1、文件 1（不會顯示成空的）",
      nv.get("counts") == {"videos": 0, "photos": 1, "documents": 1}, nv)
check("保險庫相片牆列得出那一張", names(denied, "photo", "vault") == ["p4.jpg"])
check("保險庫文件列得出那一份", names(denied, "document", "vault") == ["d4.pdf"])
check("但他看不到 SECRET 的東西", names(denied, "video", "vault") == [])
acl.set_grants(rule2["id"], [])


print("\n[9] 單筆讀取：ANY 語意不受 scope 參數影響")
SINGLE = {"video": "/api/items/2", "photo": "/api/photos/2", "document": "/api/documents/2"}
for media, path in SINGLE.items():
    for sc in (None, "shared", "vault", "any", "garbage"):
        q = {"scope": sc} if sc else {}
        r = denied.get(path, params=q)
        check(f"沒授權猜 {media} id（scope={sc}）→ 404", r.status_code == 404, r.status_code)
        r = granted.get(path, params=q)
        check(f"已授權直接讀 {media}（scope={sc}）→ 200", r.status_code == 200, r.status_code)
for path in ("/api/photos/4", "/api/documents/4"):
    check(f"已授權 SECRET 的人讀 OTHER 的 {path} → 404（授權是一條一條的）",
          granted.get(path, params={"scope": "vault"}).status_code == 404)
    check(f"管理員讀 OTHER 的 {path} → 200", admin.get(path).status_code == 200)
check("已授權的人從保險庫開條目，檔案清單不是空的（scope=vault 也一樣）",
      len(granted.get("/api/items/2?scope=vault").json().get("files") or []) == 1)
# 實際位元組端點：沒授權的人帶 scope=vault 也一樣 404
for path in ("/api/stream/2", "/api/photo/2/full", "/api/document/2/file.pdf",
             "/api/hls/2/master.m3u8"):
    r = denied.get(path, params={"scope": "vault"})
    check(f"{path}?scope=vault（沒授權）→ 404", r.status_code == 404, r.status_code)


print("\n[10] 路徑正規形：同一個資料夾只有一種寫法")
N = acl.normalize_folder_prefix
check("/A → /A", N("/A") == "/A")
check("/A/ → /A（結尾一個斜線是唯一容許的寫法差異）", N("/A/") == "/A")
check("中文路徑原樣保留", N("/媒體資料庫/私人") == "/媒體資料庫/私人")
BAD = {
    "": "空字串", "A/B": "沒有開頭斜線", "//A": "開頭連續斜線", "/A//B": "中間連續斜線",
    "/A//": "結尾連續斜線", "/A/./B": ".", "/A/../B": "..", "/..": "根的 ..",
    "/A\\B": "反斜線", "/A\x00B": "NUL", "/A\nB": "換行", "/": "根目錄", "//": "根目錄的變形",
}
for p, why in BAD.items():
    try:
        N(p)
        ok = False
    except acl.InvalidFolderPath:
        ok = True
    check(f"拒絕：{why}（{p!r}）", ok)
try:
    N(None)
    check("拒絕 None", False)
except acl.InvalidFolderPath:
    check("拒絕 None", True)


def rule_count():
    return db.q1("SELECT COUNT(*) c FROM folder_rule")["c"]


before = rule_count()


def post_rule(prefix):
    return admin.post("/api/folders/acl", json={"prefix": prefix, "note": ""})


for p in ("/媒體資料庫/公開/../私人", "/", "//媒體資料庫", "/媒體資料庫//公開", "媒體資料庫/公開",
          "/媒體資料庫\\公開"):
    r = post_rule(p)
    check(f"API 拒絕不合法路徑 {p!r} → 400", r.status_code == 400, (r.status_code, r.text[:80]))
r = post_rule("/不存在的資料夾")
check("掃描結果裡沒有的資料夾 → 400（拼錯的規則等於沒有保護）",
      r.status_code == 400, (r.status_code, r.text[:80]))
r = post_rule(SECRET + "/")
check("同一個資料夾換一種寫法（結尾斜線）→ 409 same",
      r.status_code == 409 and r.json()["detail"]["relation"] == "same", r.text[:160])
r = post_rule(SECRET + "/子資料夾")
d409 = r.json().get("detail") if r.status_code == 409 else None
check("已受限資料夾底下再加一條 → 409（衝突類型 ancestor）",
      r.status_code == 409 and d409 and d409["relation"] == "ancestor", r.text[:160])
check("409 回應帶著新規則、衝突的既有規則與訊息（管理介面要顯示）",
      bool(d409) and d409["prefix"] == SECRET + "/子資料夾"
      and d409["conflicts_with"] == {"id": rule["id"], "prefix": SECRET}
      and bool(d409["message"]), d409)
r = post_rule("/媒體資料庫")
check("底下已經有規則的上層 → 409（衝突類型 descendant，不自動合併）",
      r.status_code == 409 and r.json()["detail"]["relation"] == "descendant", r.text[:160])
check("被拒絕的嘗試一筆都沒寫進去", rule_count() == before, (rule_count(), before))
check("衝突也有留稽核紀錄",
      db.q1("SELECT COUNT(*) c FROM login_audit WHERE event='folder_rule_conflict'")["c"] >= 3)
# 直接呼叫 acl.add_rule 也一樣擋（不能只靠 API 層）
try:
    acl.add_rule(OTHER + "/更深")
    check("acl.add_rule 本身也擋巢狀規則", False)
except acl.RuleConflict as e:
    check("acl.add_rule 本身也擋巢狀規則", e.relation == "ancestor", e.detail())

# 兄弟資料夾：/lib/private 不能吃到 /lib/private2
with db.tx() as conn:
    for i, folder in ((50, "/lib/private"), (51, "/lib/private2"), (52, "/lib/private/sub")):
        conn.execute("INSERT INTO photo(id, ftp_path, folder, filename, ext, size, probe_state)"
                     " VALUES(?,?,?,?,'jpg',10,'ok')", (i, f"{folder}/x{i}.jpg", folder, f"x{i}.jpg"))
r3 = acl.add_rule("/lib/private/")                   # 結尾斜線 → 存成正規形
check("規則以正規形存下（沒有結尾斜線）", r3.get("prefix") == "/lib/private", r3)
lib_names = [i["filename"] for i in denied.get("/api/photos", params={"q": "/lib/"}).json()["items"]]
check("/lib/private 受限，/lib/private2 照樣看得到",
      "x51.jpg" in lib_names and "x50.jpg" not in lib_names and "x52.jpg" not in lib_names,
      lib_names)
check("can_read：/lib/private2 可讀、/lib/private 與子資料夾不可讀",
      acl.can_read(denied, "/lib/private2") and not acl.can_read(denied, "/lib/private")
      and not acl.can_read(denied, "/lib/private/sub/x.jpg"))
acl.remove_rule(r3["id"])

# 舊資料：這次改版之前寫進去的非正規形規則，不能因為新規矩而失效
db.execute("INSERT INTO folder_rule(prefix, note, created_at) VALUES('/lib/private/', 'legacy', 0)")
acl.invalidate()
legacy = [r for r in acl.rules() if r.prefix == "/lib/private/"][0]
check("舊的非正規形規則讀進來時比對形狀跟新規則一樣",
      legacy.normalized_prefix == "/lib/private/" and legacy.folder == "/lib/private", legacy)
check("而且照樣擋得住", not acl.can_read(denied, "/lib/private/sub/x.jpg")
      and acl.can_read(denied, "/lib/private2/x.jpg"))
r = post_rule("/lib/private")
check("跟舊規則同一個資料夾的新規則 → 409 same（不會變成兩條）",
      r.status_code == 409 and r.json()["detail"]["relation"] == "same", r.text[:160])
acl.remove_rule(legacy.id)
with db.tx() as conn:
    conn.execute("DELETE FROM photo WHERE id IN (50, 51, 52)")


print("\n[11] 帳號生命週期：刪除才收回授權，停權不收回")
u_tmp = users.upsert_from_google({"sub": "s9", "email": "tmp@example.com",
                                  "name": "暫時的"}, "127.0.0.1", {})
users.set_status(u_tmp["id"], "approved")
acl.set_grants(rule["id"], [u_ok["id"], u_tmp["id"]])
acl.set_grants(rule2["id"], [u_tmp["id"]])
r = admin.delete(f"/api/users/{u_tmp['id']}")
check("刪除帳號成功，並回報收回了 2 筆授權",
      r.status_code == 200 and r.json().get("grants_revoked") == 2, r.text[:120])
check("folder_grant 裡沒有留下孤兒",
      db.q1("SELECT COUNT(*) c FROM folder_grant WHERE user_id=?", (u_tmp["id"],))["c"] == 0)
check("快取也立刻反映（不必重啟）",
      not any(u_tmp["id"] in r.user_ids for r in acl.rules()))
check("別人的授權沒有被波及",
      [r for r in acl.rules() if r.id == rule["id"]][0].user_ids == frozenset({u_ok["id"]}))
check("刪除帳號有留稽核紀錄",
      db.q1("SELECT COUNT(*) c FROM login_audit WHERE event='user_delete' AND user_id=?",
            (u_tmp["id"],))["c"] == 1)

# 停權 → 恢復：授權要原封不動
acl.set_grants(rule["id"], [u_ok["id"], u_no["id"]])
r = admin.patch(f"/api/users/{u_no['id']}", json={"status": "disabled"})
check("停權成功", r.status_code == 200, r.text[:120])
check("停權不收回授權",
      u_no["id"] in [x for x in acl.rules() if x.id == rule["id"]][0].user_ids)
# 管理員在停權期間改了這條規則的授權。勾選清單只列 approved 的人，
# 所以送上來的整份名單**不可能**含 u_no —— 後端要替他保留。
r = admin.put(f"/api/folders/acl/{rule['id']}/grants", json={"user_ids": [u_ok["id"]]})
check("停權期間儲存授權：停權者的授權被保留下來",
      r.status_code == 200 and u_no["id"] in r.json()["user_ids"]
      and r.json().get("kept_inactive") == [u_no["id"]], r.text[:160])
r = admin.patch(f"/api/users/{u_no['id']}", json={"status": "approved"})
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
check("恢復之後，保險庫立刻看得到原本的東西（授權沒有遺失）",
      names(denied, "video", "vault") == ["片2"], names(denied, "video", "vault"))
# 已經刪掉的帳號不會被「保留」回來
db.execute("INSERT INTO folder_grant(rule_id, user_id) VALUES(?, 999999)", (rule["id"],))
acl.invalidate()
r = admin.put(f"/api/folders/acl/{rule['id']}/grants", json={"user_ids": [u_ok["id"]]})
check("不存在的帳號的舊授權在下一次儲存時被清掉（不當成停權者保留）",
      999999 not in r.json()["user_ids"], r.json())
acl.set_grants(rule["id"], [u_ok["id"]])


print("\n[12] 快取：一次查詢、改動立即生效")
calls = []
_orig_q = db.q


def _count_q(sql, params=()):
    calls.append(sql)
    return _orig_q(sql, params)


db.q = _count_q
try:
    acl.invalidate()
    acl.rules()
    check(f"重建快取只查一次資料庫（不是 1+N；有 {len(acl.rules())} 條規則）",
          len(calls) == 1, calls)
    calls.clear()
    acl.rules()
    check("快取命中時不查資料庫", calls == [], calls)
finally:
    db.q = _orig_q


# 載入的同時有人改了授權：查回來的那份是舊的，不能被寫進快取
def _racy_q(sql, params=()):
    rows = _orig_q(sql, params)
    acl.invalidate()           # 模擬「查詢進行到一半，另一個請求收回了授權」
    return rows


acl.invalidate()
db.q = _racy_q
try:
    acl.rules()
finally:
    db.q = _orig_q
check("載入期間被 invalidate 的那一份不會留在快取裡", acl._cache is None)

snap = acl.rules()[0]
try:
    snap.user_ids.add(123)           # type: ignore[attr-defined]
    check("快照的 user_ids 是唯讀的", False)
except AttributeError:
    check("快照的 user_ids 是唯讀的", True)
try:
    snap.prefix = "/x"               # type: ignore[misc]
    check("快照本身是唯讀的", False)
except Exception:
    check("快照本身是唯讀的", True)

acl.set_grants(rule["id"], [u_ok["id"], u_no["id"]])
check("給授權 → 同一張 token 立刻看得到（不必重新登入）",
      names(denied, "photo", "vault") == ["p2.jpg"], names(denied, "photo", "vault"))
acl.set_grants(rule["id"], [u_ok["id"]])
check("收回授權 → 同一張 token 立刻看不到", names(denied, "photo", "vault") == [])
check("收回之後直接讀也是 404", denied.get("/api/photos/2").status_code == 404)


print("\n[13] 日誌：擋下時不把私人路徑寫進一般日誌")
import logging  # noqa: E402


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, rec):
        self.lines.append(rec.getMessage())


cap = _Cap()
acl.log.addHandler(cap)
_lvl = acl.log.level
acl.log.setLevel(logging.INFO)
try:
    denied.get("/api/photos/2")
    denied.get("/api/documents/2")
    denied.get("/api/play/2")
finally:
    acl.log.removeHandler(cap)
    acl.log.setLevel(_lvl)
text = "\n".join(cap.lines)
check("有記下擋下的事件", len(cap.lines) == 3, cap.lines)
check("記的是規則編號與資源種類",
      all(f"rule={rule['id']}" in x for x in cap.lines)
      and "type=photo" in text and "type=document" in text and "type=video" in text, cap.lines)
check("沒有完整的私人資料夾路徑、也沒有檔名",
      SECRET not in text and "私人" not in text and "p2.jpg" not in text
      and "片2" not in text, cap.lines)
check("同一個檔案的指紋每次都一樣（看得出反覆嘗試）",
      acl._fingerprint(f"{SECRET}") == acl._fingerprint(f"{SECRET}")
      and acl._fingerprint(SECRET) != acl._fingerprint(NEAR))

acl.remove_rule(rule2["id"])
with db.tx() as conn:
    conn.execute("DELETE FROM photo WHERE id=4")
    conn.execute("DELETE FROM document WHERE id=4")


print("\n[7] 沒有規則時不應該有任何額外成本")
acl.remove_rule(rule["id"])
check("沒有規則 → filter_sql 回空字串（查詢完全不變）",
      acl.filter_sql(denied, "ftp_path") == ("", []))
check("沒有規則 → 大家都看得到全部", titles(denied) == ["片1", "片2", "片3"], titles(denied))

print("\n" + "=" * 54)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
