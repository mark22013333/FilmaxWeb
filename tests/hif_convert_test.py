"""HIF 轉 JPG 的端對端測試（規格 S）：真的 FTP、真的 SFTP、真的掃描、真的搬檔。

重點不是「轉得出來」，是「任何一步出錯，原檔都還在」：

  S-3  原檔依拍攝日期搬進 YYYYMMDD原檔/，讀不到拍攝日期用修改時間
  S-4  永遠不刪除、不覆蓋：相機同時存的同名 JPG 照原樣留著
  S-5  搬原檔失敗 → JPG 已寫好、HIF 還在；重跑接得上，不重轉
  S-7  `原檔` 資料夾掃描時整個略過
  S-8  轉檔與掃描互斥
  S-1  按鈕出現前先偵測寫入權限

    python tests/hif_convert_test.py
"""
import io, os, shutil, socket, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="filmax-hif-")).resolve()
FTP_ROOT, SFTP_ROOT, LOCAL_ROOT = TMP / "ftp", TMP / "sftp", TMP / "local"
for d in (FTP_ROOT, SFTP_ROOT, LOCAL_ROOT):
    d.mkdir()
FTP_PORT = 2189

os.environ.update({
    "FILMAX_DATA_DIR": str(TMP / "data"),
    "FILMAX_ENV_FILE": str(TMP / "absent.env"),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": str(FTP_PORT),
    "FTP_USER": "u", "FTP_PASSWORD": "p", "FTP_TLS": "false", "FTP_TIMEOUT": "5",
    "LIBRARY_ROOTS": "/|auto", "TMDB_API_KEY": "", "MIN_FILE_MB": "0",
    "MIN_PHOTO_KB": "1", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "PHOTO_CONVERT_ENABLED": "true",
})
sys.path.insert(0, str(ROOT))

import paramiko  # noqa: E402
from PIL import Image  # noqa: E402
from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

from app import db, hifconvert, scanner  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

http = TestClient(app, client=("127.0.0.1", 1))

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


# ---------------------------------------------------------------- 素材
def heif_bytes(seed, taken=None):
    import random
    random.seed(seed)
    im = Image.new("RGB", (320, 200))
    px = im.load()
    for y in range(200):
        for x in range(320):
            px[x, y] = (random.randrange(256), (x * 3 + seed) % 256, (y * 5) % 256)
    exif = Image.Exif()
    if taken:
        exif[0x0132] = taken
    buf = io.BytesIO()
    im.save(buf, format="HEIF", quality=90, exif=exif)
    return buf.getvalue()


def jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def make_pics(root: Path, folder="pics"):
    """5 張 HIF：兩張同一天、一張另一天、一張相機也存了 JPG、一張沒有拍攝日期。"""
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    originals = {
        "A.HIF": heif_bytes(1, "2021:10:02 21:35:46"),
        "B.HIF": heif_bytes(2, "2022:01:05 08:00:00"),
        "C.HIF": heif_bytes(3, "2021:10:02 23:59:59"),
        "D.HIF": heif_bytes(4, "2021:10:03 10:00:00"),
        "E.HIF": heif_bytes(5),
    }
    for n, b in originals.items():
        (d / n).write_bytes(b)
    camera = jpeg_bytes()
    (d / "D.JPG").write_bytes(camera)
    noon = time.mktime((2020, 5, 6, 12, 0, 0, 0, 0, -1))
    os.utime(d / "E.HIF", (noon, noon))
    (d / ".E.jpg.part").write_bytes(b"half written")     # 上次寫到一半的暫存檔
    return originals, camera


# ---------------------------------------------------------------- SFTP 伺服器（可寫）
READONLY = {"on": False}


