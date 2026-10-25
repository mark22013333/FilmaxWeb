"""OpenAver 的載入：預設位置、找不到時的訊息、裝好之後不必重啟就能重試。

    python tests/javscraper_load_test.py

不連網、不需要真的 OpenAver：用一份只有兩支函式的假 `core` 套件代替。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="filmax-jav-")).resolve()
FAKE = TMP / "OpenAver"
os.environ.update({
    "FILMAX_DATA_DIR": str(TMP / "data"),
    "FILMAX_ENV_FILE": str(TMP / "no-such.env"),
    "OPENAVER_PATH": str(FAKE),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false",
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


from app import config, javscraper  # noqa: E402


head("[J] 預設位置")
saved = os.environ.pop("OPENAVER_PATH")
check("OPENAVER_PATH 留空 = 安裝.bat 下載的 vendor/OpenAver",
      Path(config.Settings().openaver_path) == ROOT / "vendor" / "OpenAver",
      config.Settings().openaver_path)
os.environ["OPENAVER_PATH"] = saved


head("[J] 找不到 OpenAver")
st = javscraper.status()
check("停用", st["enabled"] is False, st)
check("訊息告訴使用者怎麼修（跑安裝.bat）", "安裝.bat" in st["error"], st["error"])
check("訊息帶著實際找的路徑", str(FAKE) in st["error"], st["error"])

(FAKE / "core").mkdir(parents=True)
st = javscraper.status()
check("資料夾在但不是 OpenAver（沒有 core/scraper.py）一樣停用",
      st["enabled"] is False and "core/scraper.py" in st["error"], st)


head("[J] 補上之後 retry() 就載得到，不必重啟")
(FAKE / "core" / "__init__.py").write_text("", encoding="utf-8")
(FAKE / "core" / "scraper.py").write_text(
    "def search_jav(number, **kw):\n    return {'number': number}\n", encoding="utf-8")
(FAKE / "core" / "scrapers").mkdir()
(FAKE / "core" / "scrapers" / "__init__.py").write_text("", encoding="utf-8")
(FAKE / "core" / "scrapers" / "utils.py").write_text(
    "def extract_number(name):\n    return 'SSIS-938' if 'SSIS' in name else None\n",
    encoding="utf-8")

check("沒 retry 之前沿用上次的失敗（不會每次掃描都重新 import）",
      javscraper.status()["enabled"] is False)
javscraper.retry()
st = javscraper.status()
check("retry 之後載入成功", st["enabled"] is True, st)
check("錯誤訊息清空", st["error"] == "", st)
check("真的用到載進來的函式", javscraper._load()["extract_number"]("SSIS-938.mp4") == "SSIS-938")
javscraper.retry()
check("已經成功時 retry 不會把它卸掉", javscraper.status()["enabled"] is True)


head("[J] 封面下載看檔案內容，不看 content-type")
# javdb 的圖床 tp.spfcas.com 回 200 + binary/octet-stream，內容是正常的 JPEG；
# 只認 image/* 的話 FC2 條目一張封面都抓不到。防盜連回的是 200 + HTML，照樣要擋。
import http.server  # noqa: E402
import threading    # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# 真正的 tp.spfcas.com 不只 content-type 不對，內容還是 XOR 過的（第一個位元組是金鑰）。
# 解碼照 OpenAver 的 core.image_codec 按 host 決定 —— 假 OpenAver 讓 127.0.0.1 走 XOR。
(FAKE / "core" / "image_codec.py").write_text(
    "def decode_image_payload(host, content):\n"
    "    if host != '127.0.0.1' or len(content) < 2:\n"
    "        return content\n"
    "    return bytes(b ^ content[0] for b in content[1:])\n", encoding="utf-8")
javscraper._mods = None             # 重新載入，讓剛補上的 image_codec 被 import
javscraper._import_error = "reload"
javscraper.retry()
XORED = bytes([0x5A]) + bytes(b ^ 0x5A for b in JPEG)
BODIES = {"/octet.jpg": ("binary/octet-stream", XORED),
          "/hotlink.jpg": ("text/html", b"<html>forbidden</html>"),
          "/lying.jpg": ("image/jpeg", b"<html>forbidden</html>")}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ctype, body = BODIES[self.path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
name = javscraper.download_image(base + "/octet.jpg")
check("octet-stream 但內容是 JPEG：收", name is not None and
      (javscraper.IMAGE_DIR / name).read_bytes() == JPEG, name)
check("防盜連的 HTML 頁：擋", javscraper.download_image(base + "/hotlink.jpg") is None)
check("標成 image/jpeg 但內容是 HTML：擋", javscraper.download_image(base + "/lying.jpg") is None)
srv.shutdown()


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
