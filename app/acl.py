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

## 規則的形狀由這裡決定，不是由前端決定

後台的資料夾挑選器只挑得到掃描到的資料夾、也不讓人選已受限資料夾底下的東西，
但那是介面提示，不是防線。「路徑長什麼樣子才算同一個資料夾」「兩條規則能不能
上下重疊」「這個資料夾存不存在」都在 `add_rule()` 裡判斷 —— 見「管理」那一段。

## 為什麼回 404 而不是 403

403 等於確認「這個東西存在，你只是沒權限」。受限資料夾的存在本身就是資訊。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from . import auth, db

log = logging.getLogger("filmax.acl")

# SQLite 的字串比較上界。用範圍比對而不是 LIKE：
#   LIKE :p || '%' 能不能吃索引，取決於 case_sensitive_like 與欄位 collation，
#   兩者都是全域設定、都可能被別人改掉。範圍比對一定吃得到索引。
_HI = "\U0010FFFF"


# ------------------------- 路徑的正規形 -------------------------
class InvalidFolderPath(ValueError):
    """資料夾路徑不合法。API 層轉成 400。"""


def normalize_folder_prefix(path: Any) -> str:
    """規則用的資料夾路徑，唯一的正規形：`/a/b`（開頭一個斜線、結尾沒有）。

    **為什麼要有唯一的正規形。** 規則是用字串比對生效的。`/A`、`/A/`、`//A`、
    `/./A` 在人眼裡是同一個資料夾，在比對裡是四條不同的規則 —— 其中幾條
    會因為形狀怪而什麼都比對不到，畫面上卻顯示「已設定」。拼錯的規則等於
    沒有保護，而且不會有人發現（見 folder_counts 的「不讓人手打路徑」）。

    **不合法的一律拒絕，而不是「幫忙修好」**，只有結尾的一個斜線例外
    （那是最常見、也唯一沒有歧義的寫法差異）：

      * `..`、`.`   解析它等於替呼叫端決定「他指的是哪一個資料夾」。
                    `/A/../B` 最後變成 `/B`，而送出的人可能以為自己在限制 `/A`。
      * 連續斜線     同上：`/A//B` 是打錯還是真的有一層空名字，這裡猜不出來。
      * 反斜線       FTP 路徑是 `/` 分隔。`\\` 出現通常是 Windows 路徑貼錯地方；
                    在 Linux 的 FTP 上它又是合法的檔名字元 —— 轉成 `/` 會指到
                    另一個資料夾，所以不轉、直接拒絕。
      * 控制字元與 NUL  資料庫與 FTP 指令都是以字串傳遞，NUL 在不同層被截斷的
                    位置不一樣，兩邊看到的會是不同的路徑。
      * 根目錄 `/`   一次誤操作就把整個媒體庫變成保險庫 —— 一般入口瞬間清空，
                    而且看起來像是「片子全部不見了」。真的要這樣做，限制最上層
                    的那幾個資料夾就好。

    **不做 Unicode 正規化（NFC/NFD）**：規則必須跟掃描寫進資料庫的字串逐字
    相同才比對得到；在這裡轉成 NFC，遇到 NFD 命名的資料夾反而會失效。
    新規則另外要求「資料夾真的存在於掃描結果」（見 add_rule），逐字不同的
    寫法在那一關就會被擋下來。
    """
    if not isinstance(path, str) or not path:
        raise InvalidFolderPath("資料夾路徑是空的")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise InvalidFolderPath("資料夾路徑含控制字元")
    if "\\" in path:
        raise InvalidFolderPath("資料夾路徑不能含反斜線（\\），請用 / 分隔")
    if not path.startswith("/"):
        raise InvalidFolderPath("資料夾路徑要以 / 開頭")
    body = path[1:]
    if body.endswith("/"):
        body = body[:-1]
    if not body.strip("/"):
        raise InvalidFolderPath("不能把根目錄 / 設為受限 —— 那會讓整個媒體庫只剩保險庫看得到")
    segs = body.split("/")
    for s in segs:
        if s == "":
            raise InvalidFolderPath("資料夾路徑有連續的斜線")
        if s in (".", ".."):
            raise InvalidFolderPath("資料夾路徑不能含 . 或 ..")
    return "/" + "/".join(segs)


