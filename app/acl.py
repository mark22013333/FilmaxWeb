"""目錄權限：某些資料夾只給特定帳號看。

## 為什麼是「標記受限」而不是「每人一張白名單」

預設是**沒被標記的資料夾，所有登入者都看得到**。被標記為受限的資料夾，
只有被授權的帳號與管理員看得到。

白名單的維護成本落在錯的地方：每次新增一個資料夾，都要回頭去每一個帳號上
補一筆，漏掉一個就是「他看不到新片」，而且沒有人會發現。標記受限的做法，
維護成本只落在真正需要保護的那幾個資料夾上。

## 身分：只有 Google 帳號算「帳號」

`auth.py` 的 token 有兩種形狀：

    密碼登入      role.exp.sig        → uid 是 None
    Google 登入   u.uid.exp.sig       → uid 是 app_user.id

所以「特定帳號」在目前的模型裡只存在於 Google 登入的使用者。密碼登入的
viewer **沒有身分可以判斷，因此一律看不到受限資料夾**；管理員看得到全部。

## 為什麼只有兩個進入點

要接的端點有二十個（清單、詳情、播放資訊、實際位元組、字幕、縮圖…）。
二十個地方各寫一次判斷，遲早會漏 —— 而漏掉的表現形式是**安靜的外洩**，
不是錯誤訊息，沒有人會發現。所以只有兩個：

    filter_sql(request, col)     清單查詢用，回一段 SQL 與參數
    assert_can_read(request, p)  單筆用，不通過就丟 404

`tests/acl_test.py` 會走過 route table，確認每一條非管理員路由都經過其中一個。

## 受限資料夾是「另一個入口」，不是「混在一起的那幾部」

被授權**不等於**應該混進共用清單。受限資料夾的內容只在保險庫入口
（`scope=vault`）出現；一般入口一律看不到，連被授權的人也一樣。
範圍怎麼運作見下面 SHARED／VAULT／ANY 那一段 —— 關鍵是**範圍不是權限**，
帶 `scope=vault` 不會讓任何人看到他本來看不到的東西。

## 為什麼回 404 而不是 403

403 等於確認「這個東西存在，你只是沒權限」。受限資料夾的存在本身就是資訊。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import auth, db

log = logging.getLogger("filmax.acl")

# SQLite 的字串比較上界。用範圍比對而不是 LIKE：
#   LIKE :p || '%' 能不能吃索引，取決於 case_sensitive_like 與欄位 collation，
#   兩者都是全域設定、都可能被別人改掉。範圍比對一定吃得到索引。
_HI = "\U0010FFFF"

_lock = threading.Lock()
_cache: Optional[List[Dict[str, Any]]] = None


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def rules() -> List[Dict[str, Any]]:
    """所有受限規則（含被授權的 user_id）。整份快取，改動時 invalidate。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
    rows = db.q("SELECT id, prefix, note FROM folder_rule ORDER BY prefix")
    out = []
    for r in rows:
        d = db.row_to_dict(r) or {}
        d["user_ids"] = [g["user_id"] for g in
                         db.q("SELECT user_id FROM folder_grant WHERE rule_id=?", (d["id"],))]
        out.append(d)
    with _lock:
        _cache = out
    return out


# ------------------------- 範圍（共用區 vs 保險庫） -------------------------
# 「看得到」與「應該混進共用清單」是兩個不同的問題，之前被當成同一個。
#
# 原本只要被授權，受限資料夾的內容就跟其他片子**混在一起**出現在片庫清單、
# 搜尋、首頁那幾排與統計數字上。對被授權的人來說這其實是個困擾：
# 他要的是「需要的時候進去看」，不是「每次打開首頁都攤在畫面上」——
# 尤其那台電視／那支手機常常不只他一個人在用，而旁邊的人不必知道
# 這些東西存在。
#
# 所以受限資料夾改成一個**獨立入口**（下面叫 vault，保險庫）：
#
#     shared  一般入口。受限資料夾一律排除 —— **連被授權的人也一樣**。
#     vault   保險庫入口。只回受限資料夾裡「這個人被授權的那幾個」。
#     any     不分範圍，看得到就算（單筆讀取用：播放、縮圖、字幕…）。
#
# **為什麼單筆讀取要用 any**：播放頁是從保險庫點進去的，但它送出的是
# `/api/play?file=123`，那個請求身上沒有「我從哪個入口來」的資訊。
# 如果單筆讀取也照 shared 擋，保險庫裡的片子會變成點得到卻播不出來。
# 能不能讀取只跟授權有關，跟從哪個入口進來無關。
#
# **範圍不是權限**。vault 只是換一份要排除的前綴清單，授權判斷
# （uid 在不在 user_ids 裡）兩邊完全一樣 —— 帶 `scope=vault` 進來
# 並不會讓任何人看到他本來看不到的東西。所以這個參數可以直接從
# 查詢字串讀，不必怕被亂改。
SHARED = "shared"
VAULT = "vault"
ANY = "any"

