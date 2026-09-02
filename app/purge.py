"""刪除媒體庫資料的唯一進入點，以及孤兒清掃。

**為什麼要有這個檔案**

SQLite 這邊其實有外鍵也有 ON DELETE CASCADE，看起來不需要它。但外鍵管不到
兩件事，而那兩件正是實際會出問題的：

1. **磁碟上的檔案。** 海報、底圖、縮圖都是 data/images/ 底下的真檔案。
   資料列被 CASCADE 掉之後，檔案就永遠留在那裡，沒有任何東西記得它存在。
2. **換一個後端就不一樣了。** 規格 A-5 的 MSSQL 媒體庫刻意不做外鍵。
   如果刪除邏輯藏在 CASCADE 裡，換後端等於整套刪除行為靜默改變。

所以這裡不靠 CASCADE，自己把連帶關係展開。同一份程式碼在哪個後端上、
外鍵有沒有打開，行為都一樣。

**縮圖是內容雜湊命名的，所以刪檔案前一定要數參照。**
兩張一模一樣的照片會共用同一個 `p480_<hash>.jpg`。刪掉其中一張就順手刪檔，
另一張的縮圖就變成破圖 —— 這是 G-5 那個修法帶進來的新風險，不特別處理不會自己好。
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Sequence

from . import db
from .config import IMAGE_DIR

log = logging.getLogger("filmax.purge")

# 孤兒的七種樣子。前四種是「指到不存在的東西」，後三種是「沒有東西指到它」。
#
# 後三種要寬限期：索引流程是「先建條目 → 再寫檔案列」，兩步之間那個條目
# 就是一個完全合法的孤兒。掃描中途去清它，等於把使用者正在建立的東西刪掉。
# 注意：前四類在 SQLite 上其實生不出來 —— 外鍵是開著的，寫進去就會被擋。
# 它們是為了另外兩種情況存在的：A-5 的 MSSQL 媒體庫刻意不做外鍵，
# 以及還原／匯入來的資料庫（外鍵是連線層級的開關，不是資料本身的性質，
# 用別的工具灌進來的資料完全可能是壞的）。清掃要能認得那個狀態。
ORPHAN_KINDS = (
    "file_item",      # 1 media_file.item_id 指到不存在的條目
    "file_episode",   # 2 media_file.episode_id 指到不存在的集數
    "episode_item",   # 3 episode.item_id 指到不存在的條目
    "play_file",      # 4 play_state.file_id 指到不存在的檔案
    "item_nofile",    # 5 條目底下沒有任何檔案       ← 要寬限
    "episode_nofile", # 6 集數底下沒有任何檔案       ← 要寬限
    "image_unused",   # 7 磁碟上沒有任何資料列參照的圖片 ← 要寬限
)
NEEDS_GRACE = ("item_nofile", "episode_nofile", "image_unused")
GRACE_SECONDS = 3600


def _ids(v) -> List[int]:
    if not v:
        return []
    return [int(x) for x in v]


def _in(col: str, ids: Sequence[int]) -> str:
    return f"{col} IN ({','.join('?' * len(ids))})"


# ---------------------------------------------------------------- 磁碟檔案
def _referenced_images() -> set:
    """目前還有資料列指到的圖片檔名。"""
    names = set()
    for sql in ("SELECT poster AS n FROM media_item WHERE poster IS NOT NULL",
                "SELECT backdrop AS n FROM media_item WHERE backdrop IS NOT NULL",
                "SELECT still AS n FROM episode WHERE still IS NOT NULL",
                "SELECT thumb AS n FROM media_file WHERE thumb IS NOT NULL",
                "SELECT thumb AS n FROM photo WHERE thumb IS NOT NULL"):
        names.update(r["n"] for r in db.q(sql) if r["n"])
    return names


def _unlink(names: Iterable[str]) -> int:
    """刪掉指定的圖片檔，但只刪「已經沒有任何資料列指到」的。

    呼叫這個函式的時候資料列已經刪掉了，所以現在數出來的參照就是真的參照。
    """
    still = _referenced_images()
    n = 0
    for name in {x for x in names if x}:
        if name in still:
            continue            # 別的資料列還在用同一個檔（內容雜湊會共用）
        f = IMAGE_DIR / name
        try:
            if f.is_file():
                f.unlink()
                n += 1
        except OSError as e:
            log.warning("刪不掉圖片 %s：%s", name, e)
    return n


# ---------------------------------------------------------------- purge
def purge(*, item_ids=None, file_ids=None, photo_ids=None, episode_ids=None,
          reason: str, dry_run: bool = False) -> Dict[str, int]:
    """刪除媒體庫資料的唯一進入點。連帶關係與磁碟檔都在這裡展開。

    reason 是必填的，而且會進紀錄。沒有理由的刪除到頭來沒有人查得出來
    是誰、在哪一段流程刪掉的 —— 而那正是使用者說「我的東西不見了」的時候
    唯一想知道的事。
    """
    if not reason:
        raise ValueError("purge() 一定要給 reason")

    items, files = _ids(item_ids), _ids(file_ids)
    photos, episodes = _ids(photo_ids), _ids(episode_ids)
    if not (items or files or photos or episodes):
        return {k: 0 for k in ("item", "episode", "file", "photo", "play_state", "image")}

    with db.tx() as conn:
        # 1) 條目 → 底下的集數與檔案一起收進來
        if items:
            files += [r["id"] for r in conn.execute(
                f"SELECT id FROM media_file WHERE {_in('item_id', items)}", items)]
            episodes += [r["id"] for r in conn.execute(
                f"SELECT id FROM episode WHERE {_in('item_id', items)}", items)]
            files, episodes = list(dict.fromkeys(files)), list(dict.fromkeys(episodes))

        # 2) 先把要刪的磁碟檔名記下來 —— 資料列刪掉之後就查不到了
        names: List[str] = []
        if items:
            for r in conn.execute(
                    f"SELECT poster, backdrop FROM media_item WHERE {_in('id', items)}", items):
                names += [r["poster"], r["backdrop"]]
        if episodes:
            names += [r["still"] for r in conn.execute(
                f"SELECT still FROM episode WHERE {_in('id', episodes)}", episodes)]
        if files:
            names += [r["thumb"] for r in conn.execute(
                f"SELECT thumb FROM media_file WHERE {_in('id', files)}", files)]
        if photos:
            names += [r["thumb"] for r in conn.execute(
                f"SELECT thumb FROM photo WHERE {_in('id', photos)}", photos)]

        counts = {k: 0 for k in ("item", "episode", "file", "photo", "play_state", "image")}
        if dry_run:
            # 到這裡為止一個字都還沒寫，所以直接回去就是了，不需要回滾
            counts.update(item=len(items), episode=len(episodes),
                          file=len(files), photo=len(photos))
            return counts

        # 3) 由下往上刪，不靠 CASCADE
        if files:
            counts["play_state"] = conn.execute(
                f"DELETE FROM play_state WHERE {_in('file_id', files)}", files).rowcount
            counts["file"] = conn.execute(
                f"DELETE FROM media_file WHERE {_in('id', files)}", files).rowcount
        if episodes:
            conn.execute(
                f"UPDATE media_file SET episode_id=NULL WHERE {_in('episode_id', episodes)}",
                episodes)
            counts["episode"] = conn.execute(
                f"DELETE FROM episode WHERE {_in('id', episodes)}", episodes).rowcount
        if items:
            counts["item"] = conn.execute(
                f"DELETE FROM media_item WHERE {_in('id', items)}", items).rowcount
        if photos:
            counts["photo"] = conn.execute(
                f"DELETE FROM photo WHERE {_in('id', photos)}", photos).rowcount

    # 4) 磁碟檔在交易外面刪。交易回滾得了資料列，回滾不了 unlink()——
    #    先刪檔再回滾就是資料還在、圖不見了。
    counts["image"] = _unlink(names)
    _log(reason, counts)
    return counts


def _log(reason: str, counts: Dict[str, int]) -> None:
    total = sum(counts.values())
    if not total:
        return
    detail = "、".join(f"{k}×{v}" for k, v in counts.items() if v)
    log.info("purge(%s)：%s", reason, detail)
    try:
        db.execute(
            "INSERT INTO purge_log(at, reason, detail, total) VALUES(?,?,?,?)",
            (int(time.time()), reason[:60], detail[:400], total))
    except Exception as e:                      # 紀錄壞掉不該讓刪除失敗
        log.warning("寫不進 purge_log：%s", e)


# ---------------------------------------------------------------- sweep
def _orphans(grace_ts: float) -> Dict[str, list]:
    """找出七類孤兒。回傳的是 id 清單（第七類是檔名）。"""
    out: Dict[str, list] = {}
    out["file_item"] = [r["id"] for r in db.q(
        "SELECT id FROM media_file WHERE item_id IS NOT NULL AND item_id NOT IN "
        "(SELECT id FROM media_item)")]
    out["file_episode"] = [r["id"] for r in db.q(
        "SELECT id FROM media_file WHERE episode_id IS NOT NULL AND episode_id NOT IN "
        "(SELECT id FROM episode)")]
    out["episode_item"] = [r["id"] for r in db.q(
        "SELECT id FROM episode WHERE item_id NOT IN (SELECT id FROM media_item)")]
    out["play_file"] = [r["file_id"] for r in db.q(
        "SELECT file_id FROM play_state WHERE file_id NOT IN (SELECT id FROM media_file)")]
    # 5、6、7 有寬限期：剛建好還沒寫檔案列的條目是合法的中間狀態
    out["item_nofile"] = [r["id"] for r in db.q(
        "SELECT id FROM media_item WHERE added_at < ? AND id NOT IN "
        "(SELECT item_id FROM media_file WHERE item_id IS NOT NULL)", (grace_ts,))]
    out["episode_nofile"] = [r["id"] for r in db.q(
        "SELECT e.id FROM episode e JOIN media_item m ON m.id = e.item_id "
        "WHERE m.added_at < ? AND e.id NOT IN "
        "(SELECT episode_id FROM media_file WHERE episode_id IS NOT NULL)", (grace_ts,))]
    used = _referenced_images()
    stale: List[str] = []
    try:
        for f in IMAGE_DIR.iterdir():
            if not f.is_file() or f.name in used:
                continue
            try:
                if f.stat().st_mtime < grace_ts:
                    stale.append(f.name)
            except OSError:
                pass
    except OSError as e:
        log.warning("讀不到圖片目錄：%s", e)
    out["image_unused"] = stale
    return out


def sweep(mode: str = "report", *, grace: int = GRACE_SECONDS,
          reason: str = "sweep") -> Dict[str, object]:
    """清掃孤兒。

    mode='report' 只回報不動手。啟動時就是用這個模式 —— 啟動路徑自動刪資料
    很糟，尤其是剛還原備份或剛換過後端之後：那時候「看起來像孤兒」的東西
    特別多，而它們多半只是還沒對上而已。
    """
    if mode not in ("report", "fix"):
        raise ValueError("mode 只能是 report 或 fix")
    grace_ts = time.time() - max(0, grace)
    found = _orphans(grace_ts)
    counts = {k: len(v) for k, v in found.items()}
    result: Dict[str, object] = {"mode": mode, "found": counts,
                                 "total": sum(counts.values())}
    if mode == "report" or not result["total"]:
        return result

    # 指到不存在東西的那幾類：把指標清掉就好，不必刪整列
    if found["file_item"]:
        ids = found["file_item"]
        db.execute(f"UPDATE media_file SET item_id=NULL WHERE {_in('id', ids)}", ids)
    if found["file_episode"]:
        ids = found["file_episode"]
        db.execute(f"UPDATE media_file SET episode_id=NULL WHERE {_in('id', ids)}", ids)
    if found["play_file"]:
        ids = found["play_file"]
        db.execute(f"DELETE FROM play_state WHERE {_in('file_id', ids)}", ids)

    fixed = purge(item_ids=found["item_nofile"],
                  episode_ids=found["episode_item"] + found["episode_nofile"],
                  reason=reason)
    fixed["image"] += _unlink(found["image_unused"])
    result["fixed"] = fixed
    return result


def counts() -> Dict[str, int]:
    """給 /api/stats 用的孤兒計數。沒有外鍵的資料庫，這就是體溫計。"""
    found = _orphans(time.time() - GRACE_SECONDS)
    c = {k: len(v) for k, v in found.items()}
    c["total"] = sum(c.values())
    return c
