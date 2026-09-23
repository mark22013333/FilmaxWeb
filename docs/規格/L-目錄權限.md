> 這是《FilmaxWeb 規格需求書》的第 **L** 章。索引、分期計畫與決策表在 [../規格需求書.md](../規格需求書.md)。
> 章別代號（A-4、G-6、J 的第 −1 層…）在全書通用，跨章引用時直接用代號。

## L. 目錄權限：特定帳號才看得到的資料夾（**已實作**）

### 先講擋路的那件事：密碼登入沒有身分

`auth.py` 的 token 有兩種格式：

| 登入方式 | token | `uid` |
|---|---|---|
| 密碼（`AUTH_PASSWORD` / `VIEWER_PASSWORD`） | `role.exp.sig` | **None** |
| Google 帳號 | `u.uid.exp.sig` | `app_user.id` |

「特定帳號」在目前的模型裡**只存在於 Google 登入的 `app_user`**。
而 `.env` 目前**沒有設 `VIEWER_PASSWORD`** —— 實際上只有「admin 密碼」與「Google 帳號」
兩種身分。

**決定：ACL 只綁 `app_user`。** 密碼登入的 viewer 一律看不到受限目錄
（沒有身分可以判斷，就不能給），admin 密碼看得到全部。
需要「不靠 Google 的具名帳號」是另一件事（`app_user` 加 `password_hash`
＋註冊／改密碼／重設流程），不在這一期。

### 資料模型

```sql
CREATE TABLE IF NOT EXISTS folder_rule (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix TEXT NOT NULL UNIQUE,      -- 受限的資料夾（前綴，子資料夾自動繼承）
    note   TEXT,
    created_at REAL);
CREATE TABLE IF NOT EXISTS folder_grant (
    rule_id INTEGER NOT NULL REFERENCES folder_rule(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (rule_id, user_id));
```

**預設是「沒被標記的資料夾，所有登入者都看得到」。**
被標記的，只有被授權的帳號 ＋ admin 看得到。

> **為什麼不是「每個帳號一張白名單」。** 白名單的維護成本落在錯的地方：
> 每次新增一個資料夾，都要回頭去每一個帳號上補一筆，漏一個就是「他看不到新片」。
> 標記受限的做法，維護成本只落在真正需要保護的那幾個資料夾上。

### 比對方式：video 用 `ftp_path`，photo/document 用 `folder`

`media_file` **沒有 `folder` 欄位**（`photo` 與 `document` 有）。所以：

| 表 | 比對 |
|---|---|
| `media_file` | `ftp_path` 前綴 |
| `photo` / `document` | `folder` 前綴 |

前綴比對要寫成**範圍**而不是 `LIKE`：

```sql
-- 會用到 ftp_path 的 UNIQUE 索引
ftp_path >= :prefix AND ftp_path < :prefix || char(0x10FFFF)
```

`LIKE :prefix || '%'` 能不能用索引取決於 `case_sensitive_like` 與 collation，
不要賭。

**影集分組跨資料夾的情況**：`media_item` 之下可能有多個 `media_file`。
規則是「**item 只要還有一個看得到的檔案就看得到**，看不到的檔案從檔案清單裡濾掉」。

### 執行點：兩個 primitive ＋ 一個把關測試

真正的工作量在這裡，不在資料模型。要接的端點：

| 類型 | 端點 |
|---|---|
| 列表／詳情 | `/library` `/genres` `/stats` `/items/{id}` `/continue` `/play/{id}` `/photos` `/photos/folders` `/photos/stats` `/photos/{id}` `/documents` `/documents/folders` `/documents/{id}` |
| 實際位元組 | `/stream` `/download` `/hls/master.m3u8` `/hls/index.m3u8` `/hls/seg-N.ts` `/photo/{id}/thumb.jpg` `/photo/{id}/full` `/photo/{id}/preview.jpg` `/document/{id}/file.pdf` `/subtitle/{id}/embedded/{n}.vtt` `/subtitle/{id}/external.vtt` |

**二十個端點不能各寫一次判斷。** 收斂成兩個：