# 請求上放解析結果的鍵。放在 scope 上而不是每個端點自己讀查詢字串：
# 二十個端點各讀一次遲早會有人漏，而漏掉的表現形式一樣是安靜的外洩。
_SCOPE_KEY = "filmax.acl_scope"


def scope_of(request) -> str:
    """這個請求走的是哪個入口。預設 shared —— 漏接的端點要往安全的那邊倒。"""
    if request is None:
        return SHARED
    scope = getattr(request, "scope", request)
    v = scope.get(_SCOPE_KEY) if hasattr(scope, "get") else None
    if v in (SHARED, VAULT, ANY):
        return v
    # 沒有被中介層標記過（例如測試直接呼叫）就自己讀一次查詢字串
    try:
        v = request.query_params.get("scope")
    except Exception:
        v = None
    return v if v in (SHARED, VAULT, ANY) else SHARED


def mark_scope(scope_dict, value: str) -> None:
    """給中介層／端點用：把解析好的範圍記在請求上。"""
    scope_dict[_SCOPE_KEY] = value if value in (SHARED, VAULT, ANY) else SHARED


def granted_prefixes(request) -> List[str]:
    """這個請求**看得到**的受限資料夾。管理員是全部。

    保險庫入口要列的就是這一份；空的代表這個人沒有保險庫可以進，
    前端據此決定要不要顯示那個入口。
    """
    rs = rules()
    if not rs:
        return []
    if auth.is_admin(request):
        return [r["prefix"] for r in rs]
    uid = auth.user_id(request)
    if uid is None:
        return []
    return [r["prefix"] for r in rs if uid in r["user_ids"]]


def has_vault(request) -> bool:
    return bool(granted_prefixes(request))


# ------------------------- 判斷 -------------------------
def denied_prefixes(request, scope: Optional[str] = None) -> List[str]:
    """這個請求看不到的資料夾前綴。

    `scope` 不給就從請求上讀（見上面的 SHARED／VAULT／ANY）。
    """
    rs = rules()
    if not rs:
        return []
    sc = scope or scope_of(request)
    allowed = set(granted_prefixes(request))

    if sc == VAULT:
        # 保險庫裡**只**有受限資料夾：沒被授權的受限資料夾要擋（它本來就看不到），
        # 而「不受限的一切」也要擋 —— 否則保險庫會變成第二個完整片庫。
        # 後者沒有前綴可以列舉，所以交給 vault_only_sql 用 OR 正面表列。
        return [r["prefix"] for r in rs if r["prefix"] not in allowed]

    if sc == ANY:
        # 單筆讀取：看得到就算，跟從哪個入口進來無關。
        return [r["prefix"] for r in rs if r["prefix"] not in allowed]

    # shared：受限資料夾一律排除，連被授權的人也一樣 —— 它們只在保險庫裡。
    return [r["prefix"] for r in rs]


def _norm(p: str) -> str:
    """前綴一律以 / 結尾再比對。

    不正規化的話 `/媒體資料庫/私人` 會連 `/媒體資料庫/私人物品` 一起擋掉 ——
    那是兩個不同的資料夾，而使用者只會看到「有些片不見了」。
    """
    p = (p or "").rstrip("/")
    return p + "/" if p else "/"


def _vault_only_sql(request, col: str) -> Tuple[str, List[Any]]:
    """保險庫入口：**只**回這個人被授權的受限資料夾底下的東西。

    這裡是正面表列（OR 起來的白名單），不是「排除」—— 因為「不受限的一切」
    沒有前綴可以列舉。沒有任何授權時回一個恆假條件，不是回 ("", [])：
    後者的意思是「不必過濾」，會讓沒有保險庫的人看到整個片庫。
    **這是這個檔案裡最容易寫錯、而且錯了不會有錯誤訊息的一行。**
    """
    good = granted_prefixes(request)
    if not good:
        return "(1=0)", []
    parts, args = [], []
    for p in good:
        pre = _norm(p)
        # 跟 subtree_sql 同一套範圍比對（含「自己那一列」的 OR）
        parts.append(f"(({col} >= ? AND {col} < ?) OR {col} = ?)")
        args += [pre, pre + _HI, pre.rstrip("/")]
    return "(" + " OR ".join(parts) + ")", args


