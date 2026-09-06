"""兩階 HLS：profile key 的階別維度、發幾階、上階的邊界與 BANDWIDTH。

    python tests/two_rung_test.py

規格：J 章第 0 層。這一支只驗播放清單與 key，不產生任何分段 ——
remux 真的產生分段是下一批（`build_transcode_cmd` 的 remux 分支）。
"""
import json
import os
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
             w=1920, h=960, kf="ok", gap=3.625):
    _seq[0] += 1
    fid = db.execute(
        """INSERT INTO media_file(ftp_path, filename, ext, size, duration,
                                  width, height, video_codec, video_profile,
                                  video_level, probe_state, kf_state, seen_at, added_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'ok',?,0,0)""",
        (f"/x/{_seq[0]}-{codec}-{kf}.mkv", "x.mkv", "mkv", size, DUR,
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
# 上階 ＋ 一條轉碼階梯（J 章第 1 層）。上階的峰值 21.7 Mbps 行動網路選不到，
# 所以「上階＋單一轉碼階」等於遠端只有一階可用、卡頓時無階可降。
check("遠端發上階＋整條階梯（不只兩階）", m_remote.count("#EXT-X-STREAM-INF") >= 3, m_remote)
check("區網只發一階（零轉碼路徑）", m_lan.count("#EXT-X-STREAM-INF") == 1, m_lan)
check("區網那一階是上階", f"?p={upper}" in m_lan and f"?p={lower}" not in m_lan, m_lan)

first = [l for l in m_remote.splitlines() if l.startswith("#EXT-X-STREAM-INF")][0]
bw = int(first.split("BANDWIDTH=")[1].split(",")[0])
avg = int(first.split("AVERAGE-BANDWIDTH=")[1].split(",")[0])
# 邊界是均勻的，所以峰值≈平均＋音訊；重點是「兩個都有、峰值不小於平均」
check("上階有 BANDWIDTH 與 AVERAGE-BANDWIDTH 兩個", bw > 0 and avg > 0, first)
check("BANDWIDTH 是峰值，不小於平均", bw >= avg, (bw, avg))
check("兩個都含音訊碼率", avg > 192_000, avg)
check("上階的解析度是片源原本的（不縮放）", "RESOLUTION=1920x960" in first, first)
check("上階的 CODECS 照片源的 profile／level 填（不是寫死的 64001f）",
      'CODECS="avc1.640029,mp4a.40.2"' in first, first)

fid_np = add_file(profile=None, level=None)
m_np = hls.build_master(fid_np, lower, remote=True, duration=DUR)
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
head("[J 0] profile key 換形狀 → 舊快取要清一次")

d = hls.seg_dir(fid, "h720_ad_b0_c2")      # 舊形狀（沒有 m）的資料夾
(d / "seg-0.ts").write_bytes(b"x")
db.kv_set("hls_profile_key_version", 1)
hls.migrate_cache()
check("舊分段被清掉（不然它們會一直佔著快取上限）", not (d / "seg-0.ts").exists())
check("版本號記下來了", db.kv_get("hls_profile_key_version") == hls.PROFILE_KEY_VERSION)
(d / "seg-1.ts").write_bytes(b"x")
hls.migrate_cache()
check("第二次啟動不會再清（不然每次重開都要重轉一遍）", (d / "seg-1.ts").exists())


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
check("-ss 就是邊界表給的時間", cmd[cmd.index("-ss") + 1] == "7.250")
check("非單調 DTS 的片源要能過（make_zero 而不是拒收）",
      cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero", cmd)
check("時間軸放回這一段該有的位置",
      cmd[cmd.index("-output_ts_offset") + 1] == "7.250")
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
                    "-t", "24", "-c:v", "libx264", "-preset", "veryfast",
                    "-g", "48", "-pix_fmt", "yuv420p", "-profile:v", "high",
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


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
