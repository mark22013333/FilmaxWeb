"""檔名解析：把 release 命名還原成 片名 / 年份 / 季集 / 品質標籤。"""
from __future__ import annotations

import posixpath
import re
import unicodedata

from . import timeparse
from dataclasses import dataclass, field
from typing import List, Optional

# 需要從標題中剔除的技術標籤
JUNK_TOKENS = [
    r"2160p", r"1080p", r"1080i", r"720p", r"576p", r"480p", r"4k", r"8k", r"uhd",
    r"x264", r"x265", r"h\.?264", r"h\.?265", r"hevc", r"avc", r"av1", r"xvid", r"divx",
    r"10bit", r"8bit", r"hi10p", r"hdr10\+?", r"hdr", r"dolby.?vision", r"dovi", r"sdr",
    r"bluray", r"blu-ray", r"bdrip", r"brrip", r"bdremux", r"remux", r"web-?dl", r"web-?rip",
    r"webrip", r"hdtv", r"hdrip", r"dvdrip", r"dvdscr", r"hdcam", r"ts", r"tc",
    r"amzn", r"nf", r"netflix", r"disney\+?", r"dsnp", r"hmax", r"atvp", r"hulu", r"iqiyi",
    r"ddp?5[\. ]1", r"dd\+?", r"eac3", r"ac3", r"aac(?:2\.0|5\.1)?", r"dts(?:-hd)?(?:[\. ]ma)?",
    r"truehd", r"atmos", r"flac", r"opus", r"mp3", r"7\.1", r"5\.1", r"2\.0",
    r"repack", r"proper", r"internal", r"limited", r"extended", r"uncut", r"remastered",
    r"directors?[\. ]cut", r"imax", r"criterion", r"complete",
    r"chs", r"cht", r"gb", r"big5", r"简体", r"繁體", r"繁体", r"中英", r"双语", r"雙語",
    r"中字", r"中文字幕", r"内嵌", r"內嵌", r"外挂", r"外掛", r"国语", r"國語", r"粤语", r"粵語",
    r"多国语言", r"多國語言", r"无水印", r"無水印", r"高清", r"超清", r"藍光", r"蓝光",
    r"subs?", r"multi", r"dual", r"vostfr",
]
JUNK_RE = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(JUNK_TOKENS) + r")(?![A-Za-z0-9])", re.I)

# 發布組 [xxx] 或結尾 -GROUP
BRACKET_RE = re.compile(r"[\[\(【（][^\]\)】）]{0,40}[\]\)】）]")
GROUP_TAIL_RE = re.compile(r"-[A-Za-z0-9]{2,15}$")

YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

SE_PATTERNS = [
    re.compile(r"(?<![a-z0-9])s(?P<s>\d{1,2})[\s\._-]*e(?P<e>\d{1,4})(?![0-9])", re.I),
    re.compile(r"(?<![a-z0-9])(?P<s>\d{1,2})x(?P<e>\d{1,3})(?![0-9])", re.I),
    re.compile(r"第\s*(?P<s>[0-9一二三四五六七八九十]{1,3})\s*季.*?第\s*(?P<e>[0-9一二三四五六七八九十百]{1,4})\s*[集话話]"),
    re.compile(r"season[\s\._-]*(?P<s>\d{1,2}).*?episode[\s\._-]*(?P<e>\d{1,4})", re.I),
]
EP_ONLY_PATTERNS = [
    re.compile(r"(?<![a-z0-9])e(?:p|pisode)?[\s\._-]*(?P<e>\d{1,4})(?![0-9])", re.I),
    re.compile(r"第\s*(?P<e>[0-9一二三四五六七八九十百]{1,4})\s*[集话話]"),
    re.compile(r"(?<![\d\w])(?P<e>\d{1,3})[\s\._-]*(?:集|话|話)"),
    # 動畫常見: "作品名 - 12" / "作品名 - 12v2"
    re.compile(r"\s[-–—]\s*(?P<e>\d{1,3})(?:v\d)?\s*$"),
]
SEASON_DIR_PATTERNS = [
    re.compile(r"(?<![a-z0-9])s(?:eason)?[\s\._-]*(?P<s>\d{1,2})(?![0-9])", re.I),
    re.compile(r"第\s*(?P<s>[0-9一二三四五六七八九十]{1,3})\s*季"),
]

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

RES_RE = re.compile(r"(2160p|1080p|720p|480p|4k|8k)", re.I)
SOURCE_RE = re.compile(r"(bluray|blu-ray|bdrip|remux|web-?dl|webrip|hdtv|dvdrip|hdrip)", re.I)