class Auth(paramiko.ServerInterface):
    def check_auth_password(self, u, p):
        return paramiko.AUTH_SUCCESSFUL if (u, p) == ("u", "p") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, u):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def local(self, path):
        p = (SFTP_ROOT / path.lstrip("/")).resolve()
        if not p.is_relative_to(SFTP_ROOT):
            raise PermissionError(path)
        return p

    def _guard(self):
        return paramiko.SFTP_PERMISSION_DENIED if READONLY["on"] else None

    def list_folder(self, path):
        try:
            out = []
            for c in self.local(path).iterdir():
                a = paramiko.SFTPAttributes.from_stat(c.stat())
                a.filename = c.name
                out.append(a)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if writing and self._guard() is not None:
            return self._guard()
        try:
            h = paramiko.SFTPHandle(flags)
            local = self.local(path)
            if writing:
                h.writefile = open(local, "wb")
            else:
                h.readfile = open(local, "rb")
            return h
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def _op(self, fn, *a):
        if self._guard() is not None:
            return self._guard()
        try:
            fn(*a)
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def remove(self, path):
        return self._op(os.remove, self.local(path))

    def rename(self, old, new):
        return self._op(os.rename, self.local(old), self.local(new))

    def mkdir(self, path, attr):
        return self._op(os.mkdir, self.local(path))

    def rmdir(self, path):
        return self._op(os.rmdir, self.local(path))

    def chattr(self, path, attr):
        if attr.st_mtime is not None:
            return self._op(os.utime, self.local(path), (attr.st_atime or attr.st_mtime, attr.st_mtime))
        return paramiko.SFTP_OK


key = paramiko.RSAKey.generate(2048)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
listener.settimeout(0.2)
SFTP_PORT = listener.getsockname()[1]
known = TMP / "known_hosts"
known.write_text(f"[127.0.0.1]:{SFTP_PORT} {key.get_name()} {key.get_base64()}\n")
stop = threading.Event()
transports = []


def serve():
    while not stop.is_set():
        try:
            sock, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = paramiko.Transport(sock)
        transports.append(t)
        t.add_server_key(key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        try:
            t.start_server(server=Auth())
        except (paramiko.SSHException, EOFError):
            t.close()


threading.Thread(target=serve, daemon=True).start()

# ---------------------------------------------------------------- FTP 伺服器（可寫 + 唯讀帳號）
_auth = DummyAuthorizer()
_auth.add_user("u", "p", str(FTP_ROOT), perm="elradfmwMT")
_auth.add_user("ro", "p", str(FTP_ROOT), perm="elr")
FTPHandler.authorizer = _auth
_srv = FTPServer(("127.0.0.1", FTP_PORT), FTPHandler)
_srv.max_cons = 50
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.5)

db.init_db()


def wait(pred, secs=60):
    for _ in range(int(secs * 10)):
        if pred():
            return True
        time.sleep(0.1)
    return False


def scan():
    assert scanner.start()
    wait(lambda: not scanner.status.running)


def tree(root: Path):
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())


# ================================================================ 一、轉檔邏輯（本機檔案系統，可注入失敗）
class LocalFS(hifconvert.SourceFS):
    def __init__(self, root):
        self.root = root
        self.fail = {}            # 方法名 -> 條件(path) 為真就丟例外

    def p(self, path):
        return self.root / path.lstrip("/")

    def _maybe(self, op, *paths):
        cond = self.fail.get(op)
        if cond and any(cond(x) for x in paths):
            raise OSError(f"注入的失敗：{op}")

    def listdir(self, path): return [x.name for x in self.p(path).iterdir()]
    def exists(self, path): return self.p(path).exists()

    def read(self, path, limit):
        b = self.p(path).read_bytes()
        if len(b) > limit:
            raise ValueError("太大")
        return b

    def write(self, path, data):
        self._maybe("write", path)
        self.p(path).write_bytes(data)

    def rename(self, src, dst):
        self._maybe("rename", src, dst)
        if self.p(dst).exists():
            raise FileExistsError(dst)
        self.p(src).rename(self.p(dst))

    def mkdir(self, path): self.p(path).mkdir(exist_ok=True)
    def remove(self, path):
        assert hifconvert._own_tmp(path), path
        self.p(path).unlink()
    def rmdir(self, path): self.p(path).rmdir()
    def set_mtime(self, path, epoch): os.utime(self.p(path), (epoch, epoch))


