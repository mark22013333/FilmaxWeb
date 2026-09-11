"""影集的集數順序：「下一集」是哪一集、同一集要播哪一個檔案。

## 為什麼要獨立一個模組

「下一集」這件事會有三個呼叫點問同一個問題：`/api/play/{file_id}` 要回
`next_episode`、`/api/continue` 要決定同一部影集留哪一筆、之後的自動播放
也會要。三個地方各寫一次排序規則的話，遲早會出現「播放器跳到 S02E03，
但繼續觀看顯示 S02E02」這種**兩邊各自對、合起來不一致**的狀態 ——
那種 bug 沒有錯誤訊息，只會讓使用者覺得系統在亂跳。

所以排序與挑檔案的規則只有這裡一份：

    EPISODE_ORDER_SQL       集數的播放順序（season, episode）
    FILE_PICK_ORDER_SQL     同一集有多個檔案時挑哪一個
    next_episode(...)       跨季、跳過不可播放的集數

## 挑檔案的規則為什麼是這個

同一個 episode 有多個 media_file 是常態（1080p 與 720p 兩版、或是
`.mkv` 與 `.mp4` 併存）。規則必須是 **deterministic** 的，否則同一集
每次算出不同的 file_id，續播進度就會跟著跳。

順序沿用 `/api/items/{id}` 的 `ORDER BY f.filename` —— 那是使用者在詳情
頁看到的順序，「下一集」跳到的檔案要跟他在清單上按第一個播的是同一個。
再補 `f.id` 當最後的 tie-break：檔名可能一樣（不同資料夾），
而 UNIQUE 的是 ftp_path 不是 filename。

## ACL

這個模組**自己不做權限判斷**，但它提供的查詢一律接受呼叫方傳進來的
ACL 條件片段，而且是接在 SQL 裡（不是查完再用 Python 濾）——
查完再濾的話「找下一個可播放的集數」會先看到不可讀的那一集、
把它當成答案回傳，然後才發現不能給。見 `next_episode()` 的說明。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import db

# 集數的播放順序。季在前、集在後 —— 這是唯一一份定義。
EPISODE_ORDER_SQL = "e.season, e.episode"

# 同一集有多個檔案時挑哪一個（見模組開頭）。
FILE_PICK_ORDER_SQL = "f.filename, f.id"

# 一次往後找幾集。跨季、又要跳過不可播放的集數，所以不能只看下一筆；
# 但也不必把整部影集抓回來 —— 使用者的片庫裡連續缺 40 集的情況不存在，
# 而真的缺那麼多的話「沒有下一集」也是合理的答案。
_LOOKAHEAD = 40


def pick_file_sql(acl_frag: str = "") -> str:
    """「這一集要播哪一個檔案」的子查詢片段（回 media_file.id）。

    acl_frag 要是針對 `f.ftp_path` 的條件（`acl.filter_sql` 的產物）。
    參數順序：先 episode_id，再 acl 的參數。
    """
    where = "f.episode_id=?" + ((" AND " + acl_frag) if acl_frag else "")
    return (f"SELECT f.id FROM media_file f WHERE {where}"
            f" ORDER BY {FILE_PICK_ORDER_SQL} LIMIT 1")


def playable_file(episode_id: int, acl_frag: str = "",
                  acl_args: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
    """這一集可播放的那一個檔案。看不到（或這一集沒有檔案）就回 None。

    「可播放」刻意**不看 probe_state**：沒探測過的檔案在詳情頁一樣有播放
    按鈕（介面上標「未探測」），所以「下一集」的條件不能比使用者自己點
    的那一顆更嚴 —— 否則會出現「清單上有這一集，下一集卻跳過它」。
    """
    where = "f.episode_id=?" + ((" AND " + acl_frag) if acl_frag else "")
    row = db.q1(
        f"""SELECT f.id, f.item_id, f.filename, f.duration, f.ftp_path
            FROM media_file f WHERE {where}
            ORDER BY {FILE_PICK_ORDER_SQL} LIMIT 1""",
        [episode_id] + list(acl_args or []))
    return db.row_to_dict(row)


def next_episode(item_id: int, season: Optional[int], episode: Optional[int],
                 acl_frag: str = "",
                 acl_args: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
    """同一部影集裡、排在 (season, episode) 之後第一個**真的播得到**的集數。

    回 None 的情形：這不是影集、已經是最後一集、或後面剩下的集數全都
    沒有可播放的檔案（例如只掃到 metadata、或檔案落在受限資料夾裡）。

    ## 為什麼是 (season, episode) 的字典序而不是「下一筆」

    同季最後一集要能跨到下一季第一集，而季與集是兩個欄位，所以比較條件是
    `(season, episode) > (s, e)` 的展開：`season > s OR (season = s AND
    episode > e)`。寫成 `episode > e` 會在跨季時把整個下一季漏掉。

    ## 為什麼要「找下一個播得到的」而不是「下一集不能播就回 None」

    片庫是掃出來的，中間缺一集很常見（沒下載到、檔名認不出來）。
    缺一集就說「沒有下一集」，等於在最常見的情況下把功能關掉。

    ## ACL 為什麼要接在 SQL 裡

    先算出「下一集是 S02E03」再檢查權限的話，不可讀的那一集會變成答案，
    而回傳 null 的同時**已經洩漏了「S02E03 存在」**（使用者看得到下一集
    按鈕忽然消失、或是拿到一個 404 的 file_id）。條件接在 SQL 裡的話，
    不可讀的集數從一開始就不在候選名單上，跳過它跟「那一集根本不存在」
    在外部完全無法區分。
    """
    if season is None or episode is None:
        return None
    args: List[Any] = [item_id, season, season, episode]
    rows = db.q(
        f"""SELECT e.id, e.season, e.episode, e.title, e.still
            FROM episode e
            WHERE e.item_id=? AND (e.season > ? OR (e.season = ? AND e.episode > ?))
            ORDER BY {EPISODE_ORDER_SQL} LIMIT {_LOOKAHEAD}""", args)
    for r in rows:
        e = db.row_to_dict(r) or {}
        f = playable_file(e["id"], acl_frag, acl_args)
        if f:
            return {
                "file_id": f["id"],
                "item_id": item_id,
                "episode_id": e["id"],
                "season": e["season"],
                "episode": e["episode"],
                "title": e.get("title") or None,
                "still": e.get("still"),
                "filename": f.get("filename"),
                "duration": f.get("duration"),
            }
    return None
