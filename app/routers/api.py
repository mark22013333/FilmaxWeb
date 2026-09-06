"""媒體庫 API：清單、詳情、搜尋、掃描控制、播放資訊、續播進度。"""
from __future__ import annotations

import anyio
import json
import posixpath
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import (acl, audit, auth, db, ftpclient, geo, hls, media, mssql, params,
                paramstore, purge, scanner, users)
from ..config import CACHE_DIR, IMAGE_DIR, settings
from ..scraper import tmdb

router = APIRouter()

# 「還要我處理的」＝不是 ok、不是使用者親手修好的（manual）、也不是
# 本來就不需要 metadata 的（skip）。跟前端角標（app.js）同一個定義 ——
# 兩邊一旦漂移，後台的數字跟格線上的紅點就對不起來。
_UNSCRAPED_SQL = "scrape_state NOT IN ('ok','manual','skip')"


# ------------------------- 資料整形 -------------------------
def _item_row(row) -> Dict[str, Any]:
    d = db.row_to_dict(row) or {}
    d["poster_url"] = f"/api/image/{d['poster']}" if d.get("poster") else None
    d["backdrop_url"] = f"/api/image/{d['backdrop']}" if d.get("backdrop") else None
    # 沒有 TMDB 海報時，用影片截圖頂替，海報牆才不會一片空白
    d["thumb_url"] = d["poster_url"] or d["backdrop_url"]
    d.pop("cast_json", None)
    return d


def admin_only(request: Request) -> str:
    """限管理員的端點都掛這個。

    唯讀角色能看能播，但不能下載原始檔、不能瀏覽 FTP 目錄、不能觸發掃描或
    清快取 —— 那些會消耗資源、洩漏檔案系統結構，或直接把片子搬走。
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")
    return auth.role_of(request)


@router.get("/me")
def me(request: Request):
    """前端拿來決定要不要顯示下載與管理功能。"""
    role = auth.role_of(request)
    out = {"role": role, "is_admin": role == auth.ADMIN,
           "auth_enabled": settings.auth_enabled,
           "google_login": settings.google_enabled,
           "uid": auth.user_id(request), "email": auth.user_email(request),
           "display_name": None, "picture_url": None}
    uid = auth.user_id(request)
    if uid:
        rec = users.by_id(uid) or {}
        out["display_name"] = rec.get("display_name")
        out["picture_url"] = rec.get("picture_url")
    if out["is_admin"] and settings.google_enabled:
        # 後台的小紅點：有幾個人在等審核
        out["pending_users"] = users.counts().get("pending", 0)
    return out


# ------------------------- 帳號管理（限管理員） -------------------------
class UserPatch(BaseModel):
    status: Optional[str] = None
    role: Optional[str] = None       # owner / viewer
    note: Optional[str] = None


def _last_owner(uid: int) -> bool:
    """他是不是最後一個能用的管理員。

    要擋住的是「管理員把自己停權／降級，結果整個服務沒有人有權限」的死局 ——
    這種狀態沒有辦法從網頁介面救回來，只能去改資料庫。
    """
    rec = users.by_id(uid)
    if not rec or rec.get("role") != users.OWNER or rec.get("status") != "approved":
        return False
    return users.owner_count() <= 1


@router.get("/users")
def user_list(status: Optional[str] = None, limit: int = 200,
              _: str = Depends(admin_only)):
    return {"items": users.listing(status, limit), "counts": users.counts(),
            "google_login": settings.google_enabled}


@router.patch("/users/{uid}")
def user_patch(uid: int, p: UserPatch, request: Request, _: str = Depends(admin_only)):
    rec = users.by_id(uid)
    if not rec:
        raise HTTPException(404, "找不到這個帳號")
    if (p.status and p.status != "approved") or (p.role and p.role != users.OWNER):
        if _last_owner(uid):
            raise HTTPException(400, "這是最後一個管理員，不能停權或降級")
    who = auth.identity(request)
    ip = auth.client_ip(request.scope)
    try:
        if p.role is not None:
            rec = users.set_role(uid, p.role)
        if p.status is not None:
            rec = users.set_status(uid, p.status, by=who)
        if p.note is not None:
            rec = users.set_note(uid, p.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 誰把誰改成什麼，一定要留下來 —— 權限變動是事後追查時第一個要看的東西
    if p.status in ("approved", "rejected", "disabled"):
        audit.record({"approved": "approve", "rejected": "reject",
                      "disabled": "disable"}[p.status],
                     ip=ip, loc=geo.from_scope(request.scope), email=rec.get("email"),
                     user_id=uid, detail=f"by {who}", ip_source=geo.ip_source(request.scope))
    if p.role is not None:
        audit.record("role_change",
                     ip=ip, loc=geo.from_scope(request.scope), email=rec.get("email"),
                     user_id=uid, detail=f"role={p.role} by {who}",
                     ip_source=geo.ip_source(request.scope))
    return rec


@router.delete("/users/{uid}")
def user_delete(uid: int, _: str = Depends(admin_only)):
    if not users.by_id(uid):
        raise HTTPException(404, "找不到這個帳號")
    if _last_owner(uid):
        raise HTTPException(400, "這是最後一個管理員，不能刪除")
    users.delete(uid)
    return {"ok": True}


@router.get("/library")
def library(
    request: Request,
    kind: Optional[str] = None,
    genre: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "added",
    page: int = 1,
    page_size: int = 60,
    unscraped: int = 0,
):
    where: List[str] = ["1=1"]
    params: List[Any] = []
    if kind in ("movie", "tv", "jav", "home"):
        where.append("i.kind=?")
        params.append(kind)
    if genre:
        where.append("i.genres LIKE ?")
        params.append(f'%"{genre}"%')
    if q:
        where.append("(i.title LIKE ? OR i.original_title LIKE ? OR EXISTS("
                     "SELECT 1 FROM media_file f WHERE f.item_id=i.id AND f.filename LIKE ?))")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if unscraped:
        where.append(f"i.{_UNSCRAPED_SQL}")
    # 受限資料夾（L）。條目沒有路徑，路徑在 media_file 上，而影集分組可能跨資料夾 ——
    # 所以規則是「只要還有一個看得到的檔案就看得到」。
    acl_frag, acl_args = acl.filter_sql(request, "f2.ftp_path")
    if acl_frag:
        where.append(f"EXISTS(SELECT 1 FROM media_file f2 WHERE f2.item_id=i.id AND {acl_frag})")
        params += acl_args

    # 每一種排序都要有決定性的收尾（`i.id`）—— 理由見 photos() 的同一段註解：
    # 並列的資料列在兩次查詢之間順序不保證，而分頁是重新查一次。
    # 「同一天加入的一批」「同年份」「都沒有評分（COALESCE 成 0）」都會並列。
    order = {
        "added": "i.added_at DESC, i.id DESC",
        "title": "i.sort_title ASC, i.id ASC",
        "year": "COALESCE(i.year,0) DESC, i.title ASC, i.id ASC",
        "rating": "COALESCE(i.rating,0) DESC, i.title ASC, i.id ASC",
    }.get(sort, "i.added_at DESC, i.id DESC")

    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    sql_where = " AND ".join(where)

    total = db.q1(f"SELECT COUNT(*) c FROM media_item i WHERE {sql_where}", params)["c"]
    rows = db.q(
        f"""SELECT i.*,
                   (SELECT COUNT(*) FROM media_file f WHERE f.item_id=i.id) file_count,
                   (SELECT COUNT(DISTINCT e.season) FROM episode e WHERE e.item_id=i.id) season_count
            FROM media_item i WHERE {sql_where}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    )
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_item_row(r) for r in rows]}


@router.get("/genres")
def genres(request: Request):
    counts: Dict[str, int] = {}
    # 類型 chips 也算資訊：受限資料夾裡獨有的類型如果出現在這裡，
    # 就等於告訴使用者「有一個你看不到的東西存在」。
    frag, args = acl.filter_sql(request, "f2.ftp_path")
    extra = (f" AND EXISTS(SELECT 1 FROM media_file f2 "
             f"WHERE f2.item_id=media_item.id AND {frag})") if frag else ""
    for r in db.q("SELECT genres FROM media_item WHERE genres IS NOT NULL" + extra, args):
        try:
            for g in json.loads(r["genres"] or "[]"):
                counts[g] = counts.get(g, 0) + 1
        except Exception:
            continue
    return {"genres": [{"name": k, "count": v}
                       for k, v in sorted(counts.items(), key=lambda x: -x[1])]}