print("[1] 轉檔邏輯：成功路徑")
orig, camera = make_pics(LOCAL_ROOT)
fs = LocalFS(LOCAL_ROOT)
sib = {n.lower(): n for n in fs.listdir("/pics")}
row = lambda n, ts=None: {"id": 1, "ftp_path": f"/pics/{n}", "mtime_ts": ts}
item, jpg = hifconvert.convert_one(fs, row("A.HIF"), sib, 100)
check("轉好了：archived", item.state == "archived", (item.state, item.reason))
check("JPG 在原資料夾", (LOCAL_ROOT / "pics/A.jpg").is_file())
check("原檔依拍攝日期搬進 20211002原檔/",
      (LOCAL_ROOT / "pics/20211002原檔/A.HIF").read_bytes() == orig["A.HIF"])
check("原位的 HIF 不見了（是搬不是複製）", not (LOCAL_ROOT / "pics/A.HIF").exists())
with Image.open(LOCAL_ROOT / "pics/A.jpg") as im:
    check("JPG 寬高與 HIF 相同", im.size == (320, 200), im.size)
    check("JPG 內嵌我們的標記", (im.info.get("comment") or b"").decode().startswith("filmax-hif:A.HIF:"))
    check("JPG 保留拍攝時間", im.getexif().get(0x0132) == "2021:10:02 21:35:46", im.getexif().get(0x0132))
check("回傳的位元組就是寫出去的檔案", jpg == (LOCAL_ROOT / "pics/A.jpg").read_bytes())

print("\n[2] 相機同時存的同名 JPG：跳過，什麼都不動")
before = tree(LOCAL_ROOT)
item, jpg = hifconvert.convert_one(fs, row("D.HIF"), sib, 100)
check("skipped 並說明原因", item.state == "skipped" and "同名 JPG" in item.reason, (item.state, item.reason))
check("D.JPG 一個位元組都沒變", (LOCAL_ROOT / "pics/D.JPG").read_bytes() == camera)
check("D.HIF 還在原位", (LOCAL_ROOT / "pics/D.HIF").read_bytes() == orig["D.HIF"])
check("整個目錄沒有任何變動", tree(LOCAL_ROOT) == before)

print("\n[3] 沒有拍攝日期：用檔案修改時間；暫存檔被清掉")
item, _ = hifconvert.convert_one(fs, row("E.HIF", int(time.mktime((2020, 5, 6, 12, 0, 0, 0, 0, -1)))), sib, 100)
check("E 進 20200506原檔/", item.state == "archived" and (LOCAL_ROOT / "pics/20200506原檔/E.HIF").exists(),
      (item.state, item.reason, tree(LOCAL_ROOT)))
check("舊的 .part 暫存檔被我們自己清掉", not (LOCAL_ROOT / "pics/.E.jpg.part").exists())

print("\n[4] 搬原檔失敗：JPG 已寫好、原檔還在；重跑接得上而且不重轉")
fs.fail["rename"] = lambda p: p.endswith("B.HIF")
try:
    hifconvert.convert_one(fs, row("B.HIF"), sib, 100)
    check("搬原檔失敗會丟例外", False)
except OSError:
    check("搬原檔失敗會丟例外", True)
check("B.HIF 還在原位", (LOCAL_ROOT / "pics/B.HIF").read_bytes() == orig["B.HIF"])
check("B.jpg 已寫好", (LOCAL_ROOT / "pics/B.jpg").is_file())
jpg_first = (LOCAL_ROOT / "pics/B.jpg").read_bytes()
fs.fail.clear()
sib = {n.lower(): n for n in fs.listdir("/pics")}
item, jpg = hifconvert.convert_one(fs, row("B.HIF"), sib, 100)
check("重跑後 archived", item.state == "archived", (item.state, item.reason))
check("沒有重新編碼（JPG 位元組不變）", (LOCAL_ROOT / "pics/B.jpg").read_bytes() == jpg_first)
check("原檔進了 20220105原檔/", (LOCAL_ROOT / "pics/20220105原檔/B.HIF").read_bytes() == orig["B.HIF"])

