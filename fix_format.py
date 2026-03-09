import re
from pathlib import Path


def fix_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    original = content

    # 修复 conn.execute(\n    """\n -> conn.execute("""
    content = re.sub(r'(\.execute|\.executemany|cursor\.execute)\(\s*\n\s*(""")', r"\1(\2", content)

    # 修复 """\n) -> """)
    content = re.sub(r'(""")\s*\n\s*\)', r"\1)", content)

    # 修复不必要的括号: var = (\n    expr\n)
    content = re.sub(r"(\w+)\s*=\s*\(\s*\n\s*([^\n]+)\s*\n\s*\)", r"\1 = \2", content)

    if content != original:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False


files = [
    "outlook_web/controllers/scheduler.py",
    "outlook_web/controllers/system.py",
    "outlook_web/repositories/groups.py",
    "outlook_web/db.py",
    "outlook_web/repositories/accounts.py",
    "start.py",
    "outlook_web/services/scheduler.py",
    "outlook_web/controllers/accounts.py",
    "outlook_web/services/refresh.py",
]

modified = 0
for f in files:
    if Path(f).exists():
        if fix_file(f):
            print(f"✓ {f}")
            modified += 1
        else:
            print(f"- {f}")
    else:
        print(f"✗ {f} 不存在")

print(f"\n修改了 {modified} 个文件")
