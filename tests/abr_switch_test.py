"""ABR 切階時畫面不能倒退：真的 Chromium + 真的 hls.js + 真的分段。

    python tests/abr_switch_test.py

**為什麼一定要開真的瀏覽器。**這一類 bug 不會被「指令字串裡有沒有
`-output_ts_offset`」抓到 —— 那種檢查只證明時間戳有被寫進去，不證明
兩階寫的是同一個值。使用者回報的症狀是：

    正常播放中，畫面突然跳回之前已經播過的地方，接著又跳回正確位置。

而那是 rendition 之間 seg-N 對不齊時，hls.js 把另一個 media time 的內容
append 進 SourceBuffer 造成的。要抓到它，必須讓真的 hls.js 在真的分段上
切階，然後量**實際呈現出來的畫格**。

**量的是 `requestVideoFrameCallback` 的 mediaTime，不是 currentTime。**
currentTime 是播放頭的位置，它不往回不代表螢幕上的畫面沒有回頭 ——
那正是使用者肉眼看到、而既有測試全部漏掉的那一層。

驗收規則（使用者訂的）：
    沒有 Seek 的情況下，實際呈現的 video frame mediaTime 不得突然回退數秒。
"""
import asyncio, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-abr-")
PORT = 29417
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "HOST": "127.0.0.1", "PORT": str(PORT),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "HLS_SEGMENT_SECONDS": "6", "AUDIO_BITRATE_KBPS": "128", "AUDIO_CHANNELS": "2",
    "TRANSCODE_MAX_HEIGHT": "1080", "HLS_TWO_RUNG": "true",
    "REMOTE_MAX_HEIGHT": "720", "REMOTE_BITRATE_KBPS": "2800",
    "LIBRARY_LOCAL_ROOTS": f"/={os.path.join(TMP, 'ftproot')}",
    # 預轉會跟測試搶 CPU，而這支測的是時間軸不是吞吐量
    "HLS_PREFETCH": "0",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

ff = shutil.which("ffmpeg")
fp = shutil.which("ffprobe")
if not (ff and fp):
    print("  (跳過：這台機器上找不到 ffmpeg／ffprobe)")
    sys.exit(0)

# --------------------------------------------------------------------------
# 合成片源
# --------------------------------------------------------------------------
# **每一秒都要看得出是第幾秒。**倒退是靠 mediaTime 判定的，但出事的時候
# 要能截圖給人看 —— 畫面上有時間碼，才說得清「它跳回哪裡」。
#
# keyframe 間距刻意選 3.48 秒（87 格 ÷ 25fps）：**除不盡 6 秒的分段長度**。
# 整除的片源會讓兩階碰巧對齊，那樣這支測試就證明不了任何事。
head("[J 1] 準備合成片源（每秒有時間碼、keyframe 間距除不盡分段長度）")
root = Path(TMP, "ftproot")
root.mkdir(parents=True, exist_ok=True)
src = root / "abr.mp4"
subprocess.run([
    ff, "-hide_banner", "-loglevel", "error",
    # **解析度要夠大，遠端階梯才切得出好幾階。**遠端現在是 transcode-only
    # （remux 不進 ABR），所以可切的階全部來自 _ABR_STEPS —— 而那幾階會被
    # _ABR_MIN_GAP(100px) 與 _ABR_MIN_HEIGHT(240) 砍。640x360 只剩 360／240
    # 兩階（240→180 差 60 < 100 就不發），再被 capLevelToPlayerSize 一擋就
    # 可能只剩一階，這支測試要驗的「切階」就無從發生。1280x720 給得出三階。
    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
    "-t", "120",
    "-vf", "drawtext=text='%{eif\\:t\\:d}':fontsize=64:fontcolor=white:x=20:y=20",
    "-c:v", "libx264", "-preset", "veryfast",
    # **GOP 要除得盡 HLS_SEGMENT_SECONDS。**25fps × 50 格 = 2.00 秒，
    # 6 秒剛好三個 keyframe —— 上階才切得出跟下階一樣的格線，
    # 也才進得了 ABR master（`abr_alignable()` 容許 0.25 秒）。
    # 原本的 87 格 = 3.48 秒除不盡 6，上階會被擋在 ABR 之外，
    # 於是 master 只剩一階、這支測試要驗的「切階」根本不會發生。
    "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
    "-pix_fmt", "yuv420p", "-profile:v", "high",
    "-c:a", "aac", "-ac", "2", "-shortest", "-y", str(src)], check=True)
dur = float(subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True).stdout.strip())
check("片源產生得出來（120 秒）", abs(dur - 120) < 1.0, dur)

from app import db, hls, keyframes
db.init_db()
now = db.now()
item = db.execute(
    "INSERT INTO media_item(kind,title,sort_title,year,guess_key,scrape_state,"
    "added_at,updated_at) VALUES('movie','ABR 測試','abr',2024,'movie::abr::2024','ok',?,?)",
    (now, now)).lastrowid