def _norm(p: str) -> str:
    """比對用的形狀：一律以 / 結尾。

    不正規化的話 `/媒體資料庫/私人` 會連 `/媒體資料庫/私人物品` 一起擋掉 ——
    那是兩個不同的資料夾，而使用者只會看到「有些片不見了」。

    這個函式**不驗證**，只補結尾斜線 —— 它也拿來處理查詢字串裡的資料夾
    （`/documents?folder=…`、資料夾樹的 prefix），那些是篩選條件，不是規則。
    規則的正規形一律走 normalize_folder_prefix。
    """
    p = (p or "").rstrip("/")
    return p + "/" if p else "/"


def _rule_match_form(stored: str) -> str:
    """資料庫裡的規則 → 比對用的形狀（正規形 ＋ 結尾斜線）。

    新規則寫進去之前就驗證過，一定是正規形；這裡要處理的是**這次改版之前**
    寫進去的舊資料。舊資料萬一不合新規矩，**不能因此讓規則失效** —— 那等於
    一次升級就把某個資料夾解鎖。所以退回舊的比對方式（只補結尾斜線），
    照舊擋得住它原本擋得住的東西，並且在日誌裡講一聲讓管理員處理。
    """
    try:
        return normalize_folder_prefix(stored) + "/"
    except InvalidFolderPath as e:
        # 不寫路徑本身：這是一般日誌，理由見 _fingerprint
        log.warning("有一條受限規則的路徑不是正規形（%s），沿用舊的比對方式", e)
        return _norm(stored)


# ------------------------- 規則快照 -------------------------
@dataclass(frozen=True)
class Rule:
    """一條受限規則的唯讀快照。

    frozen ＋ frozenset：快照是整份共用的（每個請求都拿同一份），
    任何一個呼叫端能改它，就等於能改所有人看到的權限。
    """
    id: int
    prefix: str                 # 資料庫裡存的字串（新規則一律是正規形）
    normalized_prefix: str      # 比對用：正規形 ＋ 結尾 "/"
    note: Optional[str]
    user_ids: FrozenSet[int]

    @property
    def folder(self) -> str:
        """資料夾本身（沒有結尾斜線）。photo/document 的 folder 欄位可能剛好等於它。"""
        return self.normalized_prefix[:-1]

    def covers(self, path: str) -> bool:
        return path == self.folder or path.startswith(self.normalized_prefix)

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "prefix": self.prefix, "note": self.note,
                "user_ids": sorted(self.user_ids)}


_lock = threading.Lock()
_cache: Optional[Tuple[Rule, ...]] = None
# 每次 invalidate 就 +1。rules() 在鎖外面查資料庫，查的同時如果有人改了授權，
# 查回來的那份就是舊的 —— 沒有這個版本號的話，舊的那份會被寫進快取，
# 而「剛收回的授權」要等到下一次改動才真的生效。
_gen = 0


def invalidate() -> None:
    global _cache, _gen
    with _lock:
        _cache = None
        _gen += 1


