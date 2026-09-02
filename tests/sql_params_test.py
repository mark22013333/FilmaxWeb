"""靜態檢查：每一句 SQL 的佔位符數量要跟參數數量一致。

這一類錯誤有個討厭的特性 —— 只有走到那條分支才會爆。
scanner 的 media_file UPDATE 就是這樣：第一次掃描走 INSERT 所以全綠，
只有「重新分組」時才走 UPDATE，於是錯誤到那時候才出現。
用 AST 掃一遍就能在寫完的當下抓到。

    python tests/sql_params_test.py
"""
import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {"execute", "q", "q1", "scalar", "executemany"}

bad, checked = [], 0
for f in sorted((ROOT / "app").rglob("*.py")):
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in TARGETS and len(node.args) == 2):
            continue
        sql_node, par = node.args
        if not (isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str)):
            continue                      # f-string 組出來的無法靜態判斷
        if not isinstance(par, (ast.Tuple, ast.List)):
            continue                      # 變數傳進來的也不行
        checked += 1
        n_ph, n_par = sql_node.value.count("?"), len(par.elts)
        if n_ph != n_par:
            bad.append((f.relative_to(ROOT), node.lineno, n_ph, n_par,
                        " ".join(sql_node.value.split())[:70]))

print(f"檢查了 {checked} 句可靜態判斷的 SQL")
for path, line, a, b, q in bad:
    print(f"  FAIL  {path}:{line}  佔位符 {a} 個、參數 {b} 個")
    print(f"        {q}")
print(f"\n{'全部一致' if not bad else str(len(bad)) + ' 處不一致'}")
sys.exit(1 if bad else 0)
