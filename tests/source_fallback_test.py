"""來源層的回歸測試：keyframe 重試風暴、本機直讀的診斷、FTP 502 的可觀測性、Range。

    python tests/source_fallback_test.py

背景（production，2026-09-23）：兩支手機錄影（file 3357 h264／3428 hevc）播不動。
根因不是 codec —— 那兩支在磁碟上被搬到別的資料夾，DB 還記著舊路徑，
而它們所在的 FTP mount（/Yu）根本不在 LIBRARY_LOCAL_ROOTS 裡，所以
ffmpeg 走 HTTP→FastAPI→FTP，FTP 回 550，/api/stream 回一個看不出原因的 502。
同時 keyframe 佇列把失敗的 3357 **立刻重挑**，兩個多小時重試了 4.5 萬次。

這支測試釘住的是最終驗收標準：
**即使某一支影片的 keyframe 掃描永遠失敗，也不能拖垮其他影片的正常播放。**
"""
import ftplib
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-src-")).resolve())
LOCAL = Path(TMP, "local-media")
# 每次執行隨機產生，不寫成字面值：只拿來驗「回應與 log 不會洩漏密碼」。
FTP_PW = "pw-" + secrets.token_hex(8)
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "AUTH_ENABLED": "true", "AUTH_PASSWORD": "adminpw12345",
    "VIEWER_PASSWORD": "viewerpw12345",
    "TMDB_API_KEY": "", "MSSQL_HOST": "", "AUTO_SCAN_ON_START": "false",
    # FTP 與自身 HTTP 都指到關著的 port：走到 FTP 那條路就一定失敗，
    # 跟 production 上「檔案不在」一樣是**每次都失敗**的來源。
    "FTP_HOST": "127.0.0.1", "FTP_PORT": "1", "PORT": "1",
    "FTP_PASSWORD": FTP_PW,
    # 只有 /媒體資料庫 有本機對應；/Yu 刻意沒有（production 的狀況）
    "LIBRARY_LOCAL_ROOTS": f"/媒體資料庫={LOCAL}",
    "HLS_SEGMENT_SECONDS": "6", "HLS_TWO_RUNG": "true", "HLS_PREFETCH_SEGMENTS": "0",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


def head(t):
    print(f"\n{t}")


from app import auth, db, ftpclient, hls, keyframes, localfs, media   # noqa: E402
from app.main import app                                              # noqa: E402
from starlette.testclient import TestClient                           # noqa: E402

db.init_db()


def add_file(path, codec="h264", probe="ok", kf="pending", ext="mp4",
             size=1000, duration=20.0):
    return db.execute(
        """INSERT INTO media_file(ftp_path, filename, ext, size, duration, width, height,
                                  video_codec, audio_codec, play_mode, probe_state,
                                  kf_state, seen_at, added_at)
           VALUES(?,?,?,?,?,320,180,?,'aac','hls',?,?,0,0)""",
        (path, Path(path).name, ext, size, duration, codec, probe, kf)).lastrowid


def reset_queue_state():
    keyframes.clear_failed_cooldown()
    with keyframes._priority_lock:
        keyframes._priority.clear()


def run_queue_bounded(timeout=15.0, **kw):
    """在另一條執行緒跑 run_queue，逾時就當作「停不下來」—— 測試不能跟著卡死。"""
    box = {}

    def go():
        box["stats"] = keyframes.run_queue(**kw)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("stats")


# ============================================================ A. retry storm
head("[A] 失敗的檔案同一輪只試一次，而且不擋住後面的檔案")

real_scan = keyframes.scan_one
calls = []


def fake_scan(row, cancel=None):
    fid = int(row["id"])
    calls.append(fid)
    if fid == FA:
        db.execute("UPDATE media_file SET kf_state='failed', kf_error='550' WHERE id=?", (fid,))
        return "failed"
    db.execute("UPDATE media_file SET kf_state='ok' WHERE id=?", (fid,))
    return "ok"