```python
folder_filter_sql(sess) -> (sql_fragment, params)   # 列表查詢用
assert_can_read(sess, path_or_folder)               # 單筆用，失敗丟 404
```

**回 404 不回 403** —— 403 等於確認「這個東西存在，你只是沒權限」。

而且要有一支**走過 route table 的測試**：列舉所有非 admin-only 路由，
確認每一條都呼叫過閘門，否則要出現在明列的例外清單裡。
沒有這支測試，下一次新增端點就會漏掉一個 —— 而漏掉的表現形式是「安靜的外洩」，
不是錯誤訊息。

### 實作時真實資料教我的一件事

規劃時我說「後台從實際掃到的資料夾清單勾選」。做出來一跑，
`folder_counts()` 回了 **1224 個資料夾，其中 1168 個在第 8 層** ——
那是相片掃描帶進來的一堆 `.../images` 葉目錄。一個 1224 項的下拉選單是不能用的。

而且更重要的是：**限制一個葉目錄幾乎永遠不是使用者想做的事。**
他想限制的是 `/媒體資料庫/APPLE` 這個層級，然後讓底下自動跟著。

所以改成：把葉目錄的數量**往上累加到每一層祖先**，只回第 4 層以內
（實測 74 個，剛好一個下拉選單挑得完），再加一個純前端的篩選框
（只藏選項、不接受手打路徑）。已經有規則的資料夾不管在第幾層都一定會回，
否則後台會列出一條「畫面上找不到對應項目」的規則。

| 深度 | 資料夾數 |
|---|---|
| 1 | 3 |
| 2 | 9 |
| 3 | 34 |
| 4 | 23 |
| 8 | **1168** |

### 實測（Yu 的真實片庫）

把 `/媒體資料庫/APPLE` 設為受限：

```
影片檔：全部 117，未授權的 viewer 看得到 81（受限 36）
授權該帳號之後 → 117（不需要重新登入）
收回授權 → 81
```

### 一個前綴比對的 bug（測試抓到的）

上界原本寫成「不含斜線的前綴 ＋ 最大字元」：

```python
args += [pre, pre[:-1] + _HI, ...]      # 錯
```

於是限制 `/媒體資料庫/私人` 會把 `/媒體資料庫/私人物品` **整個一起擋掉** ——
因為「物」比「/」大，落在那個範圍裡。而使用者只會看到「有些片不見了」。
正確的上界是「前綴（含結尾斜線）＋ 最大字元」：

```python
args += [pre, pre + _HI, pre.rstrip("/")]
```

`tests/acl_test.py` 有一項專門釘這件事（`/媒體資料庫/私人物品` 必須還看得到）。

### 刻意留下的縫

| 縫 | 判斷 |
|---|---|
| `/api/image/{name}` 封面圖沒有把關 | **接受。**名字是內容雜湊，猜不到；為一張海報把封面路徑接上 ACL，複雜度不值得。寫進文件，不要假裝它不存在 |
| HLS 的內部 token（`INTERNAL_HEADER`） | 不受影響：只認 loopback 的**真實 socket 位址**＋固定 token，而且那是 ffmpeg 自己讀來源檔用的 |
| 已經發出去的 session | ACL 改動即時生效（每次查詢都重算），不需要踢人下線 |

### 後台 UI

- 「媒體庫」分頁加一區「受限資料夾」：從**實際掃到的資料夾清單勾選**，不讓人手打路徑
  （手打路徑 = 拼錯就等於沒保護，而且不會有人發現）。
- 每一條規則列出被授權的帳號，可以直接勾選增減。
- 「使用者」分頁的每個帳號，反過來列出他被授權的資料夾。

#### 管理員不進勾選清單（2026-09-04 修正）

原本的授權清單把**所有**已核准的帳號列成一排 checkbox，管理員也在裡面而且是沒勾的。
使用者的反應是「我把某人升成管理員，這裡卻沒有自動勾起來」—— **畫面在說謊**：
`acl.can_read()` 的第一句就是 `if auth.is_admin(request): return True`，
那兩個沒勾的框其實看得到全部。

**決定：不要在升級時自動寫入授權。**三個理由，第一個是決定性的：

