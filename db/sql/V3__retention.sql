/* 稽核紀錄保留 180 天。沒有清理機制的日誌表遲早會吃光磁碟。
   Express 版沒有 SQL Agent，所以做成預存程序由應用程式每天呼叫一次。 */

CREATE OR ALTER PROCEDURE dbo.filmax_purge_audit
    @days INT = 180
AS
BEGIN
    SET NOCOUNT ON;
    DELETE TOP (5000) FROM dbo.filmax_audit
    WHERE at < DATEADD(DAY, -@days, SYSUTCDATETIME());
    RETURN @@ROWCOUNT;
END;
