"""串流端點：原始位元組 (Range) / HLS 播放清單與分段 / 字幕。"""
from __future__ import annotations

import logging
import posixpath
import re
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from .. import acl, auth, db, ftpclient, hls, keyframes, media, photo
from ..config import CACHE_DIR, IMAGE_DIR, settings

log = logging.getLogger("filmax.stream")
router = APIRouter()

MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "ts": "video/mp2t", "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _file_or_404(file_id: int, request: Request) -> dict:
    """取檔案列，順手做受限資料夾的判斷（L）。

    **閘門放在這裡而不是每個端點各寫一次**：這個檔案裡有七個端點會吐出真正的
    位元組（原始串流、下載、HLS 三支、兩支字幕），每一個都要判斷。
    各寫一次遲早會漏，而漏掉的表現形式是安靜的外洩，不是錯誤訊息。
    """
    row = db.q1("SELECT * FROM media_file WHERE id=?", (file_id,))
    if not row:
        raise HTTPException(404, "找不到檔案")
    acl.assert_can_read(request, row["ftp_path"])
    return db.row_to_dict(row)  # type: ignore[return-value]


@router.get("/stream/{file_id}")
def stream_raw(file_id: int, request: Request, raw: int = 0):
    """支援 HTTP Range 的原始檔串流。瀏覽器直接播放、以及 ffmpeg 讀取來源都走這裡。"""
    f = _file_or_404(file_id, request)
    path = f["ftp_path"]

    size = f.get("size") or 0
    if not size:
        try:
            size = ftpclient.file_size(path)
            db.execute("UPDATE media_file SET size=? WHERE id=?", (size, file_id))
        except ftpclient.FtpError as e:
            raise HTTPException(502, str(e))

    start, end = 0, size - 1
    rng = request.headers.get("range") or request.headers.get("Range")
    partial = False
    if rng:
        m = RANGE_RE.search(rng)
        if m:
            g1, g2 = m.group(1), m.group(2)
            if g1:
                start = int(g1)
                if g2:
                    end = min(int(g2), size - 1)
            elif g2:  # bytes=-N 取最後 N bytes
                start = max(0, size - int(g2))
            partial = True
    if start >= size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    try:
        stream = ftpclient.FtpReadStream(path, start)
    except ftpclient.FtpError as e:
        raise HTTPException(502, f"FTP 讀取失敗: {e}")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    mime = MIME.get((f.get("ext") or "").lower(), "application/octet-stream")
    return StreamingResponse(
        stream.iter_chunks(limit=length),
        status_code=206 if partial else 200,
        headers=headers,
        media_type=mime,
    )


