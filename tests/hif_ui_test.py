"""相片頁「轉成 JPG」在真的瀏覽器裡的操作（規格 S）：提示列、確認框、進度、結果、單張。

    python tests/hif_ui_test.py
"""
import asyncio, io, os, shutil, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="filmax-hifui-")).resolve()
FTP_ROOT = TMP / "ftp"
FTP_ROOT.mkdir()
PORT, FTP_PORT = 29322, 2190
SHOTS = os.environ.get("SHOT_DIR", str(TMP))
os.environ.update({
    "FILMAX_DATA_DIR": str(TMP / "data"), "FILMAX_ENV_FILE": str(TMP / "absent.env"),
    "FTP_HOST": "127.0.0.1", "FTP_PORT": str(FTP_PORT), "FTP_USER": "u", "FTP_PASSWORD": "p",
    "FTP_TLS": "false", "LIBRARY_ROOTS": "/|auto", "TMDB_API_KEY": "", "MIN_FILE_MB": "0",
    "MIN_PHOTO_KB": "1", "AUTH_ENABLED": "false", "MSSQL_HOST": "", "AUTO_SCAN_ON_START": "false",
    "HOST": "127.0.0.1", "PORT": str(PORT), "PHOTO_CONVERT_ENABLED": "true",
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


import random
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer


def heif(seed, taken):
    random.seed(seed)
    im = Image.new("RGB", (320, 200))
    px = im.load()
    for y in range(200):
        for x in range(320):
            px[x, y] = (random.randrange(256), (x * 3) % 256, (y * 5) % 256)
    ex = Image.Exif()
    ex[0x0132] = taken
    b = io.BytesIO()
    im.save(b, format="HEIF", quality=90, exif=ex)
    return b.getvalue()


pics = FTP_ROOT / "旅行"
pics.mkdir()
for n, (seed, t) in {"A.HIF": (1, "2021:10:02 10:00:00"), "B.HIF": (2, "2021:10:03 10:00:00"),
                     "C.HIF": (3, "2022:01:05 10:00:00")}.items():
    (pics / n).write_bytes(heif(seed, t))
other = FTP_ROOT / "別的"
other.mkdir()
for i in range(3):
    im = Image.new("RGB", (200, 120), (i * 60, 80, 120))
    im.save(other / f"J{i}.jpg", "JPEG", quality=90)

_auth = DummyAuthorizer()
_auth.add_user("u", "p", str(FTP_ROOT), perm="elradfmwMT")
FTPHandler.authorizer = _auth
_srv = FTPServer(("127.0.0.1", FTP_PORT), FTPHandler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
time.sleep(0.5)

from app import db, scanner
db.init_db()
scanner.start()
for _ in range(300):
    if not scanner.status.running:
        break
    time.sleep(0.1)

import uvicorn
from app.main import app
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
        b = await pw.chromium.launch()
        pg = await (await b.new_context(viewport={"width": 1280, "height": 900})).new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: print("console:", m.type, m.text) if m.type in ("error", "warning") else None)
        await pg.goto(BASE + "/", wait_until="networkidle")
        await pg.click('nav [data-media="photo"]')
        await pg.wait_for_selector("#pgrid .ph")

        print("[1] 沒選資料夾時沒有提示列")
        check("提示列隱藏", await pg.locator("#convBar").is_hidden())

        print("\n[2] 選到有 HIF 的資料夾")
        await pg.click('.fchip[data-folder="/旅行"]')
        await pg.wait_for_selector("#convBar:not([hidden])")
        txt = await pg.inner_text("#convBar")
        check("提示列說有 3 張 HIF", "3" in txt and "HIF" in txt, txt)
        check("HIF 在相片牆有縮圖（瀏覽器真的載得出來）",
              await pg.evaluate("""() => [...document.querySelectorAll('#pgrid .ph img')]
                   .every(i => i.complete && i.naturalWidth > 0)"""))

        print("\n[3] 選到沒有 HIF 的資料夾，提示列收起來")
        await pg.click('.fchip[data-folder="/別的"]')
        await pg.wait_for_function("document.querySelector('#convBar').hidden")
        check("提示列隱藏", True)
        await pg.click('.fchip[data-folder="/旅行"]')
        await pg.wait_for_selector("#convBar:not([hidden])")

        print("\n[3b] 從檢視器對單張轉")
        await pg.click("#pgrid .ph >> nth=0")
        await pg.wait_for_selector(".pswp .row")
        btn = pg.locator(".pswp button:has-text('轉成 JPG')")
        await btn.wait_for(timeout=10000)
        check("HIF 的側欄有「轉成 JPG」", await btn.count() == 1)
        await btn.click()
        await pg.wait_for_selector("#cvGo:not([disabled])", timeout=15000)
        body = await pg.inner_text("#modal")
        check("單張的說法", "這 1 張" in body, body)
        await pg.click("#cvGo")
        await pg.wait_for_selector("#cvDone", timeout=60000)
        check("單張結果：成功 1 張", "成功 1 張" in await pg.inner_text("#modal"))
        files1 = sorted(p.name for p in pics.iterdir())
        check("磁碟上只動了那一張", sum(f.endswith(".jpg") for f in files1) == 1
              and sum(f.endswith(".HIF") for f in files1) == 2, files1)
        await pg.click("#cvDone")
        await pg.wait_for_selector("#convBar:not([hidden])")
        bar = await pg.inner_text("#convBar")
        check("提示列剩 2 張", "2" in bar, bar)

        print("\n[4] 確認框")
        await pg.click("#convAll")
        await pg.wait_for_selector("#cvGo:not([disabled])")
        body = await pg.inner_text("#modal")
        check("講清楚 JPG 放哪、原檔去哪、不刪除",
              "同一個資料夾" in body and "原檔" in body and "不會刪除任何檔案" in body, body)
        check("寫入權限檢查通過", "可以寫入" in body, body)
        await pg.screenshot(path=os.path.join(SHOTS, "dialog.png"))

        print("\n[5] 按下開始 → 進度 → 結果")
        await pg.click("#cvGo")
        await pg.wait_for_selector("#cvDone", timeout=60000)
        body = await pg.inner_text("#modal")
        check("結果：成功 2 張", "成功 2 張" in body and "失敗 0 張" in body, body)
        await pg.screenshot(path=os.path.join(SHOTS, "summary.png"))
        files = sorted(p.name for p in pics.iterdir())
        check("磁碟上：3 張 JPG、3 個日期資料夾", sum(f.endswith(".jpg") for f in files) == 3
              and "20211002原檔" in files and "20211003原檔" in files and "20220105原檔" in files, files)
        check("沒有任何 HIF 留在原位", not any(f.endswith(".HIF") for f in files), files)
        await pg.click("#cvDone")
        await pg.wait_for_function("document.querySelector('#convBar').hidden", timeout=10000)
        check("完成後提示列消失（資料夾已經沒有 HIF）", True)
        names = await pg.eval_on_selector_all("#pgrid .ph", "els => els.map(e => e.getAttribute('title') || e.textContent)")
        check("相片牆已經換成 JPG，不必手動重整", len(names) == 3, names)

        check("沒有 JavaScript 錯誤", not errors, errors)
        await b.close()


asyncio.run(main())
print(f"\n{OK} passed, {FAIL} failed")
_server.should_exit = True
shutil.rmtree(TMP, ignore_errors=True) if FAIL == 0 else print("暫存保留在", TMP)
sys.exit(1 if FAIL else 0)
