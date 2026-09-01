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

load_dotenv(BASE_DIR / ".env")


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

    tmdb_api_key: str = field(default_factory=lambda: _s("TMDB_API_KEY", ""))
    tmdb_language: str = field(default_factory=lambda: _s("TMDB_LANGUAGE", "zh-TW"))
    tmdb_fallback_language: str = field(default_factory=lambda: _s("TMDB_FALLBACK_LANGUAGE", "en-US"))
    tmdb_rate_limit: int = field(default_factory=lambda: _i("TMDB_RATE_LIMIT", 8))

    ffmpeg: str = field(default_factory=lambda: _s("FFMPEG_PATH", "ffmpeg") or "ffmpeg")
    ffprobe: str = field(default_factory=lambda: _s("FFPROBE_PATH", "ffprobe") or "ffprobe")
    hls_segment_seconds: int = field(default_factory=lambda: _i("HLS_SEGMENT_SECONDS", 6))
    hwaccel: str = field(default_factory=lambda: _s("FFMPEG_HWACCEL", "none").lower() or "none")
    # 解碼加速跟編碼加速是兩套不同的硬體單元（NVDEC vs NVENC），
    # 驅動擋掉編碼不代表解碼也不能用。auto 讓 ffmpeg 自己挑、失敗自動退回 CPU。
    # 實測發現：軟體濾鏡要用畫格時，GPU→CPU 的搬運成本常常比省下的解碼還貴
    # （在 GTX 1070 上實測慢 30%）。所以預設關閉，要開之前先用 /api/diagnostics/bench 量。
    decode_hwaccel: str = field(default_factory=lambda: _s("FFMPEG_DECODE_HWACCEL", "none").lower() or "none")
    crf: int = field(default_factory=lambda: _i("TRANSCODE_CRF", 21))
    # CPU 編碼速度/畫質取捨。你這台 1080p 有 3x 餘裕，可以往品質那邊調
    x264_preset: str = field(default_factory=lambda: _s("X264_PRESET", "veryfast") or "veryfast")
    # HDR 轉 SDR：auto（4K 用 fast、其餘用 quality）/ quality / fast / off
    hdr_tonemap: str = field(default_factory=lambda: _s("HDR_TONEMAP", "auto").lower() or "auto")
    hdr_tonemap_algo: str = field(default_factory=lambda: _s("HDR_TONEMAP_ALGO", "hable") or "hable")
    max_height: int = field(default_factory=lambda: _i("TRANSCODE_MAX_HEIGHT", 1080))
    hls_prefetch: int = field(default_factory=lambda: _i("HLS_PREFETCH_SEGMENTS", 3))
    # 轉碼速度低於這個倍數就停止預轉，把 CPU 全留給使用者正在等的那一段
    prefetch_min_speed: float = field(
        default_factory=lambda: float(_s("PREFETCH_MIN_SPEED", "1.5") or 1.5))
    hls_cache_max_mb: int = field(default_factory=lambda: _i("HLS_CACHE_MAX_MB", 4096))

    # ---- 登入驗證 ----
    auth_enabled: bool = field(default_factory=lambda: _b("AUTH_ENABLED", False))
    auth_password: str = field(default_factory=lambda: _s("AUTH_PASSWORD", ""))
    # 唯讀密碼。設了之後，用這組密碼登入的人只能看與播，不能下載、
    # 不能瀏覽 FTP、不能觸發掃描或清快取。留空 = 不開放唯讀登入。
    viewer_password: str = field(default_factory=lambda: _s("VIEWER_PASSWORD", ""))
    auth_secret: str = field(default_factory=lambda: _s("AUTH_SECRET", ""))
    session_days: int = field(default_factory=lambda: _i("SESSION_DAYS", 30))
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
    google_allowed_domains: str = field(default_factory=lambda: _s("GOOGLE_ALLOWED_DOMAINS", ""))
    # 這些 email 一登入就是管理員，且免審核。第一個管理員一定要用這個設進來，
    # 否則沒有人有權限去審核別人 —— 會變成所有人都卡在待審核。
    google_admin_emails: str = field(default_factory=lambda: _s("GOOGLE_ADMIN_EMAILS", ""))
    # 新帳號是否自動核准（角色仍是唯讀）。預設 false：陌生人登入後只會看到
    # 「等待管理員核准」，看不到任何影片。
    google_auto_approve: bool = field(default_factory=lambda: _b("GOOGLE_AUTO_APPROVE", False))

    # ---- 遠端畫質 ----
    remote_max_height: int = field(default_factory=lambda: _i("REMOTE_MAX_HEIGHT", 720))
    remote_bitrate_kbps: int = field(default_factory=lambda: _i("REMOTE_BITRATE_KBPS", 2800))
    lan_bitrate_kbps: int = field(default_factory=lambda: _i("LAN_BITRATE_KBPS", 0))
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
    scan_exclude_dir_prefixes: str = field(
        default_factory=lambda: _s("SCAN_EXCLUDE_DIR_PREFIXES", ""))
    # 掃描時要略過的副檔名，逗號分隔，不分大小寫，寫不寫點都可以。
    # 例如 SCAN_EXCLUDE_EXTS=iso,ts,m2ts —— 想跳過藍光原盤或錄影檔時很有用。
    scan_exclude_exts: str = field(default_factory=lambda: _s("SCAN_EXCLUDE_EXTS", ""))
    # 只收錄這些副檔名（留空 = 不限制）。設了之後排除清單仍然有效，
    # 兩個都符合才會被收進來。
    scan_only_exts: str = field(default_factory=lambda: _s("SCAN_ONLY_EXTS", ""))
    # 相片庫。MIN_FILE_MB 是為影片設的（預設 50MB），套在圖片上會全部濾掉，
    # 所以另外給一個 KB 級的門檻，用來擋掉圖示與版面小圖。
    min_photo_kb: int = field(default_factory=lambda: _i("MIN_PHOTO_KB", 40))
    # 超過這個大小的圖片不讀（掃描檔、大張 TIFF），避免一張圖把記憶體吃光
    max_photo_mb: int = field(default_factory=lambda: _i("MAX_PHOTO_MB", 80))
    audio_channels: int = field(default_factory=lambda: _i("AUDIO_CHANNELS", 2))
    audio_bitrate_kbps: int = field(default_factory=lambda: _i("AUDIO_BITRATE_KBPS", 192))
    # 額外要視為「本地」的網段，逗號分隔。例如把 Tailscale 的 100.64.0.0/10 加進來
    extra_local_networks: str = field(default_factory=lambda: _s("LOCAL_NETWORKS", ""))

    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("PORT", 8080))
    probe_concurrency: int = field(default_factory=lambda: _i("PROBE_CONCURRENCY", 3))
    min_file_mb: int = field(default_factory=lambda: _i("MIN_FILE_MB", 50))
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

    def self_base_url(self) -> str:
        """ffmpeg 讀取影片時用的自身位址 (走本機 HTTP，避免 ffmpeg 處理 FTP 編碼問題)。"""
        return f"http://127.0.0.1:{self.port}"


settings = Settings()

# 設了 Google 登入卻沒開 AUTH_ENABLED，等於前門上鎖、後門大開：
# 中介層在 auth_enabled=false 時是「區網全放行、外網全擋」，Google 登入完全不會被用到。
# 與其讓人以為設好了，不如直接把驗證打開。
if settings.google_enabled and not settings.auth_enabled:
    settings.auth_enabled = True

for _d in (DATA_DIR, IMAGE_DIR, CACHE_DIR, SUB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
