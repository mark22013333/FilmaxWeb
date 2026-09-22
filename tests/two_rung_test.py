"""兩階 HLS：profile key 的階別維度、發幾階、上階的邊界與 BANDWIDTH。

    python tests/two_rung_test.py

規格：J 章第 0 層。這一支只驗播放清單與 key，不產生任何分段 ——
remux 真的產生分段是下一批（`build_transcode_cmd` 的 remux 分支）。
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-2r-")).resolve())
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "HLS_SEGMENT_SECONDS": "6", "AUDIO_BITRATE_KBPS": "192",
    "AUDIO_CHANNELS": "2", "TRANSCODE_MAX_HEIGHT": "1080",
    "HLS_TWO_RUNG": "true",
    # 階梯與政策的測試都拿這兩個當基準，釘住才不會被開發機的值影響
    "REMOTE_MAX_HEIGHT": "720", "REMOTE_BITRATE_KBPS": "2800",
    "LAN_BITRATE_KBPS": "0",
    # 高畫質頂階（B 案）預設開著；下面有一段會把它關掉再驗一次
    "REMOTE_HIGH_RUNG": "true",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import db, hls
db.init_db()

DUR = 47.0
# 3.625 秒一個 keyframe，湊滿 >= 6 秒 → 每段兩個 = 7.25 秒（實測就是這個形狀）
TIMES = [round(i * 3.625, 3) for i in range(13)]
POSS = [i * 1_000_000 for i in range(13)]


_seq = [0]


def add_file(codec="h264", profile="High", level=41, size=60_000_000,
             w=1920, h=960, kf="ok", gap=3.625, ext="mkv"):
    _seq[0] += 1
    fid = db.execute(
        """INSERT INTO media_file(ftp_path, filename, ext, size, duration,
                                  width, height, video_codec, video_profile,
                                  video_level, probe_state, kf_state, seen_at, added_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'ok',?,0,0)""",
        (f"/x/{_seq[0]}-{codec}-{kf}.{ext}", f"x.{ext}", ext, size, DUR,
         w, h, codec, profile, level, kf)).lastrowid
    if kf == "ok":
        db.execute("""INSERT INTO media_keyframe(file_id, times, positions, count,
                                                 gap_min, gap_med, gap_max, updated_at)
                      VALUES(?,?,?,?,?,?,?,0)""",
                   (fid, json.dumps(TIMES), json.dumps(POSS), len(TIMES),
                    gap, gap, gap))
    return fid


# ============================================================ profile key
head("[J 0] profile key 的階別維度")

lower = hls.profile_key(720, None, 8000, hls.RUNG_TRANSCODE)
upper = hls.profile_key(0, None, 0, hls.RUNG_REMUX)
check("key 裡有階別", lower.endswith("_m0") and upper.endswith("_m1"), (lower, upper))
check("兩階的 key 不一樣（不然分段會混進同一個資料夾）", lower != upper)
check("解析回來的階別對得上",
      hls._parse_profile(upper)[3] == hls.RUNG_REMUX
      and hls._parse_profile(lower)[3] == hls.RUNG_TRANSCODE)
check("沒寫 m 的舊網址當下階（相容且安全）",
      hls._parse_profile("h720_ad_b8000_c2")[3] == hls.RUNG_TRANSCODE)
check("normalize 會補上階別", hls.normalize_profile("h720_ad_b8000_c2") .endswith("_m0"),
      hls.normalize_profile("h720_ad_b8000_c2"))
check("階別是亂填的就退回下階（key 直接當資料夾名，不能放行任何東西）",
      hls.normalize_profile("h720_ad_b0_c2_m9").endswith("_m0"))
check("路徑穿越擋掉", "/" not in hls.normalize_profile("../../etc_m1"))


# ============================================================ 發幾階
head("[J 0] 哪些片源有上階")

fid = add_file()
check("h264 ＋ 有邊界表 → 兩階", hls.rungs_for(fid, DUR) == [hls.RUNG_REMUX, hls.RUNG_TRANSCODE],
      hls.rungs_for(fid, DUR))

fid_hevc = add_file(codec="hevc", kf="skipped")
check("hevc（沒有邊界表）→ 只有下階", hls.rungs_for(fid_hevc, DUR) == [hls.RUNG_TRANSCODE])

fid_pending = add_file(kf="pending")
check("邊界表還沒掃完 → 只有下階（不能照舊資料切分段）",
      hls.rungs_for(fid_pending, DUR) == [hls.RUNG_TRANSCODE])

fid_wide = add_file(gap=10.01)
check("keyframe 間距大於分段長度 → 只有下階（4K DV 那部就是 10.01 秒）",
      hls.rungs_for(fid_wide, DUR) == [hls.RUNG_TRANSCODE])

os.environ["HLS_TWO_RUNG"] = "false"
check("開關關著就是現在的行為（只有下階）", hls.rungs_for(fid, DUR) == [hls.RUNG_TRANSCODE])
os.environ["HLS_TWO_RUNG"] = "true"


# ============================================================ 上階的播放清單
head("[J 0] 上階的分段邊界")

pl = hls.build_playlist(fid, DUR, upper)
extinf = [float(l.split(":")[1].rstrip(",")) for l in pl.splitlines()
          if l.startswith("#EXTINF")]
check("段長是 keyframe 間距的整數倍，不是設定的 6 秒",
      abs(extinf[0] - 7.25) < 0.01, extinf[:3])
check("TARGETDURATION 照實際最長段填（填 6 是規格違反）",
      "#EXT-X-TARGETDURATION:8" in pl, [l for l in pl.splitlines() if "TARGET" in l])
check("總長等於片長（片尾那幾秒不能掉）", abs(sum(extinf) - DUR) < 0.01, sum(extinf))
check("每一段都指向上階的 profile", pl.count(f"?p={upper}") == len(extinf))

pl_low = hls.build_playlist(fid, DUR, lower)
extinf_low = [float(l.split(":")[1].rstrip(",")) for l in pl_low.splitlines()
              if l.startswith("#EXTINF")]
check("下階維持固定 6 秒（兩階刻意不共用邊界）", extinf_low[0] == 6.0, extinf_low[:3])
check("下階的總長也等於片長", abs(sum(extinf_low) - DUR) < 0.01)

pl_noekf = hls.build_playlist(fid_hevc, DUR, upper)
check("沒有邊界表卻要上階 → 退回固定分段，不是空清單",
      "#EXTINF:6.000," in pl_noekf and "#EXT-X-ENDLIST" in pl_noekf)

check("段數：上階由邊界表決定", hls.segment_count(DUR, upper, fid) == len(extinf))
check("段數：下階照長度算", hls.segment_count(DUR, lower, fid) == len(extinf_low))


# ============================================================ master
head("[J 0] master playlist")

m_remote = hls.build_master(fid, lower, remote=True, duration=DUR)
m_lan = hls.build_master(fid, lower, remote=False, duration=DUR)
# **遠端一律 transcode-only。**退格補償（seek_at）已經讓 remux 分段的接縫
# 重疊降到 0.042 秒，但實機重開後遠端仍會回退 —— 代表退格不是唯一成因，
# 在查清楚之前不把 remux 放回遠端 ABR。MKV 在區網也先停用，理由相同。
check("遠端仍發完整轉碼階梯", m_remote.count("#EXT-X-STREAM-INF") >= 2, m_remote)
check("遠端不混 remux 上階", "_m1" not in m_remote, m_remote)
check("MKV 區網暫停 remux，改發單一轉碼階", m_lan.count("#EXT-X-STREAM-INF") == 1, m_lan)
check("MKV 區網那一階是 transcode", "_m1" not in m_lan and "_m0" in m_lan, m_lan)

# 非 MKV 的區網 fast path 保留，避免把所有可安全 remux 的片源一起退化。
fid_mp4 = add_file(ext="mp4")
m_lan_mp4 = hls.build_master(fid_mp4, lower, remote=False, duration=DUR)
check("MP4 區網仍保留單一 remux 零轉碼路徑",
      f"?p={upper}" in m_lan_mp4 and m_lan_mp4.count("#EXT-X-STREAM-INF") == 1,
      m_lan_mp4)

# 上階那一行的屬性要驗，就得用一個**真的進得了 ABR** 的片源 ——
# 預設那組 fixture 的 keyframe 是 3.625 秒，除不盡 6 秒（偏差 1.5 秒），
# 嚴格規則會把它擋在 ABR 之外，於是 master 的第一行變成轉碼階，
# 拿它來驗「上階的 CODECS」等於什麼都沒驗到。
def _aligned_file(**kw):
    """keyframe 剛好 2 秒（除得盡 6 秒）的片源，上階進得了 ABR master。"""
    f = add_file(gap=2.0, **kw)
    t = [round(i * 2.0, 3) for i in range(31)]
    db.execute("UPDATE media_keyframe SET times=?, positions=?, count=? WHERE file_id=?",
               (json.dumps(t), json.dumps([i * 1_000_000 for i in range(len(t))]),
                len(t), f))
    db.execute("UPDATE media_file SET duration=? WHERE id=?", (60.0, f))
    return f


# **上階那一行只剩區網的 mp4 拿得到**（遠端一律 transcode-only、mkv 區網也停用），
# 所以屬性測試要從區網的 mp4 master 取。
fid_al = _aligned_file(ext="mp4")
m_al = hls.build_master(fid_al, lower, remote=False, duration=60.0)
check("對得齊的 mp4 在區網有上階", "_m1" in m_al, m_al)
first = [l for l in m_al.splitlines() if l.startswith("#EXT-X-STREAM-INF")][0]
bw = int(first.split("BANDWIDTH=")[1].split(",")[0])
avg = int(first.split("AVERAGE-BANDWIDTH=")[1].split(",")[0])
# 邊界是均勻的，所以峰值≈平均＋音訊；重點是「兩個都有、峰值不小於平均」
check("上階有 BANDWIDTH 與 AVERAGE-BANDWIDTH 兩個", bw > 0 and avg > 0, first)
check("BANDWIDTH 是峰值，不小於平均", bw >= avg, (bw, avg))
check("兩個都含音訊碼率", avg > 192_000, avg)
check("上階的解析度是片源原本的（不縮放）", "RESOLUTION=1920x960" in first, first)
check("上階的 CODECS 照片源的 profile／level 填（不是寫死的 64001f）",
      'CODECS="avc1.640029,mp4a.40.2"' in first, first)

fid_np = _aligned_file(profile=None, level=None, ext="mp4")
m_np = hls.build_master(fid_np, lower, remote=False, duration=60.0)
up_np = [l for l in m_np.splitlines() if l.startswith("#EXT-X-STREAM-INF")][0]
check("推不出 profile／level 就整個省略 CODECS（規格允許；填錯會被 Safari 拒收）",
      "CODECS" not in up_np, up_np)

# 主階（使用者選的那一檔）在階梯裡，但**不保證是第 1 行** —— 自動模式下
# 上面還有一階高畫質頂階。用碼率去找它，不要用位置。
infs = [l for l in m_remote.splitlines() if l.startswith("#EXT-X-STREAM-INF")]
base_inf = [l for l in infs if "BANDWIDTH=8000000" in l and "x960" not in l]
check("主轉碼階的 BANDWIDTH 是 VBV 上限", len(base_inf) == 1, infs)
check("主轉碼階的解析度是縮放後的",
      base_inf and "RESOLUTION=" in base_inf[0] and "x960" not in base_inf[0], base_inf)

m_single = hls.build_master(fid_hevc, lower, remote=True, duration=DUR)
check("沒有上階可給 → 遠端仍然發得出轉碼階梯（這正是最需要降階的片源）",
      m_single.count("#EXT-X-STREAM-INF") >= 2, m_single)
check("階梯的頂端是使用者選的那一檔", f"?p={lower}" in m_single, m_single)
check("沒有上階時不會混進 m1 的階", "_m1" not in m_single, m_single)

m_single_lan = hls.build_master(fid_hevc, lower, remote=False, duration=DUR)
check("區網沒有上階 → 只發單一轉碼階（不必多養快取）",
      m_single_lan.count("#EXT-X-STREAM-INF") == 1, m_single_lan)


# ============================================================ 轉碼階梯
head("[J 1] 遠端的轉碼階梯（ABR 要有得降）")

# auto=False = 使用者手動挑過畫質，不加高畫質頂階；先驗這條乾淨的階梯。
lad = hls.abr_ladder(1920, 1080, 720, 2800, auto=False)
check("主階排第一，而且就是傳進來的那一檔（使用者選的）",
      lad[0] == (720, 2800), lad)
check("階梯往下走，不是往上", [x[0] for x in lad] == sorted([x[0] for x in lad], reverse=True), lad)
check("碼率也跟著往下", [x[1] for x in lad] == sorted([x[1] for x in lad], reverse=True), lad)
check("三階（720/480/360）", len(lad) == 3, lad)

check("每一階的高度都不重複（重複的話 ABR 白切一次）",
      len({x[0] for x in lad}) == len(lad), lad)
gaps = [lad[i][0] - lad[i + 1][0] for i in range(len(lad) - 1)]
check("階與階至少差 100px（差太少省不到頻寬，卻要多養一份快取）",
      all(g >= 100 for g in gaps), gaps)

# 主階被調低時階梯要跟著縮，不能寫死 480／360
lad_low = hls.abr_ladder(1920, 1080, 480, 1400, auto=False)
check("主階 480p 時階梯從 480 往下（不是還在發 480）",
      lad_low[0][0] == 480 and all(x[0] < 480 for x in lad_low[1:]), lad_low)
check("不會發低到不能看的階（240p 以下就停）",
      all(x[0] >= 240 for x in lad_low), lad_low)

lad_orig = hls.abr_ladder(1920, 1080, 0, 0)
check("主階是「原畫質」(0) → 0 要原樣保住，不能被當成高度 0",
      lad_orig[0] == (0, 0), lad_orig)
check("原畫質底下的階梯從片源實際高度往下算（不是 0 乘比例還是 0）",
      len(lad_orig) > 1 and all(0 < x[0] < 1080 for x in lad_orig[1:]), lad_orig)
check("原畫質底下的階都有碼率（0 = 不鎖只能給主階，下面幾階要算得出數字）",
      all(x[1] > 0 for x in lad_orig[1:]), lad_orig)

lad_small = hls.abr_ladder(640, 360, 720, 2800, auto=False)
check("片源比主階還小 → 不發比片源高的階（不放大）",
      all((x[0] == 720 or x[0] < 360) for x in lad_small), lad_small)


head("[J 1] 高畫質頂階：遠端用大螢幕看不該被 720p 綁死（B 案）")

# 這一階只放寬解析度、不放寬碼率 —— 上傳頻寬由 REMOTE_BITRATE_KBPS 管，
# 解析度上限只決定畫面多大。同樣 2800 kbps，1080p 在大螢幕上明顯清楚。
lad_hi = hls.abr_ladder(1920, 1080, 720, 2800, auto=True)
check("自動模式會多發一階更高解析度", lad_hi[0][0] == 1080, lad_hi)
check("**頂階的碼率跟主階一樣，沒有放寬**（放寬了就是在偷吃上傳頻寬）",
      lad_hi[0][1] == 2800, lad_hi)
check("頂階排在最前面（master 裡最好的要排前面）",
      [x[0] for x in lad_hi] == sorted([x[0] for x in lad_hi], reverse=True), lad_hi)
check("主階還在（網路不夠時 ABR 要降得回來）", (720, 2800) in lad_hi, lad_hi)
check("底下那幾階沒有被影響", lad_hi[1:] == lad, (lad_hi[1:], lad))

check("手動挑過畫質就不加頂階（挑 480p 的意思是「最多 480p」）",
      all(x[0] <= 480 for x in hls.abr_ladder(1920, 1080, 480, 2800, auto=False)),
      hls.abr_ladder(1920, 1080, 480, 2800, auto=False))
check("手動挑原畫質也不加（已經是最高了）",
      hls.abr_ladder(1920, 1080, 0, 0, auto=True)[0] == (0, 0),
      hls.abr_ladder(1920, 1080, 0, 0, auto=True))

# 天花板：片源與 TRANSCODE_MAX_HEIGHT 取低的那一個
check("4K 片源的頂階被 TRANSCODE_MAX_HEIGHT(1080) 壓住，不會發 2160p",
      hls.abr_ladder(3840, 2160, 720, 2800, auto=True)[0][0] == 1080,
      hls.abr_ladder(3840, 2160, 720, 2800, auto=True))
check("720p 片源沒有更高的可給 → 不發頂階（不放大）",
      hls.abr_ladder(1280, 720, 720, 2800, auto=True)[0][0] == 720,
      hls.abr_ladder(1280, 720, 720, 2800, auto=True))
check("寬螢幕 1920×804：跟主階只差 84px < 100 → 不值得多發一階",
      hls.abr_ladder(1920, 804, 720, 2800, auto=True)[0][0] == 720,
      hls.abr_ladder(1920, 804, 720, 2800, auto=True))

os.environ["REMOTE_HIGH_RUNG"] = "false"
check("開關關掉就回到上一版的形狀（三階，沒有頂階）",
      hls.abr_ladder(1920, 1080, 720, 2800, auto=True) == lad,
      hls.abr_ladder(1920, 1080, 720, 2800, auto=True))
os.environ["REMOTE_HIGH_RUNG"] = "true"

# 階梯上的高度要真的能對到 profile key，不然分段會落到別的資料夾
for hh, bb in lad:
    k = hls.profile_key(hh, None, bb, hls.RUNG_TRANSCODE)
    check(f"階 {hh}p 的 key 解析得回來", hls._parse_profile(k)[:3] == (hh, None, bb), k)


head("[J 1] 解析度／位元率政策：兩個端點必須問同一個函式")

check("遠端自動 = REMOTE_MAX_HEIGHT ＋ REMOTE_BITRATE_KBPS",
      hls.quality_policy(True, None) == (720, 2800), hls.quality_policy(True, None))
check("區網自動 = TRANSCODE_MAX_HEIGHT，不鎖碼率",
      hls.quality_policy(False, None) == (1080, 0), hls.quality_policy(False, None))
check("h=0（原畫質）不是「沒指定」—— 不縮放也不鎖峰值",
      hls.quality_policy(True, 0) == (0, 0), hls.quality_policy(True, 0))
check("已達上限就不鎖峰值", hls.quality_policy(True, 1080)[1] == 0,
      hls.quality_policy(True, 1080))

# master 端點自己算 profile 的話，位元率上限會掉 —— 釘住這件事
import inspect as _insp
from app.routers import stream as _sr
_src = _insp.getsource(_sr.hls_master)
check("master 端點走 quality_policy（自己算的話 REMOTE_BITRATE_KBPS 會失效）",
      "quality_policy" in _src, _src[:300])
from app.routers import api as _api
check("/api/play 也走同一個函式", "quality_policy" in _insp.getsource(_api.play_info))
check("/api/play 的 hls_url 指到 master（指到 index 等於 ABR 沒有階可選）",
      "master.m3u8" in _insp.getsource(_api.play_info), "hls_url 還指著 index.m3u8")
check("master 端點把「有沒有手動挑畫質」傳下去（不然分不出自動與剛好選到預設值）",
      "auto=" in _src, _src[-400:])

# _qs：h=0（原畫質）與 h=None（沒指定）是不同的意思，不能用真假值濾
check("_qs 留得住 h=0（用 `if v` 會把原畫質一起丟掉）",
      _api._qs(h=0, a=None) == "?h=0", _api._qs(h=0, a=None))
check("_qs 濾掉 None", _api._qs(h=None, a=None) == "", _api._qs(h=None, a=None))
check("_qs 兩個都在時用 & 接", _api._qs(h=720, a=2) == "?h=720&a=2", _api._qs(h=720, a=2))


# ============================================================ 速度樣本
head("[J 0] 上階不能汙染速度樣本")

with hls._speed_lock:
    hls._speed_samples.clear()
hls.record_speed(6.0, 3.0)
check("下階的樣本照記", hls.recent_speed() == 2.0, hls.recent_speed())
check("上階的耗時不算進樣本（remux 17x 會讓預轉誤判機器很閒）",
      hls.should_record_speed(hls.RUNG_REMUX, False) is False)
check("下階的前景樣本要記", hls.should_record_speed(hls.RUNG_TRANSCODE, False) is True)
check("預轉不記（低優先權跑的，速度不代表機器餘裕）",
      hls.should_record_speed(hls.RUNG_TRANSCODE, True) is False)

d = hls.seg_dir(fid, upper)
hls._schedule_prefetch(fid, 0, upper, DUR)
check("上階不排預轉", not list(d.glob("seg-*.ts")), list(d.glob("seg-*.ts")))

# ============================================================ 快取版本
head("[J 0] 快取格式換版 → 舊分段要清一次")

d = hls.seg_dir(fid, "h720_ad_b0_c2")      # 舊形狀（沒有 m）的資料夾
(d / "seg-0.ts").write_bytes(b"x")
db.kv_set("hls_profile_key_version", 1)
hls.migrate_cache()
check("舊分段被清掉（不然它們會一直佔著快取上限）", not (d / "seg-0.ts").exists())
check("版本號記下來了",
      db.kv_get("hls_profile_key_version") == hls.HLS_CACHE_FORMAT_VERSION)
(d / "seg-1.ts").write_bytes(b"x")
hls.migrate_cache()
check("第二次啟動不會再清（不然每次重開都要重轉一遍）", (d / "seg-1.ts").exists())

# **這一條是這次修正的回歸測試。**v2 的上階分段是用舊的邊界規則
# （「湊滿 >= 6 秒的最少 keyframe」）切的，seg-N 代表的 media time 跟 v3
# 不一樣。而 `get_segment()` 看到檔案存在就直接回傳、不驗內容 ——
# 沒有這次清除的話，升級後舊 seg-N 會被當成新 seg-N 餵出去，
# 播放清單說 600 秒、實際吐 1060 秒的內容。
head("[J 0] v2 → v3：邊界規則改了，舊 .ts 一定要失效")

import math
from app.config import CACHE_DIR

check("版本號有升上去（邊界規則改了就必須升）",
      hls.HLS_CACHE_FORMAT_VERSION >= 3, hls.HLS_CACHE_FORMAT_VERSION)
old_dir = CACHE_DIR / str(fid) / f"v2_{hls.profile_key(0, None, 0, hls.RUNG_REMUX)}"
old_dir.mkdir(parents=True, exist_ok=True)
(old_dir / "seg-100.ts").write_bytes(b"v2-content")
new_dir = hls.seg_dir(fid, hls.profile_key(0, None, 0, hls.RUNG_REMUX))
# 綁常數而不是寫死 "v3_"：版本每升一次就要改一次測試的話，改的人會傾向
# 把測試改成符合現況，而不是去想「這次升版是不是真的該升」。
check("分段路徑帶版本（新舊 binary 短暫交錯也不會共用同一批 .ts）",
      new_dir.name.startswith(f"v{hls.HLS_CACHE_FORMAT_VERSION}_"), new_dir.name)
check("v2 與 v3 不是同一個資料夾", old_dir.resolve() != new_dir.resolve())
db.kv_set("hls_profile_key_version", 2)
hls.migrate_cache()
check("v2 的舊分段被清掉（不能讓它被當成 v3 的 seg-100 餵出去）",
      not (old_dir / "seg-100.ts").exists())
check("升級之後版本號是 v3",
      db.kv_get("hls_profile_key_version") == hls.HLS_CACHE_FORMAT_VERSION)


# ============================================================ ABR 對齊
head("[J 1] 同一份 ABR master 裡各階的 seg-N 必須是同一段")

# 3.625 秒一個 keyframe 的那個 fid：舊規則每段 7.25 秒，seg-N 會一路漂走；
# 新規則對齊到 6 秒格線，偏差不累加。
_seg = 6.0
_spans = hls.bounds_for(fid, DUR)
check("上階切得出分段", len(_spans) > 1, len(_spans))
_worst = max(abs(s0 - i * _seg) for i, (s0, _e, _b) in enumerate(_spans))
check("舊的寬鬆檢查確實可能只看到『沒有累積到幾百秒』",
      _worst < _seg * 1.5, (_worst, [round(s0, 2) for s0, _, _ in _spans]))
# 3.625 除不盡 6，所以邊界最多只能貼到 1.5 秒 —— 那是肉眼看得到的錯位，
# 嚴格規則（0.25 秒）要擋下來。**這不是 remux 壞了**，是這個片源的
# keyframe 密度拼不出跟下階一樣的格線。
check("嚴格 ABR 驗證不接受肉眼可見的 sub-segment 偏差",
      hls.abr_alignable(fid, DUR)[0] is False, hls.abr_alignable(fid, DUR))

_m = hls.build_master(fid, lower, remote=True, duration=DUR)
check("即使邊界看起來接近，遠端也一律不混 remux", "_m1" not in _m, _m)

# `abr_alignable()` 本身仍然要能分辨對得齊與對不齊 —— 遠端目前不靠它決定
# 發不發上階（一律不發），但它是將來把 remux 放回 ABR 的前置條件，
# 而且後台診斷要靠它說得出「為什麼這個檔案不能混階」。
_even = add_file(gap=2.0)
_even_times = [round(i * 2.0, 3) for i in range(31)]
db.execute("UPDATE media_keyframe SET times=?, positions=? WHERE file_id=?",
           (json.dumps(_even_times),
            json.dumps([i * 1_000_000 for i in range(len(_even_times))]), _even))
db.execute("UPDATE media_file SET duration=? WHERE id=?", (60.0, _even))
_even_ok, _even_why = hls.abr_alignable(_even, 60.0)
check("keyframe 除得盡分段長度 → abr_alignable 回 True", _even_ok, (_even_ok, _even_why))

# **片源的 keyframe 比分段長度還疏**就對不齊，而且不是理論上的：
# 實測 file 297 的 `gap_med` 是 4.67 秒（看起來很安全），但那個中位數是被
# 片頭那幾個密集的 keyframe 拉下來的 —— 正片的實際間距是 **10.39 秒**。
# 間距比 6 秒大的時候，第 i 段再怎麼挑都不可能貼著 i*6，偏差只會一路累加
# （實測到最後差了 1256 秒）。
#
# 這也是為什麼 `rungs_for()` 的 `gap_med > seg` 那道檢查擋不住它：
# **中位數會說謊，要看的是實際切出來的邊界。**`abr_alignable()` 直接量
# 邊界本身，所以擋得住。
_odd = add_file(gap=4.67)
_odd_times = [0.0, 4.09, 5.84, 8.18] + [round(8.18 + i * 10.39, 3) for i in range(1, 700)]
db.execute("UPDATE media_keyframe SET times=?, positions=? WHERE file_id=?",
           (json.dumps(_odd_times),
            json.dumps([i * 1_000_000 for i in range(len(_odd_times))]), _odd))
db.execute("UPDATE media_file SET duration=? WHERE id=?", (_odd_times[-1], _odd))
_odd_dur = _odd_times[-1]
_ok, _why = hls.abr_alignable(_odd, _odd_dur)
check("keyframe 間距除不盡 → 不准進 ABR master", not _ok, (_ok, _why))
check("而且要說得出原因（後台看得到為什麼少一階）", ("秒" in _why) or ("段" in _why), _why)

# 真實長片最危險的是「大部分 GOP 正常，中途突然一個 long GOP」。
# 30 → 42 會直接漏掉 36 秒格線；舊容忍 9 秒會把後續 6 秒錯位判成可切。
_long = add_file(gap=3.0)
_long_times = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0, 24.0, 27.0, 30.0,
               42.0, 45.0, 48.0, 51.0, 54.0, 57.0, 60.0]
db.execute("UPDATE media_keyframe SET times=?, positions=? WHERE file_id=?",
           (json.dumps(_long_times),
            json.dumps([i * 1_000_000 for i in range(len(_long_times))]), _long))
db.execute("UPDATE media_file SET duration=? WHERE id=?", (60.0, _long))
_long_ok, _long_why = hls.abr_alignable(_long, 60.0)
check("中途 long GOP 漏掉一個 6 秒格線 → 必須判定不能混 ABR",
      not _long_ok, (_long_ok, _long_why))
check("long GOP 的遠端 master 不發上階（只剩轉碼階梯）",
      "_m1" not in hls.build_master(_long, lower, remote=True, duration=60.0))

_m2 = hls.build_master(_odd, lower, remote=True, duration=_odd_dur)
check("對不齊 → 遠端 master 不發上階（寧可少一階，也不要壞掉的時間軸）",
      "_m1" not in _m2, _m2)
check("但轉碼階梯照發，播放不中斷", _m2.count("index.m3u8") >= 2, _m2)
# MKV 的單一 remux 也先停用：使用者回報的 Reacher 類型就是 Matroska/H.264，
# 而畫面回退不一定伴隨 LEVEL_SWITCHED，所以不能只修遠端混階。
_lan = hls.build_master(_odd, lower, remote=False, duration=_odd_dur)
check("MKV 區網也不發 remux", "_m1" not in _lan, _lan)
check("區網仍有單一轉碼 rendition 可播", _lan.count("index.m3u8") == 1, _lan)


# ============================================================ remux 指令
head("[J 0] remux 的 ffmpeg 指令")

import json as _json
from app import media

db.execute("""UPDATE media_file SET audio_codec='ac3',
              audio_tracks=? WHERE id=?""",
           (_json.dumps([{"index": 1, "codec": "ac3", "channels": 6, "default": 1}]), fid))
cmd = media.build_remux_cmd(fid, 7.25, 7.25, None)
check("視訊照抄", "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy", cmd)
check("ac3 要轉 AAC（瀏覽器不吃 ac3）",
      cmd[cmd.index("-c:a") + 1] == "aac", cmd)
check("-ss 在 -i 前面（輸入端 seek；copy 只能從 keyframe 起頭）",
      cmd.index("-ss") < cmd.index("-i"), cmd)
check("沒給 seek_at 時退回 start（呼叫端漏傳不能炸）",
      cmd[cmd.index("-ss") + 1] == "7.250")

# **這一組是回歸測試。**copy 模式下 ffmpeg 一律退到前一個 keyframe，
# **即使 start 本身就是貨真價實的 keyframe**（實測 file 360：邊界 600.600
# 在容器裡是 flags=K__，-ss 600.600 的落點仍然是 598.598）。-copyts 只讓那段
# 多抄的內容標對位置，並沒有讓它消失 —— 於是每個接縫都重疊一整個 keyframe
# 間距（實測 2.044／2.169 秒），而「重疊是冪等的」對這條路徑不成立：
# 音訊要重編（eac3／5.1 AAC），重編的音訊不冪等，每段開頭那個 2 秒的洞
# 會蓋掉前一段的聲音；hls.js 也會因為 buffered 對不上而 seek 回去修正。
# 修法是 -ss 餵下一格 keyframe，讓退格剛好被補償掉。
_cmd_ss = media.build_remux_cmd(fid, 7.25, 7.25, None, seek_at=10.875)
check("給了 seek_at 就用它當 -ss（補償 copy 的退格）",
      _cmd_ss[_cmd_ss.index("-ss") + 1] == "10.875", _cmd_ss)
check("但 -to 仍然是這一段真正的結束時間（不跟著推後）",
      _cmd_ss[_cmd_ss.index("-to") + 1] == "14.500", _cmd_ss)
# 只推後 -ss 的話音訊會落後一整格（音訊是精確 seek 的，它真的從推後那一格
# 開始）—— 實測 video 頭 600.600、audio 頭 602.580，比原本更糟。
check("-noaccurate_seek 要在（不然音訊會落後推後量那麼多）",
      "-noaccurate_seek" in _cmd_ss, _cmd_ss)
check("-noaccurate_seek 也在 -i 前面（它是輸入端選項）",
      _cmd_ss.index("-noaccurate_seek") < _cmd_ss.index("-i"), _cmd_ss)
# **這一條是回歸測試。**copy 模式下 ffmpeg 一律退到前一個 keyframe 才起頭
# （實測 file 334：要求 49.091 拿到 46.338，連續 12 個 keyframe 每個都退一格，
# 且完全穩定、微調 -ss 也躲不掉）。舊寫法用 -output_ts_offset 無條件平移到
# 宣告值，於是那 2.75 秒「剛播過的畫面」被標成從 49.091 開始 —— append 進
# SourceBuffer 就是畫面倒退。改用 -copyts 保留片源原始時間戳讓標籤說實話。
check("上階要用 -copyts（時間戳照片源，不要平移到宣告值）",
      "-copyts" in cmd, cmd)
check("-copyts 之下要用 -to（絕對時間）而不是 -t（會吐出空檔案）",
      "-to" in cmd and "-t" not in cmd, cmd)
check("-to 是這一段的結束絕對時間", cmd[cmd.index("-to") + 1] == "14.500")
# **這一條是回歸測試，不是風格偏好。**原本寫的是 make_zero，而 make_zero
# 是在 -output_ts_offset 之後才套用的 —— 它會把 offset 剛寫進去的絕對時間戳
# 整個抹成 0。實測（file 433，2:33:55）：1540 段每段都從 0 起算，hls.js 只好
# 一段接一段串起來，MediaSource 的 duration 變成 1540 × 4.816 = 7416 秒，
# 播放器右下角就從 2:33:55 變成 2:03:36，seek 到 95% 還會直接 ended。
# 而下階 transcode 沒有這個旗標 —— 所以兩階的時間軸原本是對不起來的。
check("上階不可以用 make_zero（它會抹掉 -copyts 保住的絕對時間戳）",
      cmd[cmd.index("-avoid_negative_ts") + 1] == "disabled", cmd)
check("上階不該再用 -output_ts_offset（-copyts 已經給了絕對時間，再平移會錯一次）",
      "-output_ts_offset" not in cmd, cmd)
# 兩階的時間戳都必須是「片源的絕對 media time」，否則切畫質時 currentTime
# 會對到另一條軸上。手段不同（上階 -copyts、下階重編碼後平移），結果要一致。
_tr = media.build_transcode_cmd(fid, 7.25, 7.25, 720, None)
check("下階把時間軸放回絕對位置（切畫質才不會跳）",
      _tr[_tr.index("-output_ts_offset") + 1] == "7.250", _tr)
check("下階本來就沒有 avoid_negative_ts，上階也不該再靠它平移",
      "make_zero" not in cmd and "make_zero" not in _tr, (cmd, _tr))
for flag in ("-vf", "-crf", "-cq", "-preset", "-force_key_frames", "-sc_threshold",
             "-profile:v", "-b:v", "-maxrate"):
    check(f"remux 不能出現 {flag}（出現就不是零損失了）", flag not in cmd, cmd)

db.execute("""UPDATE media_file SET audio_codec='aac',
              audio_tracks=? WHERE id=?""",
           (_json.dumps([{"index": 1, "codec": "aac", "channels": 2, "default": 1}]), fid))
cmd2 = media.build_remux_cmd(fid, 0.0, 7.25, None)
check("已經是 2 聲道 AAC → 音訊也照抄", cmd2[cmd2.index("-c:a") + 1] == "copy", cmd2)

db.execute("""UPDATE media_file SET audio_tracks=? WHERE id=?""",
           (_json.dumps([{"index": 1, "codec": "aac", "channels": 6, "default": 1}]), fid))
cmd3 = media.build_remux_cmd(fid, 0.0, 7.25, None)
check("5.1 的 AAC 仍然要轉（不然 AUDIO_CHANNELS 的降混會在上階失效）",
      cmd3[cmd3.index("-c:a") + 1] == "aac" and "-ac" in cmd3, cmd3)


# ============================================================ 接起來
head("[J 0] _produce 真的把下一格 keyframe 傳下去")

# **上面那幾條只驗 build_remux_cmd 自己。**真正會壞掉的是接線：
# seek_at 算在 _produce() 裡（刻意不讓 build_remux_cmd 自己查表，否則兩邊
# 各查一次就可能拿到不同的答案）—— 所以要驗的是「跑一段出來，-ss 是下一格」。
# 攔 subprocess.run 拿到真正送出去的指令，不實際叫 ffmpeg。
import subprocess as _sp
from app import keyframes as keyframes_mod

_seen = []
_orig_run = _sp.run


def _fake_run(cmd, *a, **kw):
    _seen.append(cmd)
    # _produce 會檢查 returncode 與檔案大小，餵一個「成功」的假結果，
    # 並把 stdout 那個檔案寫進去（它用 open(tmp,"wb") 接 stdout）
    fh = kw.get("stdout")
    if fh is not None and hasattr(fh, "write"):
        fh.write(b"x" * 16)
    class _R:
        returncode = 0
        stderr = b""
    return _R()


def _produce_cmd(fid_, index, prof):
    """跑一次 _produce，回傳它真正送出去的 ffmpeg 指令。"""
    _seen.clear()
    _sp.run = _fake_run
    try:
        hls._produce(fid_, index, prof, DUR, hls.seg_dir(fid_, prof) / f"seg-{index}.ts")
    finally:
        _sp.run = _orig_run
    return _seen[0] if _seen else []


_pfid = add_file()
_spans = hls.bounds_for(_pfid, DUR)
_start1, _end1, _ = _spans[1]
_prof = hls.profile_key(None, None, 0, hls.RUNG_REMUX)

# **退格是量出來的，不是假設的。**實測把同一份視訊 -c copy 換個容器，
# 退格行為就變了（mp4 不退、mkv 退一整格）—— 所以推不推由 seek_backoff 決定。
# 沒量過（NULL）一律當作不退：不該推卻推了會讓那一段開頭整個缺一格，
# 比「該推沒推」（只是重疊）嚴重得多。
db.execute("UPDATE media_file SET seek_backoff=NULL WHERE id=?", (_pfid,))
_pc0 = _produce_cmd(_pfid, 1, _prof)
check("還沒量過退格 → -ss 就是 start（保守，不推）",
      _pc0[_pc0.index("-ss") + 1] == f"{_start1:.3f}", _pc0)

db.execute("UPDATE media_file SET seek_backoff=0 WHERE id=?", (_pfid,))
_pc1 = _produce_cmd(_pfid, 1, _prof)
check("量到不會退格 → -ss 還是 start（推了會缺開頭）",
      _pc1[_pc1.index("-ss") + 1] == f"{_start1:.3f}", _pc1)

db.execute("UPDATE media_file SET seek_backoff=1 WHERE id=?", (_pfid,))
# seg-2 跨兩個 keyframe，推得動；seg-1 只跨一個，推過去就是空段（下面驗）。
_start2, _end2, _ = _spans[2]
_pc = _produce_cmd(_pfid, 2, _prof)
_next_kf = keyframes_mod.seek_start_for(TIMES, _start2, _end2)
check("量到會退格 → -ss 推到下一格 keyframe",
      _pc[_pc.index("-ss") + 1] == f"{_next_kf:.3f}",
      (_pc[_pc.index("-ss") + 1], _start2, _next_kf))
check("而且它確實比 start 大（真的有推後）", _next_kf > _start2,
      (_next_kf, _start2))
check("-to 仍然是這一段的結束（不跟著推後）",
      _pc[_pc.index("-to") + 1] == f"{_start2 + max(_end2 - _start2, 0.05):.3f}", _pc)
check("-noaccurate_seek 有跟著出去", "-noaccurate_seek" in _pc, _pc)

# **只跨一個 keyframe 的短段不能推。**下一格就是這一段的結尾，推過去等於
# `-ss X -to X` —— 實測吐出來的分段從 415,668 位元組掉到 18,424（只有兩個
# 封包），那一段的畫面整個不見。重疊只是瑕疵，空段是整段播不出來。
_pc_short = _produce_cmd(_pfid, 1, _prof)
check("會退格、但短段仍然不推（推過去就是空段）",
      _pc_short[_pc_short.index("-ss") + 1] == f"{_start1:.3f}",
      (_pc_short[_pc_short.index("-ss") + 1], _start1, _end1))

# 下階完全不受影響：它沒有退格問題（重新編碼，不是 copy），
# 推後 -ss 只會讓它少掉開頭那一段。
_lp = hls.profile_key(720, None, 2800, hls.RUNG_TRANSCODE)
_lc = _produce_cmd(_pfid, 1, _lp)
check("下階的 -ss 仍然是 index*seg（沒有被推後）",
      _lc[_lc.index("-ss") + 1] == "6.000", _lc)
check("下階沒有 -noaccurate_seek（它不是 copy，沒有退格要補）",
      "-noaccurate_seek" not in _lc, _lc)


# ============================================================ 下線
head("[J 0] 一段失敗 → 整個檔案的上階下線")

check("下線前有兩階", hls.rungs_for(fid, DUR) == [hls.RUNG_REMUX, hls.RUNG_TRANSCODE])
hls.take_remux_offline(fid, "Non-monotonous DTS in output stream\n第二行不要進資料庫")
check("下線後只剩下階（不是只退那一段）",
      hls.rungs_for(fid, DUR) == [hls.RUNG_TRANSCODE])
row = db.q1("SELECT remux_state, remux_error FROM media_file WHERE id=?", (fid,))
check("狀態寫進 DB", row["remux_state"] == "failed", dict(row))
check("原因只留第一行（後台清單看得到）",
      row["remux_error"] == "Non-monotonous DTS in output stream", row["remux_error"])
m = hls.build_master(fid, lower, remote=True, duration=DUR)
check("master 不再發上階（轉碼階梯還在，播放不中斷）", "_m1" not in m, m)

import time as _time
with db.batch() as bt:
    bt.upsert_file(item_id=None, episode_id=None,
                   path=db.q1("SELECT ftp_path FROM media_file WHERE id=?", (fid,))["ftp_path"],
                   name="x.mkv", size=1, mtime="x", mtime_ts=int(_time.time()),
                   ext="mkv", at=_time.time())
row = db.q1("SELECT remux_state, kf_state FROM media_file WHERE id=?", (fid,))
check("檔案變動後下線紀錄一起清掉（之前的失敗理由不再適用）",
      row["remux_state"] is None, dict(row))


# ============================================================ 回填
head("[J 0] profile／level 的升級回填")

from app import keyframes
db.execute("UPDATE media_file SET kf_state='ok', video_profile=NULL WHERE id=?", (fid,))
check("有東西要補", keyframes.backfill_count() >= 1, keyframes.backfill_count())
n = keyframes.backfill_stream_info(limit=5)
check("補過之後不會再排進來（問不到也要寫 sentinel，不然會每次啟動都問）",
      keyframes.backfill_count() == 0, keyframes.backfill_count())
prof = db.q1("SELECT video_profile FROM media_file WHERE id=?", (fid,))["video_profile"]
check("問不到就寫 unknown", prof == keyframes.STREAM_UNKNOWN, prof)
check("unknown 推不出 CODECS（所以那個屬性會被省略）",
      hls.codecs_attr(keyframes.STREAM_UNKNOWN, 41) is None)

# **退格也要有升級路徑。**seek_backoff 是後來才加的欄位，而既有的檔案全都是
# kf_state='ok'（不會再被排進佇列）—— 不補的話它們永遠是 NULL，
# 而 NULL 一律當作「不退」，於是這次修的重疊對它們全部不會生效。
db.execute("UPDATE media_file SET kf_state='ok', seek_backoff=NULL WHERE id=?", (fid,))
check("有退格要量", keyframes.seek_backfill_count() >= 1,
      keyframes.seek_backfill_count())
# 不給 limit：要驗的是「排完之後佇列真的空了」，
# 而這個檔案裡有幾個 fixture 會隨著測試增減 —— 寫死一個數字遲早會對不上。
keyframes.backfill_seek_backoff()
check("量過之後不會再排進來（量不出來也要寫 0，不然每次啟動都重跑）",
      keyframes.seek_backfill_count() == 0, keyframes.seek_backfill_count())
_bk = db.q1("SELECT seek_backoff FROM media_file WHERE id=?", (fid,))["seek_backoff"]
check("量不出來就寫 0（保守：不推，寧可重疊也不要缺開頭）", _bk == 0, _bk)


# ============================================================ 真的 remux 一段
head("[J 0] 真的產生一段上階（需要 ffmpeg／ffprobe）")

import shutil, subprocess

ff = shutil.which(media.resolve_tool("ffmpeg")) or shutil.which("ffmpeg")
fp = shutil.which(media.resolve_tool("ffprobe")) or shutil.which("ffprobe")
if not (ff and fp):
    print("  (跳過：這台機器上找不到 ffmpeg／ffprobe)")
else:
    from app import keyframes as kf
    root = Path(TMP, "ftproot", "movie")
    root.mkdir(parents=True, exist_ok=True)
    real = root / "real.mkv"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    # **keyframe 間距刻意選除不盡分段長度的值**（24fps ÷ 87
                    # = 3.625 秒，而分段是 6 秒）。整除的間距（例如 2 秒）會
                    # 讓兩階碰巧對齊，那樣這支測試就證明不了任何事 ——
                    # 舊的邊界規則在整除的片源上也是對的。
                    "-t", "60", "-c:v", "libx264", "-preset", "veryfast",
                    "-g", "87", "-keyint_min", "87", "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-c:a", "aac", "-ac", "2", "-shortest",
                    "-y", str(real)], check=True)
    os.environ["LIBRARY_LOCAL_ROOTS"] = f"/={Path(TMP, 'ftproot')}"
    real_dur = float(subprocess.run(
        [fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(real)], capture_output=True, text=True).stdout.strip())

    rid = db.execute(
        """INSERT INTO media_file(ftp_path, filename, ext, size, duration, width, height,
                                  video_codec, audio_codec, audio_tracks, probe_state,
                                  kf_state, seen_at, added_at)
           VALUES(?,?,?,?,?,?,?,'h264','aac',?,'ok','pending',0,0)""",
        ("/movie/real.mkv", "real.mkv", "mkv", real.stat().st_size, real_dur,
         320, 180, _json.dumps([{"index": 1, "codec": "aac", "channels": 2,
                                 "default": 1}]))).lastrowid

    check("本機直讀有接上轉碼路徑（第 −1 層）：-i 直接指到檔案",
          str(real) in media.input_args(rid), media.input_args(rid))

    state = kf.scan_one(dict(db.q1(
        "SELECT id, ftp_path, video_codec FROM media_file WHERE id=?", (rid,))))
    check("邊界表掃得起來", state == "ok", state)
    pl_row = db.q1("SELECT video_profile, video_level FROM media_file WHERE id=?", (rid,))
    check("profile／level 也一起記下來了（CODECS 要用）",
          pl_row["video_profile"] == "High" and pl_row["video_level"] > 0, dict(pl_row))
    check("這一組推得出 CODECS",
          hls.codecs_attr(pl_row["video_profile"], pl_row["video_level"])
          and hls.codecs_attr(pl_row["video_profile"], pl_row["video_level"]).startswith("avc1.6400"),
          hls.codecs_attr(pl_row["video_profile"], pl_row["video_level"]))

    rungs = hls.rungs_for(rid, real_dur)
    check("這個檔案有上階", hls.RUNG_REMUX in rungs, rungs)
    up = hls.profile_key(0, None, 0, hls.RUNG_REMUX)
    spans = hls.bounds_for(rid, real_dur)
    seg0 = hls.get_segment(rid, 0, up, real_dur)
    check("第 0 段真的產生出來了", seg0.exists() and seg0.stat().st_size > 0,
          seg0.stat().st_size if seg0.exists() else "沒有")

    info = subprocess.run(
        [fp, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height:format=duration",
         "-of", "csv=p=0", str(seg0)], capture_output=True, text=True).stdout
    check("視訊還是 h264、解析度沒有被縮", "h264" in info and "320,180" in info, info.strip())
    seg_dur = float([x for x in info.replace("\n", ",").split(",") if x][-1])
    want = spans[0][1] - spans[0][0]
    check("段長等於邊界表算出來的長度", abs(seg_dur - want) < 0.35, (seg_dur, want))

    # 零損失的證明：解碼出來的畫格要跟原始檔同一段逐格相同
    def framemd5(args):
        r = subprocess.run([ff, "-v", "error", *args, "-map", "0:v:0",
                            "-f", "framemd5", "-"], capture_output=True, text=True)
        return [l.split(",")[-1].strip() for l in r.stdout.splitlines()
                if l and not l.startswith("#")]

    a = framemd5(["-ss", f"{spans[0][0]:.3f}", "-i", str(real), "-t", f"{want:.3f}"])
    b = framemd5(["-i", str(seg0)])
    # 段的畫格數可能比「-t 切出來的那一段」多一格：-t 是按時間截、
    # 而 copy 是按封包收尾。逐格比對共同的那一段就夠證明沒有重編。
    n = min(len(a), len(b))
    check("上階是零損失：逐格 md5 與原始檔完全相同（不是「看起來一樣」）",
          n > 10 and a[:n] == b[:n], (len(a), len(b), a[:2], b[:2]))
    # 段裡的畫格只會多不會少：`-t` 是按時間截，而 copy 收在封包邊界。
    # 少了才是問題（那就是掉格）；多出來的部分由上面那項段長檢查（±0.35 秒）擋著。
    check("段裡的畫格沒有比要求的少（少了才是掉格）", len(b) >= len(a), (len(a), len(b)))

    # 播放清單說的長度要跟實際產生的段數一致
    pl = hls.build_playlist(rid, real_dur, up)
    n = pl.count("#EXTINF")
    check("播放清單的段數與邊界表一致", n == len(spans), (n, len(spans)))
    check("最後一段拿得到（片尾那幾秒是已知的坑）",
          hls.get_segment(rid, n - 1, up, real_dur).stat().st_size > 0)

    # ------------------------------------------------------------------
    # 真的產生兩種 rendition，比對 seg-N 各自代表哪一段 media time。
    #
    # **這一段抓得到的東西，前面那些指令字串的檢查抓不到。**
    # 「兩邊都有 -output_ts_offset」只證明時間戳有被寫進去，不證明寫進去的
    # 是同一個值 —— 而使用者看到的「畫面跳回之前」正是兩邊寫了不同的值。
    # 所以這裡要問 ffprobe 實際的 PTS，不是問我們自己組的指令。
    #
    # 修正前的實測（3.48 秒一個 keyframe 的 120 秒片源）：
    #     seg-15  上階 = 104.40s，720/480 = 90.00s   ← 差 14.4 秒
    # 修正後：
    #     seg-15  上階 =  90.48s，720/480 = 90.00s   ← 差 0.48 秒
    head("[J 1] 各 rendition 的 seg-N 是不是同一段（實際產生、實際 ffprobe）")

    def seg_pts(path):
        """回傳 (video 第一個 PTS, video 最後一個 PTS, audio 第一個 PTS)。"""
        def pts(stream):
            out = subprocess.run(
                [fp, "-v", "error", "-select_streams", stream, "-show_entries",
                 "packet=pts_time", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True).stdout
            vals = sorted(float(x) for x in out.replace(",", " ").split() if x)
            return vals
        vv, aa = pts("v:0"), pts("a:0")
        return (vv[0] if vv else None, vv[-1] if vv else None, aa[0] if aa else None)

    rend = [("m1", up),
            ("720", hls.profile_key(720, None, 2800, hls.RUNG_TRANSCODE)),
            ("480", hls.profile_key(480, None, 1260, hls.RUNG_TRANSCODE))]
    # 取中段而不是第 0 段：偏差是累加出來的，第 0 段永遠是對的。
    probe_idx = min(3, len(spans) - 1)
    got = {}
    for name, prof in rend:
        p = hls.get_segment(rid, probe_idx, prof, real_dur)
        got[name] = seg_pts(p)
    check("三個 rendition 的 seg-%d 都產生得出來" % probe_idx,
          all(g[0] is not None for g in got.values()), got)
    starts = {k: g[0] for k, g in got.items()}
    spread = max(starts.values()) - min(starts.values())
    # 容許一個 keyframe 間距：邊界只能落在 keyframe 上，不可能比片源更準。
    check("同一個 seg-%d 在各階代表同一段 media time（修正前差 14.4 秒）" % probe_idx,
          spread <= 4.0, starts)
    check("兩個轉碼階之間完全對齊（它們用的是同一條固定格線）",
          abs(starts["720"] - starts["480"]) < 0.1, starts)

    # 音訊的時間軸也要跟著視訊走。A/V 差太多的話播放器為了同步會等或丟格，
    # 看起來也像 lag —— 而那跟時間軸倒退是兩種不同的病。
    for name, (v0, _v1, a0) in got.items():
        check(f"[{name}] 音訊與視訊的起點差在半秒內（不然 A/V 同步會拖慢播放）",
              a0 is not None and abs(a0 - v0) < 0.5, (name, v0, a0))

    # EXTINF 與實際封包長度的累積誤差：EXTINF 的總和就是 MediaSource 的
    # duration，每段差一點的話幾百段之後就會差好幾秒。
    pl_lines = hls.build_playlist(rid, real_dur, up).splitlines()
    extinfs = [float(l[len("#EXTINF:"):].rstrip(",")) for l in pl_lines
               if l.startswith("#EXTINF")]
    check("上階的 EXTINF 總和等於片長（這就是播放器右下角那個總時間）",
          abs(sum(extinfs) - real_dur) < 0.5, (sum(extinfs), real_dur))
    tr_pl = hls.build_playlist(rid, real_dur, rend[1][1]).splitlines()
    tr_ext = [float(l[len("#EXTINF:"):].rstrip(",")) for l in tr_pl
              if l.startswith("#EXTINF")]
    check("兩階的 EXTINF 總和一致（不然切階時 duration 會跳）",
          abs(sum(extinfs) - sum(tr_ext)) < 0.5, (sum(extinfs), sum(tr_ext)))
    check("兩階的段數一致（seg-N 要能一一對應）",
          abs(len(extinfs) - len(tr_ext)) <= 1, (len(extinfs), len(tr_ext)))

    os.environ["LIBRARY_LOCAL_ROOTS"] = ""


# ============================================================ 開播插隊
head("[J 0] 開播時要把還沒算的檔案插到佇列前面")

import inspect
from app.routers import stream as stream_router
src = inspect.getsource(stream_router.hls_master)
check("master 端點會呼叫 request_soon（不然那個插隊機制沒有人用）",
      "request_soon" in src, src[:200])
check("而且會把佇列叫起來（掃描時那條執行緒早就跑完了）",
      "start_background" in src)
check("不等它算完（一個檔案 30 秒，等於開不起來）",
      "run_queue" not in src)


# ============================================================ 畫質標示
head("[J] 畫質標示：級別依寬度，不依高度")

check("1920×804 的寬螢幕片是 1080p，不是 804p", media.quality_class(1920, 804) == "1080p")
check("1920×1080 也是 1080p", media.quality_class(1920, 1080) == "1080p")
check("1920×960（2:1）也是 1080p", media.quality_class(1920, 960) == "1080p")
check("3840×1608 的 4K 寬螢幕是 4K", media.quality_class(3840, 1608) == "4K")
check("1280×720 是 720p", media.quality_class(1280, 720) == "720p")
check("真的很小的片源就照高度講", media.quality_class(720, 404) == "404p")
check("沒有資料不要亂猜", media.quality_class(None, None) == "—")

check("縮放後的解析度跟濾鏡鏈算的一樣（寬度是偶數）",
      media.scaled_size(1920, 804, 720) == (1718, 720), media.scaled_size(1920, 804, 720))
check("目標比片源高就不放大", media.scaled_size(1280, 720, 1080) == (1280, 720))
check("target 0 = 不縮放", media.scaled_size(1920, 804, 0) == (1920, 804))
vf, out_h = media.build_video_filters(1920, 804, 720, False)
check("跟 build_video_filters 的輸出高度一致", out_h == media.scaled_size(1920, 804, 720)[1])


def _js_quality_class(src: str, w: int, h: int) -> str:
    """把 playback-state.js 的 qualityClass() 抽出來，用 Python 跑一次。

    **為什麼要這樣測而不是「檢查有沒有這段字」**：這兩個函式必須給出
    一模一樣的答案，而「原始碼裡有 1900 這個數字」證明不了那件事。
    直接把它的門檻表讀出來重跑，改了任一邊而忘了另一邊就會在這裡爆。

    只認得它現在的形狀（一串 `if (w >= A || h >= B) return 'X';`）——
    形狀變了這個 helper 會抓不到門檻，然後測試會失敗。那是刻意的：
    改了形狀就該回來確認兩邊還是一致的。
    """
    body = src[src.index("function qualityClass"):]
    body = body[:body.index("\n  }")]
    rules = re.findall(r"if \(w >= (\d+) \|\| h >= (\d+)\) return '([^']+)';", body)
    if len(rules) < 5:
        raise AssertionError("playback-state.js 的 qualityClass 形狀變了，"
                             "這個 helper 抓不到門檻 —— 回去確認兩邊還一致")
    for wt, ht, label in rules:
        if w >= int(wt) or h >= int(ht):
            return label
    return f"{h}p" if h else "—"


# ============================================================ 遠端 direct 政策
head("[J 5] 遠端該不該走 direct：看片源餵不餵得動，不是看有沒有 remux 上階")

# **舊的條件是反的。**舊碼是「remote and mode==direct and 有 remux → 改 HLS」，
# 於是一部 17 Mbps、沒有邊界表的 mp4 反而被判定「適合遠端 direct」——
# 正是最不適合的那一種（direct 沒有階可降，鏈路不夠就整段卡住）。
ok, why = hls.direct_ok_for_remote(3_000_000, has_remux=False)
check("片源 3 Mbps（在上限內）→ 繼續走 direct，畫質最好、成本最低", ok is True, why)

ok, why = hls.direct_ok_for_remote(17_000_000, has_remux=True)
check("片源 17 Mbps ＋ 有 remux 上階 → 改走 HLS（畫質一樣，而且降得下去）",
      ok is False, why)
# 這一項就是舊條件漏掉的那個洞
ok, why = hls.direct_ok_for_remote(17_000_000, has_remux=False)
check("**片源 17 Mbps ＋ 沒有 remux → 一樣要改走 HLS**"
      "（重編有損，但留在 direct 是完全播不動）", ok is False, why)
check("而且要講得出理由（後台與前端都要看得到）", "17000 kbps" in why, why)

ok, why = hls.direct_ok_for_remote(None, has_remux=True)
check("位元率不明 ＋ 有上階 → 不要賭（改 HLS 的代價是零，賭輸的代價是播不動）",
      ok is False, why)
ok, why = hls.direct_ok_for_remote(None, has_remux=False)
check("位元率不明 ＋ 沒有上階 → 改 HLS 一定會重編，那就先讓它試 direct",
      ok is True, why)

os.environ["REMOTE_DIRECT_MAX_KBPS"] = "0"
check("上限設 0 = 不檢查，回到舊行為（保留退路）",
      hls.direct_ok_for_remote(99_000_000, has_remux=True)[0] is True)
os.environ["REMOTE_DIRECT_MAX_KBPS"] = "6000"

check("剛好等於上限 → 還在安全範圍內（邊界是含的）",
      hls.direct_ok_for_remote(6_000_000, has_remux=False)[0] is True)
check("超過一點點就不行", hls.direct_ok_for_remote(6_001_000, has_remux=False)[0] is False)

# 預設值是量出來的，不是猜的（2026-09-11：經對外網址的**單條連線**只有
# 7.3 Mbps，而對外總頻寬有 220-310 Mbps —— 播放器抓一段就是一條連線，
# 所以照總頻寬設這個值等於讓每部大檔去賭一條餵不動的線）。
# 這裡釘住它，改的時候要連同 .env.example 那段量測紀錄一起更新。
del os.environ["REMOTE_DIRECT_MAX_KBPS"]
from app import params as _params
_default = _params.REGISTRY["REMOTE_DIRECT_MAX_KBPS"].default
check("預設 4000 = 單條實測 7.3 Mbps 的一半多一點（留餘裕給訊號差的手機）",
      _default == 4000, _default)
check("而且一部 10 Mbps 的片在預設值下不會被放行走遠端 direct",
      hls.direct_ok_for_remote(10_000_000, has_remux=True)[0] is False)
os.environ["REMOTE_DIRECT_MAX_KBPS"] = "6000"

# L：區網不能被這條政策動到 —— 那裡 direct/remux 一直是最佳解
_src = _insp.getsource(_api.play_info)
check("[L] 這條政策只在 remote 時套用（區網的零轉碼路徑不能被破壞）",
      "if remote and mode == \"direct\"" in _src, _src[:200])
check("[L] 而且使用者手動要求 direct 時要讓路", "force_direct" in _src)
check("play_info 會把判斷結果回給前端（前端的 fallback 門檻要用它收緊）",
      "direct_advised" in _src)


# ============================================================ 前端播放狀態
head("[J] 播放狀態：前端只能有一份事實來源")

_pjs = (ROOT / "app" / "static" / "player.js").read_text(encoding="utf-8")
_pst = (ROOT / "app" / "static" / "playback-state.js").read_text(encoding="utf-8")
_html = (ROOT / "app" / "static" / "player.html").read_text(encoding="utf-8")

check("playback-state.js 有被載進 player.html（不然 player.js 一開頁就炸）",
      "playback-state.js" in _html, _html[-400:])
check("而且排在 player.js 前面（它是相依）",
      _html.index("playback-state.js") < _html.index("/static/player.js"))

# **原本的 bug 就是這一行**：renderTags() 拿 info.height 當「現在在播的畫質」。
# 註解裡會提到那個舊寫法（說明為什麼要改），所以比對前要先把註解剝掉 ——
# 不然這一項會被自己的說明文字絆倒。
_tags = _pjs[_pjs.index("function renderTags"):]
_tags = _tags[:_tags.index("\n}")]
_tags_code = "\n".join(ln for ln in _tags.splitlines()
                       if not ln.lstrip().startswith("//"))
check("[A] renderTags 不再拿 info.height／info.width 當「目前播放解析度」",
      "info.height" not in _tags_code and "info.width" not in _tags_code, _tags_code[:400])
check("[A] renderTags 改成從 runtime state 取（statusLine）",
      "PS.statusLine(playbackState)" in _tags_code, _tags_code[:400])

check("[C] curLevel 這個各自為政的全域已經拿掉",
      "\nlet curLevel" not in _pjs and "curLevel =" not in _pjs)
check("[C] 換片／換模式／換畫質都會把狀態清乾淨",
      "function resetPlaybackState" in _pjs and "PS.emptyState()" in _pjs)
check("[C] play() 一開始就 reset（不 reset 的話舊的階會留在畫面上）",
      "const gen = resetPlaybackState(which)" in _pjs)

check("[B/D] UI 只有一支更新入口",
      "function updatePlaybackState" in _pjs and "function renderPlaybackStatus" in _pjs)
for ev in ("MANIFEST_PARSED", "LEVEL_SWITCHING", "LEVEL_SWITCHED",
           "FRAG_LOADING", "FRAG_LOADED", "FRAG_BUFFERED", "ERROR"):
    check(f"[D] 狀態來源含 Hls.Events.{ev}", f"Hls.Events.{ev}" in _pjs)
for ev in ("loadedmetadata", "playing", "waiting", "canplay", "progress", "resize"):
    check(f"[D] 狀態來源含 video 的 {ev}", f"'{ev}'" in _pjs)

check("[I] 所有非同步 callback 都過 generation 閘（切換之後舊事件要丟掉）",
      "function isStale" in _pjs and "isStale(gen)" in _pjs)
check("[I] hls 事件的 handler 統一包一層 isStale ＋ 確認還是同一個實例",
      "if (!isStale(gen) && hlsObj === H)" in _pjs)

check("[G] direct fallback 會把 currentTime 帶過去（不能跳回開頭）",
      "const at = v.currentTime;" in _pjs and "play('hls', at)" in _pjs)
check("[H] fallback 只做一次（fellBack 統一由 metrics 管，不再有第二個旗標）",
      "\nlet fellBack" not in _pjs and "metrics.fellBack" in _pjs)

check("[J 3] 有啟用 capLevelToPlayerSize（手機 390px 不該去拉 4K）",
      "capLevelToPlayerSize: true" in _pjs)
check("[J 3] 全螢幕／轉向之後會重新評估（不能永久鎖死低畫質）",
      "orientationchange" in _pjs and "fullscreenchange" in _pjs)
check("[J 4] 降階壓的是 autoLevelCapping（上限），不是鎖死 currentLevel",
      "autoLevelCapping = target" in _pjs and "hlsObj.currentLevel =" not in _pjs)
check("[J 4] 恢復穩定會把上限拿掉，交還 hls.js 的 Auto ABR",
      "function releaseCap" in _pjs and "autoLevelCapping = -1" in _pjs)

check("[F] 選單標籤講「上限」，不是讓人以為鎖死", "' 上限'" in _pjs)
check("[8] debug 疊圖要靠 query parameter 開，不能預設塞在畫面上",
      "params.get('debugPlayer')" in _pjs and "if (!debugPlayer) return;" in _pjs)

# 級別判定兩邊必須一致 —— 分岔的話同一階在標籤與面板上會被標成不同級別
for w, h in [(1920, 804), (1920, 1080), (1920, 960), (3840, 1608), (1280, 720),
             (854, 480), (720, 404)]:
    check(f"級別判定 {w}×{h} 前後端一致",
          media.quality_class(w, h) == _js_quality_class(_pst, w, h),
          (media.quality_class(w, h), _js_quality_class(_pst, w, h)))


# ============================================================ 串流量測
head("[J 7] 效能修改要留下量測依據，不是憑感覺調參數")

from app import streamstat
from app.config import settings
streamstat.reset()
streamstat.record_ftp("/x/a.mkv", 10 * 1024 * 1024, 1.0)
streamstat.record_ftp("/x/a.mkv", 10 * 1024 * 1024, 2.0)
snap = streamstat.snapshot()
check("搬運吞吐記得下來", snap["summary"]["ftp_mbps"]["n"] == 2, snap["summary"])
check("而且算得出中位數", snap["summary"]["ftp_mbps"]["med"] > 0)

# 走本機路徑的小 Range 常常在 Windows 上量到 0.0（計時器粒度 15.6ms）。
# **丟掉那些樣本會讓最快的請求整批消失**，命中率與樣本數都失真。
streamstat.reset()
streamstat.record_ftp("/x/a.mkv", 4096, 0.0, source="local")
snap = streamstat.snapshot()
check("快到量不出來的請求也要留（丟掉的話診斷頁看不到最快的那些）",
      snap["summary"]["ftp_mbps"]["n"] == 1, snap["summary"])
check("但要標出來是被夾過的，不然「819 Mbps」會被當成真的量測結果",
      snap["recent"]["ftp"][0]["capped"] is True, snap["recent"]["ftp"][0])
check("真的量得到的就不標", (streamstat.reset(),
      streamstat.record_ftp("/x/a.mkv", 4096, 0.5),
      streamstat.snapshot()["recent"]["ftp"][0]["capped"])[2] is False)
streamstat.reset()
streamstat.record_ftp("/x/a.mkv", 10 * 1024 * 1024, 1.0)
streamstat.record_ftp("/x/a.mkv", 10 * 1024 * 1024, 2.0)

streamstat.record_segment(1, 0, "h720_m0", 12.5, 2_000_000, cached=False)
streamstat.record_segment(1, 1, "h720_m0", 0.01, 2_000_000, cached=True)
snap = streamstat.snapshot()
check("分段命中率算得出來（分辨「ffmpeg 太慢」與「prefetch 沒跟上」）",
      snap["segment_hit_rate"] == 0.5, snap["segment_hit_rate"])
check("**沒命中的那些才問得出「等 ffmpeg 等多久」**（混在一起會被稀釋成沒有意義的數字）",
      snap["summary"]["segment_miss_elapsed"]["med"] == 12.5,
      snap["summary"]["segment_miss_elapsed"])

streamstat.record_produce(1, 0, hls.RUNG_TRANSCODE, 6.0, 3.0, background=False)
snap = streamstat.snapshot()
check("ffmpeg 速度倍率記得下來（小於 1 就是追不上播放）",
      snap["summary"]["produce_speed"]["med"] == 2.0, snap["summary"]["produce_speed"])

os.environ["STREAM_DIAG"] = "false"
streamstat.reset()
streamstat.record_ftp("/x/a.mkv", 1024, 1.0)
check("關掉之後取樣點就是一個 early return（極低階機器的退路）",
      streamstat.snapshot()["summary"]["ftp_mbps"] is None)
os.environ["STREAM_DIAG"] = "true"

check("搬運塊大小可以調（但預設沒有動：256 KiB，改預設等於在沒有證據下改所有人的行為）",
      settings.stream_chunk_kb == 256, settings.stream_chunk_kb)
_ftp_src = (ROOT / "app" / "ftpclient.py").read_text(encoding="utf-8")
check("iter_chunks 吃設定值而不是寫死 256 KiB",
      "stream_chunk_kb" in _ftp_src and "chunk_size: Optional[int] = None" in _ftp_src)
check("而且每一次搬運都會被量到（含中途被切斷的那些 —— 那正是最要看的）",
      "streamstat.record_ftp" in _ftp_src and "finally:" in _ftp_src)

_str_src = (ROOT / "app" / "routers" / "stream.py").read_text(encoding="utf-8")
check("[J −1] direct 走本機路徑（不要繞 FTP loopback 讀同一顆磁碟上的同一個檔案）",
      "localfs.resolve_local" in _str_src, _str_src[:200])
check("對不到本機路徑就照舊走 FTP（別台機器掛遠端 FTP 時的退路）",
      "if local is not None:" in _str_src)
check("分段端點量得到快取命中（在 get_segment 之前問，之後問一定是命中）",
      "segment_cached" in _str_src
      and _str_src.index("segment_cached") < _str_src.index("hls.get_segment"))
check("有一支看得到量測結果的端點", "/diagnostics/stream" in _insp.getsource(_api))


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
