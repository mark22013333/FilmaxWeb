"""啟動入口：python run.py"""
import os

import uvicorn

from app.config import BASE_DIR, settings

# DEV_RELOAD=true 時，改動 app/ 底下的檔案會自動重啟服務，不必手動 Ctrl+C
RELOAD = (os.getenv("DEV_RELOAD", "") or "").strip().lower() in ("1", "true", "yes", "on")

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
        timeout_keep_alive=75,
        reload=RELOAD,
        reload_dirs=[str(BASE_DIR / "app")] if RELOAD else None,
    )