def cn_to_int(s: str) -> Optional[int]:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:2], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return CN_NUM.get(a, 0) * 10 + (CN_NUM.get(b, 0) if b else 0)
    total = 0
    for ch in s:
        if ch in CN_NUM:
            total = total * 10 + CN_NUM[ch]
        else:
            return None
    return total or None


@dataclass
class ParsedName:
    raw: str
    title: str = ""
    alt_title: str = ""          # 通常是中文片名 / 英文片名的另一個
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    is_tv: bool = False
    resolution: str = ""
    source: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def search_title(self) -> str:
        return self.title or self.alt_title or self.raw


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = BRACKET_RE.sub(" ", text)
    text = text.replace("_", " ").replace("+", " ")
    # 只有在看起來像 release 命名（點很多、沒空白）時才把點當分隔符
    if text.count(".") >= 2 and " " not in text.replace(".", " ").strip()[:0] or text.count(".") >= 2:
        text = text.replace(".", " ")
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip(" -._")


def _split_titles(text: str) -> tuple[str, str]:
    """中文片名與英文片名常並列，拆成兩個以便刮削時都試。"""
    text = text.strip()
    has_cjk = re.search(r"[一-鿿぀-ヿ]", text)
    if not has_cjk:
        return text, ""
    # 中文區塊 + 之後的英文區塊
    m = re.match(r"^\s*([^\x00-\x7F][^A-Za-z]*?)\s*[\-–—:：/|]?\s*([A-Za-z][A-Za-z0-9\s'&,\.\-:!\?]{2,})$", text)
    if m:
        cn, en = m.group(1).strip(" -._:："), m.group(2).strip(" -._:：")
        if cn and en:
            return cn, en
    # 分隔符切一刀
    for sep in ("/", "|", " - ", "－"):
        if sep in text:
            a, _, b = text.partition(sep)
            a, b = a.strip(), b.strip()
            if a and b and bool(re.search(r"[一-鿿]", a)) != bool(re.search(r"[一-鿿]", b)):
                return a, b
    return text, ""


# 「一集一資料夾」的資料夾名稱：純數字，或 EP01 / E01 / 第01集 這類
EPISODE_DIR_RE = re.compile(
    r"^(?:ep?|e|第)?\s*(?P<e>\d{1,3})\s*(?:集|話|话)?$", re.I)


def _episode_dir_num(name: str) -> Optional[int]:
    """這個資料夾名稱看起來像不像「第 N 集」，是的話回傳 N。"""
    m = EPISODE_DIR_RE.match(unicodedata.normalize("NFKC", (name or "").strip()))
    if not m:
        return None
    n = int(m.group("e"))
    return n if 0 < n <= 999 else None


# 檔名尾端的集數：標題後面緊接 1~3 位數字，中間可以有空白或分隔符
TRAILING_EP_RE = re.compile(r"^(?P<base>.*?)[\s._\-]*(?P<e>\d{1,3})$")


def _trailing_episode(title_part: str, sibling_files: Optional[List[str]]) -> Optional[tuple]:
    """檔名尾端的數字是不是集數。

    **單看一個檔名是判斷不出來的** —— 「玩命關頭 7」的 7 是片名的一部分，
    「來！金來號 ！01」的 01 是集數。差別在於同一群檔案裡有沒有別人
    共用同樣的前綴、只有尾數不同。所以這條規則需要兄弟檔案的資訊，
    這也是為什麼解析器要從「逐檔判斷」改成「知道群組」。
    """
    if not sibling_files or len(sibling_files) < 2:
        return None
    m = TRAILING_EP_RE.match(title_part.strip())
    if not m:
        return None
    base = m.group("base").strip(" ._-")
    if len(base) < 2:
        return None                      # 純數字檔名交給別的規則處理
    ep = int(m.group("e"))
    if not (0 < ep <= 999):
        return None
    # 同一個資料夾裡，有幾個檔案共用這個前綴但尾數不同
    others = 0
    seen_nums = set()
    for other in sibling_files:
        stem = other.rsplit(".", 1)[0] if "." in other else other
        om = TRAILING_EP_RE.match(_clean(stem).strip())
        if not om:
            continue
        if om.group("base").strip(" ._-").lower() == base.lower():
            n = int(om.group("e"))
            seen_nums.add(n)
            others += 1
    if others < 2 or len(seen_nums) < 2:
        return None
    if not _looks_like_episodes(seen_nums, m.group("e")):
        return None
    return base, ep


