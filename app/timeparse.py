"""把各種來源的時間字串統一成 epoch 整數秒。

會出現的格式有五種，來源不同、長相完全不一樣：

  MLSD          2026-08-31T09:48:11        ISO，T 分隔
  LIST（Unix）  Mar 15 10:22               沒有年份（近半年的檔案）
                Mar 15  2024               沒有時間（半年以上的檔案）
  LIST（DOS）   03-15-24 10:22AM
  EXIF          2024:03:09 14:05:22        冒號分隔日期
  NLST 後備     （空字串）

這些以前是直接以**字串**存進資料庫，然後拿來排序。
` `(0x20) < `T`(0x54) 的結果是：同一天裡只有 mtime 的照片永遠排在
有 EXIF 拍攝時間的照片前面，不管實際差幾個小時；而 `Mar` 開頭的
會跳到全表最上面。統一成數字之後這一整類問題就不存在了。

時區：EXIF 的 DateTimeOriginal 沒有時區資訊，FTP 的 LIST 也沒有。
沒有的話一律以本機時區解讀 —— 那是最可能正確的假設（相片是本人拍的、
FTP 通常在同一個時區），而且原始字串會保留下來，日後要改判斷規則
不必重新讀一次檔案。

但這個假設在「出國拍的照片」上一定是錯的，差距可以到十幾個小時。
EXIF 2.31 之後有 OffsetTimeOriginal 這個標籤（`+09:00`），有的話就用它，
並且記下這個 epoch 到底是**算出來的**還是**猜出來的** —— 兩者不該長得一樣。
"""
from __future__ import annotations

import calendar
import re
import time
from datetime import datetime
from typing import Optional

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")
_EXIF = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
# Mar 15 10:22   /   Mar 15  2024
_UNIX = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(?:(\d{1,2}):(\d{2})|(\d{4}))$")
# 03-15-24 10:22AM
_DOS = re.compile(r"^(\d{2})-(\d{2})-(\d{2,4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?$")

# 合理的時間範圍：1990-01-01 到 2100-01-01。
# 超出這個範圍幾乎一定是解析錯誤或單位搞錯（毫秒當秒），
# 而那種值會安靜地存進去然後永遠排在最前面。
MIN_TS = 631152000
MAX_TS = 4102444800


# +09:00 / -0330 / Z
_OFFSET = re.compile(r"^(?:(Z)|([+-])(\d{2}):?(\d{2}))$")


def offset_seconds(value) -> Optional[int]:
    """把 EXIF 的時區字串轉成秒。認不出來回 None（而不是當成 0）。

    當成 0 就是把「不知道」偷偷改寫成「UTC」，那是會差好幾個小時的謊。
    """
    if value is None:
        return None
    m = _OFFSET.match(str(value).strip())
    if not m:
        return None
    if m.group(1):
        return 0
    sec = int(m.group(3)) * 3600 + int(m.group(4)) * 60
    return -sec if m.group(2) == "-" else sec


def _mk(y: int, mo: int, d: int, h: int = 0, mi: int = 0, se: int = 0) -> Optional[int]:
    try:
        return int(time.mktime((y, mo, d, h, mi, se, 0, 0, -1)))
    except (ValueError, OverflowError):
        return None


def _mk_utc(y: int, mo: int, d: int, h: int, mi: int, se: int, off: int) -> Optional[int]:
    """已知時區偏移時用這個，不經過本機時區。"""
    try:
        return calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)) - off
    except (ValueError, OverflowError):
        return None


def parse(value, *, now: Optional[float] = None, tz_offset=None) -> Optional[int]:
    """回傳 epoch 整數秒，解析不出來就 None。

    tz_offset 給了（EXIF 的 OffsetTimeOriginal，例如 `+09:00`）就照它算，
    沒給就以本機時區解讀。只對有時分秒的格式有意義。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v if MIN_TS <= v <= MAX_TS else None
    if isinstance(value, datetime):
        return int(value.timestamp())
    s = str(value).strip()
    if not s:
        return None

    m = _EXIF.match(s) or _ISO.match(s)
    if m:
        g = m.groups()
        y, mo, d = int(g[0]), int(g[1]), int(g[2])
        h, mi = int(g[3]), int(g[4])
        se = int(g[5]) if len(g) > 5 and g[5] else 0
        off = offset_seconds(tz_offset)
        ts = _mk_utc(y, mo, d, h, mi, se, off) if off is not None else _mk(y, mo, d, h, mi, se)
        return ts if ts and MIN_TS <= ts <= MAX_TS else None

    m = _UNIX.match(s)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            return None
        day = int(m.group(2))
        if m.group(5):                      # 有年份、沒時間
            ts = _mk(int(m.group(5)), mon, day)
        else:                               # 有時間、沒年份 → 推年份
            # LIST 只在檔案「近半年」時給時間。所以先假設今年，
            # 如果算出來是未來（超過一天的誤差容忍），那就是去年的。
            ref = datetime.fromtimestamp(now if now is not None else time.time())
            ts = _mk(ref.year, mon, day, int(m.group(3)), int(m.group(4)))
            if ts and ts > ref.timestamp() + 86400:
                ts = _mk(ref.year - 1, mon, day, int(m.group(3)), int(m.group(4)))
        return ts if ts and MIN_TS <= ts <= MAX_TS else None

    m = _DOS.match(s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:                         # 兩位數年份：70~99 當 19xx，其餘 20xx
            y += 1900 if y >= 70 else 2000
        h, mi = int(m.group(4)), int(m.group(5))
        ap = (m.group(6) or "").lower()
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        ts = _mk(y, mo, d, h, mi)
        return ts if ts and MIN_TS <= ts <= MAX_TS else None
    return None


def valid(ts) -> bool:
    """值域檢查。整數欄位擋不住亂值，所以寫入前一定要過這一關。"""
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return False
    return MIN_TS <= v <= MAX_TS