def rules() -> Tuple[Rule, ...]:
    """所有受限規則（含被授權的 user_id）。整份快取，改動時 invalidate。

    一次 JOIN 載完，不是一條規則再各查一次授權：這支在每個請求的熱路徑上
    （快取失效後的第一個請求要付這個成本），規則一多就是 1+N 趟資料庫。
    """
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        gen = _gen
    rows = db.q("""SELECT r.id, r.prefix, r.note, g.user_id
                   FROM folder_rule r LEFT JOIN folder_grant g ON g.rule_id = r.id
                   ORDER BY r.prefix, r.id""")
    order: List[int] = []
    base: Dict[int, Tuple[str, Optional[str]]] = {}
    grants: Dict[int, set] = {}
    for r in rows:
        rid = r["id"]
        if rid not in base:
            order.append(rid)
            base[rid] = (r["prefix"], r["note"])
            grants[rid] = set()
        if r["user_id"] is not None:
            grants[rid].add(int(r["user_id"]))
    snap = tuple(Rule(id=rid, prefix=base[rid][0], normalized_prefix=_rule_match_form(base[rid][0]),
                      note=base[rid][1], user_ids=frozenset(grants[rid]))
                 for rid in order)
    with _lock:
        if _gen == gen:
            _cache = snap
    return snap


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
# 範圍跟媒體類型（影片／相片／文件）是兩個獨立的維度：保險庫裡一樣有
# 相片與文件，`/photos?scope=vault` 與 `/library?scope=vault` 是同一套條件。
#
# **為什麼單筆讀取要用 any**：播放頁是從保險庫點進去的，但它送出的是
# `/api/play?file=123`，那個請求身上沒有「我從哪個入口來」的資訊。
# 如果單筆讀取也照 shared 擋，保險庫裡的片子會變成點得到卻播不出來。
# 能不能讀取只跟授權有關，跟從哪個入口進來無關 —— 反過來也一樣：
# 單筆讀取帶了 `scope=vault` 也不會因此放寬或收緊（can_read 根本不看它）。
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


def _granted(request, rs: Sequence[Rule]) -> List[Rule]:
    """這個請求**看得到**的規則。管理員是全部（靠角色，不靠授權）。"""
    if not rs:
        return []
    if auth.is_admin(request):
        return list(rs)
    uid = auth.user_id(request)
    if uid is None:
        return []
    return [r for r in rs if uid in r.user_ids]


def granted_prefixes(request) -> List[str]:
    """這個請求**看得到**的受限資料夾。管理員是全部。

    保險庫入口要列的就是這一份；空的代表這個人沒有保險庫可以進，
    前端據此決定要不要顯示那個入口。
    """
    return [r.prefix for r in _granted(request, rules())]


def has_vault(request) -> bool:
    return bool(_granted(request, rules()))


# ------------------------- 判斷 -------------------------
def _denied(request, scope: str) -> List[Rule]:
    rs = rules()
    if not rs:
        return []
    if scope == SHARED:
        # 受限資料夾一律排除，連被授權的人也一樣 —— 它們只在保險庫裡。
        return list(rs)
    # vault 與 any：看不到的那幾條要擋。vault 另外還要擋「不受限的一切」，
    # 那一半沒有前綴可以列舉，交給 _include_sql 用 OR 正面表列。
    ok = {r.id for r in _granted(request, rs)}
    return [r for r in rs if r.id not in ok]


def denied_prefixes(request, scope: Optional[str] = None) -> List[str]:
    """這個請求看不到的資料夾前綴。`scope` 不給就從請求上讀。"""
    return [r.prefix for r in _denied(request, scope or scope_of(request))]


def _exclude_sql(col: str, rs: Sequence[Rule]) -> Tuple[str, List[Any]]:
    """「不在這幾條規則底下」。沒有規則回 ("", [])（不必過濾）。"""
    if not rs:
        return "", []
    parts, args = [], []
    for r in rs:
        pre = r.normalized_prefix
        # 上界是「前綴（含結尾斜線）＋ 最大字元」。
        # **不能寫 pre[:-1] + _HI**（也就是不含斜線的前綴＋最大字元）——
        # 那個範圍會把 `/私人物品/…` 一起吃進去，因為「物」比「/」大。
        # 實測就是這樣壞的：名字以受限資料夾為開頭的另一個資料夾整個消失。
        # 另外 folder 欄位剛好等於前綴本身（沒有結尾斜線）的那一列也要擋。
        parts.append(f"NOT ({col} >= ? AND {col} < ?) AND {col} <> ?")
        args += [pre, pre + _HI, r.folder]
    return "(" + " AND ".join(parts) + ")", args