def _disposition(name: str) -> str:
    ascii_name = re.sub(r'[^\w.\- ]', "_", name) or "download"
    quoted = quote(name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'


@router.get("/download/{file_id}")
def download(file_id: int, request: Request):
    # 唯讀角色只能看與播，不能把原始檔搬走
    if not auth.is_admin(request):
        raise HTTPException(403, "沒有下載權限")
    f = _file_or_404(file_id, request)
    try:
        size = f.get("size") or ftpclient.file_size(f["ftp_path"])
        stream = ftpclient.FtpReadStream(f["ftp_path"], 0)
    except ftpclient.FtpError as e:
        # FTP 連不上是上游的問題，回 502 才看得出是哪一段壞掉，不要一律 500
        raise HTTPException(502, str(e))
    return StreamingResponse(
        stream.iter_chunks(),
        headers={
            "Content-Length": str(size),
            # 檔名來自 FTP 目錄列表，含有引號就能操縱這個標頭的參數解析。
            # ASCII 部分先清乾淨，非 ASCII 走 RFC 6266 的 filename*。
            "Content-Disposition": _disposition(posixpath.basename(f["filename"])),
        },
        media_type="application/octet-stream",
    )


# --------------------------- HLS ---------------------------
def _duration_or_404(f: dict) -> float:
    d = f.get("duration")
    if not d or d <= 0:
        raise HTTPException(409, "尚未取得影片長度，請先在後台重新掃描/探測此檔案")
    return float(d)


@router.get("/hls/{file_id}/master.m3u8")
def hls_master(file_id: int, request: Request, h: Optional[int] = None, a: Optional[int] = None):
    f = _file_or_404(file_id, request)
    duration = _duration_or_404(f)
    # 區網發一階、遠端發整條階梯（J 章第 0／1 層）。**一律走 auth.client_ip()** ——
    # 自己在這裡解 X-Forwarded-For 的話，送一個內網位址就能讓遠端拿到
    # 只給區網的那一階（21.7 Mbps 峰值），把上傳頻寬吃光。
    remote = not auth.is_local_network(auth.client_ip(request.scope))
    # 位元率上限一定要跟 /api/play 算出同一個值，不然階梯的頂階會變成
    # 「不鎖峰值」—— REMOTE_BITRATE_KBPS 就整個失效了。
    chosen_h, chosen_b = hls.quality_policy(remote, h)
    profile = hls.profile_key(chosen_h, a, chosen_b)
    # 開播插隊：這個檔案還沒算好邊界表的話，把它排到佇列最前面。
    # **不等它** —— 一個檔案要 30 秒，等於開不起來。這一次照舊發單階，
    # 算好之後下一次開播就有上階（keyframes.request_soon 的註解寫了同一件事）。
    if settings.hls_two_rung:
        try:
            row = db.q1("SELECT kf_state FROM media_file WHERE id=?", (file_id,))
            if row and row["kf_state"] in ("pending", "failed"):
                keyframes.request_soon(file_id)
                # 佇列是掃描結束時啟動的，那條執行緒早就跑完了 ——
                # 插了隊沒人處理的話這個插隊永遠不會生效。
                keyframes.start_background()
        except Exception as e:
            log.debug("邊界表插隊失敗 file=%s: %s", file_id, e)
    # h 沒帶 = 使用者沒有手動挑畫質。這一維決定要不要多發高畫質頂階 ——
    # 手動挑過的那一檔就是他要的上限，不能自作主張在上面再加一階。
    return PlainTextResponse(hls.build_master(file_id, profile, remote=remote,
                                              duration=duration, auto=(h is None)),
                             media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{file_id}/index.m3u8")
def hls_index(file_id: int, request: Request, p: Optional[str] = None, h: Optional[int] = None, a: Optional[int] = None):
    f = _file_or_404(file_id, request)
    duration = _duration_or_404(f)
    profile = hls.normalize_profile(p) if p else hls.profile_key(h, a)
    return PlainTextResponse(hls.build_playlist(file_id, duration, profile),
                             media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{file_id}/seg-{index}.ts")
def hls_segment(file_id: int, index: int, request: Request, p: str = Query(default="")):
    f = _file_or_404(file_id, request)
    duration = _duration_or_404(f)
    profile = hls.normalize_profile(p)
    try:
        path = hls.get_segment(file_id, index, profile, duration)
    except Exception as e:
        log.warning("分段失敗 %s/%s: %s", file_id, index, e)
        raise HTTPException(500, str(e))
    return FileResponse(path, media_type="video/mp2t",
                        headers={"Cache-Control": "public, max-age=86400"})


# --------------------------- 相片 ---------------------------
@router.get("/photo/{photo_id}/thumb.jpg")
def photo_thumb(photo_id: int, request: Request):
    r = db.q1("SELECT thumb, folder FROM photo WHERE id=?", (photo_id,))
    if not r or not r["thumb"]:
        raise HTTPException(404, "沒有縮圖")
    acl.assert_can_read(request, r["folder"])
    # thumb 是我們自己產生的 ph_<id>.jpg，不是使用者輸入，但還是擋一下路徑字元
    name = str(r["thumb"])
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法檔名")
    path = IMAGE_DIR / name
    if not path.exists():
        raise HTTPException(404, "縮圖不存在")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


PHOTO_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "gif": "image/gif"}


def _photo_row(photo_id: int, request: Request) -> dict:
    r = db.q1("""SELECT ftp_path, folder, filename, size, ext, width, height, thumb
                 FROM photo WHERE id=?""", (photo_id,))
    if not r:
        raise HTTPException(404, "找不到相片")
    acl.assert_can_read(request, r["folder"])
    return db.row_to_dict(r)  # type: ignore[return-value]


def _stream_original(d: dict) -> StreamingResponse:
    """原圖直接從 FTP 串出去，不經過轉檔。"""
    mime = PHOTO_MIME.get((d.get("ext") or "").lower(), "application/octet-stream")
    try:
        stream = ftpclient.FtpReadStream(d["ftp_path"], 0)
    except ftpclient.FtpError as e:
        raise HTTPException(502, str(e))
    headers = {"Cache-Control": "public, max-age=86400"}
    if d.get("size"):
        headers["Content-Length"] = str(d["size"])
    return StreamingResponse(stream.iter_chunks(), media_type=mime, headers=headers)


@router.get("/photo/{photo_id}/full")
def photo_full(photo_id: int, request: Request):
    """原圖。"""
    return _stream_original(_photo_row(photo_id, request))


@router.get("/photo/{photo_id}/preview.jpg")
def photo_preview(photo_id: int, request: Request):
    """看大圖用的衍生圖，長邊 PHOTO_PREVIEW_PX。

    **為什麼不是直接給原圖。** 原圖沒有任何快取，每開一次燈箱就從 FTP 重拉
    一次整檔（實測中位數 2.02MB、p95 19.3MB、最大 30.9MB）。preview 產生一次
    之後就是本機的一個檔案，第二次之後成本是零，在行動網路上差距是數量級的。

    檔名靠內容雜湊推算（見 photo.derived_name），所以命中快取時完全不必碰 FTP，
    資料庫也不需要多一個欄位。真的要看細節時，前端會在縮放超過 preview 的
    實際像素時自己換成 /full。
    """
    d = _photo_row(photo_id, request)
    box = int(getattr(settings, "photo_preview_px", 0) or photo.PREVIEW_BOX)
    w, h = d.get("width") or 0, d.get("height") or 0

    # 原圖本來就比 preview 小 —— 產生衍生圖只會變大又變模糊
    if w and h and max(w, h) <= box:
        return _stream_original(d)

    name = photo.derived_name(d.get("thumb") or "", box)
    if name:
        path = IMAGE_DIR / name
        if path.exists() and path.stat().st_size > 0:
            return FileResponse(path, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})

    # 還沒產生過：抓一次原圖，做完就快取住
    limit = int(settings.max_photo_mb) * 1024 * 1024
    if d.get("size") and d["size"] > limit:
        log.info("相片 %s 超過 %s MB，preview 跳過，直接給原圖", photo_id, settings.max_photo_mb)
        return _stream_original(d)
    try:
        stream = ftpclient.FtpReadStream(d["ftp_path"], 0)
        data = b"".join(stream.iter_chunks(limit=limit))
    except ftpclient.FtpError as e:
        raise HTTPException(502, str(e))

    out = photo.make_thumb(data, name, box=box, quality=88)
    if not out:
        # 產生失敗（壞檔、沒見過的格式）不該變成破圖：把剛剛抓到的原圖直接回去
        mime = PHOTO_MIME.get((d.get("ext") or "").lower(), "application/octet-stream")
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "no-store"})
    return FileResponse(IMAGE_DIR / out, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


# --------------------------- 文件（PDF） ---------------------------
DOC_CACHE = CACHE_DIR / "docs"
DOC_MAX_BYTES = 1024 * 1024 * 1024      # 單份 1GB 上限，擋掉壞檔與掃描巨檔
DOC_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _ranged_file(path, media_type: str, request: Request,
                 filename: Optional[str] = None) -> Response:
    """支援 Range 的檔案回應。

    PDF.js 會發一堆小的 Range 請求（預設 64KB 一塊），有 Range 才能「開了就先
    看到第一頁」而不是等整份下載完。Starlette 各版本對 FileResponse 的 Range
    支援不一致，所以這裡自己處理 —— 邏輯跟上面影片那段一樣。
    """
    size = path.stat().st_size
    start, end, partial = 0, size - 1, False
    rng = request.headers.get("range") or request.headers.get("Range")
    if rng:
        m = RANGE_RE.search(rng)
        if m:
            g1, g2 = m.group(1), m.group(2)
            if g1:
                start = int(g1)
                if g2:
                    end = min(int(g2), size - 1)
            elif g2:
                start = max(0, size - int(g2))
            partial = True
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, max-age=3600",
    }
    if filename:
        # inline：讓瀏覽器交給我們的閱讀器／內建檢視器，而不是直接下載
        headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(filename)}"
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    def chunks():
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                buf = fh.read(min(256 * 1024, left))
                if not buf:
                    break
                left -= len(buf)
                yield buf

    return StreamingResponse(chunks(), status_code=206 if partial else 200,
                             media_type=media_type, headers=headers)