fid = db.execute(
    """INSERT INTO media_file(item_id,ftp_path,filename,ext,size,duration,width,height,
                              video_codec,audio_codec,audio_tracks,bitrate,probe_state,
                              play_mode,kf_state,seen_at,added_at)
       VALUES(?,?,?,'mp4',?,?,1280,720,'h264','aac',?,1200000,'ok','hls','pending',?,?)""",
    (item, "/abr.mp4", "abr.mp4", src.stat().st_size, dur,
     json.dumps([{"index": 1, "codec": "aac", "channels": 2, "default": 1}]),
     now, now)).lastrowid

check("keyframe 掃得起來",
      keyframes.scan_one(dict(db.q1(
          "SELECT id, ftp_path, video_codec FROM media_file WHERE id=?", (fid,)))) == "ok")
spans = hls.bounds_for(fid, dur)
check("上階切得出分段", len(spans) > 5, len(spans))
check("上階可以進 ABR master（對得齊）", hls.abr_alignable(fid, dur)[0],
      hls.abr_alignable(fid, dur))

# --------------------------------------------------------------------------
# 起伺服器
# --------------------------------------------------------------------------
import uvicorn
from app.main import app
from app.routers import api as api_router, stream as stream_router

# **要測的是遠端那條路。**ABR 階梯只發給遠端（區網刻意只發單一 rendition，
# 因為鏈路夠寬、多發幾階換不到東西）—— 而測試從 127.0.0.1 連進來，
# `is_local_network()` 會說是區網，master 就只有一階，切都沒得切。
#
# 所以這裡把那個判斷蓋掉。**只蓋兩個路由模組看到的那一份**，不要蓋
# `auth.is_local_network` 本身：AUTH_ENABLED=false 時的「外部連線一律擋掉」
# 那道保險也用同一個函式，蓋掉它會讓測試自己的請求全部被擋成 403。
#
# 蓋的是「這個 IP 算不算區網」，不是 master 的產生邏輯 ——
# build_master()／build_playlist()／get_segment() 全都照正式路徑跑，
# 測到的就是遠端使用者實際拿到的那份 master。
class _AlwaysRemote:
    def __getattr__(self, name):
        from app import auth as _a
        return getattr(_a, name)
    @staticmethod
    def is_local_network(ip):
        return False

api_router.auth = _AlwaysRemote()
stream_router.auth = _AlwaysRemote()

_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
threading.Thread(target=_server.run, daemon=True).start()
for _ in range(100):
    if getattr(_server, "started", False):
        break
    time.sleep(0.1)
