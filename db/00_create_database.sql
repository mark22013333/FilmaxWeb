/* ============================================================================
   FilmaxWeb — 建立資料庫與帳號（只跑這一次，之後的異動一律交給 Flyway）

   請用有 sysadmin 權限的帳號執行（例如 sa）。
   兩個帳號是刻意分開的：

     filmax_flyway  只有 Flyway 做結構異動時用，權限大
     filmax_app     服務平常用，只能讀寫資料、不能改結構

   這樣就算服務本身被入侵，對方也改不掉資料表結構。
   下面的密碼請自己換掉，不要沿用範例。
   ============================================================================ */

/* ---------- 1. 資料庫 ---------- */
IF DB_ID('filmax') IS NULL
BEGIN
    CREATE DATABASE filmax;
END
GO

ALTER DATABASE filmax SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;
GO

/* ---------- 2. 登入帳號（伺服器層級）---------- */
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'filmax_flyway')
BEGIN
    CREATE LOGIN filmax_flyway
        WITH PASSWORD = N'flyway1qaz@WSX',
             CHECK_POLICY = ON,
             DEFAULT_DATABASE = filmax;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'filmax_app')
BEGIN
    CREATE LOGIN filmax_app
        WITH PASSWORD = N'flyway1qaz@WSX',
             CHECK_POLICY = ON,
             DEFAULT_DATABASE = filmax;
END
GO

/* ---------- 3. 資料庫使用者與權限 ---------- */
USE filmax;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'filmax_flyway')
    CREATE USER filmax_flyway FOR LOGIN filmax_flyway;
GO
ALTER ROLE db_owner ADD MEMBER filmax_flyway;   -- Flyway 需要建表改表的權限
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'filmax_app')
    CREATE USER filmax_app FOR LOGIN filmax_app;
GO
ALTER ROLE db_datareader ADD MEMBER filmax_app;
ALTER ROLE db_datawriter ADD MEMBER filmax_app;
GO
-- 明確擋掉結構異動，就算日後有人不小心加了角色也擋得住
DENY ALTER, CREATE TABLE, CREATE VIEW, CREATE PROCEDURE TO filmax_app;
GO

/* ---------- 4. 確認 ---------- */
SELECT name AS 資料庫使用者, type_desc AS 類型 FROM sys.database_principals
WHERE name IN ('filmax_flyway','filmax_app');
GO