@router.get("/stats")
def stats(request: Request):
    def c(sql, p=()):
        return db.q1(sql, p)["c"]

    # 數字也會洩漏。「電影 117 部」但只列得出 98 部，等於告訴使用者
    # 有 19 部他看不到的東西存在 —— 受限資料夾的存在本身就是資訊。
    ff, fa = acl.filter_sql(request, "ftp_path")
    fw = f" WHERE {ff}" if ff else ""
    item_ex = (f" AND EXISTS(SELECT 1 FROM media_file f2 "
               f"WHERE f2.item_id=media_item.id AND {ff.replace('ftp_path', 'f2.ftp_path')})"
               if ff else "")
    pf, pa = acl.filter_sql(request, "folder")
    pw = f" WHERE {pf}" if pf else ""

    out = {
        "movies": c("SELECT COUNT(*) c FROM media_item WHERE kind='movie'" + item_ex, fa),
        "shows": c("SELECT COUNT(*) c FROM media_item WHERE kind='tv'" + item_ex, fa),
        "javs": c("SELECT COUNT(*) c FROM media_item WHERE kind='jav'" + item_ex, fa),
        "homes": c("SELECT COUNT(*) c FROM media_item WHERE kind='home'" + item_ex, fa),
        "files": c("SELECT COUNT(*) c FROM media_file" + fw, fa),
        # 兩個不同的問題，之前被塞進同一個數字：
        #   unscraped  = 我還要做什麼（只有這個該拉警報）
        #   no_metadata = 資料完整度多少（忠實，但沒有行動意義）
        # 舊的 `!='ok'` 把 manual（使用者親手修好的）也算成問題，跟前端角標
        # （app.js 一直都排除 manual）對不上 —— 後台說有 3 筆、格線上只有 1 個
        # 紅角標。現在兩邊同一個定義。
        "unscraped": c(f"SELECT COUNT(*) c FROM media_item WHERE {_UNSCRAPED_SQL}" + item_ex, fa),
        "no_metadata": c("SELECT COUNT(*) c FROM media_item WHERE scrape_state!='ok'" + item_ex, fa),
        "unprobed": c("SELECT COUNT(*) c FROM media_file WHERE probe_state!='ok'"
                      + (f" AND {ff}" if ff else ""), fa),
        "total_size": db.q1("SELECT COALESCE(SUM(size),0) c FROM media_file" + fw, fa)["c"],
        "tmdb_enabled": tmdb.enabled,
    }
    # 沒有外鍵可以靠的資料庫，孤兒計數就是體溫計 ——
    # 這個數字持續往上，代表刪除路徑有地方漏了。
    try:
        out["orphans"] = purge.counts()
    except Exception:
        out["orphans"] = None
    # FTP 位址、片庫路徑、硬體資訊對「能看片」沒有用處，卻是很好的偵察材料
    if auth.is_admin(request):
        out.update({
            "hwaccel": media.resolve_hwaccel(),
            "hwaccel_probe": media.hwaccel_probe_report(),
            "hwaccel_setting": settings.hwaccel,
            "ftp": {"host": settings.ftp_host, "port": settings.ftp_port,
                    "roots": [r.path for r in settings.library_roots]},
        })
    return out


@router.get("/items/{item_id}")
def item_detail(item_id: int, request: Request):
    row = db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "找不到條目")
    # 條目層：只要還有一個看得到的檔案就看得到（影集分組可能跨資料夾）
    if not acl.visible_item(request, item_id):
        raise HTTPException(404, "找不到條目")
    item = _item_row(row)
    item["cast"] = db.row_to_dict(row).get("cast_json") or []

    # 而看不到的那幾個檔案要從檔案清單裡濾掉 —— 條目看得到不代表底下每個檔案都看得到
    ff, fa = acl.filter_sql(request, "f.ftp_path")
    files = [db.row_to_dict(r) for r in db.q(
        f"""SELECT f.*, p.position, p.duration AS watched_duration, p.finished
           FROM media_file f LEFT JOIN play_state p ON p.file_id=f.id
           WHERE f.item_id=?{(' AND ' + ff) if ff else ''} ORDER BY f.filename""",
        [item_id] + fa)]
    for f in files:
        f["size_gb"] = round((f.get("size") or 0) / 1024 ** 3, 2)

    if item["kind"] == "tv":
        eps = [dict(r) for r in db.q(
            "SELECT * FROM episode WHERE item_id=? ORDER BY season, episode", (item_id,))]
        by_ep = {}
        for f in files:
            by_ep.setdefault(f.get("episode_id"), []).append(f)
        seasons: Dict[int, List[dict]] = {}
        for e in eps:
            e["still_url"] = f"/api/image/{e['still']}" if e.get("still") else None
            e["files"] = by_ep.get(e["id"], [])
            seasons.setdefault(e["season"], []).append(e)
        item["seasons"] = [{"season": s, "episodes": v} for s, v in sorted(seasons.items())]
        item["files"] = [f for f in files if not f.get("episode_id")]
    else:
        item["files"] = files
    return item


def _qs(**kw) -> str:
    """把非 None 的參數組成查詢字串（含開頭的 `?`）；全是 None 就回空字串。

    `h=None` 與 `h=0` 是不同的意思（沒指定 vs 原畫質），所以只能濾掉 None，
    不能用真假值判斷 —— `if v` 會把 0 一起丟掉。
    """
    parts = [f"{k}={v}" for k, v in kw.items() if v is not None]
    return "?" + "&".join(parts) if parts else ""


