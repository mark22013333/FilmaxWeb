"""把 pyodbc 換掉，用 SQLite 當後面那台 SQL Server。

**這不是在測 SQL Server**，沙箱連不到任何一台。它測的是 users_mssql.py 的
邏輯與參數順序：SELECT/INSERT/UPDATE 的欄位有沒有對齊、參數有沒有排錯、
時間轉換有沒有搞反。這幾種錯佔了實務上大半，而且不需要真的資料庫就抓得到。

抓不到的是方言層面的東西：定序、DATETIME2 精度、OUTPUT 在有觸發程序時的行為、
權限。那些只能在真的資料庫上驗，所以另外有 app/dbcheck.py 給使用者在自己機器上跑。

翻譯只處理我們真的會用到的幾個 T-SQL 構造，不是通用轉譯器：
  SELECT TOP (?) ...        → ... LIMIT ?      （參數要從最前面搬到最後面）
  INSERT ... OUTPUT INSERTED.id → 插入後回 lastrowid
  sys.tables / sys.columns / @@VERSION → 直接給答案
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

DDL = """
CREATE TABLE filmax_users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE,
    google_sub         TEXT UNIQUE,
    display_name       TEXT,
    picture_url        TEXT,
    role               TEXT NOT NULL DEFAULT 'viewer',
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TS,
    registered_ip      TEXT,
    registered_country TEXT,
    registered_city    TEXT,
    approved_at        TS,
    approved_by        TEXT,
    last_login_at      TS,
    last_login_ip      TEXT,
    last_login_country TEXT,
    last_login_city    TEXT,
    last_login_ua      TEXT,
    login_count        INTEGER NOT NULL DEFAULT 0,
    sess_ver           INTEGER NOT NULL DEFAULT 0,
    note               TEXT,
    CHECK (role IN ('owner','viewer')),
    CHECK (status IN ('pending','approved','rejected','disabled'))
);
CREATE TABLE filmax_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TS, email TEXT, action TEXT, detail TEXT,
    ip TEXT, ip_source TEXT, country TEXT, region TEXT, city TEXT, timezone TEXT,
    latitude REAL, longitude REAL, user_agent TEXT
);
"""

# DATETIME2 要進出成 datetime 物件，才驗得到 to_epoch/from_epoch 有沒有搞反時區
sqlite3.register_adapter(datetime, lambda d: d.isoformat(" "))
sqlite3.register_converter("TS", lambda b: datetime.fromisoformat(b.decode()) if b else None)

_TABLES = {"filmax_users", "filmax_audit"}
# 只取 filmax_users 那一段的欄位名，給模擬 sys.columns 用
_USER_DDL = DDL.split("CREATE TABLE filmax_users")[1].split("CREATE TABLE")[0]
_COLUMNS = {"dbo.filmax_users":
            [c for c in re.findall(r"^\s{4}(\w+)\s", _USER_DDL, re.M) if c != "CHECK"]}


class Error(Exception):
    pass


class OperationalError(Error):
    pass


class DataError(Error):
    pass


class IntegrityError(Error):
    pass


def _translate(sql: str):
    """回傳 (sqlite_sql, 參數重排函式, 是否要回 lastrowid)。"""
    sql = sql.replace("dbo.filmax_users", "filmax_users").replace("dbo.filmax_audit", "filmax_audit")
    reorder = None
    m = re.search(r"SELECT\s+TOP\s*\(\s*\?\s*\)", sql, re.I)
    if m:
        sql = sql[:m.start()] + "SELECT" + sql[m.end():] + " LIMIT ?"
        # T-SQL 的 TOP 參數在最前面，SQLite 的 LIMIT 在最後面
        reorder = lambda p: tuple(p[1:]) + (p[0],)
    else:
        # TOP (20) 這種寫死數字的形式
        m = re.search(r"SELECT\s+TOP\s*\(\s*(\d+)\s*\)", sql, re.I)
        if m:
            sql = sql[:m.start()] + "SELECT" + sql[m.end():] + f" LIMIT {m.group(1)}"
    want_id = "OUTPUT INSERTED.id" in sql
    sql = sql.replace("OUTPUT INSERTED.id", "")
    return sql, reorder, want_id


class FakeCursor:
    def __init__(self, conn):
        self._c = conn
        self._cur = conn._sq.cursor()
        self.description = None
        self.rowcount = -1
        self._rows = None
        # pyodbc 的 cursor 有這兩個東西，而且 app.mssql 會用到。
        # 頂替品沒有它們的話，測到的是「有沒有寫這行程式」而不是行為。
        self.fast_executemany = False
        self._sizes = None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.upper().startswith("SET NOCOUNT"):
            self.description = None
            return self
        if "@@VERSION" in s.upper():
            self._rows = [("Microsoft SQL Server 2022 (FakeODBC for tests)",)]
            self.description = [("", None)]
            return self
        if s.upper().startswith("EXEC"):
            # 預存程序沒辦法在 SQLite 上跑。這裡回一個「成功但沒做事」，
            # 讓呼叫端的流程走得完；程序本身要在真的資料庫上驗。
            self.description = None
            self._rows = []
            self.rowcount = 0
            return self
        if "collation_name" in s:
            self._rows = [("Chinese_Taiwan_Stroke_CI_AS",)]
            self.description = [("collation_name", None)]
            return self
        if "sys.tables" in s:
            self._rows = [(t,) for t in sorted(_TABLES)]
            self.description = [("name", None)]
            return self
        if "sys.columns" in s:
            self._rows = [(c,) for c in _COLUMNS["dbo.filmax_users"]]
            self.description = [("name", None)]
            return self

        sql2, reorder, want_id = _translate(sql)
        p = tuple(params)
        if reorder:
            p = reorder(p)
        try:
            self._cur.execute(sql2, p)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from None
        except sqlite3.Error as e:
            raise Error(str(e)) from None
        self.rowcount = self._cur.rowcount
        if want_id:
            self._rows = [(self._cur.lastrowid,)]
            self.description = [("id", None)]
        elif self._cur.description:
            self.description = [(d[0], None) for d in self._cur.description]
            self._rows = self._cur.fetchall()
        else:
            self.description = None
            self._rows = []
        if self._c.autocommit:
            self._c._sq.commit()
        return self

    def setinputsizes(self, sizes):
        self._sizes = list(sizes)

    def executemany(self, sql, rows):
        rows = list(rows)
        # 真的 pyodbc 開了 fast_executemany 又沒 setinputsizes 的話，
        # 會拿第一列去猜整批的長度，比較長的字串被默默截斷。
        # 頂替品把那個「默默」變成「大聲」，測試才抓得到。
        if self.fast_executemany and self._sizes is None:
            raise Error("fast_executemany 沒有先 setinputsizes()，"
                        "pyodbc 會拿第一列猜欄寬，後面比較長的字串會被截斷")
        if self._sizes is not None:
            for r in rows:
                for val, size in zip(r, self._sizes):
                    width = size[1] if isinstance(size, (tuple, list)) and len(size) > 1 else None
                    if isinstance(val, str) and width and len(val) > width:
                        raise DataError(f"字串長度 {len(val)} 超過欄寬 {width}")
        sql2, reorder, _ = _translate(sql)
        if reorder:
            rows = [reorder(tuple(r)) for r in rows]
        try:
            self._cur.executemany(sql2, [tuple(r) for r in rows])
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from None
        except sqlite3.Error as e:
            raise Error(str(e)) from None
        self.rowcount = len(rows)
        self.description = None
        self._rows = []
        if self._c.autocommit:
            self._c._sq.commit()
        return self

    def fetchall(self):
        return list(self._rows or [])

    def fetchone(self):
        rows = self._rows or []
        return rows[0] if rows else None

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class FakeConn:
    def __init__(self, path, autocommit=True):
        self._sq = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES,
                                   check_same_thread=False)
        self.autocommit = autocommit

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self._sq.commit()

    def rollback(self):
        self._sq.rollback()

    def close(self):
        self._sq.close()


DB_FILE = {"path": ":memory:"}


def connect(conn_str, autocommit=True, timeout=None):
    if "SERVER=unreachable" in conn_str:
        raise OperationalError("08001 unable to connect (fake)")
    return FakeConn(DB_FILE["path"], autocommit=autocommit)


def drivers():
    return ["ODBC Driver 18 for SQL Server"]


version = "fake-5.0"


def install(db_path: str):
    """把 app.mssql 的 pyodbc 換成這個模組，並把兩張表建好。"""
    import sys
    from app import mssql
    DB_FILE["path"] = db_path
    c = sqlite3.connect(db_path)
    c.executescript(DDL)
    c.commit()
    c.close()
    mssql.pyodbc = sys.modules[__name__]
    mssql.close_all()
    return mssql
