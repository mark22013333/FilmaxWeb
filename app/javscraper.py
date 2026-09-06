"""JAV 刮削 —— 番號類作品走 javbus / fc2 / d2pass / jav321，不走 TMDB。

TMDB 查不到番號。`SSIS-938` 丟進去只會是 `scrape_state='failed'`，
所以這類作品需要另一組來源。實作不自己重寫爬蟲，而是接 OpenAver
（https://github.com/slive777/OpenAver）已經寫好的那一份。

**為什麼是 import 而不是打它的 HTTP API**：OpenAver 是 pywebview 桌面程式，
沒有 headless 模式（`windows/standalone.py` 直接綁 GUI），要打 API 就得先讓
使用者開著那個視窗。直接 import 它的 `core.scraper` 只需要 `requests` 與
`beautifulsoup4`，跑在本專案的 venv 裡（實測 Python 3.10.11 對 OpenAver 的
embedded 3.12.4 沒有語法問題）。

**只借兩支**：`extract_number()` 解析番號、`search_jav()` 查 metadata。
刻意不碰 `core.enricher` —— 它會拉進 OpenAver 自己的 SQLite 與 NFO 產生器，
而本專案要的是把結果寫進 `media_item`，不是在影片檔旁邊放 .nfo。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional

import httpx

from .config import IMAGE_DIR, settings

log = logging.getLogger("filmax.javscraper")

_import_lock = threading.Lock()
_mods: Optional[Dict[str, Any]] = None      # {'extract_number':…, 'search_jav':…}
_import_error: str = ""


def _load() -> Optional[Dict[str, Any]]:
    """把 OpenAver 的兩支函式載進來。失敗只記一次，不重試。

    第一次呼叫才做，理由是 import 它要走一段不算短的 module 初始化
    （`core.scrapers.__init__` 會把所有來源的 scraper 都載入），
    沒設 `OPENAVER_PATH` 的人不該付這個代價。
    """
    global _mods, _import_error
    if _mods is not None or _import_error:
        return _mods

    with _import_lock:
        if _mods is not None or _import_error:
            return _mods

        path = (settings.openaver_path or "").strip()
        if not path:
            _import_error = "未設定 OPENAVER_PATH"
            return None
        if not os.path.isdir(path):
            _import_error = f"OPENAVER_PATH 不存在: {path}"
            log.warning("JAV 刮削停用 —— %s", _import_error)
            return None

        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            from core.scraper import search_jav                    # type: ignore
            from core.scrapers.utils import extract_number         # type: ignore
        except Exception as e:                                     # noqa: BLE001
            _import_error = f"{type(e).__name__}: {e}"
            log.warning("JAV 刮削停用 —— 載入 OpenAver 失敗: %s", _import_error)
            return None

        _mods = {"extract_number": extract_number, "search_jav": search_jav}
        log.info("JAV 刮削就緒（OpenAver: %s）", path)
        return _mods


def enabled() -> bool:
    return _load() is not None


def status() -> Dict[str, Any]:
    """給 /api/health 之類的地方看的診斷資訊。"""
    ok = enabled()
    return {"enabled": ok, "path": settings.openaver_path or "", "error": "" if ok else _import_error}


def extract_number(filename: str) -> Optional[str]:
    """從檔名解析番號。解不出來回 None。

    純 CPU、不連外網。OpenAver 的 8 組 pattern 依序比對，第一個中的贏。
    實測對本專案既有的院線片／影集檔名（`Reacher.S03E01...`、
    `Captain.America...`）一律回 None，不會誤判。
    """
    m = _load()
    if not m:
        return None
    try:
        return m["extract_number"](filename) or None
    except Exception as e:                                          # noqa: BLE001
        log.warning("解析番號失敗 %s: %s", filename, e)
        return None


def search(number: str) -> Optional[Dict[str, Any]]:
    """查 metadata。查無資料回 None（不是丟例外）。

    OpenAver 內部已限流（MAX_WORKERS=2、REQUEST_DELAY=0.3）且**沒有 retry**，
    失敗就是失敗。呼叫端把它當 `scrape_state='failed'` 處理即可，
    下次掃描會自動再試一次（`_scrape_items` 的 where 收 'failed'）。
    """
    m = _load()
    if not m or not number:
        return None
    try:
        return m["search_jav"](number) or None
    except Exception as e:                                          # noqa: BLE001
        log.warning("JAV 查詢失敗 %s: %s", number, e)
        return None


def _ext_of(url: str) -> str:
    tail = url.rsplit("?", 1)[0].rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else "jpg"
    return "." + (ext if ext in ("jpg", "jpeg", "png", "webp") else "jpg")


def download_image(url: Optional[str], referer: str = "") -> Optional[str]:
    """把封面抓下來存進 IMAGE_DIR，回傳檔名（比照 TmdbClient.download_image）。

    **Referer 是必要的**：javbus 等站有防盜連，沒帶會拿到 HTML 不是圖。
    有些來源（如 aventertainments）擋得更死，抓不到就回 None ——
    條目其他欄位仍然有效，只是沒有封面。
    """
    if not url:
        return None
    name = f"jav_{hashlib.md5(url.encode()).hexdigest()}{_ext_of(url)}"
    dest = IMAGE_DIR / name
    if dest.exists() and dest.stat().st_size > 0:
        return name

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) filmax-web/1.0"}
    if referer:
        headers["Referer"] = referer
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as c:
            r = c.get(url)
        # 防盜連的表現形式是 200 配一頁 HTML，不是 4xx —— 要看 content-type。
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and r.content and ctype.startswith("image/"):
            dest.write_bytes(r.content)
            return name
        log.warning("下載封面失敗 %s: status=%s content-type=%s", url, r.status_code, ctype or "-")
    except Exception as e:                                          # noqa: BLE001
        log.warning("下載封面失敗 %s: %s", url, e)
    return None


def _rating_of(d: Dict[str, Any]) -> Optional[float]:
    """評分正規化到 0-10（media_item.rating 跟 TMDB 同一個值域）。

    來源給的是 0-5 制，所以乘 2。超出範圍的一律當沒有 —— 寧可沒有評分，
    也不要一個看起來合理但其實是別的刻度的數字。
    """
    v = d.get("_rating")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return round(f * 2, 1) if f <= 5 else None


def normalize(d: Dict[str, Any]) -> Dict[str, Any]:
    """把 OpenAver 的回傳整理成 media_item 要的形狀。

    欄位對應刻意保守：拿不準的一律留空，不猜。掛錯 metadata 比沒有
    metadata 難清理得多 —— 事後看不出哪些是錯的。
    """
    def _s(v: Any) -> str:
        return str(v).strip() if v not in (None, "") else ""

    actors = d.get("actors") or []
    if isinstance(actors, str):
        actors = [actors]
    tags = d.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    date = _s(d.get("date"))
    year: Optional[int] = None
    if len(date) >= 4 and date[:4].isdigit():
        year = int(date[:4])

    runtime: Optional[int] = None
    dur = d.get("duration")
    try:
        if dur not in (None, ""):
            runtime = int(float(dur))
    except (TypeError, ValueError):
        runtime = None

    return {
        "number": _s(d.get("number")),
        "title": _s(d.get("title")),
        "year": year,
        "date": date,
        "runtime": runtime,
        # 劇情在 `_summary`（底線開頭的是 OpenAver 的內部 carrier 欄位，
        # 不是私有慣例 —— to_legacy_dict 會刻意把它補回來）。
        "overview": _s(d.get("_summary")) or _s(d.get("plot")) or _s(d.get("summary")),
        "rating": _rating_of(d),
        "cover": _s(d.get("cover")),
        "url": _s(d.get("url")),
        "maker": _s(d.get("maker")),
        "label": _s(d.get("label")),
        "director": _s(d.get("director")),
        "source": _s(d.get("source")),
        "cast": [{"name": _s(a), "character": ""} for a in actors if _s(a)],
        "genres": [_s(t) for t in tags if _s(t)],
    }