def _enforce_doc_cache() -> None:
    """文件快取超過上限就刪最久沒被讀的。"""
    if not DOC_CACHE.exists():
        return
    files = []
    total = 0
    for p in DOC_CACHE.glob("*.pdf"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, p))
        total += st.st_size
    if total <= DOC_CACHE_MAX_BYTES:
        return
    files.sort()
    for _, sz, p in files:
        try:
            p.unlink()
            total -= sz
        except OSError:
            pass
        if total <= DOC_CACHE_MAX_BYTES * 0.8:
            break


@router.get("/document/{doc_id}/file.pdf")
def document_file(doc_id: int, request: Request):
    """PDF 本體。第一次開啟時整份抓到本機快取，之後就是本機檔案。

    **為什麼不讓 PDF.js 直接對 FTP 做 Range**：它會發幾十上百個小請求，
    而現有的 Range 實作是「每個請求開一條 FTP 連線、從 offset 重讀」——
    FTP 的併發上限實測是個位數到十幾條，一份幾百頁的 PDF 就能把連線池打爆，
    正在看片的人一起被拖下水。片庫其實在本機硬碟上（見規格書 J 的第 −1 層），
    所以「先抓整份」在體感上是一瞬間；等 LIBRARY_LOCAL_ROOTS 做好之後，
    這一層快取可以直接換成本機路徑。
    """
    r = db.q1("SELECT ftp_path, folder, filename, size, mtime_ts FROM document WHERE id=?",
              (doc_id,))
    if not r:
        raise HTTPException(404, "找不到文件")
    acl.assert_can_read(request, r["folder"])
    d = db.row_to_dict(r)
    size = int(d.get("size") or 0)
    if size > DOC_MAX_BYTES:
        raise HTTPException(413, "這份文件太大（超過 1GB）")

    # 快取檔名帶 mtime 與大小：檔案在 FTP 上被換掉，快取就自動失效
    DOC_CACHE.mkdir(parents=True, exist_ok=True)
    cache = DOC_CACHE / f"d{doc_id}_{int(d.get('mtime_ts') or 0)}_{size}.pdf"
    if not (cache.exists() and cache.stat().st_size > 0):
        tmp = cache.with_suffix(".pdf.part")
        try:
            stream = ftpclient.FtpReadStream(d["ftp_path"], 0)
            with open(tmp, "wb") as fh:
                for buf in stream.iter_chunks(limit=DOC_MAX_BYTES):
                    fh.write(buf)
            tmp.replace(cache)
        except ftpclient.FtpError as e:
            tmp.unlink(missing_ok=True)
            raise HTTPException(502, str(e))
        except Exception as e:
            tmp.unlink(missing_ok=True)
            log.warning("抓取文件失敗 %s: %s", doc_id, e)
            raise HTTPException(500, "讀取文件失敗")
        _enforce_doc_cache()
    return _ranged_file(cache, "application/pdf", request, d.get("filename"))


