import ast, py_compile

files = [
    "src/fpl_intelligence/ingestion/historical.py",
    "src/fpl_intelligence/validation/edge.py",
    "src/fpl_intelligence/providers/real_football_stats.py",
    "src/fpl_intelligence/scripts/run_phase475_gate.py",
]
for f in files:
    py_compile.compile(f, doraise=True)
    ast.parse(open(f, encoding="utf-8").read())
    print("OK", f)