1. **降級會變成安靜的權限殘留。** 升級寫一筆 grant，之後降回一般帳號 ——
   grant 還在，他仍然看得到受限資料夾，而後台看起來完全正常。
   這正是本章開頭寫的那種失效：**不是錯誤訊息，沒有人會發現。**
2. **為了修第 1 點而在降級時自動刪 grant，會刪掉他升級之前本來就有的授權**，
   而那救不回來 —— 沒有人記得他原本被勾了哪幾個。
3. **同一件事會有兩個真相來源**（role 說「看得到全部」、grant 說「看得到這一個」），
   遲早不一致，而不一致的那一邊是安靜的那一邊。

不寫還有個附帶好處：某人被授權 → 升管理員 → 降回來，他的勾選原封不動，行為是連續的。

**所以改的是畫面：**

| 之前 | 現在 |
|---|---|
| 管理員與一般帳號混在同一排 checkbox | 勾選清單**只列一般帳號**；管理員另起一行「管理員一律看得到，不需要（也不能）在這裡勾選」 |
| 「看得到的人：目前沒有人（只有管理員看得到）」 | 「目前沒有人 **＋ 全部管理員（N 人）**」；有授權時同樣附上那一段 |
| 沒有說勾選到底代表什麼 | **「勾選 = 即使不是管理員也看得到」**，並說明降級後沒勾的就看不到 |
| 管理員身上的既有授權完全看不見 | 那一行會多寫「其中 X 另外保有授權，降為一般帳號後仍看得到」 |

**兩個實作上的坑（都在這次踩到）：**

* **儲存是「整份取代」。** 管理員從勾選清單拿掉之後，按一次儲存就會把
  「先被授權、後來升管理員」那個人的 grant 一起洗掉 —— 而那筆 grant 正是他降級之後
  唯一的依據。前端送出時要把管理員身上的既有授權原封帶回去。
* **`role` 是資料庫的字彙。** DB 是 `owner`／`viewer`，畫面上講的是「管理員」。
  端點改成多回一個 `is_admin` 布林，前端不必知道這件事 ——
  第一版就是拿 `u.role === 'admin'` 去比，永遠比不到。

### 驗收（`tests/acl_test.py`，43 項全過）

```
三種身分：admin 3 部 / 被授權 3 部 / 沒授權 2 部 / 密碼登入 viewer 2 部
管理員沒有任何授權也看得到全部（角色就夠了，不靠 grant）
升管理員不會動到既有授權；降回來之後那筆授權仍然有效
沒有授權的人升成管理員 → 立刻看得到；降回來 → 立刻看不到（權限不殘留）
改角色會讓舊 token 立刻失效（降權不能等 token 過期）
名字相近的資料夾沒有被一起擋掉（前綴正規化）
相片與文件的列表、資料夾清單、統計數字都跟著縮
「繼續看」不會漏出受限的片（被授權的人看得到）
搜尋搜不到
直接猜 id：12 條路徑全部 404（items/play/photos/documents/stream/
  photo full+preview+thumb/document file/hls master+index/subtitle）
/download 回 403 —— 下載對任何唯讀角色都是 403，不是 ACL 洩漏
條目層：還看得到，但受限的那個檔案被濾掉
授權改動即時生效，不用重新登入
route table 掃描：每一條 GET 路由都經過閘門或明列例外
沒有規則時 filter_sql 回空字串（查詢完全不變，零成本）
```

`/download` 那一項值得記一下：它對**任何**唯讀角色都是 403（不能把原始檔搬走），
受限與不受限的檔案回一樣的碼，所以 403 不洩漏任何東西。閘門仍然掛在上面 ——
哪天下載政策放寬，那道判斷已經在了。

### Vault V2：範圍與媒體是兩個維度（2026-09-23）

2026-09-17 把受限資料夾改成獨立入口（保險庫，`scope=vault`）之後，後端的
清單端點（`/library` `/photos` `/documents` 與它們的資料夾、統計）都認得
`scope=vault`。但前端把三件事塞在同一組導覽裡：

