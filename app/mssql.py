"""MSSQL 連線層（pyodbc）。

只有這個檔案知道怎麼連資料庫，上層一律透過 q / q1 / execute / executemany / tx。

幾件刻意的設計：

* **pyodbc 是選用相依。** 沒裝、或沒設定連線資訊時，import 這個模組不會爆炸，
  `available()` 回 False，服務照樣用 SQLite 跑起來。把它列成硬相依的話，
  只是想在區網看影片的人也得先裝 ODBC driver。

* **連線字串永遠不進日誌。** 裡面有密碼。所有錯誤訊息都經過 scrub()。

* **每個執行緒一條連線**，跟 db.py 的做法一致。連線斷了（伺服器重開、網路中斷、
  閒置被踢）第一次用到時會自動重連一次再放棄 —— 不重試的話，資料庫重開一次
  就要跟著重啟服務。

* **時間一律用 epoch 秒在程式裡流動。** MSSQL 存 DATETIME2 (UTC)，SQLite 存 float，
  兩邊的上層程式碼看到的都是 float，前端也是。轉換只在這裡跟 users_mssql.py 發生。

* **平常 autocommit，要原子性就用 tx()。** 帳號那些單句寫入 autocommit 最單純；
  批次寫入不行 —— 一批 500 列寫到第 300 列斷線，autocommit 下就是「前 300 列
  進去了，後 200 列沒有」，而且沒有任何東西記得停在哪裡。

* **executemany 一定要先 setinputsizes()。** 開了 fast_executemany 而不指定欄寬，
  pyodbc 會拿**第一列**去猜整批的型別與長度。後面比較長的字串會被默默截斷，
  更糟的是前一列的字串殘留可能覆蓋下一列 —— 兩種都不會有錯誤訊息。
  這是正確性問題，不是效能調校。

* **executemany 寫完要對帳。** fast_executemany 遇到違反約束的列**不會拋例外**：
  有效的列照樣寫進去，失敗的列靜默消失。所以寫完一定要數一次。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import settings

log = logging.getLogger("filmax.mssql")

try:
    import pyodbc                      # type: ignore
except Exception:                      # 沒裝就當成沒有這個功能
    pyodbc = None                      # type: ignore

_local = threading.local()

# 連線字串裡的密碼欄位。ODBC 的分隔符是分號，所以取到分號為止。
_PWD = re.compile(r"(PWD|PASSWORD)\s*=\s*[^;]*", re.I)


def scrub(text: Any) -> str:
    return _PWD.sub(r"\1=***", str(text))


def available() -> bool:
    """有沒有裝 pyodbc、而且設定齊全。"""
    return bool(pyodbc and settings.mssql_configured)


def why_unavailable() -> str:
    if not settings.mssql_configured:
        return "沒有設定 MSSQL_HOST / MSSQL_DATABASE / MSSQL_USER"
    if not pyodbc:
        return "沒有安裝 pyodbc（pip install pyodbc）"
    return ""


def conn_str() -> str:
    if settings.mssql_odbc_dsn:
        return settings.mssql_odbc_dsn
    parts = [
        f"DRIVER={{{settings.mssql_driver}}}",
        f"SERVER={settings.mssql_host},{settings.mssql_port}",
        f"DATABASE={settings.mssql_database}",
        f"UID={settings.mssql_user}",
        f"PWD={settings.mssql_password}",
        f"Encrypt={'yes' if settings.mssql_encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if settings.mssql_trust_cert else 'no'}",
        f"Connection Timeout={settings.mssql_timeout}",
    ]
    return ";".join(parts) + ";"


def safe_target() -> str:
    """可以放進日誌與介面的連線描述，不含密碼。"""
    if settings.mssql_odbc_dsn:
        return "（自訂 ODBC 連線字串）"
    return (f"{settings.mssql_host},{settings.mssql_port}/{settings.mssql_database}"
            f" as {settings.mssql_user}")


class MssqlUnavailable(RuntimeError):
    """連不上或沒設定。上層要能分辨「資料庫掛了」跟「這筆資料不存在」。"""


def _new_conn():
    if not available():
        raise MssqlUnavailable(why_unavailable())
    try:
        c = pyodbc.connect(conn_str(), autocommit=True, timeout=settings.mssql_timeout)
        # autocommit 是預設值不是唯一值；tx() 會暫時關掉它。
    except Exception as e:
        raise MssqlUnavailable(f"連不上 {safe_target()}：{scrub(e)}") from None
    # 這幾個設定要在連線建立後就下，之後每一句都適用
    cur = c.cursor()
    cur.execute("SET NOCOUNT ON")
    cur.close()
    return c


def get_conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _new_conn()
        _local.conn = c
    return c


def _drop_conn() -> None:
    c = getattr(_local, "conn", None)
    _local.conn = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _cursor(sql: str, params: Iterable = ()):
    """執行一句，斷線時自動重連一次。

    只重試一次，而且只在「看起來是連線問題」時重試。無條件重試會把
    唯一鍵衝突之類的真錯誤也重跑一遍，那是在製造難查的重複資料。
    """
    for attempt in (1, 2):
        try:
            cur = get_conn().cursor()
            cur.execute(sql, tuple(params))
            return cur
        except MssqlUnavailable:
            raise
        except Exception as e:
            broken = pyodbc and isinstance(e, (pyodbc.OperationalError, pyodbc.Error)) and \
                any(k in str(e) for k in
                    ("08S01", "08001", "HYT00", "HY000", "Communication link failure",
                     "Connection is busy", "not connected", "Login timeout"))
            _drop_conn()
            if attempt == 1 and broken:
                log.warning("MSSQL 連線中斷，重連一次：%s", scrub(e))
                continue
            raise


def _rows(cur) -> List[Dict[str, Any]]:
    cols = [d[0].lower() for d in cur.description or []]
    out = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return out


def q(sql: str, params: Iterable = ()) -> List[Dict[str, Any]]:
    return _rows(_cursor(sql, params))


def q1(sql: str, params: Iterable = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable = ()) -> int:
    cur = _cursor(sql, params)
    n = cur.rowcount
    cur.close()
    return n


@contextmanager
def tx():
    """把一批寫入包成一個交易。離開時 commit，出例外時 rollback。

    平常連線是 autocommit，所以這裡要先關掉、結束後再打開。
    連線本身出問題的話一律丟掉重建 —— 一條狀態不明的連線繼續留在
    執行緒本地變數裡，下一個使用者會拿到一個 autocommit 可能已經被關掉、
    交易可能還開著的連線，那種問題查起來完全沒有頭緒。
    """
    c = get_conn()
    c.autocommit = False
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            _drop_conn()
            raise
        raise
    finally:
        try:
            c.autocommit = True
        except Exception:
            _drop_conn()


def executemany(sql: str, rows: Sequence[Sequence[Any]], *,
                sizes: Optional[Sequence[Any]] = None,
                verify: Optional[str] = None,
                verify_params: Iterable = ()) -> int:
    """批次寫入。整批在一個交易裡，全部成功才算數。

    sizes  —— 傳給 setinputsizes() 的欄位定義，開 fast_executemany 時**必填**。
              不給的話 pyodbc 會拿第一列去猜整批的型別與長度，比較長的字串
              會被默默截斷。這不是優化，是正確性。
    verify —— 一句 SELECT COUNT(*)，寫完拿來對帳。fast_executemany 遇到違反
              約束的列不會拋例外，有效列照樣寫入、失敗列靜默消失；不數一次
              的話，資料少了也沒有人會知道。對不上就整批退回。
    """
    rows = list(rows)
    if not rows:
        return 0
    with tx() as c:
        cur = c.cursor()
        try:
            if sizes is not None:
                cur.fast_executemany = True
                cur.setinputsizes(list(sizes))
            cur.executemany(sql, rows)
            if verify:
                got = cur.execute(verify, tuple(verify_params)).fetchone()[0]
                if got != len(rows):
                    raise RuntimeError(
                        f"批次寫入對帳不符：送出 {len(rows)} 列，資料庫只有 {got} 列。"
                        "整批退回（fast_executemany 不會為被約束擋掉的列拋例外）。")
        finally:
            cur.close()
    return len(rows)


def scalar(sql: str, params: Iterable = ()) -> Any:
    cur = _cursor(sql, params)
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


# ---------------------------------------------------------------- 時間轉換
def to_epoch(v: Any) -> Optional[float]:
    """DATETIME2 → epoch 秒。

    SQL Server 存的是 SYSUTCDATETIME()，pyodbc 交回來的是**沒有時區**的 datetime。
    直接 .timestamp() 會被當成本機時間，在 UTC+8 會整整差 8 小時 ——
    畫面上就是「上次登入」永遠比實際晚 8 小時。所以先補上 UTC 再轉。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.timestamp()
    return None


