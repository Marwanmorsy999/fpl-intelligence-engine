import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()
for line in lines:
    if "passed" in line or "failed" in line or "error" in line:
        print(line)
