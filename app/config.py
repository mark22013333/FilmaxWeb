"""設定載入：從 .env 讀取，全部有合理預設值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# 資料目錄。預設是專案底下的 data/，用 FILMAX_DATA_DIR 可以搬到別的磁碟
# （快取分段會長到好幾 GB，放 SSD 以外的地方很常見），測試也靠它隔離。
# 這一行必須在 load_dotenv 之前，所以只吃真正的環境變數，不吃 .env。
DATA_DIR = Path(os.getenv("FILMAX_DATA_DIR") or (BASE_DIR / "data")).resolve()
IMAGE_DIR = DATA_DIR / "images"
CACHE_DIR = DATA_DIR / "hls"
SUB_DIR = DATA_DIR / "subs"
DB_PATH = DATA_DIR / "library.db"

# 讀哪一個 .env。FILMAX_ENV_FILE 指到別的路徑（或一個不存在的路徑）就能完全隔離
# 開發機自己的 .env —— 跟上面 FILMAX_DATA_DIR 同一個道理，所以也必須在
# load_dotenv 之前，只吃真正的環境變數。
#
# 為什麼需要它：測試想驗「什麼都沒設時是預設值」，做法是 import app 之前把那些鍵
# 從 os.environ 移掉。但 load_dotenv 是在 import 時才跑，而它的預設是 override=False
# —— 鍵剛被移掉，於是它**正好會被 .env 重新填回去**。「先 pop 再 import」對 .env
# 完全無效，它只擋得住真正的環境變數。實際後果：每往 .env 加一個參數，
# 就有一組「解析順序」的測試變紅，而那份 pop 清單愈補愈長也補不完。
ENV_FILE = Path(os.getenv("FILMAX_ENV_FILE") or (BASE_DIR / ".env"))
load_dotenv(ENV_FILE)


def _b(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or default)
    except ValueError:
        return default


def _s(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return default if v is None else v.strip()


@dataclass
class LibraryRoot:
    path: str
    kind: str  # movie / tv / auto


@dataclass
class Settings:
    ftp_host: str = field(default_factory=lambda: _s("FTP_HOST", "127.0.0.1"))
    ftp_port: int = field(default_factory=lambda: _i("FTP_PORT", 21))
    ftp_user: str = field(default_factory=lambda: _s("FTP_USER", "anonymous"))
    ftp_password: str = field(default_factory=lambda: _s("FTP_PASSWORD", ""))
    ftp_tls: bool = field(default_factory=lambda: _b("FTP_TLS", False))
    ftp_passive: bool = field(default_factory=lambda: _b("FTP_PASSIVE", True))
    ftp_encoding: str = field(default_factory=lambda: _s("FTP_ENCODING", "utf-8"))
    ftp_timeout: int = field(default_factory=lambda: _i("FTP_TIMEOUT", 20))


    ffmpeg: str = field(default_factory=lambda: _s("FFMPEG_PATH", "ffmpeg") or "ffmpeg")
    ffprobe: str = field(default_factory=lambda: _s("FFPROBE_PATH", "ffprobe") or "ffprobe")
    # 預設 auto，不是 none。理由有兩個：
    # (1) J 章結論 (a)：必須重編時 NVENC 是首選 —— 同碼率下每一級都贏 x264
    #     veryfast，而且快 1.5 倍（NVENC 2800k 拿 92.3，x264 veryfast 要 6000k
    #     才追平）。預設走 CPU 等於預設選了較差的那一邊。
    # (2) auto 是安全的：resolve_hwaccel() 會實際試編一小段，硬體不在或驅動
    #     擋掉就自動退回 libx264，而 params.py 與 .env.example 早就寫著 auto ——
    #     這裡原本是 none，等於後台顯示的預設值跟真的預設值不一致。
    hwaccel: str = field(default_factory=lambda: _s("FFMPEG_HWACCEL", "auto").lower() or "auto")
    # 解碼加速跟編碼加速是兩套不同的硬體單元（NVDEC vs NVENC），
    # 驅動擋掉編碼不代表解碼也不能用。auto 讓 ffmpeg 自己挑、失敗自動退回 CPU。
    # 實測發現：軟體濾鏡要用畫格時，GPU→CPU 的搬運成本常常比省下的解碼還貴
    # （在 GTX 1070 上實測慢 30%）。所以預設關閉，要開之前先用 /api/diagnostics/bench 量。
    decode_hwaccel: str = field(default_factory=lambda: _s("FFMPEG_DECODE_HWACCEL", "none").lower() or "none")
    # CPU 編碼速度/畫質取捨。你這台 1080p 有 3x 餘裕，可以往品質那邊調
    # HDR 轉 SDR：auto（4K 用 fast、其餘用 quality）/ quality / fast / off
    # 轉碼速度低於這個倍數就停止預轉，把 CPU 全留給使用者正在等的那一段
    hls_cache_max_mb: int = field(default_factory=lambda: _i("HLS_CACHE_MAX_MB", 4096))

    # ---- 登入驗證 ----
    auth_enabled: bool = field(default_factory=lambda: _b("AUTH_ENABLED", False))
    auth_password: str = field(default_factory=lambda: _s("AUTH_PASSWORD", ""))
    # 唯讀密碼。設了之後，用這組密碼登入的人只能看與播，不能下載、
    # 不能瀏覽 FTP、不能觸發掃描或清快取。留空 = 不開放唯讀登入。
    viewer_password: str = field(default_factory=lambda: _s("VIEWER_PASSWORD", ""))
    auth_secret: str = field(default_factory=lambda: _s("AUTH_SECRET", ""))
    trust_proxy: bool = field(default_factory=lambda: _b("TRUST_PROXY", False))

    # ---- Google 登入 ----
    # 在 Google Cloud Console → API 與服務 → 憑證 建立「OAuth 用戶端 ID / 網頁應用程式」，
    # 授權的重新導向 URI 填 https://你的網域/auth/google/callback
    google_client_id: str = field(default_factory=lambda: _s("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: _s("GOOGLE_CLIENT_SECRET", ""))
    # 對外網址（含 https://，結尾不要斜線）。留空就用 GOOGLE_REDIRECT_URI，
    # 兩個都留空則從請求的 Host 推導 —— 但 Google 只認事先登記的那一個，
    # 所以正式環境務必明寫，不要靠推導。
    public_base_url: str = field(default_factory=lambda: _s("PUBLIC_BASE_URL", ""))
    google_redirect_uri: str = field(default_factory=lambda: _s("GOOGLE_REDIRECT_URI", ""))
    # 只允許這些網域的 Google 帳號註冊，逗號分隔；留空 = 任何 Google 帳號都能送出申請
    # （送出不等於能進來，預設仍要管理員審核）。
    # 這些 email 一登入就是管理員，且免審核。第一個管理員一定要用這個設進來，
    # 否則沒有人有權限去審核別人 —— 會變成所有人都卡在待審核。
    # 新帳號是否自動核准（角色仍是唯讀）。預設 false：陌生人登入後只會看到
    # 「等待管理員核准」，看不到任何影片。

    # ---- 使用者資料存哪裡 ----
    # 三個都設了就用 MSSQL（dbo.filmax_users），否則用本機 SQLite。
    # 刻意不做「連不上就自動退回 SQLite」：那會變成兩份使用者資料各說各話，
    # 在 A 核准的人在 B 不存在，權限判斷失去單一依據 —— 比連不上更危險。
    mssql_host: str = field(default_factory=lambda: _s("MSSQL_HOST", ""))
    mssql_port: int = field(default_factory=lambda: _i("MSSQL_PORT", 1433))
    mssql_database: str = field(default_factory=lambda: _s("MSSQL_DATABASE", ""))
    mssql_user: str = field(default_factory=lambda: _s("MSSQL_USER", ""))
    mssql_password: str = field(default_factory=lambda: _s("MSSQL_PASSWORD", ""))
    mssql_driver: str = field(
        default_factory=lambda: _s("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
                                or "ODBC Driver 18 for SQL Server")
    # Driver 18 起預設就加密。自簽憑證的內網伺服器要把 TRUST_CERT 設 true，
    # 不然會卡在憑證驗證失敗。對外的正式環境不要打開。
    mssql_encrypt: bool = field(default_factory=lambda: _b("MSSQL_ENCRYPT", True))
    mssql_trust_cert: bool = field(default_factory=lambda: _b("MSSQL_TRUST_CERT", False))
    mssql_timeout: int = field(default_factory=lambda: _i("MSSQL_TIMEOUT", 10))
    # 上面全部不管，直接給一整串 ODBC 連線字串
    mssql_odbc_dsn: str = field(default_factory=lambda: _s("MSSQL_ODBC_DSN", ""))

    # 明講要用哪個後端，而不是從「有沒有填連線資訊」推導。
    # 留空 = 沿用舊行為（有填 MSSQL_HOST 就走 MSSQL），不破壞既有設定。
    user_store_setting: str = field(default_factory=lambda: _s("USER_STORE", "").lower())
    media_store_setting: str = field(default_factory=lambda: _s("MEDIA_STORE", "").lower())

    # ---- 遠端畫質 ----
    # 轉碼輸出的聲道數。預設 2（降混成立體聲）—— 多聲道 AAC 包在 mpegts 裡，
    # 瀏覽器與 hls.js 的支援度並不一致，降混最穩。
    # 家裡有環繞喇叭又確定播得動的話，設 AUDIO_CHANNELS=6 保留 5.1，0 = 不動原始聲道。
    # session cookie 是否只在 HTTPS 下送出。對外開放（Cloudflare Tunnel、
    # 反向代理）時務必設 true，否則只要有一次 http 連線，session 就明文上線。
    cookie_secure: bool = field(default_factory=lambda: _b("COOKIE_SECURE", False))
    # 掃描時要略過的資料夾，比對「名稱開頭」，逗號分隔，不分大小寫。
    # 例：SCAN_EXCLUDE_DIR_PREFIXES=_,temp,備份
    #   → _old、_tmp、TempFiles、備份2024 都會整個跳過，連同底下的子目錄。
    # 比的是資料夾名稱不是完整路徑，所以放在哪一層都有效。
    # 掃描時要略過的副檔名，逗號分隔，不分大小寫，寫不寫點都可以。
    # 例如 SCAN_EXCLUDE_EXTS=iso,ts,m2ts —— 想跳過藍光原盤或錄影檔時很有用。
    # 只收錄這些副檔名（留空 = 不限制）。設了之後排除清單仍然有效，
    # 兩個都符合才會被收進來。
    # 相片庫。MIN_FILE_MB 是為影片設的（預設 50MB），套在圖片上會全部濾掉，
    # 所以另外給一個 KB 級的門檻，用來擋掉圖示與版面小圖。
    # 超過這個大小的圖片不讀（掃描檔、大張 TIFF），避免一張圖把記憶體吃光
    # 影片旁邊的封面圖（poster.jpg、與資料夾同名的圖、與某個影片同主檔名的圖）
    # 不要收進相片庫。關掉的話那些封面會出現在相片牆上。
    # 額外要視為「本地」的網段，逗號分隔。例如把 Tailscale 的 100.64.0.0/10 加進來
    extra_local_networks: str = field(default_factory=lambda: _s("LOCAL_NETWORKS", ""))

    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("PORT", 8080))
    probe_concurrency: int = field(default_factory=lambda: _i("PROBE_CONCURRENCY", 3))
    auto_scan_on_start: bool = field(default_factory=lambda: _b("AUTO_SCAN_ON_START", False))

    def _ext_set(self, raw: str) -> set:
        """把 "iso, .TS , m2ts" 這種輸入正規化成 {"iso","ts","m2ts"}。

        使用者會怎麼寫不好預期：有人加點、有人大寫、有人用空格分隔，
        所以這裡一律收乾淨，不要讓格式差異變成「設了卻沒作用」。
        """
        out = set()
        for chunk in (raw or "").replace(";", ",").replace(" ", ",").split(","):
            chunk = chunk.strip().lstrip(".").lower()
            if chunk:
                out.add(chunk)
        return out

    @property
    def exclude_exts(self) -> set:
        return self._ext_set(self.scan_exclude_exts)

    @property
    def only_exts(self) -> set:
        return self._ext_set(self.scan_only_exts)

    @property
    def exclude_dir_prefixes(self) -> tuple:
        """回傳小寫的前綴 tuple，直接給 str.startswith 用。

        不用 _ext_set：那個會把空白也當分隔符，但資料夾名稱本來就可能有空白
        （"New Folder"、"未 分類"）。這裡只用逗號與分號分隔。
        """
        out = []
        for chunk in (self.scan_exclude_dir_prefixes or "").replace(";", ",").split(","):
            chunk = chunk.strip().lower()
            if chunk:
                out.append(chunk)
        return tuple(out)

    @property
    def mssql_configured(self) -> bool:
        if self.mssql_odbc_dsn:
            return True
        return bool(self.mssql_host and self.mssql_database and self.mssql_user)

    def _store(self, explicit: str, name: str) -> str:
        """決定一個後端要用哪個實作。

        明講的值優先。沒明講就沿用舊行為（有 MSSQL 連線資訊就走 MSSQL），
        這樣既有的 .env 不會因為升級而改變行為。
        """
        if explicit in ("sqlite", "mssql"):
            return explicit
        if explicit:
            raise ValueError(
                f"{name}={explicit!r} 不是合法的值，只能是 sqlite 或 mssql")
        return "mssql" if self.mssql_configured else "sqlite"

    @property
    def user_store(self) -> str:
        return self._store(self.user_store_setting, "USER_STORE")

    @property
    def media_store(self) -> str:
        # 媒體庫預設 SQLite。要走 MSSQL 必須明講 —— 只填了連線資訊
        # 不代表想把整個片庫搬過去。
        if self.media_store_setting in ("sqlite", "mssql"):
            return self.media_store_setting
        if self.media_store_setting:
            raise ValueError(
                f"MEDIA_STORE={self.media_store_setting!r} 不是合法的值，只能是 sqlite 或 mssql")
        return "sqlite"

    def check_stores(self) -> None:
        """啟動時呼叫。設定不合法或不完整就丟例外，不要等到有人登入才爆。"""
        for name, value in (("USER_STORE", self.user_store),
                            ("MEDIA_STORE", self.media_store)):
            if value == "mssql" and not self.mssql_configured:
                raise ValueError(
                    f"{name}=mssql 但沒有設定連線資訊。"
                    f"請在 .env 補上 MSSQL_HOST / MSSQL_DATABASE / MSSQL_USER / MSSQL_PASSWORD，"
                    f"或把 {name} 改成 sqlite。")
        if self.media_store == "mssql":
            raise ValueError(
                "MEDIA_STORE=mssql 尚未實作（規格書第四期）。目前請設 sqlite。")

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def _csv(self, raw: str) -> tuple:
        out = []
        for chunk in (raw or "").replace(";", ",").split(","):
            chunk = chunk.strip().lower()
            if chunk:
                out.append(chunk)
        return tuple(out)

    @property
    def allowed_domains(self) -> tuple:
        return tuple(d.lstrip("@") for d in self._csv(self.google_allowed_domains))

    @property
    def admin_emails(self) -> tuple:
        return self._csv(self.google_admin_emails)

    def redirect_uri(self) -> str:
        if self.google_redirect_uri:
            return self.google_redirect_uri
        if self.public_base_url:
            return self.public_base_url.rstrip("/") + "/auth/google/callback"
        return ""

    @property
    def library_roots(self) -> List[LibraryRoot]:
        raw = _s("LIBRARY_ROOTS", "/|auto")
        roots: List[LibraryRoot] = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "|" in chunk:
                p, k = chunk.rsplit("|", 1)
            else:
                p, k = chunk, "auto"
            k = k.strip().lower()
            if k not in ("movie", "tv", "auto"):
                k = "auto"
            p = "/" + p.strip().strip("/")
            roots.append(LibraryRoot(path=p if p != "/" else "/", kind=k))
        return roots or [LibraryRoot("/", "auto")]

    @property
    def library_local_roots(self) -> List[Tuple[str, str]]:
        """FTP 路徑 → 本機路徑的對應表（第 −1 層）。

        格式跟 LIBRARY_ROOTS 一樣用分號分隔、等號左右是「FTP 前綴＝本機根目錄」：

            LIBRARY_LOCAL_ROOTS=/=D:\\1.FTP
            LIBRARY_LOCAL_ROOTS=/媒體資料庫=D:\\1.FTP\\媒體資料庫;/相片=E:\\photos

        為什麼只能放 .env（Tier 4）：它是路徑，會擴大服務能讀到的範圍 ——
        跟 LIBRARY_ROOTS、FFMPEG_PATH 同一類，不進後台 UI。

        回傳的前綴一律正規化成「開頭有 /、結尾沒有 /」，長的排前面，
        這樣比對時先命中最specific 的那一條。
        """
        raw = _s("LIBRARY_LOCAL_ROOTS", "")
        out: List[Tuple[str, str]] = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            prefix, local = chunk.split("=", 1)
            prefix = "/" + prefix.strip().strip("/")
            local = local.strip().strip('"')
            if not local:
                continue
            out.append((prefix, local))
        out.sort(key=lambda t: len(t[0]), reverse=True)
        return out

    def self_base_url(self) -> str:
        """ffmpeg 讀取影片時用的自身位址 (走本機 HTTP，避免 ffmpeg 處理 FTP 編碼問題)。"""
        return f"http://127.0.0.1:{self.port}"


# --------------------------------------------------------------------------
# 即時生效的設定（Tier 2 / applyMode=hot）
# --------------------------------------------------------------------------
# 這幾項不是 dataclass 欄位，是每次讀都重新解析的 property。
#
# **為什麼一定要這樣做**：dataclass 欄位在 import 時就定案了。後台改了值、
# 存進資料庫、UI 顯示「已生效」，而程式仍然在用啟動當下讀到的那個數字 ——
# 這是規格裡點名「最容易悄悄漂移」的那一項，因為它不會出任何錯，
# 只是沒有作用。改成 property 之後，既有的 settings.crf 這類寫法
# 完全不用動就變成即時的。
#
# 同樣的道理：**任何模組都不可以在頂層寫 CRF = settings.crf**。
# 那等於又把它凍回 import 當下的值，而 UI 仍然顯示「已生效」。
# tests/phase3_test.py 有一項專門釘住這件事。
_HOT_ATTRS = {
    "tmdb_api_key": "TMDB_API_KEY",
    "tmdb_language": "TMDB_LANGUAGE",
    "tmdb_fallback_language": "TMDB_FALLBACK_LANGUAGE",
    "tmdb_rate_limit": "TMDB_RATE_LIMIT",
    "crf": "TRANSCODE_CRF",
    "nvenc_cq": "NVENC_CQ",
    "max_height": "TRANSCODE_MAX_HEIGHT",
    "x264_preset": "X264_PRESET",
    "hdr_tonemap": "HDR_TONEMAP",
    "hdr_tonemap_algo": "HDR_TONEMAP_ALGO",
    "hls_segment_seconds": "HLS_SEGMENT_SECONDS",
    # 兩階 HLS 的開關（J 章第 0 層）。預設關 —— 它改的是播放路徑，
    # 而 remux 的時間戳行為要在真的片源上驗過才敢開。關著的時候
    # rungs_for() 只會回下階，也就是現在的行為。
    "hls_two_rung": "HLS_TWO_RUNG",
    "hls_prefetch": "HLS_PREFETCH_SEGMENTS",
    "prefetch_min_speed": "PREFETCH_MIN_SPEED",
    "audio_channels": "AUDIO_CHANNELS",
    "audio_bitrate_kbps": "AUDIO_BITRATE_KBPS",
    "remote_max_height": "REMOTE_MAX_HEIGHT",
    "remote_bitrate_kbps": "REMOTE_BITRATE_KBPS",
    "remote_high_rung": "REMOTE_HIGH_RUNG",
    "lan_bitrate_kbps": "LAN_BITRATE_KBPS",
    "min_file_mb": "MIN_FILE_MB",
    "scan_exclude_dir_prefixes": "SCAN_EXCLUDE_DIR_PREFIXES",
    "scan_exclude_exts": "SCAN_EXCLUDE_EXTS",
    "scan_only_exts": "SCAN_ONLY_EXTS",
    "min_photo_kb": "MIN_PHOTO_KB",
    "max_photo_mb": "MAX_PHOTO_MB",
    "photo_exclude_artwork": "PHOTO_EXCLUDE_VIDEO_ARTWORK",
    "photo_preview_px": "PHOTO_PREVIEW_PX",
    "subtitle_prefetch": "SUBTITLE_PREFETCH",
    "session_days": "SESSION_DAYS",
    "google_allowed_domains": "GOOGLE_ALLOWED_DOMAINS",
    "google_admin_emails": "GOOGLE_ADMIN_EMAILS",
    "google_auto_approve": "GOOGLE_AUTO_APPROVE",
}
# 這幾項舊寫法有 .lower()，保留原本的正規化，否則 FFMPEG_HWACCEL=NVENC
# 這種大寫輸入會突然變成不認得的值
_HOT_LOWER = {"hdr_tonemap"}


def _hot_prop(key: str, lower: bool):
    def getter(self):
        from . import paramstore          # 延後 import：paramstore 會 import 回 config
        v = paramstore.get(key)
        return v.lower() if lower and isinstance(v, str) else v

    def setter(self, _v):
        # 指派全域設定本來就是壞主意 —— 它會影響到每一個正在使用服務的人，
        # 而且沒有任何紀錄。錯誤訊息直接講出兩條正確的路。
        raise AttributeError(
            f"settings.{key.lower()} 不能直接指派。"
            f"要改值請用 paramstore.set_value({key!r}, ...)（會存進資料庫並留稽核），"
            f"或設環境變數 {key}（優先度最高）。")
    getter.__name__ = key
    return property(getter, setter)


for _attr, _key in _HOT_ATTRS.items():
    setattr(Settings, _attr, _hot_prop(_key, _attr in _HOT_LOWER))


settings = Settings()

# 設了 Google 登入卻沒開 AUTH_ENABLED，等於前門上鎖、後門大開：
# 中介層在 auth_enabled=false 時是「區網全放行、外網全擋」，Google 登入完全不會被用到。
# 與其讓人以為設好了，不如直接把驗證打開。
if settings.google_enabled and not settings.auth_enabled:
    settings.auth_enabled = True

for _d in (DATA_DIR, IMAGE_DIR, CACHE_DIR, SUB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
