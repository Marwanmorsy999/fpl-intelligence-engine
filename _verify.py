import ast, py_compile, sys
f = "src/fpl_intelligence/ingestion/historical.py"
py_compile.compile(f, doraise=True)
ast.parse(open(f, encoding="utf-8").read())
print("historical.py OK")
f2 = "src/fpl_intelligence/validation/edge.py"
py_compile.compile(f2, doraise=True)
print("edge.py OK")
