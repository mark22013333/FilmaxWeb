# FilmaxWeb 資料庫

使用者帳號存在 MSSQL，結構異動一律走 Flyway。

## 在哪裡跑？

**全部在你自己的電腦上跑，不需要登入資料庫主機。**

Flyway 和 `sqlcmd` 都是用戶端工具，跟 SSMS、DataGrip 一樣透過網路連到 1433 埠：

```
你的電腦                                    DB 主機 your-db-host
─────────────────────────                  ──────────────────────
  00_create_database.sql  ──┐
  （用 sqlcmd 或 DataGrip） │
                            ├── TCP 1433 ──▶   MSSQL
  migrate.bat               │                  └─ filmax 資料庫
  （Flyway CLI）          ──┘
```

所以你只要：能從這台電腦連到那台 MSSQL 的 1433 埠、有帳號密碼，就這樣。

### 執行 SQL 的工具，挑一個就好

| 工具 | 說明 |
|---|---|
| **DataGrip** | 你已經有了（`~/DataGripProjects`）。新增 MSSQL 資料來源後直接開檔案執行 |
| **Azure Data Studio** | `winget install Microsoft.AzureDataStudio` |
| **sqlcmd** | 命令列，指令見下方 |
| SSMS | 傳統選擇，安裝檔比較大 |

## 為什麼要用 Flyway，不直接手動貼 SQL？

老實說，**你也可以**把 `sql/` 底下三個檔案照順序貼進 DataGrip 執行，結果一樣。
Flyway 帶來的價值不在第一次建表，而在之後：

- **它記得跑過什麼。** `filmax_schema_history` 表會記錄每個檔案何時套用、checksum 多少。
  你不用回想「V3 那支到底跑了沒」。
- **之後改結構只要丟新檔案。** 第四期我要加欄位時，你只要 `git pull` 再跑一次
  `migrate.bat`，它自己知道要套用哪幾支。
- **重建環境時能完整重現。** 換機器、災難復原、或想開一套測試庫，一個指令就到位。
- **服務啟動時可以驗證版本。** 程式能檢查資料表版本跟自己預期的一致，
  不一致就明確報錯，而不是跑到一半才出現看不懂的錯誤。

如果你的結構真的不會再變，手動執行也合理。但這個專案還有第三、四期要做，
到時候至少還會再改一次結構。

---

## 這兩支做的事不一樣

| 檔案 | 建了什麼 | 何時跑 |
|---|---|---|
| `00_create_database.sql` | **資料庫容器**與**兩個登入帳號**。裡面沒有任何 `CREATE TABLE` | 只做一次 |
| `migrate.bat`（Flyway） | **資料表**、檢視、預存程序 | 每次有新的遷移檔就跑 |

跑完第一支之後，你手上是一個「有帳號、沒有任何表」的空資料庫。**兩支都要跑。**

想知道現在的狀態，執行 `99_verify.sql`（唯讀，不會改任何東西），
它會列出帳號、資料表、已套用的遷移，並直接告訴你還缺什麼。

## 服務不會自己建表

如果你習慣 Hibernate 的 `ddl-auto=update`，這裡刻意**不做那件事**。
`00_create_database.sql` 裡有這一行：

```sql
DENY ALTER, CREATE TABLE, CREATE VIEW, CREATE PROCEDURE TO filmax_app;
```

服務用的帳號在資料庫層級就被禁止改結構 —— 就算程式碼寫了建表邏輯也會被 SQL Server 擋下。
這是刻意的：服務被入侵時，對方讀寫得到資料，但改不掉結構、刪不掉表。

所以之後任何結構異動的順序都是：

```
拿到新的 V5__xxx.sql  →  跑 migrate.bat  →  才重啟服務
```

---

## 一、建立資料庫（只做一次）

用 **sysadmin 權限**的帳號（例如 `sa`）在 SSMS 或 sqlcmd 執行 `00_create_database.sql`。
執行前請先把檔案裡兩處 `請換成你的密碼_*` 改掉。

```
sqlcmd -S your-db-host,1433 -U sa -P 你的sa密碼 -i 00_create_database.sql -C
```

`-C` 等同 `trustServerCertificate=true`（MSSQL 沒有正式憑證時需要）。

**如果你已經自己建好 `filmax` 資料庫了**，那就只要執行檔案裡「2. 登入帳號」
與「3. 資料庫使用者與權限」那兩段就好 —— 建資料庫那段本來就會自己跳過。
重點是要有 `filmax_flyway` 和 `filmax_app` 這兩個帳號，服務用的是後者。

它會建立：

| 物件 | 用途 |
|---|---|
| 資料庫 `filmax` | 存使用者資料 |
| 登入 `filmax_flyway` | 只給 Flyway 做結構異動，`db_owner` |
| 登入 `filmax_app` | 服務平常用，只能讀寫資料、**明確禁止改結構** |

分兩個帳號是刻意的：服務本身若被入侵，對方也改不掉資料表結構。

## 二、套用遷移

1. 下載 [Flyway Community 命令列工具](https://documentation.red-gate.com/fd/command-line-184127404.html)，
   選 `flyway-commandline-*-windows-x64.zip`（**內含 JRE，不必另外裝 Java**）。
   解壓後把 `flyway.cmd` 所在資料夾加進 PATH。
2. 確認 `flyway.conf` 的 `flyway.url` 沒問題（已經填好你的主機）。
3. 雙擊 `migrate.bat`。它會先顯示狀態，你確認後才套用。

`migrate.bat` 連不上時最常見的原因：那台 MSSQL 沒開放 1433 給你的來源 IP，
或是 SQL Server 沒啟用 TCP/IP 通訊協定。

密碼永遠用輸入或 `FLYWAY_PASSWORD` 環境變數帶入，**不寫進檔案**。

## 三、遷移檔

| 版本 | 內容 |
|---|---|
| `V1__create_users.sql` | `filmax_users` —— 帳號、角色、審核狀態、註冊與最近登入的來源 |
| `V2__create_audit.sql` | `filmax_audit` —— 完整的登入與審核歷史（含 IP、位置、裝置）＋ `filmax_login_activity` 檢視 |
| `V3__retention.sql` | `filmax_purge_audit` —— 稽核紀錄保留 180 天 |

Flyway 自己的紀錄表是 `filmax_schema_history`（刻意加前綴，避免跟那台資料庫上的其他東西撞名）。

### 之後要改結構

**永遠新增一個檔案，不要改已經套用過的。** Flyway 會記錄每個檔案的 checksum，
改動已套用的檔案會導致 `validate` 失敗。

命名規則 `V<版本>__<說明>.sql`，注意是**兩條底線**：

```
V4__add_user_quota.sql
V5__add_watch_history.sql
```

常用指令：

```
flyway info       看每個遷移的狀態
flyway migrate    套用還沒跑過的
flyway validate   檢查已套用的檔案有沒有被改過
```

`flyway clean`（清空整個 schema）已在設定裡停用，避免手滑清掉正式資料。
