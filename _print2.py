p = "src/fpl_intelligence/scripts/run_phase475_gate.py"
lines = open(p, encoding="utf-8").read().split("\n")
hi = min(428, len(lines))
for n in range(395, hi):
    print("%4d | %s" % (n + 1, lines[n]))
print("--- end 12345")


