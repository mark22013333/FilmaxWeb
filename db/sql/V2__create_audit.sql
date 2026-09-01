/* 稽核紀錄：誰、什麼時候、從哪裡、用什麼裝置，做了什麼。
   出事時這張表是唯一能回溯的依據。

   位置欄位一律來自 Cloudflare 的 CF-* 標頭，不呼叫第三方 API，
   也不在本機維護 GeoIP 資料庫。判斷標頭可不可信的規則見 app/geoip.py。 */

CREATE TABLE dbo.filmax_audit (
    id          BIGINT IDENTITY(1,1) NOT NULL,
    at          DATETIME2(0)  NOT NULL CONSTRAINT DF_filmax_audit_at DEFAULT (SYSUTCDATETIME()),
    email       NVARCHAR(320) NULL,
    action      VARCHAR(40)   NOT NULL,
    detail      NVARCHAR(500) NULL,

    ip          NVARCHAR(64)  NULL,
    ip_source   VARCHAR(16)   NULL,   -- cloudflare / lan / direct / internal
    country     CHAR(2)       NULL,   -- 未知時 Cloudflare 給 XX
    region      NVARCHAR(120) NULL,
    city        NVARCHAR(120) NULL,
    timezone    NVARCHAR(64)  NULL,
    latitude    DECIMAL(9,6)  NULL,   -- 城市中心點，不是使用者的實際位置
    longitude   DECIMAL(9,6)  NULL,
    user_agent  NVARCHAR(400) NULL,

    CONSTRAINT PK_filmax_audit PRIMARY KEY CLUSTERED (id),
    CONSTRAINT CK_filmax_audit_action CHECK (action IN (
        'register','login','login_denied','approve','reject','disable','enable','forbidden')),
    CONSTRAINT CK_filmax_audit_ipsrc CHECK (ip_source IS NULL OR ip_source IN (
        'cloudflare','lan','direct','internal'))
);
GO

CREATE INDEX IX_filmax_audit_at    ON dbo.filmax_audit (at DESC);
GO
CREATE INDEX IX_filmax_audit_email ON dbo.filmax_audit (email, at DESC);
GO
-- 「最近從哪些國家登入」
CREATE INDEX IX_filmax_audit_country ON dbo.filmax_audit (country, at DESC)
    WHERE country IS NOT NULL;
GO

/* 後台用的檢視。
   刻意不放 ORDER BY —— SQL Server 會忽略檢視裡的排序（TOP 100 PERCENT + ORDER BY
   是無效的老寫法），排序交給呼叫端。需要 SQL Server 2016 SP1 以上才支援 CREATE OR ALTER。 */
CREATE OR ALTER VIEW dbo.filmax_login_activity
AS
SELECT
    a.at, a.email, a.action, a.ip, a.ip_source,
    a.country, a.region, a.city, a.timezone,
    a.user_agent, a.detail
FROM dbo.filmax_audit AS a
WHERE a.action IN ('register','login','login_denied','forbidden');
GO
