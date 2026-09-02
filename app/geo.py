"""從 Cloudflare 的標頭取出來源位置。

Cloudflare Tunnel 會在轉送給我們時附上訪客資訊，不必自己接第三方 IP 定位 API。

三件事要知道：

1. **CF-Connecting-IP 是 Cloudflare 自己填的**，會覆蓋掉用戶端送的同名標頭，
   所以它可信。X-Forwarded-For 的最左邊那段則是用戶端自己寫的，不可信
   （來源判斷請走 auth.client_ip）。

2. **位置標頭預設不會送。** 要到 Cloudflare 後台開
   Rules → Managed Transforms → Add visitor location headers。
   沒開的話這裡只會拿到國家，其餘都是 None —— 不是壞掉。

3. **經緯度是城市中心點，不是使用者的實際位置。** 精度大概到城市等級，
   不要拿它當定位用，也不要在介面上暗示那是使用者所在地。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# 標頭名稱 → 我們的欄位名
_FIELDS = {
    b"cf-ipcountry": "country",
    b"cf-region": "region",
    b"cf-ipcity": "city",
    b"cf-timezone": "timezone",
}
_FLOATS = {
    b"cf-iplatitude": "latitude",
    b"cf-iplongitude": "longitude",
}


def _headers(scope) -> Dict[bytes, str]:
    out = {}
    for k, v in scope.get("headers") or []:
        out[k.lower()] = v.decode("utf-8", "ignore").strip()
    return out


def _float(v: str) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if -180 <= f <= 180 else None


def from_scope(scope) -> Dict[str, Any]:
    """回傳 country / region / city / timezone / latitude / longitude / user_agent。

    拿不到的欄位是 None，呼叫端不需要另外判斷有沒有走 Cloudflare。
    """
    h = _headers(scope)
    out: Dict[str, Any] = {k: None for k in
                           ("country", "region", "city", "timezone", "latitude", "longitude")}
    for header, field in _FIELDS.items():
        v = h.get(header)
        # Cloudflare 對無法判斷的來源會送 XX 或 T1（Tor），當成沒有比較不會誤導
        if v and v not in ("XX", "T1"):
            out[field] = v[:120]
    for header, field in _FLOATS.items():
        out[field] = _float(h.get(header))
    out["user_agent"] = (h.get(b"user-agent") or "")[:400] or None
    return out


def describe(rec: Dict[str, Any]) -> str:
    """組成一行人看得懂的位置，給日誌與介面用。"""
    parts = [rec.get("city"), rec.get("region"), rec.get("country")]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " / ".join(out) if out else "位置未知"


def ip_source(scope) -> str:
    """這個請求的來源位址是怎麼判斷出來的：cloudflare / lan / direct / internal。

    值域由 dbo.filmax_audit 的 CHECK 限制決定（見 db/sql/V2__create_audit.sql）。
    有意義的地方在於「這個 IP 可不可信」：cloudflare 是 Cloudflare 自己填的，
    direct 則是有人繞過 Tunnel 直接連進來 —— 那件事本身就值得注意。
    """
    from . import auth
    h = _headers(scope)
    peer = auth.peer_ip(scope)
    if auth.is_loopback(peer) and h.get(b"x-filmax-internal"):
        return "internal"
    if h.get(b"cf-connecting-ip"):
        return "cloudflare"
    if auth.is_local_network(auth.client_ip(scope)):
        return "lan"
    return "direct"
