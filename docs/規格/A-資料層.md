> 這是《FilmaxWeb 規格需求書》的第 **A** 章。索引、分期計畫與決策表在 [../規格需求書.md](../規格需求書.md)。
> 章別代號（A-4、G-6、J 的第 −1 層…）在全書通用，跨章引用時直接用代號。

## A. 儲存後端與資料層

**預設 SQLite，MSSQL 為選配。** 這一節多數工作與用哪個資料庫無關。

> **狀態（2026-09-04）：A-1～A-4 全部完成並有測試；A-5 決定不做。**
> 這一章的規劃比實作早很多，中間是分散在各輪順手做掉的，所以索引一直把它掛在「待做」——
> 這次逐條對照過程式與測試之後改成事實。每一節末尾都標了實際的落點與偏差。

| 節 | 狀態 | 實作 | 測試 |
|---|---|---|---|
| A-1 設定開關 | ✅ | `config.user_store`／`media_store`／`check_stores()` | `phase1 [A-1]` |
| A-2 批次寫入 | ✅ | `db.Batch`／`db.batch()`；條目走記憶體註冊表 `scanner._item_reg` | `phase2 [A-2]`、`scan_e2e`（15 列重掃只用 4 個交易） |
| A-2 的 MSSQL 三條件 | ✅ | `mssql.tx()`／`executemany(sizes=, verify=)` | `mssql_test [A-2]` |
| A-3 時間欄位 | ✅ | epoch 整數、`sort_ts`、`taken_tz`、trigger 值域守衛 | `phase2 [A-3]`、`startup [A-3]` |
| A-4 單一刪除進入點 | ✅ | `purge(reason=)`／`sweep()`／`counts()` | `phase2 [A-4]` ×2 |
| A-5 MSSQL 媒體庫 | ❌ **不做** | — | — |

### A-1 設定開關

```ini
USER_STORE=mssql      # 帳號
MEDIA_STORE=sqlite    # 媒體庫，預設值
```

- 兩個開關獨立；值只接受 `sqlite` / `mssql`，寫錯就啟動報錯。
- 設 `mssql` 但連線資訊不齊 → 報錯指出缺哪一項，**不自動退回 SQLite**。
- 沒設 `USER_STORE` 但有 `MSSQL_HOST` → 沿用現有行為並在日誌提醒。

### A-2 批次寫入

```python
with store.batch() as b:
    b.upsert_file({...})     # 只進緩衝
    # 累積到 N 筆自動寫出並 commit；離開 with 強制寫出
```

**不是每張表都適合批次：**

| 表 | 批次 | 原因 |
|---|---|---|
| `media_file` / `photo` | 可以 | 純 upsert，不需當場拿 id |
| `media_item` / `episode` | **不批次** | 寫檔案列前必須先有 `item_id`。改用行程內註冊表：掃描開始時把 15 筆 `(guess_key → id)` 載進記憶體，解析時持鎖序列化 |

只有 15 個條目，序列化成本是零；硬做成批次只會製造難修的競態
（兩執行緒同時建同一個 `guess_key` → 同一部影集出現兩張海報，一張 12 集一張 1 集）。

**若日後啟用 MSSQL，批次有三個必要條件：**

1. `fast_executemany` 必須搭配 `setinputsizes()`（用 schema 寬度，不讓 pyodbc 猜）。
   這是正確性不是優化 — 已知會截斷，更糟的是**前一列的字串殘留覆蓋下一列，無錯誤訊息**。
2. **寫完要對帳。** `fast_executemany` 遇到違反約束的列**不拋例外**，有效列照樣寫入、
   失敗列靜默消失。必須 `SELECT COUNT(*)` 核對，不符就整批退回。
3. `executemany` 不能取回結果集，拿不到 `OUTPUT INSERTED.id`。

`app/mssql.py` 目前 `autocommit=True` 寫死、沒有 `tx()`/`commit`/`rollback`/`executemany`，
而它的模組說明宣稱有 `executemany`。**這是我寫的，一併修。**

### A-3 時間欄位

| 類別 | 欄位 | Python | SQLite | MSSQL |
|---|---|---|---|---|
| 時間點 | `added_at` `updated_at` `seen_at` `mtime` `taken_at` | epoch 整數秒 | `INTEGER` | `DATETIME2(0)` |
| 日期 | `episode.air_date` | 字串 | `TEXT` | `DATE` |
| **時間長度** | `duration` `position` | **維持浮點，一個字都不要動** | | |

> **時間點與時間長度這條界線畫錯就是使用者立刻有感的災難。**
> `play_state.position` 若取整成秒，續播每次最多漂 1 秒，`WHERE position > 30`
> 這種門檻的行為也會變。使用者的抱怨會是「每次繼續看都往前跳」。

**三個配套：**

1. **具體化排序鍵 `sort_ts`** — 寫入時算好 `COALESCE(taken_at, mtime, added_at)`，
   `NOT NULL` 加索引。同時解掉排序錯誤與「`COALESCE` 用不到索引」。
   代價：衍生資料只能在 `upsert_photo()` 一處被寫，要有測試釘住。
