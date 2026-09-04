"""比對 app/config.py（與 run.py）真正讀的環境變數，跟 .env.example 寫的是不是同一組。

會漏掉是因為兩邊在不同檔案：加了一個設定卻忘了寫進範例檔，
使用者永遠不知道它存在；反過來範例檔留著早就刪掉的鍵，
使用者照著設卻毫無作用 —— 兩種都不會有任何錯誤訊息。

    python tests/env_drift_test.py
"""
import ast, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  → {extra}")


def keys_read(path: Path) -> set:
    """找出這個檔案讀了哪些環境變數。

    兩種寫法都要抓：config.py 的 _s/_b/_i/_f 包裝，以及 os.getenv 直接讀。
    """
    found = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = None
        if isinstance(n.func, ast.Name):
            name = n.func.id
        elif isinstance(n.func, ast.Attribute):
            name = n.func.attr
        if name not in ("_s", "_b", "_i", "_f", "getenv"):
            continue
        if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            found.add(n.args[0].value)
    return found


# 這兩個是在讀 .env 之前就要拿到的（一個決定資料目錄、一個決定 .env 在哪），
# 放進 .env.example 會誤導人以為寫在裡面有用。
NOT_IN_EXAMPLE = {"FILMAX_DATA_DIR", "FILMAX_ENV_FILE"}

read = set()
for f in ("app/config.py", "run.py"):
    read |= keys_read(ROOT / f)
# 即時生效的設定不再是 config.py 裡的 _s()/_i() 呼叫，而是 params.py 的登記表。
# 兩邊都要算進來，否則把一項改成即時生效就會被誤判成「範例檔裡的死鍵」。
sys.path.insert(0, str(ROOT))
from app.params import REGISTRY
read |= set(REGISTRY)
read -= NOT_IN_EXAMPLE

env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", env_text, re.M))

print("[env] config.py / run.py ↔ .env.example")
missing = sorted(read - declared)
check(f"程式讀的 {len(read)} 個設定，範例檔都有寫", not missing, missing)
extra = sorted(declared - read)
check(f"範例檔的 {len(declared)} 個設定，程式都真的會讀", not extra, extra)

# 每個設定都該有說明。註解可以罩住底下一整串連續的鍵
# （FTP_HOST/PORT/USER/PASSWORD 共用一段說明是合理的），
# 但空一行之後就要重新說明，不然使用者只看得到一個名字。
lines = env_text.splitlines()
undocumented, documented = [], False
for ln in lines:
    st = ln.strip()
    if not st:
        documented = False          # 空行 = 換一段，說明不再延續
    elif st.startswith("#"):
        documented = True
    elif re.match(r"^[A-Z][A-Z0-9_]*=", st):
        if not documented:
            undocumented.append(st.split("=", 1)[0])
check("每個設定上面都有註解說明", not undocumented, undocumented)

print(f"\n{'=' * 50}\n通過 {OK}，失敗 {FAIL}")
sys.exit(1 if FAIL else 0)
