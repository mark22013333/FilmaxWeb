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


check("管理員看得到全部 3 部", titles(admin) == ["片1", "片2", "片3"], titles(admin))
check("被授權的帳號看得到全部 3 部", titles(granted) == ["片1", "片2", "片3"], titles(granted))
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
check("「繼續看」不會漏出受限的片",
      [x["file_id"] for x in denied.get("/api/continue").json()["items"]] == [],
      denied.get("/api/continue").json()["items"])
check("被授權的人在「繼續看」裡看得到",
      [x["file_id"] for x in granted.get("/api/continue").json()["items"]] == [2])
check("搜尋也搜不到", denied.get("/api/library?q=片2").json()["total"] == 0)

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
acl.set_grants(rule["id"], [u_ok["id"], u_no["id"]])
check("加了授權立刻看得到", titles(denied) == ["片1", "片2", "片3"], titles(denied))
acl.set_grants(rule["id"], [u_ok["id"]])
check("收回授權立刻看不到", titles(denied) == ["片1", "片3"], titles(denied))

print("\n[6] 走過 route table：每一條非管理員路由都要經過閘門")
# 明列的例外，每一條都要有理由。新增端點時如果沒有接閘門，
# 這支測試會失敗，而不是安靜地放行。
EXEMPT = {
    "/api/me": "只回目前這個請求自己的角色與帳號",
    "/api/prefs": "使用者自己的介面偏好，跟媒體無關",
    "/api/progress": "寫入播放進度；file_id 猜得到但寫進去也讀不出來（/continue 有濾）",
    "/api/genres": "已在函式內用 filter_sql 過濾（下面的靜態檢查認得）",
    "/api/scan/status": "只回掃描進度的數字，不含路徑",
    "/api/image/{name}": "封面圖，內容雜湊命名。刻意接受這道縫，見 L 章",
    "/api/healthz": "健康檢查",
    "/api/openapi.json": "整支端點是 admin-only（內嵌 auth.is_admin 檢查）",
    "/api/docs": "整支端點是 admin-only（內嵌 auth.is_admin 檢查）",
}
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

print("\n[6.5] 管理員的存取來自角色，不是來自授權")

# 一個管理員帳號
u_adm = users.upsert_from_google({"sub": "s3", "email": "adm@example.com",
                                  "name": "管理員甲"}, "127.0.0.1", {})
users.set_status(u_adm["id"], "approved")
users.set_role(u_adm["id"], users.OWNER)
adm_cl = TestClient(app, client=("127.0.0.1", 1))
adm_cl.cookies.set(auth.COOKIE, auth.make_user_token(u_adm["id"]))

acl.set_grants(rule["id"], [u_ok["id"]])        # 管理員刻意不給授權
check("管理員沒有任何授權也看得到全部（角色就夠了）",
      titles(adm_cl) == ["片1", "片2", "片3"], titles(adm_cl))

payload = admin.get("/api/folders/acl").json()
who = {u["name"]: u.get("is_admin") for u in payload["users"]}
check("授權清單有回 is_admin，前端才分得出誰是管理員",
      who.get("管理員甲") is True and who.get("有權限") is False, who)
check("**不要讓前端自己去比對 DB 的字彙**（那裡是 owner，畫面上是管理員）",
      all("is_admin" in u for u in payload["users"]))

# 「先被授權、後來升管理員」的那一筆授權不能消失 —— 它是降級之後唯一的依據
acl.set_grants(rule["id"], [u_ok["id"], u_adm["id"]])
users.set_role(u_adm["id"], users.OWNER)
still = [r for r in acl.rules() if r["id"] == rule["id"]][0]["user_ids"]
check("升管理員不會動到既有授權", u_adm["id"] in still, still)

users.set_role(u_adm["id"], "viewer")
acl.invalidate()
# 改角色會把 sess_ver +1，手上那張舊 token 立刻失效 —— 這是刻意的：
# 「拔掉權限」如果要等 token 過期才生效，那就是一句空話。
check("改角色會讓舊 token 立刻失效（降權不能等過期）",
      adm_cl.get("/api/library").status_code == 401)
adm_cl.cookies.set(auth.COOKIE, auth.make_user_token(u_adm["id"]))
check("降回一般帳號之後，靠那筆授權仍然看得到",
      titles(adm_cl) == ["片1", "片2", "片3"], titles(adm_cl))

users.set_role(u_no["id"], users.OWNER)
acl.invalidate()
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
check("沒有授權的人升成管理員 → 立刻看得到（不需要補授權）",
      titles(denied) == ["片1", "片2", "片3"], titles(denied))
users.set_role(u_no["id"], "viewer")
acl.invalidate()
denied.cookies.set(auth.COOKIE, auth.make_user_token(u_no["id"]))
check("降回來就看不到了（權限不會殘留）", titles(denied) == ["片1", "片3"], titles(denied))
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
sum_adm = sum(i["c"] for i in t_adm)
sum_deny = sum(i["c"] for i in t_deny)
check(f"節點的遞迴份數跟著縮（管理員 {sum_adm}、沒權限 {sum_deny}）",
      sum_deny < sum_adm, (sum_adm, sum_deny))
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


print("\n[7] 沒有規則時不應該有任何額外成本")
acl.remove_rule(rule["id"])
check("沒有規則 → filter_sql 回空字串（查詢完全不變）",
      acl.filter_sql(denied, "ftp_path") == ("", []))
check("沒有規則 → 大家都看得到全部", titles(denied) == ["片1", "片2", "片3"], titles(denied))

print("\n" + "=" * 54)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
