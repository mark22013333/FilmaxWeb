/* 唯讀檢查：看看 filmax 資料庫現在到底有什麼。
   隨時可以跑，不會改動任何東西。 */

USE filmax;
GO

PRINT '===== 1. 登入帳號（應該要有 filmax_flyway 與 filmax_app）=====';
SELECT
    dp.name                AS 使用者,
    dp.type_desc           AS 類型,
    STRING_AGG(r.name, ', ') AS 隸屬角色
FROM sys.database_principals dp
LEFT JOIN sys.database_role_members rm ON rm.member_principal_id = dp.principal_id
LEFT JOIN sys.database_principals r    ON r.principal_id = rm.role_principal_id
WHERE dp.name IN ('filmax_flyway','filmax_app')
GROUP BY dp.name, dp.type_desc;

PRINT '';
PRINT '===== 2. 資料表（跑過 migrate 才會有）=====';
SELECT
    t.name AS 資料表,
    (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS 欄位數,
    (SELECT SUM(p.rows) FROM sys.partitions p
      WHERE p.object_id = t.object_id AND p.index_id IN (0,1))          AS 資料筆數
FROM sys.tables t
WHERE t.name LIKE 'filmax[_]%'
ORDER BY t.name;

PRINT '';
PRINT '===== 3. 檢視與預存程序 =====';
SELECT name AS 物件, type_desc AS 類型
FROM sys.objects
WHERE name LIKE 'filmax[_]%' AND type IN ('V','P')
ORDER BY type_desc, name;

PRINT '';
PRINT '===== 4. Flyway 已套用的遷移 =====';
IF OBJECT_ID('dbo.filmax_schema_history') IS NULL
    PRINT '  >> 還沒有 filmax_schema_history —— 代表 migrate.bat 從沒跑過。';
ELSE
    SELECT installed_rank AS 序, version AS 版本, description AS 說明,
           success AS 成功, installed_on AS 套用時間
    FROM dbo.filmax_schema_history
    ORDER BY installed_rank;

PRINT '';
PRINT '===== 結論 =====';
IF OBJECT_ID('dbo.filmax_users') IS NULL OR OBJECT_ID('dbo.filmax_audit') IS NULL
    PRINT '  >> 資料表還沒建好，請執行 migrate.bat。';
ELSE
    PRINT '  >> 資料表齊全，可以啟動服務。';
GO
