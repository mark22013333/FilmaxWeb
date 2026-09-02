/* 註冊與登入的來源記錄：IP、地理位置、裝置。

   ── 為什麼整個檔案都包在 IF ... IS NULL 裡面 ────────────────────────────
   V1 與 V2 後來被補上了這些欄位，所以在全新的資料庫上，這支遷移想加的東西
   其實都已經存在了。沒有防護的 ALTER TABLE ADD 會直接失敗：
       Column names in each table must be unique.
   ——— Flyway 停在 V4，後面全部不會套用，而且失敗訊息看起來像是「Flyway 壞了」。

   正確的做法不是回頭改 V1/V2（Flyway 記了每個檔案的 checksum，只要在任何一台
   機器套用過，事後修改就會讓 validate 失敗），而是讓這一支變成可以重複執行：
   已經有的就跳過，缺的才補。舊資料庫照樣補得到欄位，新資料庫則安靜通過。

   位置資料一律來自 Cloudflare 的 CF-* 標頭（見 app/geo.py），
   不呼叫任何第三方 API，也不在本機維護 GeoIP 資料庫。
   ────────────────────────────────────────────────────────────────── */

/* ---------- 使用者：註冊當下與最近一次登入的來源 ---------- */
IF COL_LENGTH('dbo.filmax_users', 'registered_ip') IS NULL
    ALTER TABLE dbo.filmax_users ADD registered_ip NVARCHAR(64) NULL;
GO
IF COL_LENGTH('dbo.filmax_users', 'registered_country') IS NULL
    ALTER TABLE dbo.filmax_users ADD registered_country CHAR(2) NULL;
GO
IF COL_LENGTH('dbo.filmax_users', 'registered_city') IS NULL
    ALTER TABLE dbo.filmax_users ADD registered_city NVARCHAR(120) NULL;
GO
IF COL_LENGTH('dbo.filmax_users', 'last_login_country') IS NULL
    ALTER TABLE dbo.filmax_users ADD last_login_country CHAR(2) NULL;
GO
IF COL_LENGTH('dbo.filmax_users', 'last_login_city') IS NULL
    ALTER TABLE dbo.filmax_users ADD last_login_city NVARCHAR(120) NULL;
GO
IF COL_LENGTH('dbo.filmax_users', 'last_login_ua') IS NULL
    ALTER TABLE dbo.filmax_users ADD last_login_ua NVARCHAR(400) NULL;
GO

/* ---------- 稽核：每一筆事件的完整來源 ---------- */
IF COL_LENGTH('dbo.filmax_audit', 'ip_source') IS NULL
    ALTER TABLE dbo.filmax_audit ADD ip_source VARCHAR(16) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'country') IS NULL
    ALTER TABLE dbo.filmax_audit ADD country CHAR(2) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'region') IS NULL
    ALTER TABLE dbo.filmax_audit ADD region NVARCHAR(120) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'city') IS NULL
    ALTER TABLE dbo.filmax_audit ADD city NVARCHAR(120) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'timezone') IS NULL
    ALTER TABLE dbo.filmax_audit ADD timezone NVARCHAR(64) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'latitude') IS NULL
    ALTER TABLE dbo.filmax_audit ADD latitude DECIMAL(9,6) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'longitude') IS NULL
    ALTER TABLE dbo.filmax_audit ADD longitude DECIMAL(9,6) NULL;
GO
IF COL_LENGTH('dbo.filmax_audit', 'user_agent') IS NULL
    ALTER TABLE dbo.filmax_audit ADD user_agent NVARCHAR(400) NULL;
GO

/* 「最近從哪些國家登入」這種查詢的索引。V2 已經建過同名索引，
   同名索引重建會直接報錯，所以一樣要先確認。 */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_filmax_audit_country'
                 AND object_id = OBJECT_ID('dbo.filmax_audit'))
    CREATE INDEX IX_filmax_audit_country ON dbo.filmax_audit (country, at DESC)
        WHERE country IS NOT NULL;
GO

/* ---------- 後台用的檢視 ---------- */
/* 檢視裡不放 ORDER BY —— SQL Server 會忽略它（TOP 100 PERCENT + ORDER BY 是無效的
   老寫法），排序交給呼叫端。CREATE OR ALTER 本來就可重複執行。
   需要 SQL Server 2016 SP1 以上。 */
CREATE OR ALTER VIEW dbo.filmax_login_activity
AS
SELECT
    a.at,
    a.email,
    a.action,
    a.ip,
    a.ip_source,
    a.country,
    a.region,
    a.city,
    a.timezone,
    a.user_agent,
    a.detail
FROM dbo.filmax_audit AS a
WHERE a.action IN ('register','login','login_denied','forbidden');
GO