| 維度 | 值 | 以前在哪 |
|---|---|---|
| 範圍 | shared / vault | nav 裡的一顆 `data-kind="vault"` |
| 媒體 | video / photo / doc | 同一排的 `data-kind="photo"`、`"doc"` |
| 影片細分 | '' / movie / tv / jav / home | 同一排的其他 `data-kind` |

於是進保險庫只能看影片（`showView('video')` 寫死），而從保險庫點「相片」會順手把
scope 設回 shared —— **受限資料夾裡的相片與 PDF 後端支援、前端卻走不過去**。

#### 二維模型

```text
scope = shared | vault          從哪個入口看
media = video  | photo | doc    看哪一種東西
（videoKind 是 media=video 底下的細分）
```

兩個維度互不干涉：換媒體不動 scope（在保險庫點相片 → 保險庫的相片牆），
換 scope 不動媒體（在相片牆切到私人 → 私人的相片牆）。6 種組合全部可用。

* **scope 不是 permission。** vault 只是換一份要排除／要列出的前綴，授權判斷
  （uid 在不在 `user_ids` 裡、是不是管理員）兩邊完全一樣。帶 `scope=vault`
  不會讓任何人看到他本來看不到的東西 —— 所以它可以直接從查詢字串讀。
* **media type 不是 scope。** 相片與文件在保險庫裡是正式支援的，不是例外。
* **單筆讀取一律 ANY。** `can_read`／`assert_can_read`／`visible_item`／
  `filter_sql_any` 不看請求的 scope：帶 `scope=vault`、`scope=shared`、
  亂寫或不帶，結果都一樣。從保險庫點進去的播放、相片、PDF、字幕、串流因此讀得到；
  沒授權的人猜 id 一律 404（不是 403）。

#### 前端

* 狀態是 `viewState = { scope, media, videoKind }`（`app/static/app.js`）。
* 「私人」改成 header 裡一個低調的範圍開關（共用 ｜ 🔒私人），插在分類導覽前面。
  **只在 `/api/vault` 回 `has_vault=true` 時才建立 DOM**，靜態 HTML 裡沒有它。
  數量只放在滑鼠提示裡，不印在按鈕上（旁邊常常有別人在看）。
* **`api()` 在保險庫裡對所有 `/api/` 的 GET 都帶 `scope=vault`**，不再用端點名稱
  的正則（舊的 `SCOPED = /^\/api\/(library|continue|…)/`）。那份清單是安靜的漂移點：
  新增一支清單端點忘了加進去，它在保險庫裡就會去查一般片庫。全部都帶是安全的，
  因為 scope 不是權限，而單筆讀取本來就不看它。
* **不用 cookie 記 scope**：cookie 跨分頁共用，一個分頁進保險庫會汙染另一個分頁。
  scope 只活在該頁的 `viewState`（重新整理後回到共用）。
* **換 scope 時立刻清空**（`clearScopedUI()`）：搜尋、分頁、分類、繼續觀看、
  相簿選取、文件樹選取與快取，以及所有檢視的 DOM —— 包括目前沒在看、藏起來的
  相片牆與文件牆，還有可能正開著一部私人片的詳情彈窗。
* **換 scope 之前發出、之後才回來的 GET 回應直接丟掉**（`scopeEpoch`）。清空只擋得住
  已經畫上去的東西；慢一步回來的保險庫回應會把私人內容畫進已經是共用的畫面。
  丟掉的方式是回一個永遠不完成的 promise，不是丟例外 —— 呼叫端的 catch 會把錯誤
  畫到畫面上，而這種「錯誤」不該被看到。唯一的例外是掃描進度輪詢（`crossScope`）。

#### `/api/vault` 摘要

```json
{ "has_vault": true,
  "counts": { "videos": 12, "photos": 284, "documents": 17 },
  "total": 313, "items": 12, "folders": ["Private", "Documents"] }
```

* 沒授權（含密碼登入）只回 `{"has_vault": false}`，**沒有任何其他欄位**。
* 三個數字都用 `acl.filter_sql(..., scope=VAULT)` 算 —— 跟 `/library?scope=vault`、
  `/photos?scope=vault`、`/documents?scope=vault` 是同一個函式，所以數字跟清單
  實際列得出的筆數一致。`videos` 以 media_item 計（同一部片多個檔案只算一次）；
  `photos`／`documents` 跟清單一樣只算 `probe_state='ok'`。