def _include_sql(col: str, rs: Sequence[Rule]) -> Tuple[str, List[Any]]:
    """「在這幾條規則的其中一條底下」。**沒有規則時回恆假條件，不是 ("", [])**。

    這裡是正面表列（OR 起來的白名單），不是「排除」—— 因為「不受限的一切」
    沒有前綴可以列舉。沒有任何授權時回 ("", []) 的意思是「不必過濾」，
    會讓沒有保險庫的人看到整個片庫。
    **這是這個檔案裡最容易寫錯、而且錯了不會有錯誤訊息的一行。**
    """
    if not rs:
        return "(1=0)", []
    parts, args = [], []
    for r in rs:
        pre = r.normalized_prefix
        # 跟 _exclude_sql／subtree_sql 同一套範圍比對（含「自己那一列」的 OR）
        parts.append(f"(({col} >= ? AND {col} < ?) OR {col} = ?)")
        args += [pre, pre + _HI, r.folder]
    return "(" + " OR ".join(parts) + ")", args


def filter_sql(request, col: str, scope: Optional[str] = None) -> Tuple[str, List[Any]]:
    """清單查詢用。回 ("", []) 表示不必過濾。

    col 要是完整路徑或資料夾欄位：media_file 用 ftp_path（它沒有 folder 欄位），
    photo 與 document 用 folder。

    **二十個呼叫點一行都不必改**：範圍是從請求上讀的，所以 `/library`、
    `/search`、`/stats`、`/continue`、`/photos`、`/documents` 這些端點原本怎麼
    呼叫就怎麼呼叫，帶 `scope=vault` 進來時它們自動變成「只看保險庫」。
    `scope` 只給那種「不管請求從哪來、都要問某個範圍」的端點用（`/vault` 的摘要）。
    """
    sc = scope or scope_of(request)
    if sc == VAULT:
        return _include_sql(col, _granted(request, rules()))
    return _exclude_sql(col, _denied(request, sc))


def filter_sql_any(request, col: str) -> Tuple[str, List[Any]]:
    """單筆讀取的延伸查詢用的清單條件（scope 固定 ANY）。

    `/api/play` 的 next_episode 就是這種東西：它是一筆播放的延伸，不是一份
    共用清單。用 shared 的話，從保險庫播受限影集會沒有下一集可以接 ——
    自動播放在保險庫裡整個斷掉，而畫面上只會看到「這部影集好像只有一集」。
    """
    return filter_sql(request, col, ANY)


def subtree_sql(col: str, prefix: str) -> Tuple[str, List[Any]]:
    """「這個資料夾與它底下的一切」的條件。回 ("", []) 表示不必過濾（prefix 是根）。

    刻意跟 filter_sql 用同一套範圍比對（>= 前綴／< 前綴＋最大字元），理由也一樣：
    LIKE 'x/%' 吃不吃得到索引取決於全域的 case_sensitive_like 與欄位 collation，
    範圍比對則一定吃得到 idx_doc_folder。順帶避開 LIKE 的萬用字元逸出問題 ——
    資料夾名字裡的 % 與 _ 在這裡完全不必特別處理。

    自己那一列（folder 剛好等於前綴、沒有結尾斜線）要用 OR 併進來，
    否則選中一個「自己就有檔案」的資料夾會看不到它本層的東西。

    這是**篩選**，不是授權：呼叫端一定還要另外套 filter_sql。
    """
    pre = _norm(prefix)
    if pre == "/":
        return "", []
    return (f"(({col} >= ? AND {col} < ?) OR {col} = ?)",
            [pre, pre + _HI, pre.rstrip("/")])


def _denying_rule(request, path: Optional[str]) -> Optional[Rule]:
    """擋下這一筆的是哪一條規則（看得到就是 None）。**一律用 ANY**。"""
    if not path:
        return None
    path = str(path)
    for r in _denied(request, ANY):
        if r.covers(path):
            return r
    return None


def can_read(request, path: Optional[str]) -> bool:
    """能不能讀這一筆。**一律用 ANY** —— 見上面的範圍說明。

    單筆讀取（播放、實際位元組、字幕、縮圖）不看入口：從保險庫點進去的
    播放頁送出的 `/api/play?file=123` 身上沒有入口資訊，照 shared 擋的話
    保險庫裡的片子會變成點得到卻播不出來。
    """
    return _denying_rule(request, path) is None


