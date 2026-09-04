"""設定的解析與儲存：env > DB > 預設值。

登記表在 params.py，這裡負責「現在這一項的值是多少、誰決定的」。

**API 不能只回生效值。** 只回生效值的話，使用者在後台改了一個被 .env 蓋住的
項目，看到「儲存成功」而行為完全沒變，畫面上找不到任何線索。所以每一項都回
三個候選值（env / db / default）、鎖不鎖、以及有沒有被遮蔽（shadowed）——
`shadowed` 一個欄位就解決「.env 改了但資料庫已經有值」的全部歧義。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import db, params
from .params import REGISTRY, Param

log = logging.getLogger("filmax.params")

# 啟動時間，用來判斷「需重啟」的橫幅該不該消失
STARTED_AT = time.time()

_cache: Dict[str, str] = {}
_cache_at = 0.0
_CACHE_TTL = 2.0        # 秒。hot 讀取每次都查資料庫太浪費，但也不能永遠不更新


def env_raw(key: str) -> Optional[str]:
    """環境變數 / .env 的原始值。

    支援 XXX_FILE 從檔案讀 —— docker secrets 與 systemd 的 LoadCredential
    都是這個慣例，把密碼寫進 .env 反而是比較差的做法。
    """
    fp = os.getenv(key + "_FILE")
    if fp:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            log.warning("%s_FILE 讀不到（%s），當成沒有設定", key, e)
    v = os.getenv(key)
    if v is None:
        return None
    v = v.strip()
    # 空字串當成「沒設定」。.env 範本裡大量的 `KEY=` 是佔位用的，
    # 把它當成「明確設成空值」會讓每一個佔位符都變成一把鎖。
    return v if v != "" else None


def _db_all() -> Dict[str, str]:
    global _cache, _cache_at
    now = time.time()
    if _cache_at and now - _cache_at < _CACHE_TTL:
        return _cache
    try:
        rows = db.q("SELECT k, v FROM config_param")
        _cache = {r["k"]: r["v"] for r in rows}
    except Exception as e:
        log.debug("讀不到 config_param（%s），當成空的", e)
        _cache = {}
    _cache_at = now
    return _cache


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


def db_raw(key: str) -> Optional[str]:
    v = _db_all().get(key)
    if v is None:
        return None
    p = REGISTRY.get(key)
    if p is not None and p.secret:
        return params.decrypt(v)
    return v


def resolve(key: str) -> Tuple[Any, str]:
    """回 (生效值, 來源)。來源是 env / db / default。

    順序只有一條規則，全部一致，沒有例外清單。
    """
    p = REGISTRY.get(key)
    if p is None:
        raise KeyError(f"沒有登記的設定：{key}")

    raw = env_raw(key)
    if raw is not None:
        try:
            return params.parse(p, raw), "env"
        except ValueError as e:
            # .env 無效是啟動就該擋下來的等級（見 validate_env），
            # 但 resolve 本身不能爆炸 —— 後台要能顯示「這一項設錯了」。
            log.error("環境變數 %s 無效：%s —— 這一項改用預設值", key, e)
            return p.default, "default"

    if p.editable:
        raw = db_raw(key)
        if raw is not None:
            try:
                return params.parse(p, raw), "db"
            except ValueError as e:
                # 資料庫裡的壞值退回預設就好，不讓服務起不來：
                # 一個舊版留下的壞值不該讓整台機器開不了機。
                log.warning("資料庫裡的 %s 無效：%s —— 改用預設值", key, e)
                return p.default, "default"
    return p.default, "default"


def get(key: str) -> Any:
    return resolve(key)[0]


def describe(key: str) -> Dict[str, Any]:
    """一項設定的完整狀態。這就是後台看到的東西。"""
    p = REGISTRY[key]
    value, source = resolve(key)
    env_v = env_raw(key)
    db_v = db_raw(key) if p.editable else None

    def show(raw):
        if raw is None:
            return None
        if p.secret:
            return params.MASK
        try:
            return params.parse(p, raw)
        except ValueError:
            return raw          # 無效值也要看得到，不然沒人知道為什麼沒生效

    out = {
        "key": key,
        "label": p.label or key,
        "help": p.help,
        "section": p.section,
        "type": p.type,
        "choices": p.choices,
        "min": p.minimum,
        "max": p.maximum,
        "applyMode": p.apply,
        "secret": p.secret,
        "sensitive": p.sensitive,
        "lockout": p.lockout,
        "editable": p.editable,
        "effective": {"value": params.MASK if p.secret else value, "source": source},
        "candidates": {
            "env": show(env_v),
            "db": show(db_v),
            "default": params.MASK if p.secret else p.default,
        },
        # 由環境變數決定 → 後台改不動
        "locked": env_v is not None,
        # 環境變數蓋掉了資料庫裡已經有的值 —— 這就是「存了但沒生效」的來源
        "shadowed": env_v is not None and db_v is not None,
    }
    if p.secret:
        out["isSet"] = bool(value)
        out["updatedAt"] = _updated_at(key)
    err = _error_of(p, env_v, db_v)
    if err:
        out["error"] = err
    return out


def _error_of(p: Param, env_v, db_v) -> Optional[str]:
    for raw, where in ((env_v, ".env"), (db_v, "後台")):
        if raw is None:
            continue
        try:
            params.parse(p, raw)
        except ValueError as e:
            return f"{where}的值無效：{e}"
        return None
    return None


def _updated_at(key: str) -> Optional[float]:
    try:
        r = db.q1("SELECT updated_at FROM config_param WHERE k=?", (key,))
    except Exception:
        return None
    return r["updated_at"] if r else None


def describe_all(include_hidden: bool = False) -> List[Dict[str, Any]]:
    return [describe(k) for k, p in REGISTRY.items() if p.in_ui or include_hidden]


class NotEditable(ValueError):
    pass


def set_value(key: str, raw: Any, *, actor: str = "") -> Dict[str, Any]:
    """寫進資料庫。回傳寫完之後的 describe()。

    **停用的欄位絕不能是「送出後才在後端報錯」。** 前端會把被鎖的欄位停用，
    但後端仍然要擋 —— 前端的停用只是提示，不是防線。
    """
    p = REGISTRY.get(key)
    if p is None:
        raise KeyError(f"沒有登記的設定：{key}")
    if not p.editable:
        raise NotEditable(f"{key} 只能在 .env 設定（Tier {p.tier}）")

    if p.secret and raw == params.MASK:
        # 前端把遮罩原封不動送回來 = 使用者沒有改這一格
        return describe(key)

    value = params.parse(p, raw)
    before = describe(key)

    # 只存「跟預設值不同」的項目，相等就刪列。資料庫裡留一堆等於預設值的列，
    # 之後改預設值時那些列會變成隱形的覆蓋 —— 使用者從來沒設過它，
    # 卻永遠拿不到新的預設值。
    if value == p.default and not p.secret:
        db.execute("DELETE FROM config_param WHERE k=?", (key,))
    else:
        text = params.as_text(p, value)
        if p.secret:
            text = params.encrypt(text)
        db.execute(
            "INSERT INTO config_param(k, v, updated_at, updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            (key, text, time.time(), actor[:120]))
    invalidate()
    after = describe(key)
    _audit_change(p, before, after, actor)
    return after


def set_many(values: Dict[str, Any], *, actor: str = "") -> Dict[str, Any]:
    """一次寫多項。**先全部驗證，再全部寫。**

    為什麼不讓前端自己迴圈打 set_value：那會產生「前三項成功、第四項失敗」
    的半套狀態，而使用者只會看到一個錯誤訊息，完全不知道哪些已經生效了。
    設定之間又常常互相牽連（改了 FFMPEG_PATH 才輪到 FFMPEG_HWACCEL 有意義），
    半套狀態可能比兩個舊值都更糟。

    所以這裡分兩趟：第一趟只做 parse／權限檢查，任何一項不過就整批拒絕、
    一個字都不寫；第二趟才真的寫。

    稽核仍然是每個 key 一筆（誰、何時、從什麼改成什麼），不會因為批次而合併 ——
    「這一項是什麼時候被誰改的」是事後唯一想查的東西。

    回傳 {"items": [...describe...], "errors": {key: message}}；
    errors 非空時 items 是空的（什麼都沒寫）。
    """
    errors: Dict[str, str] = {}
    plan = []
    for key, raw in values.items():
        p = REGISTRY.get(key)
        if p is None:
            errors[key] = f"沒有登記的設定：{key}"
            continue
        if not p.editable:
            errors[key] = f"只能在 .env 設定（Tier {p.tier}）"
            continue
        if p.secret and raw == params.MASK:
            continue                    # 遮罩原樣送回 = 沒有改這一格
        try:
            params.parse(p, raw)        # 只驗證，不寫
        except ValueError as e:
            errors[key] = str(e)
            continue
        plan.append((key, raw))
    if errors:
        return {"items": [], "errors": errors}

    out = []
    for key, raw in plan:
        out.append(set_value(key, raw, actor=actor))
    return {"items": out, "errors": {}}


def clear(key: str, *, actor: str = "") -> Dict[str, Any]:
    """把後台儲存的值刪掉，回到 .env 或預設值。"""
    p = REGISTRY[key]
    before = describe(key)
    db.execute("DELETE FROM config_param WHERE k=?", (key,))
    invalidate()
    after = describe(key)
    _audit_change(p, before, after, actor, cleared=True)
    return after


def _audit_change(p: Param, before, after, actor: str, cleared: bool = False) -> None:
    """設定變更稽核：誰、何時、哪一項、從什麼改成什麼。

    **秘密只記「已變更」與時間，連遮罩後的值都不記** —— 長度也是情報。
    研究過的七套設定系統沒有一套做這件事。
    """
    if p.secret:
        old = new = params.MASK
    else:
        old = "" if before is None else str(before["effective"]["value"])
        new = str(after["effective"]["value"])
        if old == new and not cleared:
            return
    try:
        db.execute(
            "INSERT INTO config_audit(at, k, old_value, new_value, actor, action)"
            " VALUES(?,?,?,?,?,?)",
            (time.time(), p.key, old[:400], new[:400], actor[:120],
             "clear" if cleared else "set"))
    except Exception as e:
        log.warning("寫不進 config_audit：%s", e)


def audit(limit: int = 100) -> List[Dict[str, Any]]:
    return [db.row_to_dict(r) for r in db.q(
        "SELECT * FROM config_audit ORDER BY at DESC LIMIT ?", (limit,))]


def validate_env() -> List[str]:
    """.env 裡有沒有無效的值。**無效就該讓啟動失敗。**

    理由：`.env` 是使用者明確寫下的指令。裡面寫錯了還默默用預設值跑起來，
    使用者會以為設定生效了。資料庫裡的壞值就不一樣 —— 那可能是舊版留下的，
    不該讓服務開不了機。兩邊嚴格度不同是刻意的。
    """
    bad = []
    for key, p in REGISTRY.items():
        raw = env_raw(key)
        if raw is None:
            continue
        try:
            params.parse(p, raw)
        except ValueError as e:
            bad.append(str(e))
    return bad


def drift() -> List[Dict[str, Any]]:
    """被環境變數蓋掉的資料庫值。

    **絕不自動刪 DB 的值。** 使用者可能只是暫時用環境變數覆蓋（測試、
    容器編排），自動刪掉他們在後台設過的東西是不可逆的資料損失。
    只回報，讓後台掛橫幅。
    """
    return [describe(k) for k, p in REGISTRY.items()
            if p.editable and env_raw(k) is not None and db_raw(k) is not None]


def needs_restart() -> List[str]:
    """哪些 restart 類的設定在服務啟動之後才被改過。

    橫幅要能自己消失：重開之後最後變更時間就會早於啟動時間。
    """
    out = []
    for key, p in REGISTRY.items():
        if p.apply != params.APPLY_RESTART or not p.editable:
            continue
        at = _updated_at(key)
        if at and at > STARTED_AT:
            out.append(key)
    return out


# --------------------------------------------------------------------------
# CLI：後台進不去時唯一的救援路徑
# --------------------------------------------------------------------------
# 設定一旦能在後台改，就一定有「改到自己進不了後台」的可能 ——
# 綁定位址、對外網址、驗證方式都是。沒有 CLI 的話那是死路。
#
#   python -m app.paramstore list [區段]
#   python -m app.paramstore get KEY
#   python -m app.paramstore set KEY 值
#   python -m app.paramstore unset KEY
#   python -m app.paramstore diff
def _cli(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m app.paramstore",
        description="系統參數。後台進不去的時候用這個。")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("list", help="列出所有設定")
    q.add_argument("section", nargs="?", help="只看某個區段")
    q.add_argument("--all", action="store_true", help="連 .env 專用的也列出來")
    g = sub.add_parser("get", help="看一項的完整狀態")
    g.add_argument("key")
    st = sub.add_parser("set", help="把值存進資料庫")
    st.add_argument("key")
    st.add_argument("value")
    un = sub.add_parser("unset", help="刪掉後台儲存的值，回到 .env 或預設")
    un.add_argument("key")
    sub.add_parser("diff", help="只列出跟預設值不同的項目")
    a = ap.parse_args(argv)

    from . import db as _db
    _db.init_db()

    def line(d):
        eff = d["effective"]
        flag = ""
        if d["locked"]:
            flag = " [.env 鎖定]"
        if d["shadowed"]:
            flag += " [後台的值被蓋掉]"
        if d.get("error"):
            flag += f" [{d['error']}]"
        return f"{d['key']:32} {str(eff['value']):>24}  ({eff['source']}){flag}"

    if a.cmd == "list":
        for d in describe_all(include_hidden=a.all):
            if a.section and d["section"] != a.section:
                continue
            print(line(d))
        return 0
    if a.cmd == "get":
        if a.key not in REGISTRY:
            print(f"沒有這個設定：{a.key}")
            return 2
        d = describe(a.key)
        print(line(d))
        print(f"  來源候選：env={d['candidates']['env']!r} "
              f"db={d['candidates']['db']!r} 預設={d['candidates']['default']!r}")
        print(f"  分層 Tier {REGISTRY[a.key].tier}／生效方式 {d['applyMode']}"
              f"{'／秘密' if d['secret'] else ''}")
        return 0
    if a.cmd == "set":
        try:
            d = set_value(a.key, a.value, actor="cli")
        except (KeyError, NotEditable, ValueError) as e:
            print(f"改不了：{e}")
            return 2
        print(line(d))
        if d["locked"]:
            print("  ⚠ 存進資料庫了，但這一項現在由環境變數決定，所以**不會生效**。"
                  "要它生效請先把 .env 裡那一行拿掉。")
        return 0
    if a.cmd == "unset":
        if a.key not in REGISTRY:
            print(f"沒有這個設定：{a.key}")
            return 2
        print(line(clear(a.key, actor="cli")))
        return 0
    if a.cmd == "diff":
        n = 0
        for d in describe_all(include_hidden=True):
            if d["effective"]["source"] == "default":
                continue
            print(line(d))
            n += 1
        if not n:
            print("全部都是預設值。")
        return 0
    return 1


if __name__ == "__main__":       # pragma: no cover
    import sys as _sys
    raise SystemExit(_cli(_sys.argv[1:]))
