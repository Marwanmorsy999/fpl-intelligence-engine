import re

# Fix models.py: UP042 -> StrEnum
mpath = "src/fpl_intelligence/availability/models.py"
with open(mpath, encoding="utf-8") as f:
    src = f.read()

# Ensure StrEnum imported
if "from enum import" not in src:
    src = src.replace(
        "from datetime import datetime, timezone\n",
        "from datetime import datetime, timezone\nfrom enum import StrEnum\n",
        1,
    )

src = src.replace("class SourceReliability(str, Enum):", "class SourceReliability(StrEnum):")
src = src.replace("class AvailabilityStatus(str, Enum):", "class AvailabilityStatus(StrEnum):")
src = src.replace("class EvidenceType(str, Enum):", "class EvidenceType(StrEnum):")
with open(mpath, "w", encoding="utf-8") as f:
    f.write(src)
print("models.py updated")

# Fix prediction_wrapper.py: SIM108 -> ternary
ppath = "src/fpl_intelligence/availability/prediction_wrapper.py"
with open(ppath, encoding="utf-8") as f:
    src = f.read()
old = """        if base_start > 0:
            points_ratio = adj_start / base_start
        else:
            points_ratio = 0.0 if adj_start == 0 else 1.0"""
new = """        points_ratio = (
            adj_start / base_start if base_start > 0 else 0.0 if adj_start == 0 else 1.0
        )"""
assert old in src, "prediction_wrapper marker not found"
src = src.replace(old, new)
with open(ppath, "w", encoding="utf-8") as f:
    f.write(src)
print("prediction_wrapper.py updated")