@router.get("/play/{file_id}")
def play_info(file_id: int, request: Request,
              h: Optional[int] = None, a: Optional[int] = None):
    row = db.q1("""SELECT f.*, i.title AS item_title, i.kind, e.season, e.episode, e.title AS ep_title
                   FROM media_file f
                   LEFT JOIN media_item i ON i.id=f.item_id
                   LEFT JOIN episode e ON e.id=f.episode_id
                   WHERE f.id=?""", (file_id,))
    if not row:
        raise HTTPException(404, "找不到檔案")
    f = db.row_to_dict(row) or {}
    # 播放資訊會回檔名、路徑、字幕清單與可播網址 —— 受限的片子擋在這裡
    acl.assert_can_read(request, f.get("ftp_path"))
    mode = f.get("play_mode") or ("direct" if (f.get("ext") or "").lower() == "mp4" else "hls")

    audio_tracks = f.get("audio_tracks") or []
    # a 會被塞進 ffmpeg 的 -map 0:N，只接受這個檔案真的有的音軌編號。
    valid_audio = {t.get("index") for t in audio_tracks if t.get("index") is not None}
    chosen_a = a if a in valid_audio else None

    tracks = []
    for s in f.get("subtitles") or []:
        graphic = not s.get("text")
        tracks.append({
            "kind": "embedded",
            "label": s.get("title") or s.get("lang") or f"字幕 {s['index']}",
            "lang": s.get("lang") or "und",
            "codec": s.get("codec"),
            # 圖形字幕（PGS/VobSub）沒辦法轉成 WebVTT，標出來讓介面說明白，
            # 不要讓使用者點了以為壞掉。
            "graphic": graphic,
            "forced": bool(s.get("forced")),
            "url": (None if graphic
                    else f"/api/subtitle/{file_id}/embedded/{s['index']}.vtt"),
            # 已經抽好在快取裡的話前端就不必再預抽一次（K 的實作細節 3）。
            # 圖形字幕不會有快取，直接給 False，不要白 stat 一次。
            "cached": (False if graphic
                       else media.subtitle_cached(file_id, s["index"],
                                                  f.get("mtime"), f.get("size"))),
        })
    ext_map = db.kv_get("external_subtitles", {}) or {}
    folder = posixpath.dirname(f["ftp_path"])
    stem = f["filename"].rsplit(".", 1)[0].lower()
    for p in ext_map.get(folder, []):
        base = posixpath.basename(p)
        if base.rsplit(".", 1)[0].lower().startswith(stem[:max(6, len(stem) // 2)]):
            tracks.append({
                "kind": "external",
                "label": base,
                "lang": "und",
                "codec": base.rsplit(".", 1)[-1].lower(),
                "graphic": False,
                "forced": False,
                "url": f"/api/subtitle/{file_id}/external.vtt?path={p}",
                # 外掛字幕是整檔抓下來直接轉，沒有走 SUB_DIR 快取，
                # 但它本來就只有幾十 KB、不必 demux 整部片 —— 不值得為它加一層快取。
                "cached": False,
            })

    state = db.q1("SELECT position, finished FROM play_state WHERE file_id=?", (file_id,))

    # 區網 vs 遠端：遠端預設降解析度並鎖住峰值位元率，
    # 因為家用光纖的「上傳」頻寬通常遠小於下載。
    # 一律走 auth.client_ip：它會判斷 proxy 標頭可不可信。
    # 自己在這裡解 X-Forwarded-For 的話，只要送一個內網位址就能騙過
    # 「遠端降畫質」的保護，把上傳頻寬吃光。
    ip = auth.client_ip(request.scope)
    remote = not auth.is_local_network(ip)
    # 解析度／位元率的政策集中在 hls.quality_policy() —— master.m3u8 要問的是
    # 同一個答案（0 = 不縮放／不鎖，不是「沒設定」；h=0 是「原畫質」不是「沒指定」，
    # 這兩個坑的說明都在那個函式裡）。
    chosen_h, chosen_b = hls.quality_policy(remote, h)

    # 遠端 + 這個檔案有上階可給 → **不要走 direct**。direct 沒有階梯可以降：
    # 那 7 部走 direct 的 mp4 裡有 7.6 GB／4,661 kbps 與 4.8 GB／4,508 kbps
    # 兩部大檔，鏈路不夠就只能卡在那裡。改走兩階 HLS 之後，上階的畫質
    # 一樣是原檔（remux），而且不夠時客戶端可以自己降到下階。
    #
    # **只有真的有上階時才改。**沒有上階的話，把遠端的 direct 改成 HLS
    # 等於把「原檔直送」換成「一定重編」—— 那是退步，不是進步。
    rungs = hls.rungs_for(file_id, float(f.get("duration") or 0))
    if remote and mode == "direct" and hls.RUNG_REMUX in rungs:
        mode = "hls"

    src_h = f.get("height") or 1080
    src_w = f.get("width") or 1920
    ladder = [x for x in (2160, 1440, 1080, 720, 480, 360) if x <= max(src_h, 360)]
    if not ladder:
        ladder = [src_h]
    # 每一階都附上「級別」與「實際解析度」。只給 `${h}p` 的話，
    # 一部 1920×804 的藍光會被標成「804p」看起來像降級 —— 那是寬螢幕片的
    # 原始畫面高度（黑邊壓製時就裁掉了），片庫裡這種有 88 部。
    levels = []
    for t in ladder:
        w2, h2 = media.scaled_size(src_w, src_h, t)
        levels.append({"h": t, "width": w2, "height": h2,
                       "label": media.quality_class(w2, h2)})
    source_q = {"width": src_w, "height": src_h,
                "label": media.quality_class(src_w, src_h)}
    return {
        "file_id": file_id,
        "title": f.get("item_title") or f.get("filename"),
        "subtitle_label": (f"S{f['season']:02d}E{f['episode']:02d} {f.get('ep_title') or ''}".strip()
                           if f.get("season") else ""),
        "filename": f.get("filename"),
        "mode": mode,
        # 這個檔案在這條鏈路上實際會拿到幾階。前端不必用它來決定行為
        # （master playlist 才是事實），但診斷時要看得到「為什麼沒有上階」。
        "rungs": len(rungs) if remote else min(len(rungs), 1),
        "remux_available": hls.RUNG_REMUX in rungs,
        "duration": f.get("duration"),
        "width": f.get("width"), "height": f.get("height"),
        "video_codec": f.get("video_codec"), "audio_codec": f.get("audio_codec"),
        "is_hdr": bool(f.get("is_hdr")),
        "bit_depth": f.get("bit_depth"),
        "hdr_mode": (media.hdr_mode_for(f.get("width"), f.get("height"))
                     if f.get("is_hdr") else None),
        "tonemap_available": media.can_tonemap(),
        "container": f.get("container"), "ext": f.get("ext"),
        "size": f.get("size"),
        "bitrate": f.get("bitrate"),
        "fps": f.get("fps"),
        "pix_fmt": f.get("pix_fmt"),
        "color_transfer": f.get("color_transfer"),
        "ftp_path": f.get("ftp_path"),
        # 開播後要不要在背景把字幕抽好（K）。放在這裡而不是 /api/prefs，
        # 是因為播放器本來就會打這一支，不必為一個布林值多一趟往返。
        "subtitle_prefetch": bool(settings.subtitle_prefetch),
        "audio_tracks": audio_tracks,
        "audio_index": chosen_a if chosen_a is not None else _default_audio(audio_tracks),
        "audio_channels_out": settings.audio_channels,
        "direct_url": f"/api/stream/{file_id}",
        # **一定要指到 master，不是 index。**指到 index 等於直接給了單一階的
        # 媒體播放清單 —— hls.js 的 ABR 引擎沒有第二階可選，卡頓時無階可降，
        # 而 `build_master()` 裡那整套「區網一階、遠端整條階梯」的邏輯
        # 一行都不會被執行到。h／a 要原樣帶過去：master 要照使用者選的那一檔
        # 當階梯的頂端（見 abr_ladder）。
        #
        # **`h` 沒給的時候不能自己補上 chosen_h。**補了的話 master 端點就分不出
        # 「自動」與「手動挑了剛好等於預設的那一檔」—— 而這兩者要發的階梯不一樣：
        # 自動會多發一階高畫質頂階，手動不會（挑的那一檔就是他要的上限）。
        "hls_url": f"/api/hls/{file_id}/master.m3u8" + _qs(
            h=(chosen_h if h is not None else None), a=chosen_a),
        "remote": remote,
        "client_ip": ip,
        "quality": {"height": chosen_h, "bitrate_kbps": chosen_b, "ladder": ladder,
                    # levels／source 是新的；ladder 留著，舊的前端快取才不會空手
                    "levels": levels, "source": source_q,
                    "auto_reason": (
                        "遠端連線，已自動降到 "
                        f"{media.quality_class(*media.scaled_size(src_w, src_h, chosen_h))}"
                        f"（{'×'.join(str(x) for x in media.scaled_size(src_w, src_h, chosen_h))}）"
                        f" / {chosen_b or '不限'} kbps" if remote else None)},
        "download_url": f"/api/download/{file_id}",
        "subtitles": tracks,
        "resume": _resume_at(state, f.get("duration")),
        "probe_state": f.get("probe_state"),
        "probe_error": f.get("probe_error"),
    }


def _default_audio(tracks: List[Dict[str, Any]]) -> Optional[int]:
    """沒指定音軌時 ffmpeg 會用 0:a:0，這裡要算出同一個答案才能在介面上打勾。

    注意不能寫成 `next(...) or tracks[0]`：音軌編號可能是 0，而 0 是 falsy，
    會被誤判成「沒找到」。
    """
    for t in tracks:
        if t.get("default") and t.get("index") is not None:
            return t["index"]
    return tracks[0].get("index") if tracks else None


def _resume_at(state, duration: Optional[float]) -> float:
    """快看完才存的進度，續播時直接從頭開始比較合理（不然一進去就跳結尾）。"""
    if not state or state["finished"]:
        return 0.0
    pos = float(state["position"] or 0)
    if pos < 30:
        return 0.0
    if duration and (pos > duration - 20 or pos / duration > 0.97):
        return 0.0
    return pos


class Prefs(BaseModel):
    data: Dict[str, Any]


def _prefs_key(request: Request) -> str:
    """設定的存放位置：有帳號就每人一份，只有密碼登入時退回照角色分。"""
    uid = auth.user_id(request)
    if uid:
        return f"player_prefs:u{uid}"
    return f"player_prefs:{auth.role_of(request)}"


@router.get("/prefs")
def get_prefs(request: Request):
    """播放器偏好（字幕外觀、快轉秒數…）。"""
    rec = db.kv_get(_prefs_key(request))
    if rec is None:                       # 舊版是全站一份，第一次讀沿用過來
        rec = db.kv_get("player_prefs", {"data": {}, "updated_at": 0})
    return rec


@router.post("/prefs")
def save_prefs(p: Prefs, request: Request):
    # updated_at 一律由伺服器蓋章，不接受瀏覽器傳來的時間。
    # 瀏覽器的時鐘可能不準（手機、剛開機、時區設錯），只要有一台快幾年，
    # 它寫進來的時間戳就會讓之後所有裝置的設定都被判定為「舊的」而永遠同步不上去。
    rec = {"data": p.data, "updated_at": db.now_i()}
    db.kv_set(_prefs_key(request), rec)
    return {"ok": True, "updated_at": rec["updated_at"]}


class Progress(BaseModel):
    file_id: int
    position: float
    duration: float = 0
    finished: bool = False


@router.post("/progress")
def save_progress(p: Progress):
    db.execute(
        """INSERT INTO play_state(file_id, position, duration, finished, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(file_id) DO UPDATE SET position=excluded.position,
               duration=excluded.duration, finished=excluded.finished,
               updated_at=excluded.updated_at""",
        (p.file_id, p.position, p.duration, 1 if p.finished else 0, db.now_i()),
    )
    return {"ok": True}


@router.get("/continue")
def continue_watching(request: Request, limit: int = 20):
    # play_state 目前不分使用者，所以管理員看過的受限片子會出現在每個人的
    # 「繼續看」裡 —— 這一排是最容易漏掉的洩漏點，因為它不走 /library。
    ff, fa = acl.filter_sql(request, "f.ftp_path")
    rows = db.q(
        f"""SELECT p.file_id, p.position, p.duration, f.filename, f.item_id, f.episode_id,
                  i.title, i.poster, i.kind, e.season, e.episode
           FROM play_state p
           JOIN media_file f ON f.id=p.file_id
           LEFT JOIN media_item i ON i.id=f.item_id
           LEFT JOIN episode e ON e.id=f.episode_id
           WHERE p.finished=0 AND p.position > 30{(' AND ' + ff) if ff else ''}
           ORDER BY p.updated_at DESC LIMIT ?""", fa + [limit])
    out = []
    for r in rows:
        d = dict(r)
        d["poster_url"] = f"/api/image/{d['poster']}" if d.get("poster") else None
        d["percent"] = round(100 * d["position"] / d["duration"], 1) if d.get("duration") else 0
        out.append(d)
    return {"items": out}


# ------------------------- 掃描 -------------------------
# --------------------------------------------------------------------------
# 系統參數（規格 B+）
# --------------------------------------------------------------------------
def _actor(request: Request) -> str:
    return auth.user_email(request) or auth.role_of(request) or "?"


@router.get("/params")
def params_list(_: str = Depends(admin_only)):
    """所有可在後台調整的設定。

    每一項都回三個候選值與 locked / shadowed，不是只回生效值 ——
    只回生效值的話，使用者改了一個被 .env 蓋住的項目會看到「儲存成功」
    而行為完全沒變，畫面上找不到任何線索。
    """
    items = paramstore.describe_all()
    return {
        "sections": [s for s in params.SECTIONS
                     if any(i["section"] == s for i in items)],
        "items": items,
        "drift": [d["key"] for d in paramstore.drift()],
        "needsRestart": paramstore.needs_restart(),
        "unknownEnvKeys": [{"key": k, "guess": g} for k, g in params.unknown_env_keys()],
        "startedAt": paramstore.STARTED_AT,
    }


class ParamSet(BaseModel):
    value: object = None


@router.put("/params/{key}")
def params_set(key: str, body: ParamSet, request: Request,
               _: str = Depends(admin_only)):
    """改一項設定。

    被鎖住的欄位前端會停用，但這裡仍然要擋。前端的停用是提示，不是防線 ——
    而「停用的欄位送出之後才在後端報錯」是規格點名要避免的那個 bug。
    """
    try:
        return paramstore.set_value(key, body.value, actor=_actor(request))
    except paramstore.NotEditable as e:
        raise HTTPException(403, str(e))
    except KeyError:
        raise HTTPException(404, f"沒有這個設定：{key}")
    except ValueError as e:
        raise HTTPException(400, str(e))


class ParamBulk(BaseModel):
    values: Dict[str, object] = {}


@router.put("/params")
def params_set_many(body: ParamBulk, request: Request,
                    _: str = Depends(admin_only)):
    """一次改多項設定（後台的「儲存 N 項變更」）。

    **不是前端迴圈打 PUT /params/{key} 的糖衣。**那樣會出現「前三項寫進去了、
    第四項失敗」，而使用者只看到一個錯誤 —— 不知道哪些已經生效。
    這裡整批驗證通過才寫，任何一項不合法就 400 並回每一項的錯誤，一個字都不寫。
    """
    if not body.values:
        return {"items": [], "errors": {}}
    if len(body.values) > 200:
        raise HTTPException(400, "一次最多 200 項")
    r = paramstore.set_many(body.values, actor=_actor(request))
    if r["errors"]:
        raise HTTPException(status_code=400, detail=r["errors"])
    return r


@router.delete("/params/{key}")
def params_clear(key: str, request: Request, _: str = Depends(admin_only)):
    """刪掉後台儲存的值，回到 .env 或預設值。"""
    if key not in params.REGISTRY:
        raise HTTPException(404, f"沒有這個設定：{key}")
    return paramstore.clear(key, actor=_actor(request))


@router.get("/params/audit")
def params_audit(limit: int = Query(default=100, ge=1, le=500),
                 _: str = Depends(admin_only)):
    """設定變更紀錄。秘密只記「已變更」，不記值也不記長度。"""
    return {"entries": paramstore.audit(limit)}


@router.post("/maintenance/sweep")
def sweep(mode: str = Query(default="report", pattern="^(report|fix)$"),
          _: str = Depends(admin_only)):
    """清掃孤兒資料。

    預設是 report —— 先看清單再決定要不要動手。這個順序是刻意的：
    「按一下就刪掉一些東西，而且不知道刪了什麼」在資料清理工具上是最差的設計。
    """
    return purge.sweep(mode, reason="admin_sweep")


@router.get("/maintenance/purge-log")
def purge_log(limit: int = Query(default=50, ge=1, le=500),
              _: str = Depends(admin_only)):
    """最近的刪除紀錄。使用者說「我的東西不見了」時，這是第一個要看的地方。"""
    return {"entries": [db.row_to_dict(r) for r in db.q(
        "SELECT * FROM purge_log ORDER BY at DESC LIMIT ?", (limit,))]}


@router.post("/scan")
def scan_start(full: bool = Query(default=False),
               reparse: bool = Query(default=False),
               remanual: bool = Query(default=False),
               _: str = Depends(admin_only)):
    """開始掃描。

    full=true     重新刮削所有條目、重新分析所有檔案
    reparse=true  強制重新解析檔名並重新分組（改了解析規則之後要用）
    remanual=true 連手動修正過的條目也重新刮削。**只有跟 full 一起才有意義**，
                  而且預設是 false —— 手動修正被自動刮削蓋掉是不可逆的損失
                  （使用者挑的那個 tmdb_id 沒有留在任何地方）。
    """
    if not scanner.start(full=full, reparse=reparse, remanual=remanual and full):
        return JSONResponse({"ok": False, "message": "掃描已在進行中"}, status_code=409)
    return {"ok": True}


@router.post("/scan/cancel")
def scan_cancel(_: str = Depends(admin_only)):
    scanner.cancel()
    return {"ok": True}


# 掃描日誌裡有 FTP 的完整路徑，唯讀使用者不該看到片庫的目錄結構。
# 但「現在是不是在掃描」是無害的，而且前端的空狀態畫面需要它 ——
# 所以不是整支擋掉，是把敏感欄位拿掉。
_SCAN_PUBLIC = ("running", "phase", "files_found", "items_total",
                "photos_found", "photo_total", "docs_found", "elapsed")


@router.get("/scan/status")
def scan_status(request: Request):
    st = scanner.status_dict()
    if auth.is_admin(request):
        return st
    return {k: st.get(k) for k in _SCAN_PUBLIC}


class ItemPatch(BaseModel):
    title: Optional[str] = None
    year: Optional[int] = None
    kind: Optional[str] = None          # movie / tv


def _rekey(old_key: Optional[str], kind: str, year: Optional[int]) -> Optional[str]:
    """改了 kind 或 year 之後重算 guess_key。

    **這是這一整段最容易漏掉、後果最難查的一件事。**
    `nameparser.guess_key()` 的形狀是：

        movie::某片名::2024
        tv::某劇名

    kind 是前綴、year 是電影的第三段。所以改了 kind 或 year 卻不重算 key，
    下一次掃描時 `_upsert_item()` 用新算出來的 key 找不到這個條目，
    就會**再建一個新的**，而使用者手動修好的那一個變成孤兒被清掉 ——
    症狀是「我改好的片隔天變成兩筆，然後我改的那筆不見了」。

    這裡從舊 key 拆出「片名」那一段來重組，而不是重新解析檔名：
    片名那一段本來就是從檔名算出來的，重新解析只會多一個出錯的機會。
    """
    if not old_key or "::" not in old_key:
        return None
    base = old_key.split("::")[1]
    if kind == "tv":
        return f"tv::{base}"
    if kind == "jav":
        # 番號才是 JAV 的識別碼，不是片名。手動把條目改成 jav 而沒有番號時
        # 回 None，讓呼叫端保留原本的 key —— 硬湊一個 jav::片名 會跟真正
        # 用番號當 key 的條目混在同一個命名空間裡。
        return None
    return f"movie::{base}::{year or ''}"


@router.patch("/items/{item_id}")
def item_patch(item_id: int, body: ItemPatch, request: Request,
               _: str = Depends(admin_only)):
    """手動修正條目：片名、年份、類型。

    改過的條目一律標成 `scrape_state='manual'`，之後自動刮削不會再蓋掉它
    （見 `scanner._scrape_items`）。
    """
    row = db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "找不到條目")
    item = db.row_to_dict(row) or {}
    sets, args, notes = [], [], []

    if body.kind is not None and body.kind not in ("movie", "tv", "jav", "home"):
        raise HTTPException(400, "kind 只能是 movie 或 tv")

    kind = body.kind or item["kind"]
    year = item.get("year") if body.year is None else (body.year or None)
    if year is not None and not (1870 <= int(year) <= 2200):
        raise HTTPException(400, "年份看起來不對")

    if body.title is not None:
        t = body.title.strip()
        if not t:
            raise HTTPException(400, "片名不能空白")
        # sort_title 一定要跟著改。只改 title 的話排序還是用舊名字，
        # 而使用者會以為「改了但沒生效」。
        sets += ["title=?", "sort_title=?"]
        args += [t, t.lower()]
        notes.append("片名")

    if body.year is not None:
        sets.append("year=?")
        args.append(year)
        notes.append("年份")

    kind_changed = body.kind is not None and body.kind != item["kind"]
    if kind_changed:
        sets.append("kind=?")
        args.append(kind)
        notes.append("類型")

    # kind 或 year 動了就要重算 guess_key（見 _rekey）
    if kind_changed or body.year is not None:
        nk = _rekey(item.get("guess_key"), kind, year)
        if nk:
            sets += ["guess_key=?"]
            args.append(nk)

    if not sets:
        return {"ok": True, "changed": []}

    sets += ["scrape_state=?", "updated_at=?"]
    args += ["manual", db.now_i()]
    db.execute(f"UPDATE media_item SET {', '.join(sets)} WHERE id=?", args + [item_id])

    # 類型換了，集數結構也要跟著換
    eps = 0
    if kind_changed:
        eps = (scanner.reassign_episodes(item_id) if kind == "tv"
               else scanner.clear_episodes(item_id))

    audit.record("item_patch", role=auth.role_of(request),
                 ip=auth.client_ip(request.scope), loc={},
                 email=auth.user_email(request),
                 detail=f"#{item_id} {'/'.join(notes)}")
    out = {"ok": True, "changed": notes, "episodes": eps, "kind": kind}

    # 類型換了就順手重刮一次 —— 認錯類型的條目，TMDB 資料整份都是錯的
    if kind_changed and tmdb.enabled:
        try:
            db.execute("UPDATE media_item SET scrape_state='pending' WHERE id=?", (item_id,))
            scanner._scrape_one(dict(db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))))
            # 重刮成功會被寫成 'ok'，但這是使用者手動改過的條目 —— 標回 manual，
            # 否則下一次 force 重掃又會把它拿去自動比對。
            db.execute("UPDATE media_item SET scrape_state='manual' WHERE id=?", (item_id,))
            out["rescraped"] = True
        except Exception as e:
            db.execute("UPDATE media_item SET scrape_state='manual' WHERE id=?", (item_id,))
            out["rescraped"] = False
            out["error"] = str(e)
    return out