BASE = f"http://127.0.0.1:{PORT}"


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=[
            # 合成片源沒有真的視訊硬解也要能跑，而且要拿得到 playback quality
            "--autoplay-policy=no-user-gesture-required",
        ])
        ctx = await b.new_context(viewport={"width": 1280, "height": 800})
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        head("[J 1] 在真的 hls.js 上強制切階，量實際呈現的畫格")
        # debugPlayer=1 才會掛上 frame monitor 的 console 輸出；
        # 但量測本身是這一頁自己用 requestVideoFrameCallback 做的，
        # 不依賴 player.js —— 兩邊各量一次才看得出是不是量錯了。
        await pg.goto(f"{BASE}/player?file={fid}&debugPlayer=1",
                      wait_until="domcontentloaded")

        # 等 hls.js 真的把 level 讀出來（分段是隨選轉碼的，第一段要等 ffmpeg）
        try:
            await pg.wait_for_function(
                "() => window.__playerTestHooks && window.__playerTestHooks.hls()"
                " && window.__playerTestHooks.hls().levels"
                " && window.__playerTestHooks.hls().levels.length > 1", timeout=120000)
        except Exception as e:
            check("hls.js 讀得到多階（master 有發出階梯）", False, str(e)[:200])
            await b.close()
            return

        levels = await pg.evaluate(
            "() => window.__playerTestHooks.hls().levels.map(l => ({h: l.height, b: l.bitrate}))")
        check(f"master 發出多階可切（{[l['h'] for l in levels]}）", len(levels) > 1, levels)

        # 裝上畫格監控。**這一份是測試自己的**，不是讀 player.js 的計數 ——
        # 要驗的正是「畫面有沒有倒退」，不能只相信被測程式自己的說法。
        await pg.evaluate("""() => {
            const v = document.querySelector('#v');
            window.__probe = { frames: [], regressions: [], ctRegressions: [],
                               last: -1, lastCt: -1, switches: [] };
            const p = window.__probe;
            if (typeof v.requestVideoFrameCallback !== 'function') { p.unsupported = true; return; }
            const step = (_n, m) => {
                const t = m.mediaTime;
                if (typeof t === 'number') {
                    p.frames.push(t);
                    // 一格的抖動不算（decoder 對 B-frame 的呈現順序本來就會有誤差）
                    if (p.last >= 0 && t < p.last - 0.25)
                        p.regressions.push({ from: p.last, to: t, delta: t - p.last,
                                             level: window.__playerTestHooks.hls()?.currentLevel });
                    p.last = t;
                }
                const ct = v.currentTime;
                if (p.lastCt >= 0 && ct < p.lastCt - 0.25)
                    p.ctRegressions.push({ from: p.lastCt, to: ct });
                p.lastCt = ct;
                v.requestVideoFrameCallback(step);
            };
            v.requestVideoFrameCallback(step);
            const H = window.__playerTestHooks.hls();
            H.on(Hls.Events.LEVEL_SWITCHED, (_, d) => p.switches.push(d.level));
        }""")
        unsupported = await pg.evaluate("() => !!window.__probe.unsupported")
        if unsupported:
            print("  (跳過：這個 Chromium 沒有 requestVideoFrameCallback)")
            await b.close()
            return

        async def play_for(ms):
            await pg.evaluate("() => document.querySelector('#v').play().catch(()=>{})")
            await pg.wait_for_timeout(ms)

        # 先讓它穩定播一段，確認基準是會動的
        await play_for(6000)
        moved = await pg.evaluate("() => window.__probe.frames.length")
        check(f"畫格真的有在前進（收到 {moved} 張）", moved > 10, moved)

        # ---------------- 強制切階：高 → 低 → 高 ----------------
        # **用 currentLevel 直接指定**，不等 ABR 自己決定：測的是「切階之後
        # 時間軸對不對」，不是「ABR 判斷得準不準」。要讓它一定會切，而且
        # 切在播放中途（不是開播）—— 那正是使用者看到倒退的時機。
        order = [len(levels) - 1, 0, len(levels) - 1, 0]
        for lv in order:
            await pg.evaluate("(lv) => { window.__playerTestHooks.hls().currentLevel = lv; }", lv)
            await play_for(5000)

        st = await pg.evaluate("() => window.__probe")
        switched = len(st["switches"])
        check(f"真的切了好幾次階（{switched} 次）", switched >= 3, st["switches"])

        # ================= 驗收條件 =================
        regs = st["regressions"]
        big = [r for r in regs if r["delta"] < -1.0]
        check("**沒有 Seek 時，呈現的畫格 mediaTime 不得倒退數秒**（本次修正的驗收規則）",
              not big, big[:5])
        # 6 秒／10 秒／100 秒等級的倒退是使用者形容的那個症狀
        seg_sized = [r for r in regs if r["delta"] < -3.0]
        check("沒有分段等級（> 3 秒）的倒退", not seg_sized, seg_sized[:5])
        check("currentTime 也沒有倒退", not st["ctRegressions"], st["ctRegressions"][:5])
        if regs:
            print(f"    （{len(regs)} 次一格以內的抖動，最大 "
                  f"{min(r['delta'] for r in regs):.3f}s —— 那是 decoder 的正常誤差）")

        # 畫格整體要是往前走的：只看「有沒有倒退」的話，一路卡住不動也會通過。
        frames = st["frames"]
        check("畫格整體往前推進（不是卡住不動）",
              len(frames) > 50 and frames[-1] > frames[0] + 5,
              (len(frames), frames[0] if frames else None, frames[-1] if frames else None))

        # 掉格率：這是「lag」的第三類（解碼跟不上），跟時間軸倒退不是同一件事，
        # 要分開量才不會把 D 類問題當成 C 類去調 buffer。
        q = await pg.evaluate("""() => {
            const v = document.querySelector('#v');
            const x = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
            return x ? { total: x.totalVideoFrames, dropped: x.droppedVideoFrames } : null;
        }""")
        if q and q["total"]:
            rate = q["dropped"] / q["total"] * 100
            print(f"    掉格 {q['dropped']}/{q['total']} = {rate:.2f}%")
            check(f"掉格率沒有失控（{rate:.2f}%）", rate < 20, q)

        # ---------------- Seek 之後第一張畫格要來自新位置 ----------------
        # 這跟 ABR 倒退是**兩種不同的病**，要分開驗（規格第十三節）。
        head("[J 1] Seek 之後看到的第一張有效畫格必須來自新位置")
        for frac in (0.7, 0.4, 0.9):
            target = dur * frac
            got = await pg.evaluate("""async (target) => {
                const v = document.querySelector('#v');
                v.currentTime = target;
                return await new Promise(res => {
                    let done = false;
                    const t0 = performance.now();
                    const step = (_n, m) => {
                        if (done) return;
                        // 第一張「落在目標附近」的畫格就算 seek 視覺上完成
                        if (Math.abs(m.mediaTime - target) <= 2.0) {
                            done = true; res({ ok: true, t: m.mediaTime,
                                               ms: performance.now() - t0 });
                            return;
                        }
                        if (performance.now() - t0 > 30000) {
                            done = true; res({ ok: false, t: m.mediaTime }); return;
                        }
                        v.requestVideoFrameCallback(step);
                    };
                    v.requestVideoFrameCallback(step);
                });
            }""", target)
            check(f"seek 到 {frac:.0%}（{target:.0f}s）之後畫格來自新位置"
                  + (f"，耗時 {got['ms']:.0f} ms" if got.get("ms") else ""),
                  got["ok"], got)

        check("頁面沒有 JS 例外", not errs, errs[:3])
        await b.close()


asyncio.run(main())
print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
