"""檔名解析：把 release 命名還原成 片名 / 年份 / 季集 / 品質標籤。"""
from __future__ import annotations

import posixpath
import re
import unicodedata
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


def parse(filename: str, parent_dirs: Optional[List[str]] = None) -> ParsedName:
    """parent_dirs: 由外而內的上層資料夾名稱（用來補季別與劇名）。"""
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
