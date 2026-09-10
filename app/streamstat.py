"""串流量測：把「到底慢在哪一段」變成數字，而不是猜。

規格 J 章第 7 節要求的是**先量測再調參數**。看到 `iter_chunks()` 預設 256 KiB
就直接改成 4 MiB，然後宣稱效能修好了 —— 那是把運氣當成證據。這個模組
提供三種取樣點，全部都是環形緩衝（固定筆數、不落地、不進 DB）：

    ftp      原始位元組的讀取吞吐（direct / download / 相片 / PDF 都會經過）
    segment  HLS 一段從被要到吐出去的耗時，含是不是命中快取
    produce  ffmpeg 真的跑起來的耗時與速度倍率（x realtime）

**取樣本身要幾乎免費**，不然量測會變成它想量的那個問題。所以：
只記數字不記字串、鎖只包住 append、每種取樣點最多留 `_KEEP` 筆。

預設是**開著**的（`STREAM_DIAG=true`）—— 保留的資料只有幾百筆數字，
記憶體是 KB 級。真的要關（極低階機器）就把它設成 false，取樣點會變成
一個 early return。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .config import settings

# 每一種取樣點留幾筆。200 筆 × 6 秒一段 ≈ 20 分鐘的播放歷史，
# 足夠看出「一開播就慢」與「播到一半才慢」的差別。
_KEEP = 200

_lock = threading.Lock()
_samples: Dict[str, List[Dict[str, Any]]] = {"ftp": [], "segment": [], "produce": []}
# 累計計數：環形緩衝會把舊的丟掉，但「總共發生幾次」不能跟著被丟掉。
_totals: Dict[str, int] = {}


def enabled() -> bool:
    return bool(getattr(settings, "stream_diag", True))


def _push(kind: str, rec: Dict[str, Any]) -> None:
    rec["at"] = time.time()
    with _lock:
        buf = _samples.setdefault(kind, [])
        buf.append(rec)
        del buf[:-_KEEP]


def bump(name: str, n: int = 1) -> None:
    with _lock:
        _totals[name] = _totals.get(name, 0) + n


# 計時器的解析度下限。Windows 上 time.monotonic() 的粒度約 15.6 ms，
# 走本機路徑的小 Range 常常量到 0.0 —— **那不是「沒有發生」，是「快到量不出來」**。
# 直接丟掉的話診斷頁會看不到最快的那些請求，命中率與樣本數都會失真
# （實測：三次 Range 只有一次被記下來）。夾一個下限，寧可把吞吐低估。
_MIN_ELAPSED = 0.001


def record_ftp(path: str, nbytes: int, elapsed: float, source: str = "ftp") -> None:
    """一次原始位元組讀取。`source` 分 ftp / local，兩者的吞吐差一個數量級。"""
    if not enabled() or nbytes <= 0:
        return
    el = max(elapsed, _MIN_ELAPSED)
    _push("ftp", {"bytes": nbytes, "elapsed": round(el, 4),
                  "mbps": round(nbytes * 8 / el / 1e6, 2),
                  # 量到 0 的那些要標出來，不然「1 GB/s」會被當成真的量測結果
                  "capped": elapsed < _MIN_ELAPSED,
                  "source": source, "name": path.rsplit("/", 1)[-1][:60]})
    bump("ftp_reads")


def record_segment(file_id: int, index: int, profile: str, elapsed: float,
                   nbytes: int, cached: bool) -> None:
    """一次 HLS 分段請求。`cached` 是這一段有沒有現成的檔案可以直接回。

    命中率是這裡最重要的一個數字：命中率低代表 prefetch 沒有跟上（或是
    使用者一直在拖進度條），而那跟「ffmpeg 太慢」是兩種完全不同的病。
    """
    if not enabled():
        return
    _push("segment", {"file_id": file_id, "index": index, "profile": profile,
                      "elapsed": round(elapsed, 3), "bytes": nbytes, "cached": bool(cached)})
    bump("seg_hit" if cached else "seg_miss")


def record_produce(file_id: int, index: int, rung: int, video_seconds: float,
                   elapsed: float, background: bool) -> None:
    """一次 ffmpeg 真的跑起來。`speed` 是 x realtime，小於 1 就是追不上播放。"""
    if not enabled() or elapsed <= 0:
        return
    _push("produce", {"file_id": file_id, "index": index, "rung": rung,
                      "seconds": round(video_seconds, 2), "elapsed": round(elapsed, 3),
                      "speed": round(video_seconds / elapsed, 2),
                      "background": bool(background)})
    bump("produce_bg" if background else "produce_fg")


def _stats(rows: List[Dict[str, Any]], key: str) -> Optional[Dict[str, float]]:
    vals = sorted(r[key] for r in rows if isinstance(r.get(key), (int, float)))
    if not vals:
        return None
    def pct(p: float) -> float:
        return vals[min(len(vals) - 1, int(len(vals) * p))]
    return {"n": len(vals), "min": round(vals[0], 3), "med": round(pct(0.5), 3),
            "p95": round(pct(0.95), 3), "max": round(vals[-1], 3)}


def snapshot(limit: int = 40) -> Dict[str, Any]:
    """後台診斷頁要的那一份。摘要在前面，最近的原始樣本在後面。"""
    with _lock:
        ftp = list(_samples.get("ftp", []))
        seg = list(_samples.get("segment", []))
        prod = list(_samples.get("produce", []))
        totals = dict(_totals)
    hit, miss = totals.get("seg_hit", 0), totals.get("seg_miss", 0)
    return {
        "enabled": enabled(),
        "totals": totals,
        "segment_hit_rate": (round(hit / (hit + miss), 3) if (hit + miss) else None),
        "summary": {
            "ftp_mbps": _stats(ftp, "mbps"),
            # 沒命中快取的那些才問得出「等 ffmpeg 等多久」。命中的一律接近 0，
            # 混在一起算中位數只會被稀釋成一個好看但沒有意義的數字。
            "segment_miss_elapsed": _stats([r for r in seg if not r.get("cached")], "elapsed"),
            "segment_hit_elapsed": _stats([r for r in seg if r.get("cached")], "elapsed"),
            "produce_speed": _stats([r for r in prod if not r.get("background")], "speed"),
        },
        "recent": {"ftp": ftp[-limit:], "segment": seg[-limit:], "produce": prod[-limit:]},
    }


def reset() -> None:
    with _lock:
        for v in _samples.values():
            v.clear()
        _totals.clear()
