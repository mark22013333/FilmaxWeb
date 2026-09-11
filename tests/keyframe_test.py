"""分段邊界表：本機直讀的路徑防護、邊界推導、狀態機與背景佇列。

    python tests/keyframe_test.py

規格：J 章第 −1 層（LIBRARY_LOCAL_ROOTS）與第 0 層（兩階 HLS 的上階邊界）。
"""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# resolve()：Windows 的 TMP 常常是 8.3 短檔名（C:\Users\ADMINI~1\...），
# 而 localfs 回傳的是 resolve() 之後的長路徑 —— 不先展開的話，
# 「命中」的比對會拿短路徑去比長路徑而失敗（這是測試的問題，不是 localfs 的）。
TMP = str(Path(tempfile.mkdtemp(prefix="filmax-kf-")).resolve())
FTPROOT = Path(TMP, "ftproot")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "LIBRARY_LOCAL_ROOTS": f"/={FTPROOT}",
    "HLS_SEGMENT_SECONDS": "6",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import db, keyframes, localfs, purge
from app.config import settings
db.init_db()


# ============================================================ 第 −1 層：路徑防護
head("[J −1] 本機直讀的路徑防護")

(FTPROOT / "媒體資料庫" / "0.Movie").mkdir(parents=True)
good = FTPROOT / "媒體資料庫" / "0.Movie" / "片.mkv"
good.write_bytes(b"x" * 10)
outside = Path(TMP, "secret.txt"); outside.write_text("nope")
try:
    (FTPROOT / "link").symlink_to(outside)
    have_symlink = True
except (OSError, NotImplementedError):
    have_symlink = False

check("對應表讀得到", settings.library_local_roots == [("/", str(FTPROOT))],
      settings.library_local_roots)
check("正常路徑命中", localfs.resolve_local("/媒體資料庫/0.Movie/片.mkv") == good)
check("Windows 風格的分隔線也認",
      localfs.resolve_local("\\媒體資料庫\\0.Movie\\片.mkv") == good)
check("重複斜線與 . 正規化掉",
      localfs.resolve_local("//媒體資料庫/./0.Movie//片.mkv") == good)
check("檔案不存在回 None", localfs.resolve_local("/媒體資料庫/0.Movie/沒有.mkv") is None)
check("指到目錄回 None（不是檔案）", localfs.resolve_local("/媒體資料庫") is None)
check("含 .. 一律拒絕", localfs.resolve_local("/媒體資料庫/../secret.txt") is None)
check("含 .. 在中間也拒絕",
      localfs.resolve_local("/媒體資料庫/0.Movie/../../../secret.txt") is None)
check("NUL 截斷拒絕", localfs.resolve_local("/媒體資料庫/0.Movie/片.mkv\x00.txt") is None)
if have_symlink:
    check("連結逃出 root 擋掉", localfs.resolve_local("/link") is None)
else:
    print("  (跳過 symlink：這個系統建不了)")

# 前綴比對：長的要先命中
os.environ["LIBRARY_LOCAL_ROOTS"] = f"/={TMP}/other;/媒體資料庫={FTPROOT}/媒體資料庫"
check("多組對應表按前綴長度排序",
      [p for p, _ in settings.library_local_roots] == ["/媒體資料庫", "/"],
      settings.library_local_roots)
check("命中較長的那一條", localfs.resolve_local("/媒體資料庫/0.Movie/片.mkv") == good)
os.environ["LIBRARY_LOCAL_ROOTS"] = f"/={FTPROOT}"

os.environ["LIBRARY_LOCAL_ROOTS"] = ""
check("沒設對應表就整個關掉（照舊走 FTP）", localfs.resolve_local("/媒體資料庫/0.Movie/片.mkv") is None)
check("enabled() 回 False", localfs.enabled() is False)
os.environ["LIBRARY_LOCAL_ROOTS"] = f"/={FTPROOT}"


# ============================================================ 邊界推導
head("[J 0] 邊界推導")

T = [0.0, 3.2, 6.4, 9.6, 12.8]
P = [0, 1000, 2000, 3000, 4000]
b = keyframes.derive_bounds(T, P, 6, duration=16.0, total_size=5000)
check("段長是 keyframe 間距的整數倍（湊滿 >= 6 秒）",
      [round(x[1] - x[0], 2) for x in b[:2]] == [6.4, 6.4], b)
check("尾巴補進去，覆蓋到片尾", b[-1][1] == 16.0, b)
check("總長等於片長", abs(sum(x[1] - x[0] for x in b) - 16.0) < 1e-6, b)

# 太短的尾巴要併進前一段，不要留 0.x 秒的分段
b2 = keyframes.derive_bounds(T, P, 6, duration=13.0, total_size=5000)
check("太短的尾巴併進前一段", len(b2) == 2 and b2[-1][1] == 13.0, b2)
check("併進去之後段數不變", all(x[1] - x[0] > 1.0 for x in b2), b2)

