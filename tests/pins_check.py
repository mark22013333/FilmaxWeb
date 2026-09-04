"""比對 requirements.txt 裡 `==` 釘住的版本與實際裝在環境裡的版本。

    python tests/pins_check.py          # 印一行結果，版本不符時 exit 1

**為什麼是獨立的一支檔案，而不是 .bat 裡的 `python -c`。**
第一版寫成一行 `python -c` 塞在 `跑測試.bat` 裡，結果 cmd.exe 把兩種字元吃掉了：

* `^` 是 cmd 的轉義字元 —— `re` 的 `^`、`[^\]]`、`[^\s#]` 全部被吞掉，
  regex 從「開頭」變成「任意」、從「排除」變成「包含」。
* `!` 在 `setlocal enabledelayedexpansion` 之下是延遲展開的標記 ——
  `!=` 被吃成 `=`，於是 `if a=b` 變成語法錯誤。

也就是說：**.bat 的規則不只是「內容要純 ASCII」，還包括「不要在裡面寫
`^` 與 `!`」**。判斷邏輯放進 .py，.bat 只負責呼叫，這兩個雷就都不存在。
"""
import re
import sys
from pathlib import Path

try:
    import importlib.metadata as md
except ImportError:                      # pragma: no cover
    import importlib_metadata as md      # type: ignore

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"

PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s#]+)", re.M)


def installed() -> dict:
    out = {}
    for dist in md.distributions():
        name = (dist.metadata.get("Name") or "").strip().lower()
        if name:
            out.setdefault(name, dist.version)
    return out


def mismatches() -> list:
    if not REQ.exists():
        return []
    have = installed()
    bad = []
    for name, want in PIN.findall(REQ.read_text(encoding="utf-8")):
        got = have.get(name.lower())
        if got != want:
            bad.append((name, want, got or "沒裝"))
    return bad


def main() -> int:
    bad = mismatches()
    if not bad:
        print("套件版本與 requirements.txt 一致")
        return 0
    detail = "、".join(f"{n} 要 {w}、實際是 {g}" for n, w, g in bad)
    # 這一行的用途是「讓環境不對看起來像環境不對」：不然它會表現成
    # TypeError: unexpected keyword argument 'client'，看起來像程式壞了。
    print(f"  警告  套件版本不符：{detail} → 先跑一次 安裝.bat")
    return 1


if __name__ == "__main__":
    sys.exit(main())
