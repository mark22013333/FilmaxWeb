"""編碼器的位元率控制參數：有目標碼率走碼率導向、沒有就走品質導向。

    python tests/encoder_args_test.py

規格：J 章結論 (a)（重編預設走 NVENC）、結論 (b)（REMOTE_BITRATE_KBPS=8000）、
以及第 0 層（下階要在 master playlist 宣告 BANDWIDTH，所以需要可預測的上界）。

這一支不需要 ffmpeg —— 它只看「我們組出來的參數長什麼樣」，
把 pick_option 換成假的，這樣沒有顯卡的機器也跑得完，
而且不會因為某台機器的 NVENC 選項不同就變紅。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="filmax-enc-")
os.environ.update({
    "FILMAX_DATA_DIR": os.path.join(TMP, "data"),
    # 隔離開發機的 .env（見 config.py 的 ENV_FILE 註解）
    "FILMAX_ENV_FILE": os.path.join(TMP, "no-such.env"),
    "TMDB_API_KEY": "", "AUTH_ENABLED": "false", "MSSQL_HOST": "",
    "AUTO_SCAN_ON_START": "false", "FTP_HOST": "127.0.0.1", "FTP_PORT": "1",
    "TRANSCODE_CRF": "17", "NVENC_CQ": "25", "X264_PRESET": "medium",
})
sys.path.insert(0, str(ROOT))

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond: OK += 1; print(f"  PASS  {name}")
    else:    FAIL += 1; print(f"  FAIL  {name}  → {extra}")
def head(t): print(f"\n{t}")

from app import media
from app.config import settings

# NVENC 的可用選項：不要去問真的 ffmpeg，這一支測的是參數的形狀
media.pick_option = lambda enc, opt, prefs: {"preset": "p5", "rc": "vbr"}.get(opt)


def args(encoder, target=0):
    media._chosen_tier.clear()
    return media._encoder_quality_args(encoder, target)


def val(a, flag):
    """取某個旗標後面的值；沒有回 None。"""
    return a[a.index(flag) + 1] if flag in a else None


# ============================================================ NVENC
head("[J (a)] NVENC")

a = args("h264_nvenc", 3000)
check("有目標碼率 → -b:v 就是目標", val(a, "-b:v") == "3000k", a)
check("有目標碼率 → 不再給 -cq（cq 導向給不出上界）", "-cq" not in a, a)
check("有目標碼率 → -maxrate 等於目標", val(a, "-maxrate") == "3000k", a)
check("bufsize 是兩倍", val(a, "-bufsize") == "6000k", a)
check("-rc 還在（最好的那一層）", val(a, "-rc") == "vbr", a)
check("preset 還在", val(a, "-preset") == "p5", a)

b = args("h264_nvenc", 0)
check("沒有目標碼率 → 回到 cq 導向", val(b, "-cq") == "25", b)
check("沒有目標碼率 → -b:v 0 一定要在（少了它 cq 就沒有作用）",
      val(b, "-b:v") == "0", b)
check("沒有目標碼率 → 不要有 -maxrate", "-maxrate" not in b, b)


# ============================================================ x264
head("[J (a)] x264")

c = args("libx264", 8000)
check("有目標碼率 → capped CRF：-crf 與 -maxrate 兩個都在",
      val(c, "-crf") == "17" and val(c, "-maxrate") == "8000k", c)
check("preset 用 X264_PRESET", val(c, "-preset") == "medium", c)
d = args("libx264", 0)
check("沒有目標碼率 → 只有 CRF", val(d, "-crf") == "17" and "-maxrate" not in d, d)


# ============================================================ 不要重複
head("[J 0] 位元率參數只能有一份")

for enc in ("h264_nvenc", "libx264"):
    a = args(enc, 5000)
    for flag in ("-maxrate", "-bufsize", "-b:v", "-cq", "-crf"):
        check(f"{enc}：{flag} 不會出現兩次", a.count(flag) <= 1, a)


# ============================================================ 防漂移
head("[B+] 改設定要立刻反映（記層級、不記數字）")

os.environ["NVENC_CQ"] = "28"
os.environ["TRANSCODE_CRF"] = "21"
check("NVENC_CQ 改了，產生的參數跟著改", val(args("h264_nvenc", 0), "-cq") == "28")
check("TRANSCODE_CRF 改了，x264 跟著改", val(args("libx264", 0), "-crf") == "21")

# 探測是在沒有目標碼率的情況下做的，記下的層級要能套用到有碼率的情況
media._chosen_tier["h264_nvenc"] = "無 rc"
a = media._encoder_quality_args("h264_nvenc", 4000)
check("探測記下的層級在有目標碼率時仍然對得上（無 rc → 沒有 -rc）",
      "-rc" not in a and val(a, "-b:v") == "4000k", a)
check("而且那一層的 preset 還在", val(a, "-preset") == "p5", a)

media._chosen_tier["h264_nvenc"] = "這一層已經不存在了"
a = media._encoder_quality_args("h264_nvenc", 4000)
check("記到不存在的層級 → 有東西保底，不會空手", bool(a), a)
os.environ["NVENC_CQ"] = "25"
os.environ["TRANSCODE_CRF"] = "17"


# ============================================================ 預設編碼器
head("[J (a)] 預設要走硬體")

check("FFMPEG_HWACCEL 的預設是 auto（後台顯示的預設值也是 auto）",
      settings.hwaccel == "auto", settings.hwaccel)

from app.params import REGISTRY
entry = REGISTRY.get("FFMPEG_HWACCEL")
check("註冊表宣告的預設值與程式碼一致（原本一邊 auto 一邊 none）",
      entry is not None and str(entry.default) == str(settings.hwaccel),
      entry and entry.default)


print("\n" + "=" * 50)
print(f"通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