2. **保留 `taken_at_raw`** — EXIF 原始字串一字不改。EXIF 的 `DateTimeOriginal` 沒有時區，
   轉 epoch 必須假設一個，而那假設在出國拍、相機沒調時鐘的照片上一定是錯的。
   另加 `taken_tz` 記錄這個 epoch 是 EXIF 給的還是我們假設的。
3. **值域 CHECK 約束** — `CHECK (taken_at IS NULL OR taken_at BETWEEN 0 AND 4102444800)`。
   `DATETIME2` 天生擋得住亂值，整數欄位擋不住：把毫秒當秒傳會安靜存進去，
   然後那筆永遠排最前面。兩個後端都要加。

### A-4 不用外鍵，改用單一刪除進入點

```python
store.purge(item_ids=..., file_ids=..., photo_ids=..., reason='scan_vanished')
store.sweep(mode='report' | 'fix')
```

`purge()` 是唯一的刪除進入點，所有連帶關係（含磁碟上的縮圖檔）都在裡面展開，
`reason` 必填並進稽核。

**清掃時機與寬限期：**

| 時機 | 模式 | 理由 |
|---|---|---|
| 掃描的 `finally` | `fix` | 「被取消」與「出錯」正是孤兒最多的結局，只掛成功路徑等於永遠不清最需要清的那次 |
| 服務啟動 | `report` | **刻意不修**。啟動路徑自動刪資料很糟，尤其剛還原備份或剛換後端之後 |
| 後台按鈕 | 兩種 | 先 dry-run 看清單 |

第 5、6、7 類孤兒必須有 1 小時寬限期 — 索引流程是「先建條目、再寫檔案列」，
兩步之間條目就是合法的孤兒。

**孤兒數量要露在 `/api/stats` 與後台總覽。** 沒有外鍵的資料庫，孤兒計數就是體溫計。

### A-3 的三處偏差（實作與上面的文字不同）

1. **值域用 trigger，不是 `CHECK`。** SQLite 不能對既有表 `ALTER TABLE ADD CHECK`，
   而 `mtime_ts`／`taken_ts`／`sort_ts` 都是後來補的欄位。改用 `BEFORE INSERT`／
   `BEFORE UPDATE` 兩個 trigger（`db._time_guards`），**同時擋兩種寫入**，
   效果與 CHECK 相同而且看得到錯誤訊息（`media_file.mtime_ts 超出合理時間範圍`）。
2. **`taken_at_raw` 實際叫 `taken_at`。** EXIF 原始字串照存不動的那一欄沿用舊名，
   解析後的 epoch 是新加的 `taken_ts`。行為完全符合規格，只是名字沒改
   —— 改名要動既有資料，而它換來的只是好看。
3. **時間點整數化是 2026-09-04 才補完的。** 寫入端一直有兩個入口
   （`db.now()` 浮點與 `db.now_i()` 整數），實機上 `media_file.seen_at` 147 列、
   `photo.seen_at` 1490 列、`media_item.added_at`／`updated_at` 48 列全是浮點。
   現在 `app/` 裡**沒有任何地方用 `db.now()` 寫時間點**，並且 `init_db()` 會做
   一次性取整（`_round_time_points`，用 `kv` 記號確保只做一次）。
   **`duration`／`position` 沒有被碰到** —— 那是時間長度，取整會讓續播每次往前跳一秒。

### A-5 MSSQL 媒體庫（選配，**決定不做**）

**2026-09-04 決定不做**（決策 26）。理由：

* 媒體庫在 SQLite 上跑得很好 —— 147 個影片檔、1490 張相片、48 個條目，
  單一寫入者、無競態，批次化之後重掃的交易數是個位數。
* MSSQL 目前**只服務帳號那一半**（`USER_STORE=mssql`），那部分已經在跑而且有測試。
  把媒體庫也搬過去，換到的是「可以多台機器共用媒體庫」——**而現在只有一台**。
* 代價不是「多實作一個檔案」而已：多一個一定要在線的外部相依，
  掃描與播放的每一次寫入都變成跨行程往返，備份與還原也從「複製一個檔案」
  變成兩套流程。

**要做的前提**：真的出現第二台機器要共用同一個媒體庫。在那之前這一節維持規劃狀態。
下面的實作提示保留，將來要做時直接照著走。

#### 將來真的要做的話

A-2～A-4 做完後只是多實作一個檔案。額外工作：Flyway `V6` 建表、`guess_key` 用篩選唯一索引、
分頁改 `OFFSET/FETCH`、子查詢 `LIMIT 1` 改 `CROSS APPLY`、`lastrowid` 改 `OUTPUT INSERTED.id`。

**upsert 不要用 `MERGE`** — SQL Server 社群的權威意見是它有從未修復的缺陷。
改用暫存表 + 集合式 `UPDATE...FROM JOIN` 加 `INSERT...WHERE NOT EXISTS`，
帶 `WITH (UPDLOCK, SERIALIZABLE)`。附帶好處：`WHERE` 加變更偵測後，
重複掃描時沒變的檔案完全不產生寫入。

---