# 第一個 keyframe 不在 0 的片源：**上階的時間軸原點必須仍然是 0**。
# 不補的話上階的 EXTINF 總和會比下階短那 0.5 秒，兩階就變成兩條長度不同的
# 時間軸 —— 切畫質時 currentTime 會對到另一條軸上，播放位置與 duration 都跳。
T_LATE = [0.5, 3.7, 6.9, 10.1, 13.3]
b_late = keyframes.derive_bounds(T_LATE, P, 6, duration=16.0, total_size=5000)
check("第一個 keyframe 在 0.5 時，第一段仍然從 0 起算",
      b_late[0][0] == 0.0, b_late)
check("而且總長仍然等於片長（跟下階的時間軸對得起來）",
      abs(sum(x[1] - x[0] for x in b_late) - 16.0) < 1e-6, b_late)
check("兩階的起點與終點一致",
      (b_late[0][0], b_late[-1][1]) == (0.0, 16.0), b_late)

check("keyframe 不足回空清單", keyframes.derive_bounds([1.0], [0], 6) == [])
check("沒給 duration 就不補尾巴",
      keyframes.derive_bounds(T, P, 6)[-1][1] == 12.8)

# 峰值 vs 平均
uneven = [(0.0, 6.0, 1_000_000), (6.0, 12.0, 15_000_000)]
peak, avg = keyframes.bandwidth_for(uneven)
check("BANDWIDTH 用的是單段峰值不是平均", peak == 20_000_000, (peak, avg))
check("AVERAGE-BANDWIDTH 是整段平均", avg == 10_666_666, (peak, avg))
peak2, avg2 = keyframes.bandwidth_for(uneven, audio_kbps=192)
check("兩個都要加上音訊碼率", peak2 - peak == 192_000 and avg2 - avg == 192_000)


# ============================================================ 狀態機
head("[J 0] 狀態機與失效")

def add_file(path, codec="h264", state="ok"):
    fid = db.execute(
        """INSERT INTO media_file(ftp_path, filename, ext, size, duration,
                                  video_codec, probe_state, seen_at, added_at)
           VALUES(?,?,?,?,?,?,?,0,0)""",
        (path, Path(path).name, "mkv", 100, 60.0, codec, state)).lastrowid
    return fid

fid_h264 = add_file("/媒體資料庫/0.Movie/片.mkv")
fid_hevc = add_file("/媒體資料庫/0.Movie/4k.mkv", codec="hevc")

check("新檔案的 kf_state 預設是 pending",
      db.q1("SELECT kf_state FROM media_file WHERE id=?", (fid_h264,))["kf_state"] == "pending")

check("hevc 掃描時被標成 skipped", keyframes.scan_one(
    dict(db.q1("SELECT id, ftp_path, video_codec FROM media_file WHERE id=?",
               (fid_hevc,)))) == "skipped")
row = db.q1("SELECT kf_state, kf_error FROM media_file WHERE id=?", (fid_hevc,))
check("skipped 有寫原因（留空白會被當成還沒跑）", bool(row["kf_error"]), dict(row))

# 手動塞一份表，驗 table_for 的狀態把關
db.execute("""INSERT INTO media_keyframe(file_id, times, positions, count, updated_at)
              VALUES(?,?,?,?,0)""", (fid_h264, json.dumps(T), json.dumps(P), len(T)))
db.execute("UPDATE media_file SET kf_state='ok' WHERE id=?", (fid_h264,))
check("kf_state=ok 時拿得到表", (keyframes.table_for(fid_h264) or {}).get("count") == 5)

db.execute("UPDATE media_file SET kf_state='pending' WHERE id=?", (fid_h264,))
check("kf_state 不是 ok 就拿不到（不能照舊資料切分段）",
      keyframes.table_for(fid_h264) is None)
db.execute("UPDATE media_file SET kf_state='ok' WHERE id=?", (fid_h264,))

# 檔案變動 → 跟 probe_state 吃同一個訊號
# mtime_ts 要給真實的秒數：media_file.mtime_ts 有時間範圍的 trigger（見 db._time_guards），
# 隨手寫 1 會被 ABORT。
NOW = int(time.time())
with db.batch() as bt:
    bt.upsert_file(item_id=None, episode_id=None, path="/媒體資料庫/0.Movie/片.mkv",
                   name="片.mkv", size=999, mtime="x", mtime_ts=NOW, ext="mkv", at=NOW)
row = db.q1("SELECT probe_state, kf_state FROM media_file WHERE id=?", (fid_h264,))
check("檔案變動後 probe_state 回到 pending", row["probe_state"] == "pending", dict(row))
check("檔案變動後 kf_state 也回到 pending", row["kf_state"] == "pending", dict(row))