FA = add_file("/Yu/手機照片/A.mp4")
FB = add_file("/Yu/手機照片/B.mp4")
keyframes.scan_one = fake_scan
try:
    finished, stats = run_queue_bounded()
    check("run_queue 會返回（不是無限迴圈）", finished, calls[:20])
    check("壞檔 A 在同一輪只被呼叫一次", calls.count(FA) == 1, calls[:20])
    check("A 後面的 B 仍然被處理（一支壞檔不擋整個佇列）", calls.count(FB) == 1, calls)
    check("統計：一個 failed、一個 ok", stats == {"ok": 1, "skipped": 0, "failed": 1}, stats)

    # 只剩 A 一支、而且它是 failed 的時候（production 的形狀）：也要停得下來
    calls.clear()
    keyframes.clear_failed_cooldown()
    finished, stats = run_queue_bounded()
    check("只剩一支永遠失敗的檔案時 run_queue 仍然返回", finished, calls[:20])
    check("……而且只試了一次", calls == [FA], calls[:20])

    # scan_one 直接丟例外（逾時、ffprobe 不見）：記成這支失敗，不要讓佇列中斷
    FC = add_file("/Yu/手機照片/C.mp4")
    FD = add_file("/Yu/手機照片/D.mp4")

    def raising_scan(row, cancel=None):
        fid = int(row["id"])
        calls.append(fid)
        if fid == FC:
            raise subprocess.TimeoutExpired(["ffprobe"], 1800)
        return fake_scan(row, cancel)
    calls.clear()
    keyframes.scan_one = raising_scan
    finished, stats = run_queue_bounded()
    check("scan_one 丟例外的檔案被標成 failed",
          db.q1("SELECT kf_state FROM media_file WHERE id=?", (FC,))["kf_state"] == "failed")
    check("……而且後面的 D 照樣被處理", FD in calls and calls.count(FC) == 1, calls)
    keyframes.scan_one = fake_scan

    # ======================================================== B. retry later
    head("[B] 失敗之後：冷卻期內不重試，明確的新一輪才重試")

    calls.clear()
    finished, _ = run_queue_bounded()
    check("冷卻期內的下一輪不碰 A（每次開播都啟動佇列也不會重掃）",
          finished and FA not in calls, calls)

    keyframes.request_soon(FA)
    check("冷卻期內插隊也挑不到 A", keyframes._next_row() is None)
    check("runnable()：failed 且冷卻中 → False", not keyframes.runnable(FA, "failed"))
    check("runnable()：pending → True", keyframes.runnable(FB, "pending"))
    check("冷卻期是分鐘等級，不是毫秒", keyframes.FAILED_RETRY_COOLDOWN >= 60,
          keyframes.FAILED_RETRY_COOLDOWN)

    # 冷卻期過了
    with keyframes._failed_lock:
        keyframes._failed_at[FA] -= keyframes.FAILED_RETRY_COOLDOWN + 1
    calls.clear()
    finished, _ = run_queue_bounded()
    check("冷卻期過了之後的新一輪會再試 A 一次", calls.count(FA) == 1, calls)

    # 重新掃描／管理員手動重試：清掉冷卻期
    calls.clear()
    check("清冷卻期回傳清掉的筆數", keyframes.clear_failed_cooldown() >= 1)
    finished, _ = run_queue_bounded()
    check("明確重試（clear_failed_cooldown）之後 A 再被試一次", calls.count(FA) == 1, calls)

    # start_background(retry_failed=True) 就是重新掃描走的那條路
    calls.clear()
    reset_queue_state()
    with keyframes._failed_lock:
        keyframes._failed_at[FA] = 1e18          # 遠在未來 → 一定冷卻中
    check("冷卻中", not keyframes.retry_due(FA))
    keyframes.start_background(retry_failed=True)
    if keyframes._queue_thread is not None:
        keyframes._queue_thread.join(15)
    check("start_background(retry_failed=True) 會讓 A 再試一次", calls.count(FA) == 1, calls)
finally:
    keyframes.scan_one = real_scan

# ---- 用真的 scan_one 重演 production：來源每次都 5XX
head("[A'] 真的 scan_one：來源永遠失敗（需要 ffprobe）")
fp = shutil.which(media.resolve_tool("ffprobe")) or shutil.which("ffprobe")
if not fp:
    print("  (跳過：找不到 ffprobe)")
