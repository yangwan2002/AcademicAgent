import os, re

ROOTS = ["src", "tests", "scripts"]
files = []
for r in ROOTS:
    for dp, dn, fn in os.walk(r):
        if "__pycache__" in dp:
            continue
        for f in fn:
            if f.endswith(".py"):
                files.append(os.path.join(dp, f).replace("\\", "/"))

pat = re.compile(r"os\.environ|getenv|_env_flag|_env_int|_env_float|_env_str")
env_names = {}
print("### env read sites")
for f in files:
    for i, line in enumerate(open(f, encoding="utf-8", errors="replace"), 1):
        if pat.search(line):
            print(f"{f}:{i}: {line.rstrip()[:160]}")
            for m in re.finditer(r'"(PAPER_[A-Z0-9_]+)"', line):
                env_names.setdefault(m.group(1), []).append(f"{f}:{i}")
            for m in re.finditer(r"'(PAPER_[A-Z0-9_]+)'", line):
                env_names.setdefault(m.group(1), []).append(f"{f}:{i}")

print()
print("### distinct PAPER_* env var names:", len(env_names))
for k in sorted(env_names):
    print(f"  {k}   <- {env_names[k][:3]}")