# purge 要一起刪，不然留孤兒
check("刪檔案前 media_keyframe 有那一列",
      db.q1("SELECT 1 FROM media_keyframe WHERE file_id=?", (fid_h264,)) is not None)
purge.purge(file_ids=[fid_h264], reason="test")
check("purge 之後 media_keyframe 沒有孤兒",
      db.q1("SELECT 1 FROM media_keyframe WHERE file_id=?", (fid_h264,)) is None)


# ============================================================ 佇列
head("[J 0] 背景佇列")

for i in range(3):
    add_file(f"/媒體資料庫/0.Movie/q{i}.mkv", codec="hevc")
n_before = keyframes.pending_count()
check("待辦數算得出來", n_before >= 3, n_before)

stats = keyframes.run_queue(limit=2)
check("limit 有效（一次只做兩個）", sum(stats.values()) == 2, stats)
check("待辦數跟著減少", keyframes.pending_count() == n_before - 2)

# 插隊
last = db.q1("""SELECT id FROM media_file WHERE kf_state='pending'
                ORDER BY id DESC LIMIT 1""")["id"]
keyframes.request_soon(last)
nxt = keyframes._next_row()
check("request_soon 讓那個檔案排到最前面", nxt and nxt["id"] == last, nxt)

class _Cancelled:
    def is_set(self): return True
check("cancel 立刻停下來", keyframes.run_queue(cancel=_Cancelled()) == {"ok": 0, "skipped": 0, "failed": 0})

# probe_state 還沒 ok 的不排（沒有 codec 資訊，排了只會失敗）
add_file("/媒體資料庫/0.Movie/notprobed.mkv", state="pending")
ids = [r["id"] for r in db.q("SELECT id FROM media_file WHERE kf_state='pending'")]
picked = []
while True:
    r = keyframes._next_row()
    if not r: break
    picked.append(r["id"])
    db.execute("UPDATE media_file SET kf_state='skipped' WHERE id=?", (r["id"],))
check("probe_state 還沒 ok 的不會被排進佇列",
      all(db.q1("SELECT probe_state FROM media_file WHERE id=?", (i,))["probe_state"] == "ok"
          for i in picked), picked)


# ============================================================ 真的跑一次 ffprobe
head("[J 0] 真的掃一個檔案（需要 ffmpeg／ffprobe）")

from app import media
ff = shutil.which(media.resolve_tool("ffmpeg")) or shutil.which("ffmpeg")
fp = shutil.which(media.resolve_tool("ffprobe")) or shutil.which("ffprobe")
if not (ff and fp):
    print("  (跳過：這台機器上找不到 ffmpeg／ffprobe)")
else:
    real = FTPROOT / "媒體資料庫" / "0.Movie" / "real.mkv"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=320x180:rate=24", "-t", "20",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "48",
                    "-pix_fmt", "yuv420p", "-y", str(real)], check=True)
    fid = add_file("/媒體資料庫/0.Movie/real.mkv")
    src, is_local = keyframes.source_for(
        dict(db.q1("SELECT id, ftp_path FROM media_file WHERE id=?", (fid,))))
    check("有對應表時走本機直讀，不繞 HTTP", is_local and src == str(real), (src, is_local))
    state = keyframes.scan_one(
        dict(db.q1("SELECT id, ftp_path, video_codec FROM media_file WHERE id=?", (fid,))))
    check("掃描成功", state == "ok", state)
    tbl = keyframes.table_for(fid)
    check("表裡有 keyframe", tbl and tbl["count"] >= 3, tbl and tbl["count"])
    check("時間是遞增的", tbl and all(
        tbl["times"][i] < tbl["times"][i + 1] for i in range(len(tbl["times"]) - 1)))
    check("偏移量也拿到了（不是全 0）", tbl and any(p > 0 for p in tbl["positions"]))
    check("times 與 positions 同長",
          tbl and len(tbl["times"]) == len(tbl["positions"]) == tbl["count"])
    bounds = keyframes.derive_bounds(tbl["times"], tbl["positions"], 6,
                                     duration=20.0, total_size=real.stat().st_size)
    check("推得出邊界", len(bounds) >= 2, bounds)
    peak, avg = keyframes.bandwidth_for(bounds, audio_kbps=192)
    check("峰值 >= 平均", peak >= avg, (peak, avg))

    # 沒有對應表時要退回走 HTTP
    os.environ["LIBRARY_LOCAL_ROOTS"] = ""
    src2, is_local2 = keyframes.source_for(
        dict(db.q1("SELECT id, ftp_path FROM media_file WHERE id=?", (fid,))))
    check("沒有對應表就退回 HTTP 來源", (not is_local2) and "/api/stream/" in src2, src2)
    os.environ["LIBRARY_LOCAL_ROOTS"] = f"/={FTPROOT}"


print(f"\n{'=' * 50}\n通過 {OK}，失敗 {FAIL}")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
