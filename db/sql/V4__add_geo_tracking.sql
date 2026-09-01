/* 註冊與登入的來源記錄：IP、地理位置、裝置。

   為什麼是獨立的 V4 而不是直接改 V1/V2：
   Flyway 會記錄每個遷移檔的 checksum，只要 V1 曾經在任何一台機器套用過，
   事後修改它就會讓 validate 失敗。永遠新增檔案，不要改已經存在的。

   位置資料一律來自 Cloudflare 的 CF-* 標頭（見 app/geoip.py），
   不呼叫任何第三方 API，也不需要在本機維護 GeoIP 資料庫。 */

/* ---------- 使用者：註冊當下與最近一次登入的來源 ---------- */
ALTER TABLE dbo.filmax_users ADD
    registered_ip       NVARCHAR(64)  NULL,
    registered_country  CHAR(2)       NULL,
    registered_city     NVARCHAR(120) NULL,
    last_login_country  CHAR(2)       NULL,
    last_login_city     NVARCHAR(120) NULL,
    last_login_ua       NVARCHAR(400) NULL;
GO

/* ---------- 稽核：每一筆事件的完整來源 ---------- */
ALTER TABLE dbo.filmax_audit ADD
    ip_source   VARCHAR(16)   NULL,   -- cloudflare / lan / direct / internal
    country     CHAR(2)       NULL,   -- ISO 3166-1 alpha-2；Cloudflare 對未知會給 XX
    region      NVARCHAR(120) NULL,
    city        NVARCHAR(120) NULL,
    timezone    NVARCHAR(64)  NULL,
    latitude    DECIMAL(9,6)  NULL,   -- 城市中心點，不是使用者的實際位置
    longitude   DECIMAL(9,6)  NULL,
    user_agent  NVARCHAR(400) NULL;
GO

/* 「最近從哪些國家登入」這種查詢的索引 */
CREATE INDEX IX_filmax_audit_country ON dbo.filmax_audit (country, at DESC)
    WHERE country IS NOT NULL;
GO

/* ---------- 後台用的檢視 ---------- */
/* 檢視裡不放 ORDER BY —— SQL Server 會忽略它（TOP 100 PERCENT + ORDER BY 是無效的
   老寫法），排序交給呼叫端。需要 SQL Server 2016 SP1 以上才支援 CREATE OR ALTER。 */
CREATE OR ALTER VIEW dbo.filmax_login_activity
AS
SELECT
    a.at,
    a.email,
    a.action,
    a.ip,
    a.ip_source,
    a.country,
    a.city,
    a.timezone,
    a.user_agent,
    a.detail
FROM dbo.filmax_audit AS a
WHERE a.action IN ('register','login','login_denied','forbidden');
GO
