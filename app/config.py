"""設定載入：從 .env 讀取，全部有合理預設值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
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
    audio_channels: int = field(default_factory=lambda: _i("AUDIO_CHANNELS", 2))
    audio_bitrate_kbps: int = field(default_factory=lambda: _i("AUDIO_BITRATE_KBPS", 192))
    # 額外要視為「本地」的網段，逗號分隔。例如把 Tailscale 的 100.64.0.0/10 加進來
    extra_local_networks: str = field(default_factory=lambda: _s("LOCAL_NETWORKS", ""))

    host: str = field(default_factory=lambda: _s("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _i("PORT", 8080))
    probe_concurrency: int = field(default_factory=lambda: _i("PROBE_CONCURRENCY", 3))
    min_file_mb: int = field(default_factory=lambda: _i("MIN_FILE_MB", 50))
    auto_scan_on_start: bool = field(default_factory=lambda: _b("AUTO_SCAN_ON_START", False))

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

for _d in (DATA_DIR, IMAGE_DIR, CACHE_DIR, SUB_DIR):
    _d.mkdir(parents=True, exist_ok=True)
