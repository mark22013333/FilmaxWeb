"""TMDB 刮削：搜尋 → 取詳情 → 下載海報/劇照到本地快取。"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

import httpx

from .config import IMAGE_DIR, settings

log = logging.getLogger("filmax.scraper")

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p"


class _RateLimiter:
    def __init__(self, per_second: int):
        self.interval = 1.0 / max(1, per_second)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class TmdbClient:
    def __init__(self, api_key: str = "", language: str = "zh-TW", fallback: str = "en-US"):
        self.api_key = api_key or settings.tmdb_api_key
        self.language = language or settings.tmdb_language
        self.fallback = fallback or settings.tmdb_fallback_language
        self.limiter = _RateLimiter(settings.tmdb_rate_limit)
        self._client = httpx.Client(timeout=20, follow_redirects=True,
                                    headers={"User-Agent": "filmax-web/1.0"})
        self._cache: Dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _get(self, path: str, **params) -> Optional[dict]:
        if not self.enabled:
            return None
        params.setdefault("language", self.language)
        params["api_key"] = self.api_key
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")
        if key in self._cache:
            return self._cache[key]
        for attempt in range(3):
            self.limiter.wait()
            try:
                r = self._client.get(TMDB_API + path, params=params)
            except Exception as e:
                log.warning("TMDB 連線錯誤 %s: %s", path, e)
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                log.warning("TMDB %s -> %s %s", path, r.status_code, r.text[:200])
                return None
            data = r.json()
            self._cache[key] = data
            return data
        return None

    # ---------- 搜尋 ----------
    def search(self, kind: str, title: str, year: Optional[int] = None,
               alt_title: str = "") -> Optional[dict]:
        """最接近的結果，不管有沒有把握。要分辨把握請用 `match()`。"""
        return self.match(kind, title, year, alt_title)[0]

    def match(self, kind: str, title: str, year: Optional[int] = None,
              alt_title: str = "") -> tuple[Optional[dict], bool]:
        """(最接近的結果, 有沒有把握)。

        依序試各種查詢，**第一個有把握的就停**；全部都沒把握時回傳所有查詢裡
        分數最高的那一筆、把握為 False，讓呼叫端標成待人工確認，而不是照單全收。
        """
        endpoint = "/search/movie" if kind == "movie" else "/search/tv"
        year_key = "year" if kind == "movie" else "first_air_date_year"
        candidates: List[tuple[str, Optional[int], str]] = []
        for t in [title, alt_title]:
            if not t:
                continue
            t = re.sub(r"\s+", " ", t).strip()
            candidates.append((t, year, self.language))
            candidates.append((t, None, self.language))
            if self.fallback and self.fallback != self.language:
                candidates.append((t, year, self.fallback))
            # 去掉尾端數字（續集常見）
            stripped = re.sub(r"\s*\d+$", "", t).strip()
            if stripped and stripped != t:
                candidates.append((stripped, year, self.language))

        seen = set()
        fallback: Optional[tuple[float, dict]] = None
        for t, y, lang in candidates:
            sig = (t, y, lang)
            if sig in seen:
                continue
            seen.add(sig)
            params: Dict[str, Any] = {"query": t, "include_adult": "false", "language": lang}
            if y:
                params[year_key] = y
            data = self._get(endpoint, **params)
            results = (data or {}).get("results") or []
            if not results:
                continue
            # 比對時用檔名原本的年份：帶年份的查詢沒結果、改不帶年份查到的片，
            # 年份不合就該扣分，不能因為這次查詢沒帶年份就放過。
            hit, sure, s = self._rank(results, t, year)
            if sure:
                return hit, True
            if fallback is None or s > fallback[0]:
                fallback = (s, hit)
        return (fallback[1], False) if fallback else (None, False)

    @staticmethod
    def _best(results: List[dict], title: str, year: Optional[int],
              kind: str = "") -> tuple[dict, bool]:
        """(分數最高的結果, 有沒有把握)。"""
        hit, sure, _ = TmdbClient._rank(results, title, year)
        return hit, sure

    @staticmethod
    def _rank(results: List[dict], title: str,
              year: Optional[int]) -> tuple[dict, bool, float]:
        """有把握的條件（任一）：

        - 片名完全相符，而且年份沒有明顯不合（沒年份、或差一年以內）
        - 片名部分相符，而且年份差一年以內
        - 片名對不上，但結果裡只有這一筆的年份完全相符 —— 中文檔名常跟 TMDB 的
          zh-TW 譯名不同（簡繁、兩岸譯名），這時年份是唯一的訊號
        """
        t = _norm(title)

        def title_score(r: dict) -> int:
            names = {_norm(r.get("title") or r.get("name") or ""),
                     _norm(r.get("original_title") or r.get("original_name") or "")} - {""}
            if not t or not names:
                return 0
            if t in names:
                return 50
            if any(t in n or n in t for n in names):
                return 20
            return 0

        def year_diff(r: dict) -> Optional[int]:
            date = r.get("release_date") or r.get("first_air_date") or ""
            return abs(int(date[:4]) - year) if year and date[:4].isdigit() else None

        def score(r: dict) -> float:
            s = float(title_score(r))
            diff = year_diff(r)
            if diff is not None:
                s += 25 if diff == 0 else (10 if diff == 1 else -5 * min(diff, 5))
            s += min(float(r.get("popularity") or 0), 100) / 10.0
            s += min(float(r.get("vote_count") or 0), 5000) / 1000.0
            return s

        best = max(results, key=score)
        ts, diff = title_score(best), year_diff(best)
        if ts == 50:
            sure = diff is None or diff <= 1
        elif ts == 20:
            sure = diff is not None and diff <= 1
        else:
            sure = diff == 0 and sum(1 for r in results if year_diff(r) == 0) == 1
        return best, sure, score(best)

    # ---------- 詳情 ----------
    def details(self, kind: str, tmdb_id: int) -> Optional[dict]:
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        data = self._get(path, append_to_response="credits")
        if not data:
            return None
        if not (data.get("overview") or "").strip() and self.fallback != self.language:
            fb = self._get(path, language=self.fallback)
            if fb and fb.get("overview"):
                data["overview"] = fb["overview"]
        return data

    def season(self, tmdb_id: int, season: int) -> Optional[dict]:
        return self._get(f"/tv/{tmdb_id}/season/{season}")

    # ---------- 圖片 ----------
    def download_image(self, tmdb_path: Optional[str], size: str = "w500") -> Optional[str]:
        if not tmdb_path:
            return None
        name = f"{size}_{hashlib.md5(tmdb_path.encode()).hexdigest()}{_ext_of(tmdb_path)}"
        dest = IMAGE_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            return name
        url = f"{TMDB_IMG}/{size}{tmdb_path}"
        try:
            self.limiter.wait()
            r = self._client.get(url)
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                return name
        except Exception as e:
            log.warning("下載圖片失敗 %s: %s", url, e)
        return None


def _norm(s: str) -> str:
    """比對片名用：小寫、拿掉標點與空白。`Spider-Man` 和 `spider man` 算同名。"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s or "")).lower()


def _ext_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "jpg"
    return "." + (ext if ext in ("jpg", "jpeg", "png", "webp") else "jpg")


def normalize_details(kind: str, d: dict) -> dict:
    """把 TMDB 詳情轉成我們的欄位。"""
    cast = []
    for c in ((d.get("credits") or {}).get("cast") or [])[:12]:
        cast.append({"name": c.get("name"), "character": c.get("character"),
                     "profile": c.get("profile_path")})
    if kind == "movie":
        title = d.get("title") or d.get("original_title") or ""
        date = d.get("release_date") or ""
        runtime = d.get("runtime")
    else:
        title = d.get("name") or d.get("original_name") or ""
        date = d.get("first_air_date") or ""
        rt = d.get("episode_run_time") or []
        runtime = rt[0] if rt else None
    return {
        "title": title,
        "original_title": d.get("original_title") or d.get("original_name") or "",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "overview": (d.get("overview") or "").strip(),
        "rating": float(d.get("vote_average") or 0) or None,
        "runtime": runtime,
        "genres": [g["name"] for g in (d.get("genres") or []) if g.get("name")],
        "cast": cast,
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
        "tmdb_id": d.get("id"),
    }


tmdb = TmdbClient()