else:
    reset_queue_state()
    db.execute("UPDATE media_file SET kf_state='skipped'")    # 只留下面這一支
    FR = add_file("/Yu/手機照片/2021/7-9月/20210717_094211+0800-74828175.mp4")
    real_run = media.run_tool
    probes = []

    def counting_run(cmd, timeout=120, cancel=None):
        if cmd and "ffprobe" in os.path.basename(cmd[0]).lower() and "-skip_frame" in cmd:
            probes.append(cmd[-1])
        return real_run(cmd, timeout=timeout, cancel=cancel)
    media.run_tool = counting_run
    try:
        finished, stats = run_queue_bounded(timeout=60)
    finally:
        media.run_tool = real_run
    check("來源讀不到時 run_queue 會返回", finished, stats)
    check("同一支檔案只被 ffprobe 掃了一次（原本會無限重掃）", len(probes) == 1, len(probes))
    check("來源走的是 HTTP（/Yu 沒有本機對應）", probes and "/api/stream/" in probes[0], probes)
    check("被標成 failed", db.q1("SELECT kf_state FROM media_file WHERE id=?",
                                 (FR,))["kf_state"] == "failed")


# ============================================================ C/D. 本機直讀
head("[C] 本機對得到：ffmpeg 直接開本機檔案")

(LOCAL / "0.Movie").mkdir(parents=True, exist_ok=True)
movie = LOCAL / "0.Movie" / "video.mp4"
movie.write_bytes(b"\x00" * 4096)
FL = add_file("/媒體資料庫/0.Movie/video.mp4", size=4096)
args = media.input_args(FL)
check("input_args 是 [-i, 本機路徑]", args == ["-i", str(movie.resolve())], args)
check("不含 http://127.0.0.1", not any("http://" in a for a in args), args)
info = localfs.resolve_local_info("/媒體資料庫/0.Movie/video.mp4")
check("resolve_local_info → ok", info["reason"] == "ok" and info["prefix"] == "/媒體資料庫", info)
check("resolve_local() 行為不變", localfs.resolve_local("/媒體資料庫/0.Movie/video.mp4")
      == movie.resolve())

head("[D] 對不到：安全退回 HTTP 來源，而且說得出為什麼")

FM = add_file("/媒體資料庫/0.Movie/moved-away.mp4")       # 有對應，但檔案不在了
FY = add_file("/Yu/Home/手機照片/x.mov", codec="hevc")    # 整個 mount 沒有對應
args_m = media.input_args(FM)
check("本機檔案不存在 → 退回 HTTP", "-i" in args_m and
      args_m[args_m.index("-i") + 1] == media.source_url(FM), args_m)
check("HTTP 來源帶內部憑證標頭（遠端 FTP 部署照舊可用）", "-headers" in args_m, args_m)
check("reason=not_found（對應到了、檔案不在 —— DB 過期或檔案被搬走）",
      localfs.resolve_local_info("/媒體資料庫/0.Movie/moved-away.mp4")["reason"] == "not_found")
check("reason=no_prefix（新 mount 沒補 LIBRARY_LOCAL_ROOTS）",
      localfs.resolve_local_info("/Yu/Home/手機照片/x.mov")["reason"] == "no_prefix")
check("含 .. → invalid", localfs.resolve_local_info("/媒體資料庫/../x")["reason"] == "invalid")
os.environ["LIBRARY_LOCAL_ROOTS"] = ""
check("沒設對應表 → disabled，照舊走 HTTP",
      localfs.resolve_local_info("/媒體資料庫/0.Movie/video.mp4")["reason"] == "disabled"
      and media.source_url(FL) in media.input_args(FL))
os.environ["LIBRARY_LOCAL_ROOTS"] = f"/媒體資料庫={LOCAL}"

si = media.source_info(FY)
check("source_info：source=ftp、local_match=false、有原因",
      si["source"] == "ftp" and si["local_match"] is False and si["reason"] == "no_prefix", si)
si2 = media.source_info(FL)
check("source_info：source=local、local_exists=true",
      si2["source"] == "local" and si2["local_exists"] and si2["local_match"], si2)
cov = media.local_coverage()
check("覆蓋率統計得出 hit／miss", cov["hit"] >= 1 and cov["miss"] >= 2
      and cov["miss_by_top_folder"].get("/Yu", 0) >= 1, cov)

admin = TestClient(app, client=("127.0.0.1", 1))
admin.cookies.set(auth.COOKIE, auth.make_token(auth.ADMIN))
viewer = TestClient(app, client=("127.0.0.1", 1))
viewer.cookies.set(auth.COOKIE, auth.make_token(auth.VIEWER))

