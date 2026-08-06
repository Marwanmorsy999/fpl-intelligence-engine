import sys
p = sys.argv[1]
n = 0
with open(p, encoding="utf-8") as f:
    for line in f:
        n += 1
        if n >= 85 and n <= 296:
            sys.stdout.write(f"{n:4d} | {line}")
print("--- end", p)