def filter_sql(request, col: str) -> Tuple[str, List[Any]]:
    """清單查詢用。回 ("", []) 表示不必過濾。

    col 要是完整路徑或資料夾欄位：media_file 用 ftp_path（它沒有 folder 欄位），
    photo 與 document 用 folder。

    **二十個呼叫點一行都不必改**：範圍是從請求上讀的，所以 `/library`、
    `/search`、`/stats`、`/continue` 這些端點原本怎麼呼叫就怎麼呼叫，
    帶 `scope=vault` 進來時它們自動變成「只看保險庫」。
    """
    if scope_of(request) == VAULT:
        return _vault_only_sql(request, col)
    bad = denied_prefixes(request)
    if not bad:
        return "", []
    parts, args = [], []
    for p in bad:
        pre = _norm(p)
        # 上界是「前綴（含結尾斜線）＋ 最大字元」。
        # **不能寫 pre[:-1] + _HI**（也就是不含斜線的前綴＋最大字元）——
        # 那個範圍會把 `/私人物品/…` 一起吃進去，因為「物」比「/」大。
        # 實測就是這樣壞的：名字以受限資料夾為開頭的另一個資料夾整個消失。
        # 另外 folder 欄位剛好等於前綴本身（沒有結尾斜線）的那一列也要擋。
        parts.append(f"NOT ({col} >= ? AND {col} < ?) AND {col} <> ?")
        args += [pre, pre + _HI, pre.rstrip("/")]
    return "(" + " AND ".join(parts) + ")", args


def filter_sql_any(request, col: str) -> Tuple[str, List[Any]]:
    """單筆讀取的延伸查詢用的清單條件（scope 固定 ANY）。

    `/api/play` 的 next_episode 就是這種東西：它是一筆播放的延伸，不是一份
    共用清單。用 shared 的話，從保險庫播受限影集會沒有下一集可以接 ——
    自動播放在保險庫裡整個斷掉，而畫面上只會看到「這部影集好像只有一集」。
    """
    bad = denied_prefixes(request, ANY)
    if not bad:
        return "", []
    parts, args = [], []
    for p in bad:
        pre = _norm(p)
        parts.append(f"NOT ({col} >= ? AND {col} < ?) AND {col} <> ?")
        args += [pre, pre + _HI, pre.rstrip("/")]
    return "(" + " AND ".join(parts) + ")", args


def subtree_sql(col: str, prefix: str) -> Tuple[str, List[Any]]:
    """「這個資料夾與它底下的一切」的條件。回 ("", []) 表示不必過濾（prefix 是根）。

    刻意跟 filter_sql 用同一套範圍比對（>= 前綴／< 前綴＋最大字元），理由也一樣：
    LIKE 'x/%' 吃不吃得到索引取決於全域的 case_sensitive_like 與欄位 collation，
    範圍比對則一定吃得到 idx_doc_folder。順帶避開 LIKE 的萬用字元逸出問題 ——
    資料夾名字裡的 % 與 _ 在這裡完全不必特別處理。

    自己那一列（folder 剛好等於前綴、沒有結尾斜線）要用 OR 併進來，
    否則選中一個「自己就有檔案」的資料夾會看不到它本層的東西。
    """
    pre = _norm(prefix)
    if pre == "/":
        return "", []
    return (f"(({col} >= ? AND {col} < ?) OR {col} = ?)",
            [pre, pre + _HI, pre.rstrip("/")])


def can_read(request, path: Optional[str]) -> bool:
    """能不能讀這一筆。**一律用 ANY** —— 見上面的範圍說明。

    單筆讀取（播放、實際位元組、字幕、縮圖）不看入口：從保險庫點進去的
    播放頁送出的 `/api/play?file=123` 身上沒有入口資訊，照 shared 擋的話
    保險庫裡的片子會變成點得到卻播不出來。
    """
    if not path:
        return True
    bad = denied_prefixes(request, ANY)
    if not bad:
        return True
    path = str(path)
    for p in bad:
        pre = _norm(p)
        if path == pre.rstrip("/") or path.startswith(pre):
            return False
    return True


def assert_can_read(request, path: Optional[str]) -> None:
    """不通過就 404。**不要改成 403** —— 見模組開頭。"""
    if not can_read(request, path):
        from fastapi import HTTPException
        log.info("擋下受限路徑的存取：%s（%s）", path, auth.identity(request))
        raise HTTPException(404, "找不到")


def visible_item(request, item_id: int) -> bool:
    """條目只要還有一個看得到的檔案就看得到。

    影集分組可能跨資料夾（`/HBO/某劇/01/…`），所以不能拿條目本身判斷 ——
    條目沒有路徑，路徑在 media_file 上。
    """
    # 跟 can_read 一樣用 ANY：條目詳情頁是單筆讀取，從保險庫點進去要打得開。
    bad = denied_prefixes(request, ANY)
    if not bad:
        return True
    parts, args = [], []
    for p in bad:
        pre = _norm(p)
        parts.append("NOT (ftp_path >= ? AND ftp_path < ?) AND ftp_path <> ?")
        args += [pre, pre + _HI, pre.rstrip("/")]
    frag = "(" + " AND ".join(parts) + ")"
    r = db.q1(f"SELECT 1 FROM media_file WHERE item_id=? AND {frag} LIMIT 1",
              [item_id] + args)
    return r is not None


