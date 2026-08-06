import sys

p = "src/fpl_intelligence/ingestion/historical.py"
lines = open(p, encoding="utf-8").read().split("\n")

# Anchors (0-based)
assert lines[648].rstrip().endswith(")"), repr(lines[648])   # close db.add FPLSnapshot
assert lines[649].strip() == "", repr(lines[649])            # blank
assert "if not dry_run" in lines[650], repr(lines[650])     # broken snapshots if
assert "except NotImplementedError" in lines[655], repr(lines[655])
assert "FPL snapshots not supported" in lines[656], repr(lines[656])
assert lines[657].strip() == "", repr(lines[657])           # blank
assert "# 7. Team-level match statistics" in lines[658], repr(lines[658])

new_block = [
    "                if not dry_run:",
    "                    _save_raw_record(",
    '                        db, provider_name, provider_name, f"/fpl_snapshots/{season_code}",',
    "                        list(snapshots_data), season_code,",
    "                    )",
    "            except NotImplementedError:",
    "                logger.info(\"FPL snapshots not supported by provider %s\", provider_name)",
]

lines[650:657] = new_block  # replace indices 650..656

open(p, "w", encoding="utf-8").write("\n".join(lines))
print("rewrote snapshots block OK")

