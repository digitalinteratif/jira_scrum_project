"""Unit tests for tools/check_sql_parameterization.py utilities."""
import os
import tempfile
import importlib
import sys

# Ensure tools is importable
TOOLS_PATH = os.path.join(os.getcwd(), "tools")
if TOOLS_PATH not in sys.path:
    sys.path.insert(0, TOOLS_PATH)

import check_sql_parameterization as checker  # type: ignore

def _write_tmp_file(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".py", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def test_detects_fstring_and_format_and_allows_suppression(tmp_path):
    # unsafe f-string
    unsafe = "cur.execute(f\"SELECT * FROM users WHERE email = '{email}'\")\n"
    p1 = _write_tmp_file(unsafe)
    violations = checker.scan_paths([p1])
    assert any(v["rule"] == "fstring_sql" for v in violations)

    # unsafe .format pattern
    unsafe2 = "cur.execute(\"SELECT * FROM users WHERE email = '{}'\".format(email))\n"
    p2 = _write_tmp_file(unsafe2)
    violations2 = checker.scan_paths([p2])
    assert any(v["rule"] == "format_sql" for v in violations2)

    # suppressed line should not be reported
    suppressed = "cur.execute(f\"SELECT * FROM users WHERE email = '{email}'\")  # noqa:sql-param\n"
    p3 = _write_tmp_file(suppressed)
    violations3 = checker.scan_paths([p3])
    assert violations3 == []

    # cleanup
    os.remove(p1)
    os.remove(p2)
    os.remove(p3)
--- END FILE ---