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


# ------------------------- 判斷 -------------------------
def denied_prefixes(request) -> List[str]:
    """這個請求看不到的資料夾前綴。管理員永遠是空的。"""
    rs = rules()
    if not rs:
        return []
    if auth.is_admin(request):
        return []
    uid = auth.user_id(request)
    # uid 是 None = 密碼登入，沒有身分可以判斷 → 受限資料夾一律看不到
    return [r["prefix"] for r in rs if uid is None or uid not in r["user_ids"]]


def _norm(p: str) -> str:
    """前綴一律以 / 結尾再比對。

    不正規化的話 `/媒體資料庫/私人` 會連 `/媒體資料庫/私人物品` 一起擋掉 ——
    那是兩個不同的資料夾，而使用者只會看到「有些片不見了」。
    """
    p = (p or "").rstrip("/")
    return p + "/" if p else "/"


def filter_sql(request, col: str) -> Tuple[str, List[Any]]:
    """清單查詢用。回 ("", []) 表示不必過濾。

    col 要是完整路徑或資料夾欄位：media_file 用 ftp_path（它沒有 folder 欄位），
    photo 與 document 用 folder。
    """
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


def can_read(request, path: Optional[str]) -> bool:
    if not path:
        return True
    bad = denied_prefixes(request)
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
    frag, args = filter_sql(request, "ftp_path")
    if not frag:
        return True
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
