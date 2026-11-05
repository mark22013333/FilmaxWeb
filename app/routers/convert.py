"""相片頁「HIF 轉 JPG」的 API。邏輯在 app/hifconvert.py，規格在 S 章。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, hifconvert
from ..config import settings

router = APIRouter()


def convert_gate(request: Request) -> None:
    """會在來源目錄寫檔，所以只有管理員，而且 .env 要明確開啟（規格 S-1）。

    沒開驗證時全區網都是管理員，單看角色不夠，開關才是真正的那道閘門。
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")


def _enabled() -> None:
    if not settings.photo_convert_enabled:
        raise HTTPException(403, "轉檔功能沒有開啟（.env 設 PHOTO_CONVERT_ENABLED=true 後重新啟動）")


class Req(BaseModel):
    folder: str
    photo_ids: Optional[List[int]] = None      # 不給 = 這個資料夾這一層的全部 HIF


@router.get("/info")
def info(folder: str, _: None = Depends(convert_gate)):
    job = hifconvert.current()
    return {"enabled": bool(settings.photo_convert_enabled),
            "hif_count": len(hifconvert.hif_rows(folder)) if settings.photo_convert_enabled else 0,
            "running": hifconvert.is_running(),
            "job": job.as_dict() if job else None}


@router.get("/status")
def status(_: None = Depends(convert_gate)):
    job = hifconvert.current()
    return {"running": hifconvert.is_running(), "job": job.as_dict() if job else None}


@router.post("/probe")
def probe(body: Req, _: None = Depends(convert_gate)):
    _enabled()
    ok, why = hifconvert.probe(body.folder)
    return {"ok": ok, "reason": why}


@router.post("")
def start(body: Req, _: None = Depends(convert_gate)):
    _enabled()
    rows = (hifconvert.rows_by_ids(body.photo_ids) if body.photo_ids
            else hifconvert.hif_rows(body.folder))
    rows = [r for r in rows if r["folder"] == body.folder]
    if not rows:
        raise HTTPException(404, "這個資料夾沒有可轉的 HIF")
    try:
        job = hifconvert.start(rows, body.folder)
    except hifconvert.Busy as e:
        raise HTTPException(409, str(e))
    return job.as_dict()


@router.post("/cancel")
def cancel(_: None = Depends(convert_gate)):
    hifconvert.cancel()
    return {"ok": True}
