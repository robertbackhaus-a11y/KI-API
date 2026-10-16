import ast
from pathlib import Path

src_code = Path(r"C:\tools\reaper-api\test-p5\patch_readme.py").read_text(encoding="utf-8")
tree = ast.parse(src_code)
anchors = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "replace_once":
        anchors.append(ast.literal_eval(node.args[0]))

raw = Path(r"C:\tools\reaper-api\README.md").read_text(encoding="utf-8")
crlf = "\r\n" in raw
text = raw.replace("\r\n", "\n")
print(f"README crlf={crlf} len={len(text)} anchors={len(anchors)}")
for i, a in enumerate(anchors, 1):
    n = text.count(a)
    print(f"--- anchor {i}: count={n} len={len(a)}")
    if n != 1:
        print("  ANCHOR:", repr(a))
        head = a[:40]
        idx = text.find(head)
        print("  head at:", idx)
        if idx >= 0:
            print("  FILE  :", repr(text[idx:idx+len(a)+80]))
