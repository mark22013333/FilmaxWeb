"""系統參數登記表：哪些設定可以在後台改、改了什麼時候生效、誰說了算。

**核心原則：不要讓 .env 跟資料庫比大小。**

兩個方向各有一種最惡毒的失效模式：

* **DB 贏** → 「改 .env 沒反應」。`.env` 是自架使用者唯一熟悉的介面，
  變成裝飾品之後使用者會認定程式壞了。輪替金鑰時只改 `.env`、
  以為換好了、實際仍用舊值 —— 那是安全事故等級，不是 UX 問題。
* **env 贏** → 「存了但沒生效」。在後台改值、按存檔、看到成功提示、
  行為完全沒變，也沒有任何警告。

選 env 贏，因為它的失效模式是純 UX 問題，**而 UX 問題可以用 UI 解決**：
把「誰在管這一項」變成畫面上看得見的東西。所以 API 不能只回生效值，
要同時回三個候選值、鎖不鎖、以及有沒有被遮蔽（shadowed）。

解析順序只有一條規則，全部一致，沒有例外清單：

    .env / 環境變數  >  資料庫  >  程式內建預設值

**分層是四個機械化問句問出來的**，不是憑感覺分的：

  1. 改錯了會不會讓我連後台都進不去？（或讀它時資料庫還沒連上）  → Tier 0，只能放 .env
  2. 這個值會被當命令、路徑或程式碼執行嗎？（或會擴大信任邊界）  → Tier 4，不放 UI
  3. 洩漏了是外部系統倒楣嗎？                                    → Tier 1，秘密
  4. 啟動時被拿去建立長生命週期的東西了嗎？                       → Tier 3，需重啟／重載
  5. 以上都不是                                                → Tier 2，DB + UI，即時生效

Tier 2 剛好都是改了立刻生效的 —— 這不是巧合，而是問句 4 把「不能即時生效」
的都篩掉了。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("filmax.params")

# 分層
ENV_ONLY = 0        # Tier 0：只能放 .env，UI 上完全不出現
SECRET = 1          # Tier 1：秘密，唯寫欄位
HOT = 2             # Tier 2：DB + UI，即時生效
NEEDS_RELOAD = 3    # Tier 3：要重載或重啟才會生效
NO_UI = 4           # Tier 4：會被當命令或路徑執行，不放 UI

# 生效方式
APPLY_HOT = "hot"           # 讀取一律走 settings.get()，存檔即生效
APPLY_RELOAD = "reload"     # 存檔後重建子系統
APPLY_RESTART = "restart"   # 只寫入，要重開服務


@dataclass
class Param:
    key: str
    tier: int
    type: str = "str"                    # str / int / float / bool
    default: Any = ""
    apply: str = APPLY_RESTART
    section: str = "其他"
    label: str = ""
    help: str = ""
    choices: Optional[List[str]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # 「敏感」不等於「秘密」：MSSQL 主機名、FTP 主機名、媒體目錄絕對路徑
    # 都不是密碼，但它們是內網拓撲情報，不該隨手顯示給唯讀使用者或寫進日誌。
    # 敏感的東西管理員看得到（他本來就要能編輯），秘密則是任何人都看不到。
    sensitive: bool = False
    # 是不是秘密（唯寫、永遠只回遮罩）。
    #
    # 這一項**跟分層是獨立的**。分層講的是「可以在哪裡設定」，秘密講的是
    # 「可不可以讀出來」，兩者無關：AUTH_PASSWORD 是 Tier 0（只能放 .env，
    # 因為改錯了會把自己鎖在外面），但它同時是不折不扣的密碼。
    # 一開始把 secret 寫成 tier == SECRET 的衍生屬性，結果就是 CLI 把
    # AUTH_PASSWORD 明文印在畫面上。
    is_secret: Optional[bool] = None
    # 改錯了可能讓自己進不了後台。UI 要二次確認並顯示 CLI 復原指令。
    lockout: bool = False
    validate: Optional[Callable[[Any], Optional[str]]] = None

    @property
    def secret(self) -> bool:
        return self.tier == SECRET if self.is_secret is None else self.is_secret

    @property
    def in_ui(self) -> bool:
        return self.tier in (SECRET, HOT, NEEDS_RELOAD)

    @property
    def editable(self) -> bool:
        """能不能存進資料庫。Tier 0 與 Tier 4 只能走 .env。"""
        return self.tier in (SECRET, HOT, NEEDS_RELOAD)


def _p(key, tier, **kw) -> Param:
    return Param(key=key, tier=tier, **kw)


def _known_codec(name: str) -> bool:
    import codecs
    try:
        codecs.lookup(str(name))
        return True
    except (LookupError, TypeError):
        return False


def _positive(v):
    return None if v > 0 else "要大於 0"


def _non_negative(v):
    return None if v >= 0 else "不能是負數"


# --------------------------------------------------------------------------
# 登記表
# --------------------------------------------------------------------------
_ALL: List[Param] = [
    # ---- Tier 0：只能放 .env ----
    # 問句 1：改錯了連後台都進不去，或讀它的時候資料庫根本還沒連上。
    _p("HOST", ENV_ONLY, section="伺服器", lockout=True),
    _p("PORT", ENV_ONLY, type="int", default=8080, section="伺服器", lockout=True),
    _p("AUTH_ENABLED", ENV_ONLY, type="bool", default=False, section="登入", lockout=True),
    _p("AUTH_PASSWORD", ENV_ONLY, is_secret=True, section="登入", sensitive=True, lockout=True),
    _p("VIEWER_PASSWORD", ENV_ONLY, is_secret=True, section="登入", sensitive=True, lockout=True),
    _p("AUTH_SECRET", ENV_ONLY, is_secret=True, section="登入", sensitive=True, lockout=True),
    _p("COOKIE_SECURE", ENV_ONLY, type="bool", default=False, section="登入", lockout=True),
    _p("USER_STORE", ENV_ONLY, section="資料庫"),
    _p("MEDIA_STORE", ENV_ONLY, section="資料庫"),
    _p("MSSQL_HOST", ENV_ONLY, section="資料庫", sensitive=True),
    _p("MSSQL_PORT", ENV_ONLY, type="int", default=1433, section="資料庫"),
    _p("MSSQL_DATABASE", ENV_ONLY, section="資料庫", sensitive=True),
    _p("MSSQL_USER", ENV_ONLY, section="資料庫", sensitive=True),
    _p("MSSQL_PASSWORD", ENV_ONLY, is_secret=True, section="資料庫", sensitive=True),
    _p("MSSQL_DRIVER", ENV_ONLY, section="資料庫"),
    _p("MSSQL_ENCRYPT", ENV_ONLY, type="bool", default=True, section="資料庫"),
    _p("MSSQL_TRUST_CERT", ENV_ONLY, type="bool", default=False, section="資料庫"),
    _p("MSSQL_TIMEOUT", ENV_ONLY, type="int", default=10, section="資料庫"),
    _p("MSSQL_ODBC_DSN", ENV_ONLY, is_secret=True, section="資料庫", sensitive=True),

    # ---- Tier 4：會被當命令或路徑執行，不放 UI ----
    # 問句 2。Navidrome 把轉碼設定 UI 預設關閉，理由寫在他們的安全文件裡：
    # 「它讓攻擊者可以在你的伺服器上執行任意命令」。FFMPEG_PATH 是同一類東西。
    _p("FFMPEG_PATH", NO_UI, default="ffmpeg", section="轉碼"),
    _p("FFPROBE_PATH", NO_UI, default="ffprobe", section="轉碼"),
    _p("LIBRARY_ROOTS", NO_UI, default="/|auto", section="媒體庫", sensitive=True),
    # 第 −1 層：FTP 路徑 → 本機路徑。是路徑、會擴大服務讀得到的範圍，所以跟
    # LIBRARY_ROOTS 同一類，不進 UI。
    _p("LIBRARY_LOCAL_ROOTS", NO_UI, section="媒體庫", sensitive=True),
    _p("TRUST_PROXY", NO_UI, type="bool", default=False, section="網路"),
    _p("LOCAL_NETWORKS", NO_UI, section="網路", sensitive=True),

    # ---- Tier 1：秘密 ----
    # 問句 3。唯寫欄位，API 任何情況都不回明文。
    _p("TMDB_API_KEY", SECRET, apply=APPLY_HOT, section="刮削",
       label="TMDB API 金鑰", help="沒有它就不會去抓海報與簡介"),
    _p("FTP_PASSWORD", SECRET, apply=APPLY_RELOAD, section="FTP", label="FTP 密碼"),
    _p("GOOGLE_CLIENT_SECRET", SECRET, apply=APPLY_RELOAD, section="登入",
       label="Google 用戶端密鑰"),

    # ---- Tier 3：要重載或重啟 ----
    # 問句 4：啟動時被拿去建立長生命週期的東西（連線池、執行緒池、快取）。
    _p("FTP_HOST", NEEDS_RELOAD, default="127.0.0.1", apply=APPLY_RELOAD,
       section="FTP", label="FTP 主機", sensitive=True),
    _p("FTP_PORT", NEEDS_RELOAD, type="int", default=21, apply=APPLY_RELOAD,
       section="FTP", label="FTP 連接埠", minimum=1, maximum=65535),
    _p("FTP_USER", NEEDS_RELOAD, default="anonymous", apply=APPLY_RELOAD,
       section="FTP", label="FTP 帳號"),
    _p("FTP_TLS", NEEDS_RELOAD, type="bool", default=False, apply=APPLY_RELOAD,
       section="FTP", label="使用 FTPS"),
    _p("FTP_PASSIVE", NEEDS_RELOAD, type="bool", default=True, apply=APPLY_RELOAD,
       section="FTP", label="被動模式"),
    # 這裡刻意不列白名單。Python 認得的編碼有一百多種，還有一堆別名
    # （big5 / cp950 / ms950 是同一個東西）。列白名單的結果是：某個人的
    # NAS 用 cp950 用得好好的，升級之後服務就開不起來了。
    # 改成問 Python 認不認得 —— 那才是「這個值能不能用」的真正判準。
    _p("FTP_ENCODING", NEEDS_RELOAD, default="utf-8", apply=APPLY_RELOAD,
       section="FTP", label="檔名編碼",
       help="常見的有 utf-8、big5（等同 cp950）、gbk。填 Python 認得的編碼名稱都可以",
       validate=lambda v: None if _known_codec(v) else "Python 不認得這個編碼名稱"),
    _p("FTP_TIMEOUT", NEEDS_RELOAD, type="int", default=20, apply=APPLY_RELOAD,
       section="FTP", label="連線逾時（秒）", minimum=1, maximum=300),
    _p("FFMPEG_HWACCEL", NEEDS_RELOAD, default="auto", apply=APPLY_RESTART,
       section="轉碼", label="硬體編碼",
       choices=["auto", "none", "nvenc", "qsv", "amf", "videotoolbox"],
       help="啟動時會實際試編一小段來決定，改完要重開服務"),
    _p("FFMPEG_DECODE_HWACCEL", NEEDS_RELOAD, default="none", apply=APPLY_RESTART,
       section="轉碼", label="硬體解碼",
       choices=["auto", "none", "cuda", "qsv", "d3d11va", "videotoolbox"],
       help="跟編碼是不同的硬體單元。開之前先跑一次轉碼實測，GPU→CPU 搬運常常更貴"),
    _p("HLS_CACHE_MAX_MB", NEEDS_RELOAD, type="int", default=4096, apply=APPLY_RELOAD,
       section="轉碼", label="轉碼快取上限（MB）", minimum=256),
    _p("PROBE_CONCURRENCY", NEEDS_RELOAD, type="int", default=3, apply=APPLY_RESTART,
       section="媒體庫", label="探測併發數", minimum=1, maximum=32),
    _p("AUTO_SCAN_ON_START", NEEDS_RELOAD, type="bool", default=False, apply=APPLY_RESTART,
       section="媒體庫", label="啟動時自動掃描"),
    _p("SESSION_DAYS", NEEDS_RELOAD, type="int", default=30, apply=APPLY_HOT,
       section="登入", label="登入有效天數", minimum=1, maximum=3650),
    _p("GOOGLE_CLIENT_ID", NEEDS_RELOAD, apply=APPLY_RELOAD, section="登入",
       label="Google 用戶端 ID"),
    _p("PUBLIC_BASE_URL", NEEDS_RELOAD, apply=APPLY_RELOAD, section="登入",
       label="對外網址", lockout=True,
       help="Google 的回呼網址由它組出來。填錯會讓 Google 登入失效"),
    _p("GOOGLE_REDIRECT_URI", NEEDS_RELOAD, apply=APPLY_RELOAD, section="登入",
       label="回呼網址（通常留空）", lockout=True),
    _p("GOOGLE_ALLOWED_DOMAINS", NEEDS_RELOAD, apply=APPLY_HOT, section="登入",
       label="允許的網域", help="逗號分隔，留空 = 任何 Google 帳號都能送出申請"),
    _p("GOOGLE_ADMIN_EMAILS", NEEDS_RELOAD, apply=APPLY_HOT, section="登入",
       label="管理員 email", lockout=True,
       help="這裡列到的人一登入就是管理員且免審核。清空可能讓你失去管理權"),
    _p("GOOGLE_AUTO_APPROVE", NEEDS_RELOAD, type="bool", default=False, apply=APPLY_HOT,
       section="登入", label="新帳號自動核准"),

    # ---- Tier 2：DB + UI，即時生效 ----
    _p("TMDB_LANGUAGE", HOT, default="zh-TW", apply=APPLY_HOT, section="刮削",
       label="刮削語言"),
    _p("TMDB_FALLBACK_LANGUAGE", HOT, default="en-US", apply=APPLY_HOT, section="刮削",
       label="退回語言", help="刮不到中文簡介時改用這個語言"),
    _p("TMDB_RATE_LIMIT", HOT, type="int", default=8, apply=APPLY_HOT, section="刮削",
       label="每秒請求上限", minimum=1, maximum=50),

    _p("TRANSCODE_CRF", HOT, type="int", default=21, apply=APPLY_HOT, section="轉碼",
       label="畫質 CRF", minimum=0, maximum=51,
       help="越小畫質越好、檔越大。硬體編碼會換算成 cq"),
    _p("NVENC_CQ", HOT, type="int", default=28, apply=APPLY_HOT, section="轉碼",
       label="NVENC 畫質 CQ", minimum=0, maximum=51,
       help="硬體編碼自己的一把尺，不要跟 CRF 共用。實測同樣寫 21 時 NVENC 會吐出"
            "接近原檔的位元率（幾乎沒壓縮），28～32 才對得上 x264 crf 21 的區間"),
    _p("TRANSCODE_MAX_HEIGHT", HOT, type="int", default=1080, apply=APPLY_HOT,
       section="轉碼", label="解析度上限", minimum=0, maximum=4320,
       help="0 = 不縮放（真的不縮放，不是退回預設值）"),
    _p("X264_PRESET", HOT, default="veryfast", apply=APPLY_HOT, section="轉碼",
       label="x264 preset",
       choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow"]),
    _p("HDR_TONEMAP", HOT, default="auto", apply=APPLY_HOT, section="轉碼",
       label="HDR 轉 SDR", choices=["auto", "quality", "fast", "off"]),
    # `bt2390` 原本列在選項裡，但 **CPU 的 tonemap 濾鏡不接受這個值** ——
    # 它只認 none/linear/gamma/clip/reinhard/hable/mobius（bt2390 是 libplacebo
    # 與 tonemap_opencl 才有的）。選到它的話濾鏡鏈直接失敗，HDR 片源的每一段
    # 轉碼都會 500。已從選項移除。
    _p("HDR_TONEMAP_ALGO", HOT, default="hable", apply=APPLY_HOT, section="轉碼",
       label="色調映射演算法",
       choices=["hable", "mobius", "reinhard", "linear", "gamma", "clip"],
       help="hable 明暗兼顧（建議）；mobius 保色但亮部容易過曝；reinhard 最快、對比最平"),
    _p("HLS_SEGMENT_SECONDS", HOT, type="int", default=6, apply=APPLY_HOT,
       section="轉碼", label="分段長度（秒）", minimum=1, maximum=30),
    _p("HLS_TWO_RUNG", HOT, type="bool", default=False, apply=APPLY_HOT,
       section="轉碼", label="兩階 HLS（remux 上階）",
       help="開了之後：區網發單一的原檔 remux（零轉碼），遠端發兩階讓播放器自己選。"
            "只有掃過 keyframe 邊界表、而且 keyframe 間距小於分段長度的 h264 片源才有上階"),
    _p("HLS_PREFETCH_SEGMENTS", HOT, type="int", default=3, apply=APPLY_HOT,
       section="轉碼", label="預轉段數", minimum=0, maximum=20),
    _p("PREFETCH_MIN_SPEED", HOT, type="float", default=1.5, apply=APPLY_HOT,
       section="轉碼", label="預轉最低速度倍數", minimum=0,
       help="轉碼速度低於這個倍數就停止預轉，把 CPU 留給正在等的那一段"),
    _p("AUDIO_CHANNELS", HOT, type="int", default=2, apply=APPLY_HOT, section="轉碼",
       label="輸出聲道數", minimum=0, maximum=8,
       help="2 = 降混成立體聲（相容性最好）；0 = 不動原始聲道"),
    _p("AUDIO_BITRATE_KBPS", HOT, type="int", default=192, apply=APPLY_HOT,
       section="轉碼", label="音訊位元率（kbps）", minimum=32, maximum=640),

    _p("REMOTE_MAX_HEIGHT", HOT, type="int", default=720, apply=APPLY_HOT,
       section="遠端畫質", label="遠端解析度上限", minimum=0, maximum=4320,
       help="0 = 不縮放"),
    _p("REMOTE_BITRATE_KBPS", HOT, type="int", default=2800, apply=APPLY_HOT,
       section="遠端畫質", label="遠端位元率上限（kbps）", minimum=0),
    _p("LAN_BITRATE_KBPS", HOT, type="int", default=0, apply=APPLY_HOT,
       section="遠端畫質", label="區網位元率上限（kbps）", minimum=0,
       help="0 = 不鎖"),

    _p("MIN_FILE_MB", HOT, type="int", default=50, apply=APPLY_HOT, section="媒體庫",
       label="影片最小大小（MB）", minimum=0,
       help="小於這個大小的影片會被忽略，用來擋預告片與樣本檔"),
    _p("SCAN_EXCLUDE_DIR_PREFIXES", HOT, apply=APPLY_HOT, section="媒體庫",
       label="略過的資料夾前綴", help="逗號分隔，比對名稱開頭，整棵子樹都不走訪"),
    _p("SCAN_EXCLUDE_EXTS", HOT, apply=APPLY_HOT, section="媒體庫",
       label="略過的副檔名", help="逗號分隔。跟下面同時設定時，這裡優先"),
    _p("SCAN_ONLY_EXTS", HOT, apply=APPLY_HOT, section="媒體庫",
       label="只收這些副檔名", help="留空 = 不限制"),

    _p("MIN_PHOTO_KB", HOT, type="int", default=40, apply=APPLY_HOT, section="相片庫",
       label="相片最小大小（KB）", minimum=0, help="用來擋圖示與版面小圖"),
    _p("MAX_PHOTO_MB", HOT, type="int", default=80, apply=APPLY_HOT, section="相片庫",
       label="相片最大大小（MB）", minimum=1),
    _p("PHOTO_EXCLUDE_VIDEO_ARTWORK", HOT, type="bool", default=True, apply=APPLY_HOT,
       section="相片庫", label="排除影片封面",
       help="關掉的話點「相片」會看到一整牆的影片海報"),
    _p("SUBTITLE_PREFETCH", HOT, type="bool", default=True, apply=APPLY_HOT,
       section="字幕", label="開播後在背景把字幕抽好",
       help="內嵌字幕要把整部片 demux 一遍才收得齊，大檔案動輒幾十秒。"
            "開著的話播放器會在開播幾秒後、於背景先抽好放進快取，"
            "按下字幕時就不用等；抽好的快取在來源檔沒換過之前一直有效。"
            "代價是會跟影片本身的串流搶一點 FTP 頻寬 —— "
            "如果你幾乎不看字幕，關掉可以省下那次 demux"),
    _p("PHOTO_PREVIEW_PX", HOT, type="int", default=1920, apply=APPLY_HOT,
       section="相片庫", label="看大圖的長邊像素", minimum=640, maximum=6000,
       help="燈箱載入的衍生圖尺寸。放大到超過這個像素時前端會自己換成原圖。"
            "改了之後舊的衍生圖不會被刪，新尺寸會在下次點開時產生"),
]

REGISTRY: Dict[str, Param] = {p.key: p for p in _ALL}
SECTIONS = list(dict.fromkeys(p.section for p in _ALL if p.in_ui))


# --------------------------------------------------------------------------
# 型別轉換與驗證
# --------------------------------------------------------------------------
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def coerce(p: Param, raw: Any) -> Any:
    """字串 → 目標型別。轉不出來就丟 ValueError，不要靜靜退回預設值。

    這一點跟舊的 _i()/_b() 不一樣：那些轉失敗會安靜地用預設值，
    於是 `TRANSCODE_CRF=高畫質` 完全沒有徵兆，使用者只會覺得設定沒有作用。
    """
    if raw is None:
        return None
    if p.type == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f"{p.key} 要是 true 或 false，收到 {raw!r}")
    if p.type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{p.key} 要是整數，收到 {raw!r}") from None
    if p.type == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{p.key} 要是數字，收到 {raw!r}") from None
    return str(raw).strip()


def normalize(p: Param, value: Any) -> Any:
    """把等價的寫法收成同一個。

    這是為了不要因為大小寫讓服務起不來。舊的讀法是 `_s(...).lower()`，
    也就是 `HDR_TONEMAP=AUTO` 一直都能用；新的驗證如果直接判它無效，
    升級之後使用者的服務會**開不起來**，而他什麼都沒改。
    大小寫不是「設錯了」，是同一個值的另一種寫法。
    """
    if p.choices and isinstance(value, str):
        for c in p.choices:
            if value.lower() == str(c).lower():
                return c
    return value


def check(p: Param, value: Any) -> Optional[str]:
    """回傳錯誤訊息，沒問題就 None。"""
    if p.choices and value not in p.choices:
        return f"只能是 {' / '.join(p.choices)}"
    if p.minimum is not None and isinstance(value, (int, float)) and value < p.minimum:
        return f"不能小於 {p.minimum:g}"
    if p.maximum is not None and isinstance(value, (int, float)) and value > p.maximum:
        return f"不能大於 {p.maximum:g}"
    if p.validate:
        return p.validate(value)
    return None


def parse(p: Param, raw: Any) -> Any:
    """轉型 + 正規化 + 驗證，一次做完。任何一關不過就丟 ValueError。"""
    v = normalize(p, coerce(p, raw))
    err = check(p, v)
    if err:
        raise ValueError(f"{p.key} {err}（收到 {raw!r}）")
    return v


def as_text(p: Param, value: Any) -> str:
    """存進資料庫用的字串形式。"""
    if p.type == "bool":
        return "true" if value else "false"
    return "" if value is None else str(value)


# --------------------------------------------------------------------------
# .env 的錯字
# --------------------------------------------------------------------------
# TMDB_API_KAY=xxx 現在是完全靜默失效的：程式讀不到那個鍵，
# 使用者以為設好了，而沒有任何一行訊息提到它。
_KNOWN_EXTRA = {"FILMAX_DATA_DIR", "DEV_RELOAD"}


def _similar(a: str, b: str) -> int:
    """兩個鍵有多像。夠像才值得說「你是不是想打這個」。"""
    if a == b:
        return 100
    if abs(len(a) - len(b)) > 3:
        return 0
    same = sum(1 for x, y in zip(a, b) if x == y)
    return int(same * 100 / max(len(a), len(b)))


def unknown_env_keys(env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """.env 裡有、但程式不認得的鍵。回傳 (鍵, 最像的已知鍵或空字串)。"""
    known = set(REGISTRY) | _KNOWN_EXTRA
    src = env if env is not None else _dotenv_keys()
    out = []
    for k in src:
        if k in known or not re.match(r"^[A-Z][A-Z0-9_]*$", k):
            continue
        best, score = "", 0
        for cand in known:
            s = _similar(k, cand)
            if s > score:
                best, score = cand, s
        out.append((k, best if score >= 70 else ""))
    return sorted(out)


def _dotenv_keys() -> Dict[str, str]:
    """讀 .env 檔本身（不是行程環境變數）—— 錯字是打在檔案裡的。"""
    from .config import BASE_DIR
    path = BASE_DIR / ".env"
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# 秘密的加密
# --------------------------------------------------------------------------
# 秘密進資料庫等於「資料庫備份 = 秘密備份」。所以存的時候加密，
# 而加密金鑰本身是 Tier 0（只能放 .env / secret.key），不進資料庫 ——
# 金鑰跟密文放在同一個地方等於沒有加密。
_PREFIX = "enc:v1:"


def _key() -> bytes:
    from .auth import _secret
    return hashlib.sha256(b"filmax-config-v1" + _secret()).digest()


def encrypt(plain: str) -> str:
    """對稱加密。密文帶固定前綴，看到就知道這一格是加密過的。

    用 HMAC 產生 keystream 做 XOR，再附上認證碼：標準函式庫就有，
    不必為了三個欄位拉一個加密相依進來（自架的人裝套件失敗就用不了）。
    """
    if plain is None:
        return ""
    data = plain.encode("utf-8")
    nonce = os.urandom(16)
    stream = _stream(nonce, len(data))
    ct = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16]
    return _PREFIX + base64.b64encode(nonce + tag + ct).decode()


def decrypt(blob: str) -> Optional[str]:
    if not blob or not blob.startswith(_PREFIX):
        return None
    try:
        raw = base64.b64decode(blob[len(_PREFIX):])
        nonce, tag, ct = raw[:16], raw[16:32], raw[32:]
        if not hmac.compare_digest(
                tag, hmac.new(_key(), nonce + ct, hashlib.sha256).digest()[:16]):
            log.warning("設定的密文認證失敗 —— 金鑰換過了？當成沒有設定。")
            return None
        stream = _stream(nonce, len(ct))
        return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")
    except Exception as e:
        log.warning("解不開設定密文：%s", e)
        return None


def _stream(nonce: bytes, n: int) -> bytes:
    out = b""
    ctr = 0
    while len(out) < n:
        out += hmac.new(_key(), nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
        ctr += 1
    return out[:n]


MASK = "••••••••"


def mask(value: Optional[str]) -> str:
    """秘密一律回這個。不回長度提示 —— 長度也是情報。"""
    return MASK if value else ""


# --------------------------------------------------------------------------
# 日誌過濾
# --------------------------------------------------------------------------
class SecretFilter(logging.Filter):
    """把秘密的實際值從日誌訊息裡換成遮罩。

    **掛成 logger filter，不在每個呼叫點各自處理。** 逐點處理的問題是它
    只能防住你想得到的那些點：一個 `log.debug("連線字串 %s", cs)`
    就前功盡棄，而且那種漏洞通常是在出事之後翻日誌才發現的。
    掛在這裡的話，訊息不管從哪裡來都會經過同一道關卡。

    只換掉「夠長、值得保護」的值：三個字元的密碼換掉之後，日誌會變成
    一堆莫名其妙的遮罩（任何訊息裡出現那三個字元都會中）。
    """

    MIN_LEN = 6

    def __init__(self):
        super().__init__()
        self._values: tuple = ()
        self._at = 0.0

    def _secrets(self) -> tuple:
        import time
        now = time.time()
        if now - self._at < 5 and self._values:
            return self._values
        vals = []
        try:
            from . import paramstore
            for key, p in REGISTRY.items():
                if not p.secret:
                    continue
                v = paramstore.env_raw(key) or (paramstore.db_raw(key) if p.editable else None)
                if v and len(v) >= self.MIN_LEN:
                    vals.append(v)
        except Exception:
            pass
        self._values = tuple(sorted(vals, key=len, reverse=True))
        self._at = now
        return self._values

    def filter(self, record: logging.LogRecord) -> bool:
        vals = self._secrets()
        if not vals:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        hit = False
        for v in vals:
            if v in msg:
                msg = msg.replace(v, MASK)
                hit = True
        if hit:
            record.msg = msg
            record.args = ()
        return True


def install_log_filter() -> None:
    f = SecretFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    # uvicorn 自己掛 handler，不吃 root 的 filter
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(f)
