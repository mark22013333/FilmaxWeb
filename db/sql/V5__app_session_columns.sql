/* 應用程式需要的兩個欄位，以及服務帳號執行清理程序的權限。

   sess_ver 是「這個人的 session 版本號」。停權或改角色時 +1，
   對方手上那張 token 的簽章就對不起來，立刻失效（見 app/auth.py 的 _sign_user）。
   沒有它的話，「拔掉某個人的權限」要等到 token 過期才算數 —— 出事的時候沒有煞車。

   login_count 純粹是後台看的：這個帳號到底用過幾次。 */

IF COL_LENGTH('dbo.filmax_users', 'sess_ver') IS NULL
    ALTER TABLE dbo.filmax_users
        ADD sess_ver INT NOT NULL CONSTRAINT DF_filmax_users_sessver DEFAULT (0);
GO

IF COL_LENGTH('dbo.filmax_users', 'login_count') IS NULL
    ALTER TABLE dbo.filmax_users
        ADD login_count INT NOT NULL CONSTRAINT DF_filmax_users_logins DEFAULT (0);
GO

/* V3 的清理程序是設計成由應用程式每天呼叫一次的，但 db_datareader /
   db_datawriter 都不含 EXECUTE —— 少了這一條，服務呼叫時會拿到權限錯誤。
   帳號名稱可能被改過，所以先確認存在再授權。 */
IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'filmax_app')
    GRANT EXECUTE ON OBJECT::dbo.filmax_purge_audit TO filmax_app;
GO

/* V2 的 action 白名單裡沒有「改角色」這件事。
   硬把降級記成 'forbidden'、升級記成 'enable' 是在遷就限制式而扭曲資料 ——
   之後查稽核的人會被這種命名誤導。加一個誠實的值進去。 */
IF EXISTS (SELECT 1 FROM sys.check_constraints
           WHERE name = 'CK_filmax_audit_action'
             AND parent_object_id = OBJECT_ID('dbo.filmax_audit'))
    ALTER TABLE dbo.filmax_audit DROP CONSTRAINT CK_filmax_audit_action;
GO

ALTER TABLE dbo.filmax_audit WITH CHECK
    ADD CONSTRAINT CK_filmax_audit_action CHECK (action IN (
        'register','login','login_denied','approve','reject',
        'disable','enable','forbidden','role_change'));
GO