def _fingerprint(path: str) -> str:
    """日誌裡代替完整路徑的指紋。

    要的是「同一個檔案被反覆嘗試」看得出來，而不是把私人檔名寫進一般日誌 ——
    日誌會被複製、上傳、貼進 issue，那裡沒有任何權限控管。
    用 HMAC 而不是單純的雜湊：資料夾名稱常常猜得到，單純的 sha256 可以拿
    候選名單逐一比對反查回去；帶上這台機器的簽章金鑰之後，沒有金鑰就比對不了。
    需要對回真正的檔案時，管理員拿規則編號去後台看就好。
    """
    try:
        key = auth._secret()
    except Exception:
        key = b"filmax-acl"
    return hmac.new(key, path.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:12]


def assert_can_read(request, path: Optional[str], kind: str = "resource") -> None:
    """不通過就 404。**不要改成 403** —— 見模組開頭。

    `kind` 只用在日誌（video／photo／document…），不影響判斷。
    """
    r = _denying_rule(request, path)
    if r is None:
        return
    from fastapi import HTTPException
    log.info("acl deny actor=%s rule=%s type=%s path#=%s",
             auth.identity(request), r.id, kind, _fingerprint(str(path)))
    raise HTTPException(404, "找不到")


def visible_item(request, item_id: int) -> bool:
    """條目只要還有一個看得到的檔案就看得到。

    影集分組可能跨資料夾（`/HBO/某劇/01/…`），所以不能拿條目本身判斷 ——
    條目沒有路徑，路徑在 media_file 上。
    """
    # 跟 can_read 一樣用 ANY：條目詳情頁是單筆讀取，從保險庫點進去要打得開。
    frag, args = _exclude_sql("ftp_path", _denied(request, ANY))
    if not frag:
        return True
    r = db.q1(f"SELECT 1 FROM media_file WHERE item_id=? AND {frag} LIMIT 1",
              [item_id] + args)
    return r is not None


# ------------------------- 管理 -------------------------
class UnknownFolder(ValueError):
    """掃描結果裡沒有這個資料夾。API 層轉成 400。"""


class RuleConflict(Exception):
    """新規則跟既有規則上下重疊（或完全相同）。API 層轉成 409。

    刻意**不是** ValueError 的子類別：呼叫端常見的寫法是 `except ValueError → 400`，
    子類別的話衝突會安靜地變成 400，前端就拿不到「跟哪一條衝突」。
    """

    # relation 是「既有那一條」相對於新規則的位置
    SAME, ANCESTOR, DESCENDANT = "same", "ancestor", "descendant"

    def __init__(self, prefix: str, existing_id: int, existing_prefix: str, relation: str):
        self.prefix = prefix
        self.existing_id = existing_id
        self.existing_prefix = existing_prefix
        self.relation = relation
        super().__init__(self.message())

    def message(self) -> str:
        if self.relation == self.SAME:
            return f"{self.prefix} 已經是一條受限規則"
        if self.relation == self.ANCESTOR:
            return (f"{self.prefix} 在既有規則 {self.existing_prefix} 底下，"
                    "已經跟著受限；要個別授權請先調整那一條規則")
        return (f"{self.prefix} 底下已經有規則 {self.existing_prefix}；"
                "兩條的授權名單可能不同，請先處理（移除）那一條再設定上層")

    def detail(self) -> Dict[str, Any]:
        return {"message": self.message(), "prefix": self.prefix,
                "conflicts_with": {"id": self.existing_id, "prefix": self.existing_prefix},
                "relation": self.relation}


def _known_folder(conn, folder: str) -> bool:
    """掃描結果裡有沒有這個資料夾（或它是某個已知資料夾的上層）。

    三張表各問一次「這個前綴底下有沒有東西」。media_file 沒有 folder 欄位，
    但檔案 `/A/x.mp4` 落在 `/A/` 的範圍裡就代表 `/A` 是一個資料夾。
    """
    pre = folder + "/"
    hi = pre + _HI
    if conn.execute("SELECT 1 FROM media_file WHERE ftp_path >= ? AND ftp_path < ? LIMIT 1",
                    (pre, hi)).fetchone():
        return True
    for table in ("photo", "document"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE folder = ? OR (folder >= ? AND folder < ?)"
                        " LIMIT 1", (folder, pre, hi)).fetchone():
            return True
    return False


