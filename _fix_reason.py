path = "migrations/versions/0006_phase7_availability.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

old = '        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),\nsa.Column("reason", sa.String(100), nullable=False),'
new = '        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),\n        sa.Column("reason", sa.String(100), nullable=False),'
assert old in src, "marker not found"
src = src.replace(old, new)
with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("FIXED_INDENT")
import py_compile
py_compile.compile(path, doraise=True)
print("COMPILE_OK")