def _looks_like_episodes(nums: set, raw: str) -> bool:
    """這一組數字看起來是集數還是續集編號。

    「蠟筆小新 01 / 02 / 03」是集數，「玩命關頭 7 / 8 / 9」是續集 ——
    兩者的檔名結構一模一樣，光看單一檔案分不出來。兩個訊號可以分：

      補零   01、02 是集數的寫法；續集不會寫成「玩命關頭 07」
      起點   一整組集數幾乎都從 1 或 2 開始；續集是從系列的中段開始編號

    任一成立就當集數。兩個都不成立（沒補零又從 7 開始）就是續集。

    真的分不出來的情況仍然存在：整組 1~9 又沒補零的電影系列會被誤判。
    那種情況要靠 LIBRARY_ROOTS 把該目錄標成 movie，解析器不該假裝分得出來。
    """
    if len(raw) >= 2 and raw[0] == "0":
        return True                      # 補零
    return min(nums) <= 2                # 從頭開始編號


def parse(filename: str, parent_dirs: Optional[List[str]] = None,
          sibling_dirs: Optional[List[str]] = None,
          sibling_files: Optional[List[str]] = None) -> ParsedName:
    """parent_dirs: 由外而內的上層資料夾名稱（用來補季別與劇名）。

    sibling_dirs:  這個檔案所在資料夾的**兄弟資料夾**名稱。
    sibling_files: 同一個資料夾裡的其他檔名。

    後面兩個是為了判斷「一集一資料夾」與「檔名尾端的數字是不是集數」——
    這兩件事逐檔判斷永遠判斷不出來，一定要看群組。
    """
    parent_dirs = parent_dirs or []
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    p = ParsedName(raw=stem)

    m = RES_RE.search(stem)
    if m:
        p.resolution = m.group(1).lower()
    m = SOURCE_RE.search(stem)
    if m:
        p.source = m.group(1).lower()

    work = _clean(stem)

    # --- 季集 ---
    cut_at = len(work)
    for pat in SE_PATTERNS:
        m = pat.search(work)
        if m:
            p.season = cn_to_int(m.group("s"))
            p.episode = cn_to_int(m.group("e"))
            p.is_tv = True
            cut_at = min(cut_at, m.start())
            break
    if p.episode is None:
        for pat in EP_ONLY_PATTERNS:
            m = pat.search(work)
            if m:
                ep = cn_to_int(m.group("e"))
                if ep is not None and 0 < ep <= 2000:
                    p.episode = ep
                    p.is_tv = True
                    cut_at = min(cut_at, m.start())
                    break

    # --- 年份 ---
    head = work[:cut_at] if cut_at < len(work) else work
    years = YEAR_RE.findall(head) or YEAR_RE.findall(work)
    if years:
        p.year = int(years[0])
        ym = YEAR_RE.search(head if years == YEAR_RE.findall(head) else work)
        if ym and ym.start() < cut_at:
            cut_at = min(cut_at, ym.start())

    title_part = work[:cut_at].strip(" -._") or work

    # --- 從上層資料夾補季別 / 劇名 ---
    if parent_dirs:
        last = parent_dirs[-1]
        season_from_dir = None
        for pat in SEASON_DIR_PATTERNS:
            m = pat.search(unicodedata.normalize("NFKC", last))
            if m:
                season_from_dir = cn_to_int(m.group("s"))
                break
        if season_from_dir is not None:
            if p.season is None:
                p.season = season_from_dir
            p.is_tv = True
            if len(parent_dirs) >= 2:
                title_part = _clean(parent_dirs[-2]) or title_part
        elif p.is_tv and len(title_part) <= 2:
            title_part = _clean(last) or title_part

        # --- 規則 1：一集一資料夾 ---
        # /來！金來號 ！/01/來！金來號 ！01.mp4 這種結構。
        # 資料夾名稱本身就是集數，劇名要往上一層取。
        #
        # 一定要有「兄弟資料夾也長一樣」這個條件，否則一個剛好叫「01」的
        # 資料夾（磁碟 1、Disc 01）會被誤判。單一個 01 說明不了什麼，
        # 01 02 03 04 05 並排才是「一集一資料夾」。
        if season_from_dir is None and len(parent_dirs) >= 2:
            n = _episode_dir_num(last)
            if n is not None:
                sibs = [d for d in (sibling_dirs or [])
                        if _episode_dir_num(d) is not None]
                nums = {_episode_dir_num(d) for d in sibs}
                raw = unicodedata.normalize("NFKC", last.strip())
                if len(sibs) >= 2 and _looks_like_episodes(nums, raw.lstrip("EePp第 ")):
                    p.episode = n
                    p.is_tv = True
                    if p.season is None:
                        p.season = 1
                    title_part = _clean(parent_dirs[-2]) or title_part

    # 純數字檔名（在劇集資料夾內）視為集數
    if p.episode is None and re.fullmatch(r"\d{1,3}", work.strip()):
        looks_tv = any(
            any(pat.search(unicodedata.normalize("NFKC", d)) for pat in SEASON_DIR_PATTERNS)
            for d in parent_dirs
        )
        if looks_tv:
            p.episode = int(work.strip())
            p.is_tv = True

    if p.is_tv and p.season is None:
        p.season = 1

    # --- 清掉技術標籤 ---
    title_part = JUNK_RE.sub(" ", title_part)
    title_part = GROUP_TAIL_RE.sub("", title_part)
    title_part = YEAR_RE.sub(" ", title_part)
    title_part = re.sub(r"[\s\.\-_]{2,}", " ", title_part).strip(" -._·")

    # --- 規則 2：檔名尾端的數字是集數 ---
    # 「來！金來號 ！01」「來！金來號 ！02」…同一個資料夾裡好幾個檔案
    # 共用前綴、只有尾數不同 → 那個尾數是集數，不是片名的一部分。
    # 沒有這條規則的話，五集會算出五個不同的合併鍵，變成五張卡。
    if p.episode is None:
        hit = _trailing_episode(title_part, sibling_files)
        if hit:
            title_part, p.episode = hit[0], hit[1]
            p.is_tv = True
            if p.season is None:
                p.season = 1

    GENERIC = re.compile(r"^(movie|video|film|main|index|title\d*|video_ts|vts_\d+|\d{1,3})$", re.I)
    if parent_dirs and (not title_part or GENERIC.match(title_part) or len(title_part) < 2):
        cand = _clean(parent_dirs[-1])
        if p.year is None:
            ym = YEAR_RE.search(cand)
            if ym:
                p.year = int(ym.group(1))
        cand = YEAR_RE.sub(" ", JUNK_RE.sub(" ", cand))
        cand = GROUP_TAIL_RE.sub("", cand)
        cand = re.sub(r"[\s\.\-_]{2,}", " ", cand).strip(" -._·")
        if cand:
            title_part = cand
    # 電影：若檔名沒年份但上層資料夾有，補上
    if p.year is None and parent_dirs:
        ym = YEAR_RE.search(parent_dirs[-1])
        if ym:
            p.year = int(ym.group(1))

    p.title, p.alt_title = _split_titles(title_part)
    if not p.title:
        p.title = stem
    p.tags = [t for t in (p.resolution, p.source) if t]
    return p


