/* FilmaxWeb 使用者表
   身分由 Google OAuth 提供，這裡不存任何密碼。
   註冊與最近一次登入的來源（IP／位置／裝置）一併記在這張表，
   完整的歷史則在 filmax_audit。 */

CREATE TABLE dbo.filmax_users (
    id                  INT IDENTITY(1,1) NOT NULL,
    email               NVARCHAR(320)     NOT NULL,
    google_sub          NVARCHAR(64)      NULL,      -- Google 帳號的永久唯一 ID
    display_name        NVARCHAR(200)     NULL,
    picture_url         NVARCHAR(500)     NULL,
    role                VARCHAR(20)       NOT NULL CONSTRAINT DF_filmax_users_role    DEFAULT ('viewer'),
    status              VARCHAR(20)       NOT NULL CONSTRAINT DF_filmax_users_status  DEFAULT ('pending'),
    created_at          DATETIME2(0)      NOT NULL CONSTRAINT DF_filmax_users_created DEFAULT (SYSUTCDATETIME()),

    -- 註冊當下的來源
    registered_ip       NVARCHAR(64)      NULL,
    registered_country  CHAR(2)           NULL,      -- ISO 3166-1 alpha-2，未知為 XX
    registered_city     NVARCHAR(120)     NULL,

    -- 審核
    approved_at         DATETIME2(0)      NULL,
    approved_by         NVARCHAR(200)     NULL,

    -- 最近一次登入的來源
    last_login_at       DATETIME2(0)      NULL,
    last_login_ip       NVARCHAR(64)      NULL,
    last_login_country  CHAR(2)           NULL,
    last_login_city     NVARCHAR(120)     NULL,
    last_login_ua       NVARCHAR(400)     NULL,

    note                NVARCHAR(500)     NULL,

    CONSTRAINT PK_filmax_users PRIMARY KEY CLUSTERED (id),
    CONSTRAINT UQ_filmax_users_email UNIQUE (email),
    CONSTRAINT CK_filmax_users_role   CHECK (role   IN ('owner','viewer')),
    CONSTRAINT CK_filmax_users_status CHECK (status IN ('pending','approved','rejected','disabled'))
);
GO

-- 後台「待審核清單」的主要查詢路徑
CREATE INDEX IX_filmax_users_status ON dbo.filmax_users (status)
    INCLUDE (email, display_name, created_at);
GO

-- google_sub 可能為 NULL（管理員先建好、對方還沒登入過），所以用篩選唯一索引
CREATE UNIQUE INDEX UX_filmax_users_google_sub ON dbo.filmax_users (google_sub)
    WHERE google_sub IS NOT NULL;
GO
