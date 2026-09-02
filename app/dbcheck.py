"""MSSQL 連線與權限的逐項檢查。

    python -m app.dbcheck                 只檢查
    python -m app.dbcheck --migrate       檢查完，把本機 SQLite 既有的帳號搬進 MSSQL

為什麼要有這支：開發環境連不到你的資料庫，所以定序、權限、DATETIME2、
CHECK 限制式這些「只有真的 SQL Server 才驗得出來」的東西，一律在這裡驗。
它會逐關告訴你卡在哪，而不是丟一個 ODBC 錯誤碼讓你自己查。

每一關都獨立，前面失敗會直接停 —— 連都連不上就沒必要再測權限。
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List

from . import mssql
from .config import settings

# 測試用的資料一律用這個 email，跑完會刪掉。
# 用一個絕對不會跟真人撞到的網域，萬一沒清乾淨也一眼看得出來是測試殘留。
PROBE = "__filmax_dbcheck__@invalid.test"

OK, WARN, BAD = "  [v]", "  [!]", "  [x]"
_fail = 0


def say(mark: str, msg: str) -> None:
    global _fail
    if mark is BAD:
        _fail += 1
    print(f"{mark} {msg}")


def head(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def main(argv: List[str]) -> int:
    migrate = "--migrate" in argv
    print("=" * 66)
    print("  FilmaxWeb — MSSQL 連線檢查")
    print("=" * 66)

    head(1, "設定")
    if not settings.mssql_configured:
        say(BAD, "沒有設定 MSSQL。請在 .env 填 MSSQL_HOST / MSSQL_DATABASE / MSSQL_USER"
                 " / MSSQL_PASSWORD 後再試。")
        say(WARN, "目前使用者資料存在本機 SQLite（data/library.db 的 app_user）。")
        return 1
    say(OK, f"連線目標：{mssql.safe_target()}")
    say(OK, f"驅動：{settings.mssql_driver}"
            f"（加密={'開' if settings.mssql_encrypt else '關'}，"
            f"信任伺服器憑證={'是' if settings.mssql_trust_cert else '否'}）")

    head(2, "ODBC 驅動")
    try:
        import pyodbc
    except Exception:
        say(BAD, "沒有安裝 pyodbc。先跑：pip install pyodbc")
        return 1
    say(OK, f"pyodbc {pyodbc.version}")
    drivers = pyodbc.drivers()
    if not drivers:
        say(BAD, "系統上找不到任何 ODBC 驅動。"
                 "請安裝 Microsoft ODBC Driver 18 for SQL Server。")
        return 1
    print(f"      已安裝：{', '.join(drivers)}")
    if settings.mssql_driver not in drivers:
        say(BAD, f"設定的 MSSQL_DRIVER=「{settings.mssql_driver}」不在上面這份清單裡。"
                 "名稱要一字不差（含空格與大小寫）。")
        return 1
    say(OK, "驅動名稱對得上")

    head(3, "連線")
    try:
        ver = mssql.scalar("SELECT @@VERSION")
        say(OK, (ver or "").split("\n")[0].strip()[:90])
    except Exception as e:
        say(BAD, f"連不上：{mssql.scrub(e)}")
        print("""
      常見原因：
        * 憑證錯誤（Driver 18 預設要求加密）→ 內網自簽憑證請設 MSSQL_TRUST_CERT=true
        * 連線逾時 → 檢查防火牆、SQL Server 是否開啟 TCP/IP 與 1433 埠
        * 登入失敗 → 帳號密碼，以及該登入是否對應到這個資料庫的使用者""")
        return 1

    head(4, "資料表與欄位")
    st = mssql.ping()
    if st["missing"]:
        say(BAD, "缺少：" + "、".join(st["missing"]))
        say(WARN, "跑 db\\migrate.bat 套用 Flyway 遷移（V1～V5）後再試一次。")
        return 1
    say(OK, "dbo.filmax_users / dbo.filmax_audit 都在，欄位齊全")

    head(5, "email 欄位的定序")
    try:
        coll = mssql.scalar(
            "SELECT collation_name FROM sys.columns "
            "WHERE object_id = OBJECT_ID('dbo.filmax_users') AND name = 'email'")
        cs = coll and "_CS_" in coll
        say(WARN if cs else OK, f"{coll}"
            + ("　← 區分大小寫。程式已經一律用 LOWER() 比對，所以沒問題，"
               "但既有資料若同時存在 A@x.com 與 a@x.com 會是兩個帳號。" if cs else ""))
    except Exception as e:
        say(WARN, f"讀不到定序（不影響運作）：{mssql.scrub(e)}")

    head(6, "讀寫權限（會寫一筆測試資料再刪掉）")
    ok_rw = _probe_rw()
    if not ok_rw:
        return 1

    head(7, "稽核表與 action 限制式")
    _probe_audit()

    head(8, "清理程序的執行權限")
    try:
        mssql.execute("EXEC dbo.filmax_purge_audit @days = 36500")
        say(OK, "dbo.filmax_purge_audit 可以執行")
    except Exception as e:
        msg = mssql.scrub(e)
        if "permission" in msg.lower() or "EXECUTE" in msg:
            say(WARN, "沒有 EXECUTE 權限。跑 V5 遷移會補上；不補也只是稽核紀錄不會自動清。")
        else:
            say(WARN, f"呼叫失敗（不影響登入）：{msg[:120]}")

    head(9, "目前的帳號")
    try:
        rows = mssql.q("SELECT TOP (20) id, email, role, status, login_count "
                       "FROM dbo.filmax_users ORDER BY id")
        if not rows:
            say(WARN, "一筆都沒有。用 Google 登入一次就會自動建立。")
        for r in rows:
            print(f"      #{r['id']}  {r['email']}  {r['role']}/{r['status']}  "
                  f"登入 {r['login_count']} 次")
    except Exception as e:
        say(BAD, mssql.scrub(e))

    if migrate:
        head(10, "把本機 SQLite 的帳號搬進 MSSQL")
        _migrate()

    print("\n" + "=" * 66)
    if _fail:
        print(f"  有 {_fail} 項沒過，照上面的訊息處理後再跑一次。")
    else:
        print("  全部通過。啟動服務後，使用者資料就會存進 MSSQL。")
        if not migrate:
            print("  本機 SQLite 已經有帳號的話，跑 python -m app.dbcheck --migrate 搬過去。")
    print("=" * 66)
    return 1 if _fail else 0


def _probe_rw() -> bool:
    """真的寫一筆、改一筆、刪一筆。權限問題只有這樣才驗得出來。"""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    uid = None
    try:
        mssql.execute("DELETE FROM dbo.filmax_users WHERE email = ?", (PROBE,))
        uid = mssql.scalar(
            """INSERT INTO dbo.filmax_users
                   (email, display_name, role, status, created_at, sess_ver, login_count)
               OUTPUT INSERTED.id VALUES (?,?,?,?,?,0,0)""",
            (PROBE, "dbcheck", "viewer", "pending", now))
        say(OK, f"INSERT 成功（id={uid}，OUTPUT INSERTED.id 可用）")
    except Exception as e:
        say(BAD, f"INSERT 失敗：{mssql.scrub(e)}")
        say(WARN, "帳號需要 db_datawriter。見 db/00_create_database.sql。")
        return False
    try:
        mssql.execute("UPDATE dbo.filmax_users SET status='approved', "
                      "sess_ver = sess_ver + 1, last_login_at = ? WHERE id = ?", (now, uid))
        row = mssql.q1("SELECT status, sess_ver, last_login_at FROM dbo.filmax_users "
                       "WHERE id = ?", (uid,))
        back = mssql.to_epoch(row["last_login_at"])
        drift = abs(back - now.replace(tzinfo=timezone.utc).timestamp())
        say(OK, f"UPDATE + 讀回成功（status={row['status']}, sess_ver={row['sess_ver']}）")
        if drift < 120:
            say(OK, f"時間欄位來回一致，誤差 {drift:.0f} 秒")
        else:
            say(BAD, f"時間欄位誤差 {drift/3600:.1f} 小時 —— 很可能是伺服器時間或時區設定有問題")
    except Exception as e:
        say(BAD, f"UPDATE / 讀回失敗：{mssql.scrub(e)}")
    try:
        n = mssql.execute("DELETE FROM dbo.filmax_users WHERE email = ?", (PROBE,))
        say(OK, f"DELETE 成功，測試資料已清除（{n} 筆）")
    except Exception as e:
        say(BAD, f"DELETE 失敗，請手動刪除 email = {PROBE} 這筆：{mssql.scrub(e)}")
    return True


def _probe_audit() -> None:
    from . import audit
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    for action in ("login", "role_change"):
        try:
            mssql.execute(
                "INSERT INTO dbo.filmax_audit (at, email, action, detail) VALUES (?,?,?,?)",
                (now, PROBE, action, "dbcheck"))
            say(OK, f"可以寫入 action='{action}'")
        except Exception as e:
            msg = mssql.scrub(e)
            if action == "role_change" and "CK_filmax_audit_action" in msg:
                say(BAD, "action='role_change' 被限制式擋下 —— V5 遷移還沒套用。"
                         "跑 db\\migrate.bat。")
            else:
                say(BAD, f"寫入 filmax_audit 失敗：{msg[:140]}")
    try:
        mssql.execute("DELETE FROM dbo.filmax_audit WHERE email = ?", (PROBE,))
    except Exception as e:
        say(WARN, f"測試稽核資料沒刪掉，請手動清 email = {PROBE}：{mssql.scrub(e)}")


def _migrate() -> None:
    """把 SQLite 的 app_user 搬進 MSSQL。已經存在的 email 會跳過，不覆蓋。"""
    from . import db, users_mssql, users_sqlite
    try:
        db.init_db()
        users_sqlite.init()
        rows = users_sqlite.listing(None, 1000)
    except Exception as e:
        say(WARN, f"讀不到本機 SQLite：{e}")
        return
    if not rows:
        say(OK, "本機沒有帳號需要搬移")
        return
    moved = skipped = 0
    for r in rows:
        if users_mssql.by_email(r["email"]):
            skipped += 1
            print(f"      跳過（MSSQL 已存在）：{r['email']}")
            continue
        try:
            uid = users_mssql.create(
                r["email"], r.get("google_sub") or "", r.get("display_name") or r["email"],
                r.get("picture_url"), r.get("role") or "viewer",
                r.get("status") or "pending", r.get("registered_ip") or "",
                {"country": r.get("registered_country"), "city": r.get("registered_city")},
                r.get("approved_by"))
            # 登入紀錄也一起帶過去，不然後台看起來像是每個人都沒登入過
            if r.get("last_login_at"):
                users_mssql.record_login(
                    uid, r.get("last_login_ip") or "",
                    {"country": r.get("last_login_country"),
                     "city": r.get("last_login_city"),
                     "user_agent": r.get("last_login_ua")})
            moved += 1
            print(f"      搬移：{r['email']}（{r.get('role')}/{r.get('status')}）→ id={uid}")
        except Exception as e:
            say(BAD, f"{r['email']} 搬移失敗：{mssql.scrub(e)}")
    say(OK, f"搬移 {moved} 筆，跳過 {skipped} 筆")
    if moved:
        say(WARN, "SQLite 的舊資料刻意保留著沒刪 —— 確認 MSSQL 這邊都對了再自行清除。")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        mssql.close_all()
