p = "src/fpl_intelligence/ingestion/historical.py"
src = open(p, encoding="utf-8").read().split("\n")

start = next(i for i, l in enumerate(src) if "# 6. FPL Snapshots" in l or "# 6. Player performance snapshots" in l)
flush_idx = next(i for i in range(start, len(src)) if src[i].startswith("        if not dry_run:") and i + 1 < len(src) and "db.flush" in src[i + 1])
blank = flush_idx - 1
while blank > start and src[blank].strip() != "":
    blank -= 1
assert src[blank].strip() == "", (blank, repr(src[blank]))
assert flush_idx == blank + 1, (flush_idx, blank)

clean = open("_snap_section.txt", encoding="utf-8").read()
if clean.endswith("\n"):
    clean = clean[:-1]
replacement = clean.split("\n")
new_src = src[:start] + replacement + src[blank:]
open(p, "w", encoding="utf-8").write("\n".join(new_src))
print("spliced clean snapshot section; start=", start, "blank=", blank)