r = viewer.get(f"/api/diagnostics/source/{FY}")
check("唯讀使用者看不到來源診斷（含本機路徑）", r.status_code == 403, r.status_code)
r = viewer.post("/api/diagnostics/keyframes/retry")
check("唯讀使用者不能觸發 keyframe 重試", r.status_code == 403, r.status_code)
r = admin.get(f"/api/diagnostics/source/{FY}")
check("管理員看得到來源診斷", r.status_code == 200 and r.json()["source"] == "ftp", r.text[:200])
r = admin.get("/api/diagnostics/source/999999")
check("不存在的 file_id → 404", r.status_code == 404, r.status_code)


# ============================================================ F. 502 可觀測性
head("[F] /api/stream 的 FTP 失敗：伺服器端記得清楚，客戶端只拿到通用訊息")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
logging.getLogger("filmax.stream").addHandler(cap)
real_stream = ftpclient.FtpReadStream


def failing_stream(path, offset=0):
    try:
        raise ftplib.error_temp("421 Too many connections (8) from this IP")
    except ftplib.Error as e:
        raise ftpclient.FtpError(f"開啟串流失敗 {path}@{offset}: {e}") from e


ftpclient.FtpReadStream = failing_stream
try:
    r = admin.get(f"/api/stream/{FY}?raw=1", headers={"Range": "bytes=500-"})
finally:
    ftpclient.FtpReadStream = real_stream
logging.getLogger("filmax.stream").removeHandler(cap)
check("HTTP 502", r.status_code == 502, r.status_code)
line = next((x for x in cap.lines if "raw source failed" in x), "")
check("server log 有 file id", f"file={FY}" in line, line)
check("server log 有 source=ftp 與起點", "source=ftp" in line and "start=500" in line, line)
check("server log 有例外類別", "error=error_temp" in line, line)
check("server log 有 FTP 回覆碼", "code=421" in line, line)
check("server log 有 Range", "bytes=500-" in line, line)
check("回應不含密碼", FTP_PW not in r.text and FTP_PW not in line, r.text)
check("回應不含 FTP 路徑", "手機照片" not in r.text, r.text)
check("回應帶得出回覆碼（讓 ffmpeg 的錯誤至少有一個線索）", "421" in r.text, r.text)

# 550（production 上真正發生的那一個）
cap2 = Capture()
logging.getLogger("filmax.stream").addHandler(cap2)


def missing_stream(path, offset=0):
    try:
        raise ftplib.error_perm("550 No such file or directory")
    except ftplib.Error as e:
        raise ftpclient.FtpError(f"開啟串流失敗 {path}@{offset}: {e}") from e


ftpclient.FtpReadStream = missing_stream
try:
    r = admin.get(f"/api/stream/{FY}?raw=1")
finally:
    ftpclient.FtpReadStream = real_stream
logging.getLogger("filmax.stream").removeHandler(cap2)
check("550 也記得出回覆碼", any("code=550" in x for x in cap2.lines), cap2.lines)
cls, code, _msg = ftpclient.describe_error(ConnectionResetError("reset"))
check("非 FTP 回覆的失敗（連線被重設）：類別有、回覆碼是 None",
      cls == "ConnectionResetError" and code is None, (cls, code))


# ============================================================ Range
head("[Range] FTP 路徑的 Range：206、Content-Range、Content-Length、REST 偏移")

SIZE = 1_000_000
FRNG = add_file("/Yu/手機照片/range.mov", size=SIZE, ext="mov")
opened = []


class FakeStream:
    def __init__(self, path, offset=0):
        self.offset = offset
        self.closed = False
        opened.append(self)

    def iter_chunks(self, chunk_size=None, limit=None):
        try:
            left = limit if limit is not None else SIZE - self.offset
            pos = self.offset
            while left > 0:
                n = min(65536, left)
                yield bytes((pos + i) % 251 for i in range(n))
                pos += n
                left -= n
        finally:
            self.closed = True


def expect(pos, n):
    return bytes((pos + i) % 251 for i in range(n))


