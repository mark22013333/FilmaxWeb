"""啟動入口：python run.py"""
import os

import uvicorn

from app.config import BASE_DIR, settings

# DEV_RELOAD=true 時，改動 app/ 底下的檔案會自動重啟服務，不必手動 Ctrl+C
RELOAD = (os.getenv("DEV_RELOAD", "") or "").strip().lower() in ("1", "true", "yes", "on")

# uvicorn 會安裝自己的 logging 設定，`logging.basicConfig()` 管不到它 ——
# 所以 `Started server process`、`Application startup complete`
# 與每一筆 access log 原本都是沒有時間戳的光禿禿一行。
# 這份設定把 uvicorn 的三個 logger 拉回跟 app 一樣的格式。
def _log_config() -> dict:
    from app.main import LOG_FORMAT, LOG_DATEFMT
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "std": {"format": LOG_FORMAT, "datefmt": LOG_DATEFMT},
            # access log 的欄位是 uvicorn 自己組的，格式字串不一樣
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": "%(asctime)s %(levelname)-7s %(name)-16s %(client_addr)s "
                       "%(request_line)s %(status_code)s",
                "datefmt": LOG_DATEFMT,
            },
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "std",
                        "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "access",
                       "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }


if __name__ == "__main__":
    print(f"\n  FilmaxWeb 啟動中 →  http://localhost:{settings.port}")
    if RELOAD:
        print("  （自動重載已開啟：改動 app/ 內的程式會自己重啟）")
    print()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
        log_config=_log_config(),
        timeout_keep_alive=75,
        reload=RELOAD,
        reload_dirs=[str(BASE_DIR / "app")] if RELOAD else None,
    )