# ------------------------- 管理 -------------------------
def add_rule(prefix: str, note: str = "") -> Dict[str, Any]:
    prefix = (prefix or "").strip().rstrip("/")
    if not prefix.startswith("/"):
        raise ValueError("資料夾路徑要以 / 開頭")
    db.execute("INSERT OR IGNORE INTO folder_rule(prefix, note, created_at) VALUES(?,?,?)",
               (prefix, note or None, db.now_i()))
    invalidate()
    r = db.q1("SELECT id, prefix, note FROM folder_rule WHERE prefix=?", (prefix,))
    return db.row_to_dict(r) or {}


def remove_rule(rule_id: int) -> None:
    db.execute("DELETE FROM folder_grant WHERE rule_id=?", (rule_id,))
    db.execute("DELETE FROM folder_rule WHERE id=?", (rule_id,))
    invalidate()


def set_grants(rule_id: int, user_ids: Sequence[int]) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM folder_grant WHERE rule_id=?", (rule_id,))
        conn.executemany("INSERT OR IGNORE INTO folder_grant(rule_id, user_id) VALUES(?,?)",
                         [(rule_id, int(u)) for u in user_ids])
    invalidate()


def rules_for_user(uid: int) -> List[str]:
    return [r["prefix"] for r in rules() if uid in r["user_ids"]]


def folder_counts(max_depth: int = 4) -> List[Dict[str, Any]]:
    """給後台勾選用的資料夾清單。數量**往上層累加**，並且只回淺層。

    **不讓人手打路徑。** 手打就會拼錯，而拼錯的規則等於沒有保護，
    畫面上還會顯示「已設定」—— 沒有人會發現。

    為什麼要往上累加、又要限制深度：實測 Yu 的片庫掃出 **1224 個資料夾，
    其中 1168 個在第 8 層**（那是相片掃描帶進來的一堆 `.../images` 葉目錄）。
    把它們全部塞進一個 select 是不能用的，而且**限制一個葉目錄幾乎永遠不是
    使用者想做的事** —— 他想限制的是 `/媒體資料庫/APPLE` 這種層級，
    然後讓底下自動跟著。所以這裡把葉目錄的數量累加到每一層祖先上，
    只回第 max_depth 層以內（實測是 46 個，剛好可以用一個下拉選單挑）。

    已經有規則的資料夾一定會回，不管它在第幾層 —— 否則後台會列出一條
    「畫面上找不到對應項目」的規則。
    """
    leaf: Dict[str, Dict[str, int]] = {}

    def bump(folder: str, kind: str, n: int) -> None:
        if not folder:
            return
        d = leaf.setdefault(folder, {"videos": 0, "photos": 0, "documents": 0})
        d[kind] += n

    # media_file 沒有 folder 欄位（photo 與 document 有），要從 ftp_path 切出來。
    # 在 Python 端切而不是寫一段 SQLite 字串函式：那段 SQL 會很難讀，
    # 而這裡是後台的一次性查詢，成本可以忽略。
    for r in db.q("SELECT ftp_path FROM media_file"):
        p = r["ftp_path"] or ""
        bump(p.rsplit("/", 1)[0] if "/" in p else "", "videos", 1)
    for r in db.q("SELECT folder, COUNT(*) c FROM photo GROUP BY folder"):
        bump(r["folder"], "photos", r["c"])
    for r in db.q("SELECT folder, COUNT(*) c FROM document GROUP BY folder"):
        bump(r["folder"], "documents", r["c"])

    # 往上累加到每一層祖先
    agg: Dict[str, Dict[str, int]] = {}
    for folder, counts in leaf.items():
        segs = folder.strip("/").split("/")
        for i in range(1, len(segs) + 1):
            anc = "/" + "/".join(segs[:i])
            d = agg.setdefault(anc, {"videos": 0, "photos": 0, "documents": 0})
            for k, v in counts.items():
                d[k] += v

    marked = {x["prefix"] for x in rules()}
    out = []
    for folder, counts in agg.items():
        depth = folder.strip("/").count("/") + 1
        if depth > max_depth and folder not in marked:
            continue
        out.append({"folder": folder, **counts, "depth": depth,
                    "restricted": folder in marked,
                    # 已經在某個受限資料夾底下的，再加一條規則沒有意義
                    "covered_by": next((m for m in marked
                                        if folder != m and folder.startswith(_norm(m))), None)})
    return sorted(out, key=lambda d: d["folder"])