def add_rule(prefix: str, note: str = "") -> Dict[str, Any]:
    """新增一條受限規則。**三個不變量在這裡守，不靠前端**：

    1. 路徑是正規形（normalize_folder_prefix）。
    2. 不跟既有規則上下重疊。已有 `/A` 就不能再加 `/A/B`（它已經跟著受限，
       再加一條只會讓人以為兩條的授權各自獨立）；已有 `/A/B` 要加 `/A` 也不行 ——
       **不自動合併**，因為兩條的授權名單可能不同，合併等於替管理員決定
       `/A/B` 要給誰看。先回衝突，讓人處理既有的那一條。
    3. 資料夾真的存在於目前的掃描結果。拼錯的規則等於沒有保護，畫面上還會
       顯示「已設定」。

    第 3 點**只在建立時**檢查。既有規則即使某次掃描暫時找不到資料夾也不動它 ——
    NAS／FTP 根目錄暫時離線的時候，掃描結果會少掉一大塊；那時候把規則清掉，
    資料夾一回來就變成沒有保護的公開資料夾。

    檢查與寫入在同一個交易裡（db.tx 持有寫入鎖），兩個人同時加 `/A` 與 `/A/B`
    不會兩個都通過。
    """
    canon = normalize_folder_prefix(prefix)
    with db.tx() as conn:
        for row in conn.execute("SELECT id, prefix FROM folder_rule").fetchall():
            other = _rule_match_form(row["prefix"])[:-1]
            if other == canon:
                raise RuleConflict(canon, row["id"], row["prefix"], RuleConflict.SAME)
            if canon.startswith(other + "/"):
                raise RuleConflict(canon, row["id"], row["prefix"], RuleConflict.ANCESTOR)
            if other.startswith(canon + "/"):
                raise RuleConflict(canon, row["id"], row["prefix"], RuleConflict.DESCENDANT)
        if not _known_folder(conn, canon):
            raise UnknownFolder(f"掃描結果裡沒有 {canon} 這個資料夾")
        conn.execute("INSERT INTO folder_rule(prefix, note, created_at) VALUES(?,?,?)",
                      (canon, note or None, db.now_i()))
        r = conn.execute("SELECT id, prefix, note FROM folder_rule WHERE prefix=?",
                         (canon,)).fetchone()
    invalidate()
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


def revoke_user(uid: int) -> int:
    """收回這個帳號在所有規則上的授權。回傳收回了幾筆。

    **只在真正刪除帳號時呼叫。** 帳號存在另一個資料庫（SQLite 的 app_user
    或 MSSQL 的 dbo.filmax_users），folder_grant.user_id 沒有外鍵可以串聯刪除 ——
    不在這裡清，那幾筆授權就會留成孤兒：後台看不到它們（沒有對應的帳號名稱），
    而哪天 id 被重用（MSSQL 重設 IDENTITY、搬資料庫）就直接送給了另一個人。

    停權／拒絕**不收回**：那是暫時的狀態，恢復之後應該原封不動。
    升降管理員也不動授權 —— 角色與明確授權是兩個不同的事實（見 L 章）。
    """
    cur = db.execute("DELETE FROM folder_grant WHERE user_id=?", (int(uid),))
    invalidate()
    return max(0, cur.rowcount or 0)


def rules_for_user(uid: int) -> List[str]:
    return [r.prefix for r in rules() if uid in r.user_ids]


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

    rs = rules()
    marked = {r.folder for r in rs}
    out = []
    for folder, counts in agg.items():
        depth = folder.strip("/").count("/") + 1
        if depth > max_depth and folder not in marked:
            continue
        out.append({"folder": folder, **counts, "depth": depth,
                    "restricted": folder in marked,
                    # 已經在某個受限資料夾底下的，再加一條規則沒有意義（add_rule 也會擋）
                    "covered_by": next((r.folder for r in rs
                                        if folder != r.folder and r.covers(folder)), None)})
    return sorted(out, key=lambda d: d["folder"])
