"""媒體庫 API：清單、詳情、搜尋、掃描控制、播放資訊、續播進度。"""
from __future__ import annotations

import json
import posixpath
from typing import Any, Dict, List, Optional

from fastapi import Depends, APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import auth, db, ftpclient, hls, media, scanner
from ..config import IMAGE_DIR, settings
from ..scraper import tmdb

router = APIRouter()


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
    return {"role": role, "is_admin": role == auth.ADMIN,
            "auth_enabled": settings.auth_enabled}


@router.get("/library")
def library(
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
    if kind in ("movie", "tv"):
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
        where.append("i.scrape_state!='ok'")

    order = {
        "added": "i.added_at DESC",
        "title": "i.sort_title ASC",
        "year": "COALESCE(i.year,0) DESC, i.title ASC",
        "rating": "COALESCE(i.rating,0) DESC, i.title ASC",
    }.get(sort, "i.added_at DESC")

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
def genres():
    counts: Dict[str, int] = {}
    for r in db.q("SELECT genres FROM media_item WHERE genres IS NOT NULL"):
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
    out = {
        "movies": c("SELECT COUNT(*) c FROM media_item WHERE kind='movie'"),
        "shows": c("SELECT COUNT(*) c FROM media_item WHERE kind='tv'"),
        "files": c("SELECT COUNT(*) c FROM media_file"),
        "unscraped": c("SELECT COUNT(*) c FROM media_item WHERE scrape_state!='ok'"),
        "unprobed": c("SELECT COUNT(*) c FROM media_file WHERE probe_state!='ok'"),
        "total_size": db.q1("SELECT COALESCE(SUM(size),0) c FROM media_file")["c"],
        "tmdb_enabled": tmdb.enabled,
    }
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
def item_detail(item_id: int):
    row = db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "找不到條目")
    item = _item_row(row)
    item["cast"] = db.row_to_dict(row).get("cast_json") or []

    files = [db.row_to_dict(r) for r in db.q(
        """SELECT f.*, p.position, p.duration AS watched_duration, p.finished
           FROM media_file f LEFT JOIN play_state p ON p.file_id=f.id
           WHERE f.item_id=? ORDER BY f.filename""", (item_id,))]
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
            })

    state = db.q1("SELECT position, finished FROM play_state WHERE file_id=?", (file_id,))

    # 區網 vs 遠端：遠端預設降解析度並鎖住峰值位元率，
    # 因為家用光纖的「上傳」頻寬通常遠小於下載。
    # 一律走 auth.client_ip：它會判斷 proxy 標頭可不可信。
    # 自己在這裡解 X-Forwarded-For 的話，只要送一個內網位址就能騙過
    # 「遠端降畫質」的保護，把上傳頻寬吃光。
    ip = auth.client_ip(request.scope)
    remote = not auth.is_local_network(ip)
    if remote:
        default_h = min(settings.remote_max_height or 720, settings.max_height or 1080)
        default_b = settings.remote_bitrate_kbps
    else:
        default_h = settings.max_height
        default_b = settings.lan_bitrate_kbps
    chosen_h = h or default_h
    chosen_b = default_b if not h else (0 if h >= (settings.max_height or 1080) else default_b)
    profile = hls.profile_key(chosen_h, chosen_a, chosen_b)

    src_h = f.get("height") or 1080
    ladder = [x for x in (2160, 1440, 1080, 720, 480, 360) if x <= max(src_h, 360)]
    if not ladder:
        ladder = [src_h]
    return {
        "file_id": file_id,
        "title": f.get("item_title") or f.get("filename"),
        "subtitle_label": (f"S{f['season']:02d}E{f['episode']:02d} {f.get('ep_title') or ''}".strip()
                           if f.get("season") else ""),
        "filename": f.get("filename"),
        "mode": mode,
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
        "audio_tracks": audio_tracks,
        "audio_index": chosen_a if chosen_a is not None else _default_audio(audio_tracks),
        "audio_channels_out": settings.audio_channels,
        "direct_url": f"/api/stream/{file_id}",
        "hls_url": f"/api/hls/{file_id}/index.m3u8?p={profile}",
        "remote": remote,
        "client_ip": ip,
        "quality": {"height": chosen_h, "bitrate_kbps": chosen_b, "ladder": ladder,
                    "auto_reason": ("遠端連線，已自動降到 "
                                    f"{chosen_h}p / {chosen_b or '不限'} kbps" if remote else None)},
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
    """設定先照角色分開存。

    在有真正的帳號之前，至少別讓唯讀使用者把管理員的字幕大小、快轉秒數蓋掉 ——
    以前是全站共用一份。等 Google 登入接上就改成每人一份。
    """
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
    rec = {"data": p.data, "updated_at": db.now()}
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
        (p.file_id, p.position, p.duration, 1 if p.finished else 0, db.now()),
    )
    return {"ok": True}


@router.get("/continue")
def continue_watching(limit: int = 20):
    rows = db.q(
        """SELECT p.file_id, p.position, p.duration, f.filename, f.item_id, f.episode_id,
                  i.title, i.poster, i.kind, e.season, e.episode
           FROM play_state p
           JOIN media_file f ON f.id=p.file_id
           LEFT JOIN media_item i ON i.id=f.item_id
           LEFT JOIN episode e ON e.id=f.episode_id
           WHERE p.finished=0 AND p.position > 30
           ORDER BY p.updated_at DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["poster_url"] = f"/api/image/{d['poster']}" if d.get("poster") else None
        d["percent"] = round(100 * d["position"] / d["duration"], 1) if d.get("duration") else 0
        out.append(d)
    return {"items": out}


# ------------------------- 掃描 -------------------------
@router.post("/scan")
def scan_start(full: bool = Query(default=False), _: str = Depends(admin_only)):
    if not scanner.start(full=full):
        return JSONResponse({"ok": False, "message": "掃描已在進行中"}, status_code=409)
    return {"ok": True}


@router.post("/scan/cancel")
def scan_cancel(_: str = Depends(admin_only)):
    scanner.cancel()
    return {"ok": True}


@router.get("/scan/status")
def scan_status():
    return scanner.status_dict()


@router.post("/rescrape/{item_id}")
def rescrape(item_id: int, tmdb_id: Optional[int] = None, title: Optional[str] = None,
             _: str = Depends(admin_only)):
    row = db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))
    if not row:
        raise HTTPException(404, "找不到條目")
    item = dict(row)
    if title:
        db.execute("UPDATE media_item SET title=? WHERE id=?", (title, item_id))
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
             db.now(), item_id))
        return {"ok": True}
    db.execute("UPDATE media_item SET scrape_state='pending' WHERE id=?", (item_id,))
    try:
        scanner._scrape_one(dict(db.q1("SELECT * FROM media_item WHERE id=?", (item_id,))))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


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


@router.get("/diagnostics")
def diagnostics(_: str = Depends(admin_only)):
    """一次看完所有外部相依：ffmpeg / ffprobe / FTP / TMDB。"""
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
