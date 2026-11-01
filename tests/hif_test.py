"""HIF 相片：掃描收不收、縮圖與 EXIF 讀得出來、`原檔` 資料夾不被掃。

    python tests/hif_test.py
"""
import io, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_d = tempfile.mkdtemp()
os.environ["FILMAX_DATA_DIR"] = _d
os.environ["FILMAX_ENV_FILE"] = str(Path(_d) / ".env")

from PIL import Image  # noqa: E402
from app import ftpclient, photo, scanner  # noqa: E402

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


def make_heif(size=(640, 360)):
    im = Image.new("RGB", size, (200, 80, 40))
    exif = Image.Exif()
    exif[0x0132] = "2021:10:02 21:35:46"
    buf = io.BytesIO()
    im.save(buf, format="HEIF", quality=90, exif=exif)
    return buf.getvalue()


check("pillow-heif 載入成功", photo.HEIF_OK)
data = make_heif()

info = photo.read_info(data)
check("尺寸讀得出來", (info["width"], info["height"]) == (640, 360), info)
check("格式是 HEIF", info["format"] == "HEIF", info["format"])
check("EXIF 拍攝時間讀得出來", bool(info["taken_at"]) and info["taken_at"].startswith("2021"), info["taken_at"])

name = photo.make_thumb(data, box=200)
out = Path(_d) / "images" / name if name else None
check("縮圖產得出來", bool(name) and out.exists() and out.stat().st_size > 0, name)
with Image.open(out) as t:
    check("縮圖是 JPEG 且長邊 200", t.format == "JPEG" and max(t.size) == 200, (t.format, t.size))

full = photo.make_thumb(data, box=photo.FULL_BOX, quality=90)
with Image.open(Path(_d) / "images" / full) as t:
    check("full 版不縮小", t.size == (640, 360), t.size)


def f(ext, size=500_000):
    return SimpleNamespace(ext=ext, size=size)


check("掃描收 .HIF", scanner._wanted_photo(f("HIF")))
check("掃描收 .heic", scanner._wanted_photo(f("heic")))
check("jpg 照舊收", scanner._wanted_photo(f("jpg")))
photo.HEIF_OK = False
check("沒裝 pillow-heif 就不收 HIF", not scanner._wanted_photo(f("HIF")))
photo.HEIF_OK = True

check("`20211002原檔` 資料夾不掃", ftpclient._skip_dir("20211002原檔"))
check("名稱含原檔但不是結尾的照掃", not ftpclient._skip_dir("原檔案備份"))
check("一般資料夾照掃", not ftpclient._skip_dir("2015-09 旅遊"))

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