# --------------------------- 字幕 ---------------------------
SUB_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    # 串流回應要讓瀏覽器邊收邊處理，別讓中間的 proxy 整包緩衝起來
    "X-Accel-Buffering": "no",
}


@router.get("/subtitle/{file_id}/embedded/{index}.vtt")
async def subtitle_embedded(file_id: int, index: int, request: Request):
    f = _file_or_404(file_id, request)
    cache = media.subtitle_cache_path(file_id, index, f["mtime"], f["size"])
    if cache.exists():
        return FileResponse(cache, media_type="text/vtt; charset=utf-8",
                            headers=SUB_HEADERS)
    # 還沒抽過：邊抽邊送，播放器可以先顯示前面的字幕，不用等整部片 demux 完。
    # 抽完會寫進快取，同一個檔案下次就是秒開。
    return StreamingResponse(media.aiter_subtitle_vtt(file_id, index, cache),
                             media_type="text/vtt; charset=utf-8",
                             headers=SUB_HEADERS)


@router.get("/subtitle/{file_id}/external.vtt")
def subtitle_external(file_id: int, path: str, request: Request):
    """讀取影片旁邊的外掛字幕檔並轉成 WebVTT。"""
    f = _file_or_404(file_id, request)
    folder = posixpath.dirname(f["ftp_path"])
    if posixpath.dirname(path) != folder:
        raise HTTPException(403, "字幕檔不在影片所在目錄")
    ext = path.rsplit(".", 1)[-1].lower()
    if ext not in ftpclient.SUBTITLE_EXTS:
        raise HTTPException(400, "不支援的字幕格式")
    try:
        stream = ftpclient.FtpReadStream(path, 0)
        raw = b"".join(stream.iter_chunks(limit=8 * 1024 * 1024))
    except ftpclient.FtpError as e:
        raise HTTPException(502, str(e))
    out = media.convert_subtitle_bytes(raw, "srt" if ext == "srt" else ext)
    if not out:
        raise HTTPException(500, "字幕轉檔失敗")
    return Response(content=out, media_type="text/vtt; charset=utf-8")