@router.get("/admin/manual")
def admin_manual(_: str = Depends(admin_only)):
    """手動修正過的條目。重掃之後可以一眼檢查哪幾筆是自己修的。"""
    rows = db.q("""SELECT id, kind, title, year, tmdb_id, updated_at,
                          (SELECT COUNT(*) FROM media_file f WHERE f.item_id=media_item.id) files
                   FROM media_item WHERE scrape_state='manual'
                   ORDER BY updated_at DESC""")
    return {"items": [db.row_to_dict(r) for r in rows]}


@router.post("/rescrape/{item_id}")
def rescrape(item_id: int, tmdb_id: Optional[int] = None, title: Optional[str] = None,
             _: str = Depends(admin_only)):
    row = db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "找不到條目")
    item = dict(row)
    if title:
        # sort_title 要跟著改，否則排序還是用舊名字（PATCH /items 那邊同樣的理由）
        db.execute("UPDATE media_item SET title=?, sort_title=? WHERE id=?",
                   (title, title.lower(), item_id))
        item["title"] = title
    if tmdb_id:
        from ..scraper import normalize_details
        detail = tmdb.details(item["kind"], tmdb_id)
        if not detail:
            raise HTTPException(404, "TMDB 查無此 ID")
        n = normalize_details(item["kind"], detail)
        db.execute(
            """UPDATE media_item SET title=?, original_title=?, sort_title=?, year=?, tmdb_id=?,
                   overview=?, poster=?, backdrop=?, rating=?, runtime=?, genres=?, cast_json=?,
                   scrape_state='manual', updated_at=? WHERE id=?""",
            (n["title"], n["original_title"], (n["title"] or "").lower(), n["year"], n["tmdb_id"],
             n["overview"], tmdb.download_image(n["poster_path"], "w500"),
             tmdb.download_image(n["backdrop_path"], "w1280"), n["rating"], n["runtime"],
             json.dumps(n["genres"], ensure_ascii=False), json.dumps(n["cast"], ensure_ascii=False),
             db.now_i(), item_id))
        # **guess_key 要跟著年份重算。** 上面那句 UPDATE 把 year 換成 TMDB 的年份，
        # 而電影的 guess_key 是 `movie::片名::年份` —— 不重算的話，下一次掃描
        # 會用新算出來的 key 找不到這個條目、再建一個新的，使用者手動指定的
        # 那一筆變孤兒被清掉。症狀是「我修好的片隔天變成兩筆，然後我改的那筆不見了」。
        nk = _rekey(item.get("guess_key"), item["kind"], n["year"])
        if nk and nk != item.get("guess_key"):
            db.execute("UPDATE media_item SET guess_key=? WHERE id=?", (nk, item_id))
        return {"ok": True}
    db.execute("UPDATE media_item SET scrape_state='pending' WHERE id=?", (item_id,))
    try:
        scanner._scrape_one(dict(db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@router.post("/items/{item_id}/skip")
def item_skip(item_id: int, undo: bool = Query(default=False), _: str = Depends(admin_only)):
    """把條目標成「不需要 metadata」（或取消）。

    在這之前，「刮不到」的條目沒有出路：manual 要先成功指定一筆正確資料才拿得到
    （PATCH /items 與 rescrape?tmdb_id= 都是成功之後才標 manual），
    所以查無資料的片只能永遠停在 failed，每次掃描重打一次必然失敗的 API，
    並且永遠佔著「未刮削」那個數字。

    用 skip 而不是 manual 是因為兩者語意不同：manual 是「我已經填好正確答案」，
    skip 是「這東西根本不需要答案」。混在一起的話 /admin/manual 那張
    「哪幾筆是我自己修的」清單就沒得看了。
    """
    item = db.q1("SELECT id, scrape_state FROM media_item WHERE id=?", (item_id,))
    if not item:
        raise HTTPException(404, "找不到條目")
    # 取消時回到 pending 而不是 failed —— 使用者的意思是「再試一次」。
    state = "pending" if undo else "skip"
    db.execute("UPDATE media_item SET scrape_state=?, updated_at=? WHERE id=?",
               (state, db.now_i(), item_id))
    return {"ok": True, "scrape_state": state}


@router.get("/tmdb/search")
def tmdb_search(kind: str, q: str, year: Optional[int] = None, _: str = Depends(admin_only)):
    if not tmdb.enabled:
        raise HTTPException(400, "未設定 TMDB_API_KEY")
    endpoint = "/search/movie" if kind == "movie" else "/search/tv"
    params: Dict[str, Any] = {"query": q, "include_adult": "false"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    data = tmdb._get(endpoint, **params) or {}
    return {"results": [{
        "id": r.get("id"),
        "title": r.get("title") or r.get("name"),
        "original_title": r.get("original_title") or r.get("original_name"),
        "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
        "overview": r.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w200{r['poster_path']}" if r.get("poster_path") else None,
    } for r in (data.get("results") or [])[:20]]}


@router.post("/probe/{file_id}")
def reprobe(file_id: int, _: str = Depends(admin_only)):
    row = db.q1("SELECT id, ext, item_id FROM media_file WHERE id=?", (file_id,))
    if not row:
        raise HTTPException(404, "找不到檔案")
    scanner._probe_one(dict(row))
    r = db.q1("SELECT probe_state, probe_error, play_mode, duration FROM media_file WHERE id=?", (file_id,))
    return dict(r)


# ------------------------------- 相片庫 -------------------------------
@router.get("/photos")
def photos(request: Request, folder: Optional[str] = None, q: Optional[str] = None,
           sort: str = "taken", page: int = 1, page_size: int = 60):
    """相片列表。folder 給了就只看那個資料夾。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    where, params = ["probe_state='ok'"], []
    af, aa = acl.filter_sql(request, "folder")
    if af:
        where.append(af)
        params += aa
    if folder:
        where.append("folder=?")
        params.append(folder)
    if q:
        where.append("(filename LIKE ? OR folder LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql_where = " WHERE " + " AND ".join(where)
    # 排序走白名單，使用者輸入不會進到 SQL
    # sort_ts 是寫入時算好的排序鍵。原本是 COALESCE(taken_at, mtime)，
    # 而那兩欄是格式不同的字串 —— 字串比大小的結果是「同一天裡只有 mtime 的
    # 照片永遠排在有 EXIF 的前面」，因為 ' '(0x20) < 'T'(0x54)。
    # **每一種排序都要有決定性的收尾（`id`）。**沒有的話，並列的那幾筆在
    # 兩次查詢之間的順序不保證一樣 —— 而分頁是 LIMIT/OFFSET，第 2 頁是重新
    # 查一次。順序一變，交界處就會有相片重複出現或整個被跳過。
    # 這座片庫實測有 740 組同檔名、325 組同大小，所以這不是理論風險。
    # 無限捲動（P-2）會把這個機率放大到每次瀏覽都踩得到。
    order = {"taken": "sort_ts DESC, id DESC",
             "name": "filename COLLATE NOCASE, id DESC",
             "size": "size DESC, id DESC",
             "added": "added_at DESC, id DESC"}.get(sort, "sort_ts DESC, id DESC")
    total = db.q1(f"SELECT COUNT(*) c FROM photo{sql_where}", tuple(params))["c"]
    rows = db.q(f"""SELECT id, folder, filename, ext, size, mtime, width, height,
                           taken_at, sort_ts, thumb
                    FROM photo{sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
                tuple(params) + (page_size, (page - 1) * page_size))
    return {"total": total, "page": page, "page_size": page_size,
            # 前端的檢視器要知道 preview 的長邊才知道放大到多少該換原圖。
            # 寫死在 app.js 的話，後台改了設定它就不知道。
            "preview_px": int(getattr(settings, "photo_preview_px", 0) or 1920),
            "items": [db.row_to_dict(r) for r in rows]}


@router.get("/photos/folders")
def photo_folders(request: Request):
    """有相片的資料夾清單，附張數與代表縮圖。"""
    af, aa = acl.filter_sql(request, "folder")
    rows = db.q(f"""SELECT folder, COUNT(*) c, MAX(sort_ts) latest,
                          MIN(sort_ts) earliest,
                          SUM(size) bytes,
                          (SELECT thumb FROM photo p2
                            WHERE p2.folder = p.folder AND p2.thumb IS NOT NULL
                            ORDER BY p2.sort_ts DESC LIMIT 1) cover
                   FROM photo p WHERE probe_state='ok'{(" AND " + af) if af else ""}
                   GROUP BY folder ORDER BY latest DESC""", aa)
    return {"items": [db.row_to_dict(r) for r in rows]}


@router.get("/photos/stats")
def photo_stats(request: Request):
    af, aa = acl.filter_sql(request, "folder")
    a = (" AND " + af) if af else ""

    def c(sql):
        return db.q1(sql, aa)["c"]
    return {
        "total": c("SELECT COUNT(*) c FROM photo WHERE probe_state='ok'" + a),
        "pending": c("SELECT COUNT(*) c FROM photo WHERE probe_state='pending'" + a),
        "failed": c("SELECT COUNT(*) c FROM photo WHERE probe_state='failed'" + a),
        "folders": c("SELECT COUNT(DISTINCT folder) c FROM photo WHERE probe_state='ok'" + a),
        "bytes": db.q1("SELECT COALESCE(SUM(size),0) c FROM photo"
                       + ((" WHERE " + af) if af else ""), aa)["c"],
    }


@router.get("/photos/{photo_id}")
def photo_detail(photo_id: int, request: Request):
    r = db.q1("SELECT * FROM photo WHERE id=?", (photo_id,))
    if not r:
        raise HTTPException(404, "找不到相片")
    acl.assert_can_read(request, r["folder"])
    d = db.row_to_dict(r) or {}
    d["download_url"] = f"/api/photo/{photo_id}/full"
    d["thumb_url"] = f"/api/photo/{photo_id}/thumb.jpg" if d.get("thumb") else None
    return d


# ------------------------------- 文件庫（PDF） -------------------------------
@router.get("/documents")
def documents(request: Request, folder: Optional[str] = None, q: Optional[str] = None,
              sort: str = "name", page: int = 1, page_size: int = 60,
              subtree: bool = False):
    """文件列表。形狀跟 /photos 一致，前端才能共用分頁與資料夾那組元件。

    subtree=true 時 folder 當「子樹根」用，會連同底下所有層一起列。
    資料夾樹要能選中上層看見底下全部，靠的就是這個；預設仍是精確比對，
    舊的呼叫方（與 acl_test）行為不變。
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    where, params = ["probe_state='ok'"], []
    af, aa = acl.filter_sql(request, "folder")
    if af:
        where.append(af)
        params += aa
    if folder:
        if subtree:
            sf, sa = acl.subtree_sql("folder", folder)
            if sf:
                where.append(sf)
                params += sa
        else:
            where.append("folder=?")
            params.append(folder)
    if q:
        where.append("(filename LIKE ? OR folder LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql_where = " WHERE " + " AND ".join(where)
    # 白名單排序。文件的預設是檔名而不是時間 —— 一套規格書或一系列講義
    # 的閱讀順序是編號，不是誰先被下載。
    # 收尾一律加 id，理由同 photos()。文件庫最容易並列 —— 同一份規格書的
    # 不同版本常常同名（放在不同資料夾），而 filename 排序只比檔名。
    order = {"name": "filename COLLATE NOCASE, id DESC",
             "time": "sort_ts DESC, id DESC",
             "size": "size DESC, id DESC",
             "added": "added_at DESC, id DESC"}.get(sort,
                                                    "filename COLLATE NOCASE, id DESC")
    total = db.q1(f"SELECT COUNT(*) c FROM document{sql_where}", tuple(params))["c"]
    rows = db.q(f"""SELECT id, folder, filename, ext, size, mtime, sort_ts, pages
                    FROM document{sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
                tuple(params) + (page_size, (page - 1) * page_size))
    return {"total": total, "page": page, "page_size": page_size,
            "items": [db.row_to_dict(r) for r in rows]}


@router.get("/documents/folders")
def document_folders(request: Request, prefix: Optional[str] = None,
                     flat: bool = False):
    """資料夾導覽。預設回「prefix 底下的下一層節點」，不是整棵樹。

    這裡本來是把所有資料夾一次攤平回去 —— 實測有 452 個、97% 落在第 7～8 層，
    前端一次畫成 452 個 chip，而且名字只顯示最後一層，於是畫面上全是分不出來的
    「PDF」「教用」。改成一次只回一層，深度就不再是問題。

    flat=true 保留舊的扁平清單（相片牆與 acl_test 還在用那個形狀）。
    """
    af, aa = acl.filter_sql(request, "folder")
    if flat:
        rows = db.q(f"""SELECT folder, COUNT(*) c, SUM(size) bytes, MAX(sort_ts) latest
                       FROM document WHERE probe_state='ok'{(" AND " + af) if af else ""}
                       GROUP BY folder ORDER BY folder COLLATE NOCASE""", aa)
        return {"items": [db.row_to_dict(r) for r in rows]}

    where, params = ["probe_state='ok'"], list(aa)
    if af:
        where.append(af)
    sf, sa = acl.subtree_sql("folder", prefix or "")
    if sf:
        where.append(sf)
        params += sa
    rows = db.q(f"""SELECT folder, COUNT(*) c, SUM(size) bytes, MAX(sort_ts) latest
                   FROM document WHERE {" AND ".join(where)}
                   GROUP BY folder""", tuple(params))
    out = _folder_level(rows, prefix or "")
    # 沒指定 prefix 時，若最外層只有一個節點就自動往下走到第一個分岔為止。
    # 這個庫的根就是這種形狀（/媒體資料庫 → Book → …），不跳過的話使用者
    # 開啟文件牆看到的第一個畫面是「一個選項」，等於白點一次。
    if prefix is None:
        for _ in range(8):        # 上限純粹是防呆，避免資料異常時在這裡空轉
            if len(out["items"]) != 1 or not out["items"][0]["has_children"]:
                break
            out = _folder_level(rows, out["items"][0]["folder"])
    return out


CHAIN_MAX = 2          # 單鏈穿透一次最多併幾段（見 _folder_level 的說明）


def _folder_level(rows, prefix: str) -> Dict[str, Any]:
    """把子樹裡的資料夾統計，聚合成「下一層」的節點清單。

    每個節點的 c 是**遞迴總數**（含所有子孫）而不是本層的數量 —— 父節點的
    份數若只算本層，這個庫裡幾乎每個父節點都會顯示 0，導覽時完全看不出
    哪一條路底下有東西。

    單鏈穿透：一個節點如果只有一條路可走（唯一子節點、且自己本層沒有檔案），
    就把那一段一路併進顯示名稱，例如「資料庫 / Book / 國小講義」。
    不這樣做的話，這個庫的前三層每層都只有一個選項，等於強迫使用者連點三次
    才看得到第一個真正的分岔。
    """
    base = acl._norm(prefix) if prefix else "/"
    blen = len(base)

    # 先把每一列切成「相對於 base 的路徑片段」，之後全部在這份清單上算，
    # 不再回頭碰 SQL。這個庫只有 452 個資料夾，整批放在記憶體裡很便宜。
    split: List[Any] = []
    for r in rows:
        folder = r["folder"]
        if not folder.startswith(base):
            continue
        segs = [s for s in folder[blen:].split("/") if s]
        if segs:                          # 沒有 segs 的是前綴自己那一列，不是子節點
            split.append((segs, r["c"] or 0, r["bytes"] or 0, r["latest"]))

    # seg -> 這個子樹的統計。self 是「剛好停在這一層」的份數，
    # kids 是再下一段的名字（用來判斷還有沒有分岔）。
    level: Dict[str, Dict[str, Any]] = {}
    for segs, c, b, latest in split:
        n = level.setdefault(segs[0], {"c": 0, "bytes": 0, "latest": None,
                                       "self": 0, "kids": set()})
        n["c"] += c
        n["bytes"] += b
        if latest is not None:
            n["latest"] = max(n["latest"] or 0, latest)
        if len(segs) == 1:
            n["self"] += c
        else:
            n["kids"].add(segs[1])

    items = []
    for seg, n in level.items():
        chain = [seg]                     # 已經併進來的路徑片段
        # 單鏈穿透：只有一條路可走就一路往下併。每一步都在 split 上重算
        # 下一段的分岔情形，不遞迴、不重查 DB。
        #
        # 最多併 CHAIN_MAX 段：這個庫的資料夾名字本來就長（「115學年上學期 國小
        # 康軒版 課習教PDF(含習作、課本、教師手冊)…」），無限併下去會做出一個
        # 塞滿整個側欄、還是看不出重點的節點 —— 那只是把「太多選項」換成
        # 「太長的一個選項」。併不完的部分留給下一次展開。
        while len(chain) < CHAIN_MAX and n["self"] == 0 and len(n["kids"]) == 1:
            chain.append(next(iter(n["kids"])))
            d = len(chain)
            selfc, kids = 0, set()
            for segs, c, _b, _l in split:
                if len(segs) < d or segs[:d] != chain:
                    continue
                if len(segs) == d:
                    selfc += c
                else:
                    kids.add(segs[d])
            n = {"self": selfc, "kids": kids}
        items.append({"folder": base + "/".join(chain), "name": " / ".join(chain),
                      "c": level[seg]["c"], "bytes": level[seg]["bytes"],
                      "latest": level[seg]["latest"],
                      # 併完之後還有分岔（或併不動但底下還有層）才算有子節點
                      "has_children": bool(n["kids"])})
    items.sort(key=lambda x: x["name"].casefold())
    return {"items": items, "prefix": prefix or ""}


@router.get("/documents/{doc_id}")
def document_detail(doc_id: int, request: Request):
    r = db.q1("SELECT * FROM document WHERE id=?", (doc_id,))
    if not r:
        raise HTTPException(404, "找不到文件")
    acl.assert_can_read(request, r["folder"])
    d = db.row_to_dict(r) or {}
    d["file_url"] = f"/api/document/{doc_id}/file.pdf"
    return d


# ------------------------- 受限資料夾（L，限管理員） -------------------------
class RuleIn(BaseModel):
    prefix: str
    note: str = ""


class GrantIn(BaseModel):
    user_ids: List[int] = []


@router.get("/folders/acl")
def folder_acl(_: str = Depends(admin_only)):
    """受限規則 ＋ 實際掃到的資料夾清單 ＋ 可以授權的帳號。

    資料夾清單是**掃出來的**，不是讓人手打的。手打就會拼錯，
    而拼錯的規則等於沒有保護，畫面上還會顯示「已設定」—— 沒有人會發現。
    """
    rules = acl.rules()
    id_to_name = {}
    for u in users.listing():
        id_to_name[u["id"]] = u.get("display_name") or u.get("email") or f"#{u['id']}"
    return {
        "rules": [{**r, "user_names": [id_to_name.get(i, f"#{i}") for i in r["user_ids"]]}
                  for r in rules],
        "folders": acl.folder_counts(),
        # 只有 approved 的帳號可以被授權：pending／rejected／disabled 的人
        # 本來就進不來，出現在授權清單上只會讓人以為他有權限
        # `role` 是資料庫的字彙（owner / viewer），而畫面上講的是「管理員」。
        # 多回一個明確的布林，前端就不必知道這件事 ——
        # 之前 acl 的授權清單把管理員跟一般帳號混在一起顯示成未勾選的框，
        # 那看起來像「這個人看不到」，但管理員是靠角色看得到的（acl.can_read
        # 的第一句就是 is_admin），**畫面在說謊**。
        "users": [{"id": u["id"], "name": id_to_name[u["id"]], "email": u.get("email"),
                   "role": u.get("role"), "is_admin": u.get("role") == users.OWNER}
                  for u in users.listing() if u.get("status") == "approved"],
        # 密碼登入沒有 uid，所以無法被授權 —— 這件事要在介面上講清楚
        "password_login_note": ("密碼登入沒有帳號身分，因此一律看不到受限資料夾。"
                                "需要讓某個人看，就讓他用 Google 帳號登入。"),
    }


@router.post("/folders/acl")
def folder_acl_add(body: RuleIn, request: Request, _: str = Depends(admin_only)):
    try:
        r = acl.add_rule(body.prefix, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record("folder_rule_add", role=auth.role_of(request),
                 ip=auth.client_ip(request.scope), loc={},
                 email=auth.user_email(request), detail=body.prefix)
    return r


@router.delete("/folders/acl/{rule_id}")
def folder_acl_remove(rule_id: int, request: Request, _: str = Depends(admin_only)):
    row = db.q1("SELECT prefix FROM folder_rule WHERE id=?", (rule_id,))
    if not row:
        raise HTTPException(404, "沒有這條規則")
    acl.remove_rule(rule_id)
    audit.record("folder_rule_remove", role=auth.role_of(request),
                 ip=auth.client_ip(request.scope), loc={},
                 email=auth.user_email(request), detail=row["prefix"])
    return {"ok": True}


@router.put("/folders/acl/{rule_id}/grants")
def folder_acl_grants(rule_id: int, body: GrantIn, request: Request,
                      _: str = Depends(admin_only)):
    row = db.q1("SELECT prefix FROM folder_rule WHERE id=?", (rule_id,))
    if not row:
        raise HTTPException(404, "沒有這條規則")
    ok = {u["id"] for u in users.listing() if u.get("status") == "approved"}
    bad = [u for u in body.user_ids if u not in ok]
    if bad:
        raise HTTPException(400, f"這些帳號不存在或未核准：{bad}")
    acl.set_grants(rule_id, body.user_ids)
    audit.record("folder_grant_set", role=auth.role_of(request),
                 ip=auth.client_ip(request.scope), loc={},
                 email=auth.user_email(request),
                 detail=f"{row['prefix']} → {sorted(body.user_ids)}")
    return {"ok": True, "user_ids": sorted(body.user_ids)}


@router.get("/audit/logins")
def audit_logins(limit: int = 25, offset: int = 0, event: str = "",
                 _: str = Depends(admin_only)):
    """最近的登入紀錄，含來源 IP 與 Cloudflare 判斷的位置。

    伺服器端分頁。一次把幾千筆全部送到瀏覽器再讓它自己切，
    在手機上就是一段可見的卡頓，而且那些資料多半沒有人會看。
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, args = "", []
    if event:
        where = " WHERE event=?"
        args.append(event)
    total = db.q1(f"SELECT COUNT(*) c FROM login_audit{where}", tuple(args))["c"]
    rows = db.q(f"""SELECT at, event, role, ip, country, region, city, timezone,
                           latitude, longitude, user_agent, email, user_id, detail
                    FROM login_audit{where} ORDER BY at DESC LIMIT ? OFFSET ?""",
                tuple(args) + (limit, offset))
    out = []
    for r in rows:
        d = db.row_to_dict(r) or {}
        d["where"] = geo.describe(d)
        out.append(d)
    return {"items": out, "total": total, "limit": limit, "offset": offset,
            # 位置標頭要在 Cloudflare 後台開 Managed Transforms 才會送
            "geo_available": any(x.get("country") for x in out)}


@router.post("/admin/ftp-probe")
async def admin_ftp_probe(max_n: int = Query(default=12, ge=2, le=64),
                          speed: bool = Query(default=True),
                          _: str = Depends(admin_only)):
    """量 FTP 的併發上限與有效併發。

    會開好幾條連線並下載一小段資料，可能要跑一分鐘以上，所以丟到執行緒去跑，
    不要卡住整個事件迴圈（卡住的話所有人的播放都會停）。
    """
    from .. import ftpprobe

    def run():
        limit, first_fail, errs = ftpprobe.find_limit(max_n, float(settings.ftp_timeout or 20))
        sp = {}
        picked = None
        if speed and limit >= 2:
            picked = ftpprobe._pick_file(float(settings.ftp_timeout or 20))
            if picked:
                levels = sorted({n for n in (1, 2, 3, 4, 6, 8) if n <= limit})
                sp = ftpprobe.measure_speed(levels, float(settings.ftp_timeout or 20), picked[0])
        value, notes = ftpprobe.recommend(limit, first_fail, sp)
        return {"limit": limit, "firstFail": first_fail, "errors": errs[:5],
                "speed": {str(k): {"mbps": v[0], "rounds": v[1]} for k, v in sp.items()},
                "sample": picked[0] if picked else None,
                "recommend": value, "notes": notes}

    return await anyio.to_thread.run_sync(run)


@router.get("/admin/overview")
def admin_overview(_: str = Depends(admin_only)):
    """後台總覽。一頁看完「這台機器現在好不好」。"""
    import platform
    import shutil as _sh
    from ..config import DATA_DIR

    def c(sql, args=()):
        return db.q1(sql, args)["c"]

    try:
        du = _sh.disk_usage(str(DATA_DIR))
        disk = {"total": du.total, "used": du.used, "free": du.free,
                "path": str(DATA_DIR),
                "percent": round(du.used * 100 / du.total, 1) if du.total else None}
    except OSError as e:
        disk = {"error": str(e)}

    # 快取會長到好幾 GB，而它跟剩餘空間是同一件事的兩面
    cache = 0
    try:
        for f in CACHE_DIR.rglob("*"):
            if f.is_file():
                cache += f.stat().st_size
    except OSError:
        pass

    return {
        "startedAt": paramstore.STARTED_AT,
        "uptime": time.time() - paramstore.STARTED_AT,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "counts": {
            "movies": c("SELECT COUNT(*) c FROM media_item WHERE kind='movie'"),
            "shows": c("SELECT COUNT(*) c FROM media_item WHERE kind='tv'"),
            "javs": c("SELECT COUNT(*) c FROM media_item WHERE kind='jav'"),
            "homes": c("SELECT COUNT(*) c FROM media_item WHERE kind='home'"),
            "episodes": c("SELECT COUNT(*) c FROM episode"),
            "files": c("SELECT COUNT(*) c FROM media_file"),
            "photos": c("SELECT COUNT(*) c FROM photo"),
            "documents": c("SELECT COUNT(*) c FROM document"),
            "size": db.q1("SELECT COALESCE(SUM(size),0) c FROM media_file")["c"],
            "unscraped": c(f"SELECT COUNT(*) c FROM media_item WHERE {_UNSCRAPED_SQL}"),
            "no_metadata": c("SELECT COUNT(*) c FROM media_item WHERE scrape_state!='ok'"),
            "failed": c("SELECT COUNT(*) c FROM media_file WHERE probe_state='failed'"),
        },
        "disk": disk,
        "cacheBytes": cache,
        "orphans": purge.counts(),
        "pendingUsers": users.counts().get("pending", 0) if settings.google_enabled else 0,
        "scan": scanner.status_dict(),
        "recent": [db.row_to_dict(r) for r in db.q(
            "SELECT at, event, role, ip, email FROM login_audit ORDER BY at DESC LIMIT 10")],
        "configDrift": [d["key"] for d in paramstore.drift()],
        "needsRestart": paramstore.needs_restart(),
    }


@router.get("/admin/problems")
def admin_problems(_: str = Depends(admin_only)):
    """未刮削與探測失敗的清單。總覽只給數字，這裡給名字。"""
    return {
        # 只列真的要處理的（同 _UNSCRAPED_SQL）。之前是 `!='ok'`，於是這張
        # 表會被 127 筆手機錄影塞爆，加了操作按鈕也找不到該按哪一個。
        "unscraped": [db.row_to_dict(r) for r in db.q(
            f"SELECT id, kind, title, year, scrape_state, scrape_attempts, scrape_last_at "
            f"FROM media_item WHERE {_UNSCRAPED_SQL} ORDER BY added_at DESC LIMIT 200")],
        # 前端原本拿 list 長度當標題數字，超過 200 就跟總覽卡片對不上。
        "unscraped_total": db.q1(
            f"SELECT COUNT(*) c FROM media_item WHERE {_UNSCRAPED_SQL}")["c"],
        "failed": [db.row_to_dict(r) for r in db.q(
            "SELECT f.id, f.filename, f.ftp_path, f.probe_error, i.title "
            "FROM media_file f LEFT JOIN media_item i ON i.id=f.item_id "
            "WHERE f.probe_state='failed' ORDER BY f.id DESC LIMIT 200")],
        # 上階（remux）下線的檔案。**這個清單是把兩階 HLS 開起來的條件之一** ——
        # 一段 remux 失敗會讓整個檔案的上階下線並改發單階，播放不會中斷，
        # 所以除了這裡看得到，不會有任何地方提到它（J 章第 0 層）。
        "remuxOffline": [db.row_to_dict(r) for r in db.q(
            "SELECT f.id, f.filename, f.ftp_path, f.remux_error, i.title "
            "FROM media_file f LEFT JOIN media_item i ON i.id=f.item_id "
            "WHERE f.remux_state='failed' ORDER BY f.id DESC LIMIT 200")],
        # 邊界表掃描失敗的檔案：沒有邊界表就沒有上階，但不影響下階。
        "keyframeFailed": [db.row_to_dict(r) for r in db.q(
            "SELECT f.id, f.filename, f.ftp_path, f.kf_error, i.title "
            "FROM media_file f LEFT JOIN media_item i ON i.id=f.item_id "
            "WHERE f.kf_state='failed' ORDER BY f.id DESC LIMIT 200")],
    }


@router.get("/diagnostics")
def diagnostics(_: str = Depends(admin_only)):
    """一次看完所有外部相依：ffmpeg / ffprobe / FTP / TMDB。"""
    user_db = {"store": users.store_name(), "detail": users.describe(), "ok": True}
    if settings.mssql_configured:
        st = mssql.ping()
        user_db.update(ok=st["ok"], missing=st["missing"], error=st["error"],
                       version=st["version"])
    media.reset_tool_cache()          # 重新找一次，使用者剛裝好 ffmpeg 也能立刻抓到
    tools = media.tool_status()
    ftp = ftpclient.test_connection()
    problems: List[str] = []
    for kind, t in tools.items():
        if not t["ok"]:
            problems.append(t.get("hint") or f"{kind} 無法執行")
    degraded = bool(tools["ffprobe"].get("fallback"))
    if not ftp.get("ok"):
        problems.append(f"FTP 連線失敗：{ftp.get('error')}")
    if not tmdb.enabled:
        problems.append("未設定 TMDB_API_KEY，不會有海報與簡介（不影響播放）")
    if not user_db["ok"]:
        # 這一項壞掉代表沒有人登得進來，是所有問題裡最該先看到的
        problems.insert(0, "使用者資料庫不可用："
                        + (user_db.get("error")
                           or "缺少 " + "、".join(user_db.get("missing") or []))
                        + "　→ 跑 db\\檢查連線.bat 逐項確認")
    failed = db.q1("SELECT COUNT(*) c FROM media_file WHERE probe_state='failed'")["c"]
    sample = db.q1("SELECT probe_error FROM media_file WHERE probe_state='failed' "
                   "AND probe_error IS NOT NULL LIMIT 1")
    return {
        "ok": not problems,
        "degraded": degraded,
        "tools": tools,
        "hwaccel": media.resolve_hwaccel(),
        "hwaccel_probe": media.hwaccel_probe_report(),
        "ftp": ftp,
        "tmdb_enabled": tmdb.enabled,
        "probe_failed": failed,
        "probe_error_sample": sample["probe_error"] if sample else None,
        "user_db": user_db,
        "problems": problems,
    }


@router.get("/diagnostics/encoder/{name}")
def diagnostics_encoder(name: str, verbosity: str = "verbose", _: str = Depends(admin_only)):
    """單一硬體編碼器的深度診斷，會回傳 ffmpeg 的完整訊息。"""
    enc = media.ENCODER_OF.get(name, name)
    if not enc.startswith(("h264_", "hevc_", "libx")):
        raise HTTPException(400, "不支援的編碼器名稱")
    if verbosity not in ("error", "warning", "info", "verbose", "debug"):
        raise HTTPException(400, "verbosity 不合法")
    return media.deep_probe_encoder(enc, verbosity)


@router.get("/diagnostics/bench/{file_id}")
def diagnostics_bench(file_id: int, start: float = 60.0, seconds: float = 6.0,
                      height: Optional[int] = 1080, _: str = Depends(admin_only)):
    """同一段影片跑多種設定並計時，用來找出是哪個設定拖慢的。"""
    # 這些數字會變成 ffmpeg 的 -ss / -t。不收斂的話，seconds=99999 就是一個
    # 請求鎖住 CPU 半小時，而且可以無限併發。
    seconds = max(1.0, min(float(seconds), 30.0))
    start = max(0.0, min(float(start), 86400.0))
    if height is not None:
        height = max(144, min(int(height), 4320))
    try:
        return media.bench_transcode(file_id, start, seconds, height)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/diagnostics/gpu")
def diagnostics_gpu(_: str = Depends(admin_only)):
    """顯卡型號與驅動版本（問 nvidia-smi）。"""
    return media.gpu_info()


@router.post("/diagnostics/recheck")
def diagnostics_recheck(_: str = Depends(admin_only)):
    """重新實測硬體編碼器（換了顯卡驅動或 ffmpeg 之後用，不必重啟服務）。"""
    media.reset_encoder_cache()
    return media.hwaccel_probe_report()


@router.get("/ftp/test")
def ftp_test(_: str = Depends(admin_only)):
    return ftpclient.test_connection()


def _within_library(path: str) -> bool:
    """瀏覽範圍限制在 LIBRARY_ROOTS 之內。

    沒有這道限制的話，任何登入者都能把 FTP 帳號權限所及的整台機器目錄
    列一遍（備份、家目錄…）。設定成根目錄的人本來就是全開，那是他的選擇。
    """
    norm = "/" + path.strip("/")
    for r in settings.library_roots:
        root = "/" + (r.path or "/").strip("/")
        if root == "/" or norm == root or norm.startswith(root.rstrip("/") + "/"):
            return True
    return False


@router.get("/ftp/browse")
def ftp_browse(path: str = "/", _: str = Depends(admin_only)):
    # FTP 指令是用換行分隔的，路徑裡有控制字元就可能是想注入指令。
    # ftplib 目前會擋，但那是它的好心，不該當成我們的防線。
    if any(c in path for c in ("\r", "\n", "\x00")):
        raise HTTPException(400, "路徑含非法字元")
    if not _within_library(path):
        raise HTTPException(403, "只能瀏覽片庫設定的目錄")
    try:
        entries = ftpclient.list_dir(path)
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"path": path, "entries": [
        {"name": e.name, "path": e.path, "is_dir": e.is_dir, "size": e.size, "mtime": e.mtime}
        for e in sorted(entries, key=lambda x: (not x.is_dir, x.name.lower()))]}


@router.post("/cache/clear")
def cache_clear(file_id: Optional[int] = None, _: str = Depends(admin_only)):
    hls.clear_cache(file_id)
    media.clear_subtitle_cache(file_id)
    return {"ok": True}


@router.get("/image/{name}")
def image(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法檔名")
    p = IMAGE_DIR / name
    if not p.exists():
        raise HTTPException(404, "找不到圖片")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=604800"})