* `items` 是舊欄位（= `counts.videos`），新前端不用。
* 範圍在這支是寫死的，不是從查詢字串讀：它是在一般入口裡問「保險庫裡有什麼」。

#### 路徑正規形（`acl.normalize_folder_prefix`）

規則是用字串比對生效的，同一個資料夾有兩種寫法就是兩條規則 —— 其中一條可能
什麼都比對不到，畫面上卻顯示「已設定」。所以規則只有一種寫法：`/a/b`。

| 輸入 | 結果 |
|---|---|
| `/A`、`/A/` | `/A`（結尾一個斜線是唯一容許的差異） |
| 空字串、`A/B`（沒有開頭斜線） | 400 |
| `//A`、`/A//B`、`/A//` | 400（連續斜線：打錯還是真的有空名字，猜不出來） |
| `/A/./B`、`/A/../B` | 400（**不解析**：解析等於替送出的人決定他指的是哪個資料夾） |
| 含 `\`、NUL、控制字元 | 400（`\` 在 Linux FTP 上是合法字元，轉成 `/` 會指到別的資料夾） |
| `/`、`//` | 400（一次誤操作就把整個媒體庫變成保險庫） |

不做 Unicode 正規化：規則要跟掃描寫進資料庫的字串逐字相同才比對得到。
比對用的形狀（`Rule.normalized_prefix`）是正規形加結尾 `/`，範圍比對的正確性
（`/私人` 不吃到 `/私人物品`）不變。**舊資料**若不是正規形，讀取時退回舊的比對方式
並記一筆警告，而不是讓規則失效 —— 升級不能把任何資料夾解鎖。

#### 重疊規則：後端拒絕（409）

後台挑選器本來就不讓人選「已受限資料夾底下」的東西，但那只是提示。
`acl.add_rule()` 自己守：

| 已有 | 要加 | 結果 |
|---|---|---|
| `/A` | `/A`（或 `/A/`） | 409 `relation: same` |
| `/A` | `/A/B` | 409 `relation: ancestor`（既有的是上層；`/A/B` 已經跟著受限） |
| `/A/B` | `/A` | 409 `relation: descendant`（**不自動合併**：兩條的授權名單可能不同） |

409 的 `detail` 是 `{message, prefix, conflicts_with: {id, prefix}, relation}`，後台直接顯示
`message`。檢查與寫入在同一個交易（持有寫入鎖）裡，兩個人同時加 `/A` 與 `/A/B`
不會兩個都通過。被擋下的嘗試記 `folder_rule_conflict` 稽核。這次沒有處理「舊資料裡
已經存在的巢狀規則」—— 它們照舊生效，只是不能再新增。

#### 建立時驗證資料夾存在；既有規則不因掃描結果而消失

新規則的前綴必須是 `media_file`／`photo`／`document` 裡某個已知資料夾或它的上層，
否則 400（拼錯的規則等於沒有保護）。**只在建立時檢查**：既有規則即使某次掃描暫時
找不到資料夾也不刪 —— NAS／FTP 根目錄離線時掃描結果會少一大塊，那時候清掉規則，
資料夾一回來就是沒有保護的公開資料夾。`folder_counts()` 也照舊一定會回已受限的資料夾。

#### 規則快照與快取

`acl.rules()` 回 `Tuple[Rule, ...]`，`Rule` 是 frozen dataclass
（`id, prefix, normalized_prefix, note, user_ids: frozenset`）。快照是所有請求共用的，
能改它就等於能改所有人看到的權限，所以唯讀。

* 一次 `folder_rule LEFT JOIN folder_grant` 載完，不是 1+N。
* `invalidate()` 的時機不變（新增／移除規則、改授權、刪帳號收回授權），改完立即生效，
  不需要重新登入或重啟。另外加了版本號：載入進行到一半時被 invalidate 的那一份
  不會寫進快取（否則剛收回的授權要等下一次改動才生效）。