def from_epoch(v: Any) -> Optional[datetime]:
    """epoch 秒 → 無時區的 UTC datetime，給 DATETIME2 欄位用。"""
    if v is None:
        return None
    return datetime.fromtimestamp(float(v), tz=timezone.utc).replace(tzinfo=None)


def now_dt() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 健康檢查
def ping() -> Dict[str, Any]:
    """回傳連線狀態與缺少的物件，給啟動檢查與診斷 API 用。"""
    out: Dict[str, Any] = {"configured": settings.mssql_configured,
                           "driver_installed": bool(pyodbc),
                           "target": safe_target() if settings.mssql_configured else "",
                           "ok": False, "version": "", "missing": [], "error": ""}
    if not available():
        out["error"] = why_unavailable()
        return out
    try:
        out["version"] = (scalar("SELECT @@VERSION") or "").split("\n")[0][:120]
        want = ["filmax_users", "filmax_audit"]
        have = {r["name"].lower() for r in
                q("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('dbo')")}
        out["missing"] = [t for t in want if t not in have]
        # V5 補的兩個欄位。少了它們登入會直接失敗，要早點講而不是等出錯
        if "filmax_users" not in out["missing"]:
            cols = {r["name"].lower() for r in q(
                "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('dbo.filmax_users')")}
            for c in ("sess_ver", "login_count"):
                if c not in cols:
                    out["missing"].append(f"filmax_users.{c}")
        out["ok"] = not out["missing"]
    except Exception as e:
        out["error"] = scrub(e)
    return out


def close_all() -> None:
    _drop_conn()
