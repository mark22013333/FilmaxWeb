"""串流端點：原始位元組 (Range) / HLS 播放清單與分段 / 字幕。"""
from __future__ import annotations

import logging
import posixpath
import re
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from .. import auth, db, ftpclient, hls, media
from ..config import settings

log = logging.getLogger("filmax.stream")
router = APIRouter()

MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "ts": "video/mp2t", "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
}
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _file_or_404(file_id: int) -> dict:
    row = db.q1("SELECT * FROM media_file WHERE id=?", (file_id,))
    if not row:
        raise HTTPException(404, "找不到檔案")
    return db.row_to_dict(row)  # type: ignore[return-value]


@router.get("/stream/{file_id}")
def stream_raw(file_id: int, request: Request, raw: int = 0):
    """支援 HTTP Range 的原始檔串流。瀏覽器直接播放、以及 ffmpeg 讀取來源都走這裡。"""
    f = _file_or_404(file_id)
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
    f = _file_or_404(file_id)
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
def hls_master(file_id: int, h: Optional[int] = None, a: Optional[int] = None):
    f = _file_or_404(file_id)
    _duration_or_404(f)
    profile = hls.profile_key(h, a)
    return PlainTextResponse(hls.build_master(file_id, profile),
                             media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{file_id}/index.m3u8")
def hls_index(file_id: int, p: Optional[str] = None, h: Optional[int] = None, a: Optional[int] = None):
    f = _file_or_404(file_id)
    duration = _duration_or_404(f)
    profile = hls.normalize_profile(p) if p else hls.profile_key(h, a)
    return PlainTextResponse(hls.build_playlist(file_id, duration, profile),
                             media_type="application/vnd.apple.mpegurl")


@router.get("/hls/{file_id}/seg-{index}.ts")
def hls_segment(file_id: int, index: int, p: str = Query(default="")):
    f = _file_or_404(file_id)
    duration = _duration_or_404(f)
    profile = hls.normalize_profile(p)
    try:
        path = hls.get_segment(file_id, index, profile, duration)
    except Exception as e:
        log.warning("分段失敗 %s/%s: %s", file_id, index, e)
        raise HTTPException(500, str(e))
    return FileResponse(path, media_type="video/mp2t",
                        headers={"Cache-Control": "public, max-age=86400"})


# --------------------------- 字幕 ---------------------------
SUB_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    # 串流回應要讓瀏覽器邊收邊處理，別讓中間的 proxy 整包緩衝起來
    "X-Accel-Buffering": "no",
}


@router.get("/subtitle/{file_id}/embedded/{index}.vtt")
async def subtitle_embedded(file_id: int, index: int):
    f = _file_or_404(file_id)
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
def subtitle_external(file_id: int, path: str):
    """讀取影片旁邊的外掛字幕檔並轉成 WebVTT。"""
    f = _file_or_404(file_id)
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