ftpclient.FtpReadStream = FakeStream
try:
    cases = [
        ("bytes=0-65535", 0, 65535),
        ("bytes=-65536", SIZE - 65536, SIZE - 1),
        ("bytes=900000-", 900000, SIZE - 1),
        (f"bytes={SIZE - 1}-", SIZE - 1, SIZE - 1),         # 最後一個位元組（off-by-one）
        ("bytes=10-999999999", 10, SIZE - 1),                # end 超過檔尾要夾住
    ]
    for rng, s, e in cases:
        opened.clear()
        r = admin.get(f"/api/stream/{FRNG}?raw=1", headers={"Range": rng})
        n = e - s + 1
        good = (r.status_code == 206
                and r.headers.get("content-range") == f"bytes {s}-{e}/{SIZE}"
                and r.headers.get("content-length") == str(n)
                and len(r.content) == n and r.content == expect(s, n))
        check(f"{rng} → 206 bytes {s}-{e}/{SIZE}", good,
              (r.status_code, r.headers.get("content-range"),
               r.headers.get("content-length"), len(r.content)))
        check(f"{rng} → FTP REST 偏移 = {s}", opened and opened[0].offset == s,
              [o.offset for o in opened])
        check(f"{rng} → 搬完之後關掉 FTP 串流", opened and opened[0].closed)
    opened.clear()
    r = admin.get(f"/api/stream/{FRNG}?raw=1")
    check("沒有 Range → 200 整檔", r.status_code == 200 and len(r.content) == SIZE
          and r.headers.get("content-length") == str(SIZE), r.status_code)
    r = admin.get(f"/api/stream/{FRNG}?raw=1", headers={"Range": f"bytes={SIZE}-"})
    check("起點在檔尾之後 → 416", r.status_code == 416, r.status_code)
    r = admin.get(f"/api/stream/{FRNG}?raw=1", headers={"Range": "bytes=500-100"})
    check("start > end → 416（原本回負的 Content-Length）", r.status_code == 416,
          (r.status_code, r.headers.get("content-length")))
finally:
    ftpclient.FtpReadStream = real_stream


# ============================================================ E. HLS 不受 keyframe 失敗影響
head("[E] kf_state=failed 不等於播不動：下階照樣產生")

reset_queue_state()
ff = shutil.which(media.resolve_tool("ffmpeg")) or shutil.which("ffmpeg")
FK = None
if ff:
    src = LOCAL / "0.Movie" / "phone.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=320x180:rate=24", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000", "-t", "8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-y", str(src)], check=True)
    FK = add_file("/媒體資料庫/0.Movie/phone.mp4", kf="failed", size=src.stat().st_size,
                  duration=8.0)
else:
    FK = add_file("/媒體資料庫/0.Movie/phone.mp4", kf="failed", duration=8.0)

check("kf_state=failed → rungs_for 只有下階", hls.rungs_for(FK, 8.0) == [hls.RUNG_TRANSCODE],
      hls.rungs_for(FK, 8.0))
lower = hls.profile_key(180, None, 0, hls.RUNG_TRANSCODE)
m = hls.build_master(FK, lower, remote=False, duration=8.0)
check("master 發得出來", "#EXTM3U" in m and "#EXT-X-STREAM-INF" in m, m[:200])

# 失敗且冷卻中：開播不插隊（原本每次 master 都插隊 → 每次都重掃）
keyframes._note_failed(FK)
with keyframes._priority_lock:
    keyframes._priority.clear()
r = admin.get(f"/api/hls/{FK}/master.m3u8")
check("master 端點 200", r.status_code == 200, r.status_code)
with keyframes._priority_lock:
    queued = list(keyframes._priority)
check("failed 且冷卻中的檔案，開播不會再插隊", FK not in queued, queued)

if not ff:
    print("  (跳過實際產生分段：找不到 ffmpeg)")
else:
    r = admin.get(f"/api/hls/{FK}/seg-0.ts", params={"p": lower})
    check("GET seg-0.ts → 200（kf 失敗不影響下階）", r.status_code == 200, r.text[:300])
    check("分段是 mpegts（0x47 同步位元組）", r.content[:1] == b"\x47" and len(r.content) > 1000,
          len(r.content))
    cmd = media.build_transcode_cmd(FK, 0, 6, 180, None)
    check("下階轉碼的輸入是本機檔案，不是 loopback HTTP",
          str(src.resolve()) in cmd and not any("/api/stream/" in c for c in cmd),
          [c for c in cmd if "stream" in c or "phone" in c])


print(f"\n{'=' * 50}\n通過 {OK}，失敗 {FAIL}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