print("\n[5] 寫入失敗與驗證失敗：沒有半成品，原檔不動")
for label, setup in (("寫入失敗", lambda: fs.fail.update(write=lambda p: True)),
                     ("驗證失敗", lambda: setattr(hifconvert, "verify_jpeg",
                                              lambda j, s: (_ for _ in ()).throw(ValueError("驗證失敗"))))):
    real_verify = hifconvert.verify_jpeg
    setup()
    sib = {n.lower(): n for n in fs.listdir("/pics")}
    try:
        hifconvert.convert_one(fs, row("C.HIF"), sib, 100)
        check(f"{label}：丟例外", False)
    except Exception:
        check(f"{label}：丟例外", True)
    check(f"{label}：C.HIF 還在、沒有 C.jpg、沒有殘留暫存檔",
          (LOCAL_ROOT / "pics/C.HIF").exists() and not (LOCAL_ROOT / "pics/C.jpg").exists()
          and not (LOCAL_ROOT / "pics/.C.jpg.part").exists(), tree(LOCAL_ROOT))
    fs.fail.clear()
    hifconvert.verify_jpeg = real_verify

print("\n[6] 同一天同名：搬進去時不覆蓋")
(LOCAL_ROOT / "pics/20211002原檔").mkdir(exist_ok=True)
sib = {n.lower(): n for n in fs.listdir("/pics")}
(LOCAL_ROOT / "pics/20211002原檔/C.HIF").write_bytes(b"someone else")
item, _ = hifconvert.convert_one(fs, row("C.HIF"), sib, 100)
check("既有的檔案沒被動", (LOCAL_ROOT / "pics/20211002原檔/C.HIF").read_bytes() == b"someone else")
check("我們的原檔改名 C_2.HIF", (LOCAL_ROOT / "pics/20211002原檔/C_2.HIF").read_bytes() == orig["C.HIF"],
      tree(LOCAL_ROOT))