#### 帳號生命週期與孤兒授權

帳號在 SQLite 的 `app_user` 或 MSSQL 的 `dbo.filmax_users`，`folder_grant.user_id`
沒有外鍵可以串聯刪除。

* **真正刪除帳號**（`users.delete` → `acl.revoke_user(uid)`）才收回所有授權，
  並記在 `user_delete` 稽核的 detail 裡。先收回、再刪帳號：中途失敗時留下的是
  「還在但沒授權的帳號」，而不是「沒有主人的授權」。
* **停權／拒絕不收回**：那是暫時的狀態。而且儲存授權時（整份取代），後端會替
  **非 approved** 的帳號保留既有授權 —— 勾選清單只列 approved 的人，前端不可能
  把他們帶回來；不補的話任何一次儲存都會安靜地洗掉停權者的授權。已經不存在的帳號
  的授權則在下一次儲存時清掉。
* 升降管理員仍然不動授權（role 與明確授權是兩個不同的事實，見上面 2026-09-04 那段）。

#### 日誌

`assert_can_read` 擋下時的 INFO 日誌只記 `actor`、`rule=<id>`、`type=video|photo|document`
與路徑的 HMAC 指紋（12 字元，用 session 簽章金鑰），**不記完整路徑與檔名**。
一般日誌會被複製、上傳、貼進 issue，那裡沒有權限控管；用 HMAC 而不是單純雜湊，
是因為資料夾名稱常常猜得到，單純的 sha256 可以拿候選名單比對反查。
管理員改 ACL 的稽核（`folder_rule_add`／`remove`／`folder_grant_set`）照舊記完整路徑 ——
那是限管理員看的稽核表。

#### route table 測試

GATE 只認真的會過濾或擋下的函式（`filter_sql`／`filter_sql_any`、`assert_can_read`、
`visible_item`、`admin_only`）。`acl.has_vault` 與 `acl._vault_only_sql` 拿掉了：
前者只回布林值，「先問 has_vault、再回一份沒過濾的清單」會被誤認成有閘門；後者已經
併進 `filter_sql`。另外檢查 helper（`_file_or_404`／`_photo_row`）自己真的呼叫
`assert_can_read`，以及例外清單裡沒有已經不存在的路由（這次抓到一條過期的
`/api/healthz` —— 真正的路由是 `/healthz`，不在 `/api/` 底下）。`/api/genres` 從例外清單
移除，改由靜態檢查正常把關。

#### 驗收

`tests/acl_test.py`（207 項）新增：

* shared／vault × video／photo／document × 沒授權／已授權／管理員／密碼登入的完整矩陣，
  含第二個「只有相片與文件」的受限資料夾（摘要不能顯示成 0；被授權 A 的人看不到 B）
* 單筆讀取在 `scope` 為 無／shared／vault／any／亂寫 時結果完全一樣
* 路徑正規形的 13 種不合法輸入、API 回 400、結尾斜線視為同一條（409 same）、
  巢狀（ancestor）與上層蓋下層（descendant）都 409、失敗不寫入、直接呼叫
  `acl.add_rule` 也擋、`/lib/private` 不吃到 `/lib/private2`、舊的非正規形規則照樣生效
* 刪帳號不留孤兒授權；停權 → 儲存授權 → 恢復，授權沒有遺失
* 重建快取只查一次資料庫、載入中被 invalidate 不會寫入快取、快照唯讀、
  給／收授權同一張 token 立即生效
* 擋下時的日誌不含私人路徑與檔名

`tests/nav_scope_test.js`（45 項）改成**在 vm 裡載入真的 app.js**（舊版是抄一份邏輯來測）：
Shared Video → Vault Video → Vault Photo → Vault Document → Shared Document、
換媒體不回 Shared、保險庫裡每個 GET 都帶 scope、切回共用時「新內容還沒回來」那一刻
畫面上就沒有私人的字、晚到的私人回應被丟掉、未知的新端點也會帶 scope。

**本次不需要 DB migration**（資料表結構沒有變；新規則的格式檢查只作用在新增時）。

---
