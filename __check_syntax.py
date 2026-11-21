import ast, sys
try:
    src = open('/mnt/c/oloai/tests/olo/m3/test_m3_gate.py').read()
    ast.parse(src)
    print("Syntax OK")
except SyntaxError as e:
    print(f"Syntax ERROR: {e}")
    sys.exit(1)