# ================================================================ 二、兩種來源的完整流程
def backend_run(label, root: Path, protocol: str):
    print(f"\n[{label}] 掃描 → 偵測寫入 → 轉整個資料夾 → 再掃描")
    settings.ftp_protocol = protocol
    settings.ftp_user, settings.ftp_password = "u", "p"
    if protocol == "sftp":
        settings.ftp_port = SFTP_PORT
        settings.sftp_known_hosts = str(known)
    else:
        settings.ftp_port = FTP_PORT
    folder = f"/pics_{label}"
    originals, camera = make_pics(root, f"pics_{label}")
    scan()
    rows = {r["filename"]: r for r in db.q("SELECT * FROM photo WHERE folder=?", (folder,))}
    check(f"{label}：掃描收進 5 張 HIF 與相機的 JPG", {"A.HIF", "B.HIF", "C.HIF", "D.HIF", "E.HIF", "D.JPG"} <= set(rows),
          sorted(rows))
    check(f"{label}：HIF 讀得出尺寸與縮圖", all(rows[n]["probe_state"] == "ok" and rows[n]["thumb"]
                                              and rows[n]["format"] == "HEIF" for n in ("A.HIF", "B.HIF")),
          {n: (rows[n]["probe_state"], rows[n]["format"]) for n in rows})

    pid = rows["A.HIF"]["id"]
    r = http.get(f"/api/photo/{pid}/preview.jpg")
    check(f"{label}：HIF 的 preview 是瀏覽器看得懂的 JPEG",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
          and Image.open(io.BytesIO(r.content)).format == "JPEG", (r.status_code, r.headers.get("content-type")))
    r = http.get(f"/api/photo/{pid}/full")
    check(f"{label}：HIF 的 /full 也是 JPEG，全解析度",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
          and Image.open(io.BytesIO(r.content)).size == (320, 200), (r.status_code, r.headers.get("content-type")))
    r = http.get(f"/api/photo/{pid}/full?raw=1")
    check(f"{label}：raw=1 給原始 HIF，位元組相同",
          r.status_code == 200 and r.headers["content-type"] == "image/heif" and r.content == originals["A.HIF"],
          (r.status_code, r.headers.get("content-type")))
    d = http.get(f"/api/photos/{pid}").json()
    check(f"{label}：下載連結指向原始檔", d["download_url"].endswith("/full?raw=1"), d["download_url"])

    ok, why = hifconvert.probe(folder)
    check(f"{label}：寫入偵測通過、沒留下偵測用的檔案", ok and not any(".filmax-probe" in t for t in tree(root)), (ok, why))

    hrows = hifconvert.hif_rows(folder)
    check(f"{label}：資料夾裡的 HIF 都被列入", len(hrows) == 5, [r["filename"] for r in hrows])

    # S-8：掃描中不能轉檔、轉檔中不能掃描
    scanner.status.running = True
    try:
        hifconvert.start(hrows, folder)
        check(f"{label}：掃描中拒絕轉檔", False)
    except hifconvert.Busy:
        check(f"{label}：掃描中拒絕轉檔", True)
    finally:
        scanner.status.running = False

    fake = hifconvert.Job(id=0, folder=folder, total=1)
    real, hifconvert._job = hifconvert._job, fake
    check(f"{label}：轉檔中拒絕掃描", scanner.start() is False and not scanner.status.running)
    hifconvert._job = real
    job = hifconvert.start(hrows, folder)
    wait(lambda: job.state != "running")
    c = job.counts()
    check(f"{label}：結果 4 張完成、1 張跳過", c == {"archived": 4, "skipped": 1, "failed": 0}, (c, job.as_dict()["items"]))

    t = tree(root)
    pre = f"pics_{label}/"
    for n in ("A", "B", "C", "E"):
        check(f"{label}：{n}.jpg 在原資料夾", pre + f"{n}.jpg" in t, t)
    check(f"{label}：原檔依拍攝日期分到 3 個資料夾",
          all(pre + p in t for p in ("20211002原檔/A.HIF", "20211002原檔/C.HIF",
                                     "20220105原檔/B.HIF", "20200506原檔/E.HIF")), t)
    check(f"{label}：原檔位元組完全相同",
          (root / f"pics_{label}/20220105原檔/B.HIF").read_bytes() == originals["B.HIF"])
    check(f"{label}：相機的 D.JPG／D.HIF 原封不動",
          (root / f"pics_{label}/D.JPG").read_bytes() == camera and (root / f"pics_{label}/D.HIF").exists())

    rows = {r["filename"]: r for r in db.q("SELECT * FROM photo WHERE folder=?", (folder,))}
    check(f"{label}：相片庫裡 JPG 取代了 HIF，不必等下一次掃描",
          all(f"{n}.jpg" in rows and f"{n}.HIF" not in rows for n in "ABCE") and "D.HIF" in rows, sorted(rows))
    check(f"{label}：新 JPG 已讀好尺寸與縮圖",
          rows["A.jpg"]["probe_state"] == "ok" and rows["A.jpg"]["width"] == 320 and bool(rows["A.jpg"]["thumb"]))
    check(f"{label}：排序用的拍攝時間保留", rows["A.jpg"]["taken_at"].startswith("2021-10-02"), rows["A.jpg"]["taken_at"])

    scan()
    after = [r["ftp_path"] for r in db.q("SELECT ftp_path FROM photo WHERE folder LIKE ?", (folder + "%",))]
    check(f"{label}：再掃描，`原檔` 底下的 HIF 不會被收回來", not any("原檔" in p for p in after), after)

    before = tree(root)
    job2 = hifconvert.start(hifconvert.hif_rows(folder), folder)
    wait(lambda: job2.state != "running")
    check(f"{label}：重跑只剩 D 一張，仍然跳過、檔案系統沒有變動",
          job2.counts() == {"archived": 0, "skipped": 1, "failed": 0} and tree(root) == before,
          (job2.counts(), job2.as_dict()["items"]))


backend_run("ftp", FTP_ROOT, "ftp")
backend_run("sftp", SFTP_ROOT, "sftp")

# ================================================================ 三、唯讀帳號
print("\n[ro] 來源帳號沒有寫入權限")
settings.ftp_protocol, settings.ftp_port = "ftp", FTP_PORT
settings.ftp_user = "ro"
ok, why = hifconvert.probe("/pics_ftp")
check("FTP 唯讀帳號：偵測失敗並說出是權限", not ok and "寫入權限" in why, (ok, why))
settings.ftp_user = "u"
settings.ftp_protocol, settings.ftp_port = "sftp", SFTP_PORT
READONLY["on"] = True
ok, why = hifconvert.probe("/pics_sftp")
check("SFTP 唯讀：偵測失敗並說出是權限", not ok and "寫入權限" in why, (ok, why))
READONLY["on"] = False

stop.set()
print(f"\n{OK} passed, {FAIL} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