# 手機錄影：`20211002_213546+0800-55744.mp4`。實測這座片庫 129 個檔案
# 全部是這個格式，而且**全部帶時區偏移**，沒有一個例外。
_HOME_VIDEO_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?:([+-]\d{4}))?")


def home_video_time(filename: str) -> Optional[int]:
    """手機錄影檔名 → 拍攝時間的 epoch 整數秒。不是這個格式回 None。

    **檔名是唯一可信的來源**，這點是實測出來的，不是預設：

    - 目錄不可信：`手機照片/20230219/iPhone培` 底下 40 個檔案，實際年份是
      2019×1、**2022×38**、2023×1 —— 拿目錄名分年會把 38 支 2022 的錄影標成 2023。
      同層還有 `Cheng🏂`、`Cheng🏂💈`、`iPhone培` 這種完全沒有日期資訊的目錄名。
    - `mtime` 不可信：那是 FTP 搬檔時間不是拍攝時間（這座庫裡最新的 mtime 到 2026 年）。

    所以解不出檔名就回 None 讓呼叫端放棄，不用 mtime 兜底 —— 兜出來的是搬檔時間，
    一個看起來合理但其實是別的東西的數字，比沒有更難察覺。
    """
    m = _HOME_VIDEO_RE.match(filename or "")
    if not m:
        return None
    y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    # 月/日/時分秒要自己擋。mktime 與 timegm 都會把 13 月「正規化」成隔年 1 月
    # 而不是報錯，於是 20211302 會安靜地變成 2022-02-02 —— 一個看起來合理、
    # 但跟檔名對不上的日期。這種錯事後查不出來。
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and se < 60):
        return None
    off = timeparse.offset_seconds(m.group(7)) if m.group(7) else None
    # 有時區就照它算（不經過本機時區），沒有就以本機時區解讀。
    ts = (timeparse._mk_utc(y, mo, d, h, mi, se, off) if off is not None
          else timeparse._mk(y, mo, d, h, mi, se))
    return ts if timeparse.valid(ts) else None


def guess_key(p: ParsedName, kind: str) -> str:
    """同一部作品的合併鍵。"""
    base = re.sub(r"\s+", "", (p.title or p.raw)).lower()
    if kind == "tv":
        return f"tv::{base}"
    return f"movie::{base}::{p.year or ''}"


def dirs_of(ftp_path: str, root: str) -> List[str]:
    rel = posixpath.relpath(posixpath.dirname(ftp_path), root if root != "/" else "/")
    if rel in (".", "/", ""):
        return []
    return [d for d in rel.split("/") if d not in (".", "..")]
