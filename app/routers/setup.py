"""首次設定精靈的 API。頁面是 /setup，邏輯在 app/setup.py。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import auth, params, setup
from ..config import settings

router = APIRouter()


def setup_gate(request: Request) -> None:
    """能寫 .env 的人只有：登入的管理員；沒開驗證時只有這台機器本人。

    沒開驗證時中介層是「區網全放行、每個人都是管理員」，對一般頁面可以，
    但這裡能改密碼與片源位址，區網裡別台裝置不該能動它。
    """
    if not auth.is_admin(request):
        raise HTTPException(403, "需要管理員權限")
    if not settings.auth_enabled and not auth.is_loopback(auth.peer_ip(request.scope)):
        raise HTTPException(
            403, "還沒設定登入驗證時，設定精靈只能在執行 FilmaxWeb 的那台電腦上開"
                 "（網址用 http://127.0.0.1:%s/setup）" % settings.port)


class Body(BaseModel):
    data: Dict[str, Any] = {}


@router.get("/state")
def state(_: None = Depends(setup_gate)):
    env = setup.read_env_values()
    secret_set = {k: bool(env[k]) for k in
                  ("FTP_PASSWORD", "TMDB_API_KEY", "AUTH_PASSWORD", "VIEWER_PASSWORD")}
    shown = {k: v for k, v in env.items() if k not in secret_set}
    return {
        "needsSetup": setup.needs_setup(),
        "env": shown,
        "secretSet": secret_set,
        "ffmpeg": setup.check_ffmpeg(),
        "deps": setup.check_dependencies(),
        "port": settings.port,
        "restartSupported": setup.restart_supported(),
        "unknownEnvKeys": [{"key": k, "guess": g} for k, g in params.unknown_env_keys()],
    }


def _with_saved_password(src: Dict[str, Any]) -> Dict[str, Any]:
    """密碼欄留空且 .env 已有密碼 → 用 .env 的來測。密碼從不回傳到網頁，
    所以「已設定，留空沿用」要由這裡接起來，否則測試會拿空密碼去登入然後誤報帳密錯。"""
    if src and not src.get("password"):
        saved = setup.read_env_values(("FTP_PASSWORD",))["FTP_PASSWORD"]
        if saved:
            src = {**src, "password": saved}
    return src


@router.post("/test/source")
def test_source(body: Body, _: None = Depends(setup_gate)):
    return setup.test_source(_with_saved_password(body.data))


@router.post("/browse")
def browse(body: Body, _: None = Depends(setup_gate)):
    return setup.browse_source(_with_saved_password(body.data.get("source") or {}),
                               str(body.data.get("path") or "/"))


@router.post("/test/tmdb")
def test_tmdb(body: Body, _: None = Depends(setup_gate)):
    return setup.test_tmdb(str(body.data.get("key") or ""))


@router.post("/save")
def save(body: Body, request: Request, _: None = Depends(setup_gate)):
    """整批驗證，過了才寫 .env。回哪些鍵變了，以及要不要重啟。"""
    upd, err = setup.build_updates(body.data)
    if err:
        raise HTTPException(400, detail=err)
    try:
        changed = setup.write_env(upd)
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"寫入 .env 失敗：{e}")
    # 只回鍵名，不回值 —— 密碼不能繞一圈回到網頁上
    return {"changed": changed, "restartRequired": bool(changed),
            "authWillBeEnabled": upd.get("AUTH_ENABLED") == "true"
                                 and not settings.auth_enabled}


@router.post("/restart")
def restart(_: None = Depends(setup_gate)):
    if not setup.restart_supported():
        raise HTTPException(400, "這個平台請手動重新啟動服務")

    def later():
        time.sleep(0.8)              # 讓這個回應先送出去
        setup.spawn_restart()
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}
