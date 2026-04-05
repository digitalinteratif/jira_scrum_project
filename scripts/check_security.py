#!/usr/bin/env python3
"""
scripts/check_security.py

Simple repository scanner enforcing a small set of high-confidence security checks:
 - Fail (exit code 2) if any empty/swallowing except blocks found:
     - "except Exception: pass"
     - "except: pass"
 - Emit warnings for risky patterns (do not fail):
     - f-string usage inside execute(...) calls
     - .format() usage inside execute(...) calls
     - direct password assignment patterns (advisory)

Intended to be a deterministic, dependency-free checker usable in CI before merge.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(".").resolve()
SEARCH_DIRS = ["app_core", "utils", "routes", "scripts"]

# Patterns
EMPTY_EXCEPT_PATTERNS = [
    re.compile(r"\bexcept\s+Exception\s*:\s*pass\b"),
    re.compile(r"\bexcept\s*:\s*pass\b"),
]

FSTRING_EXECUTE_PATTERN = re.compile(r"execute\(\s*f[\"']")  # e.g., execute(f"...")
FORMAT_EXECUTE_PATTERN = re.compile(r"execute\([^)]*\.format\(")  # execute("...".format(...))
PASSWORD_ASSIGN_PATTERN = re.compile(r"\b(password|passwd|pwd)\s*(=|:)\s*", re.IGNORECASE)

PY_EXT = ".py"
FILES_TO_SCAN: List[Path] = []

for d in SEARCH_DIRS:
    p = ROOT / d
    if p.exists():
        for fp in p.rglob("*.py"):
            # skip virtual envs or hidden dirs if any
            if any(part.startswith(".") for part in fp.parts):
                continue
            FILES_TO_SCAN.append(fp)

# Also include top-level scripts that may not be under the above directories
for extra in ["scripts", "tools"]:
    p = ROOT / extra
    if p.exists():
        for fp in p.rglob("*.py"):
            FILES_TO_SCAN.append(fp)

def scan_file(path: Path) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    empty_except_matches: List[Tuple[int, str]] = []
    risky_sql_matches: List[Tuple[int, str]] = []
    password_matches: List[Tuple[int, str]] = []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return empty_except_matches, risky_sql_matches, password_matches

    for i, line in enumerate(text.splitlines(), start=1):
        for p in EMPTY_EXCEPT_PATTERNS:
            if p.search(line):
                empty_except_matches.append((i, line.strip()))
        if FSTRING_EXECUTE_PATTERN.search(line) or FORMAT_EXECUTE_PATTERN.search(line):
            risky_sql_matches.append((i, line.strip()))
        if PASSWORD_ASSIGN_PATTERN.search(line):
            password_matches.append((i, line.strip()))
    return empty_except_matches, risky_sql_matches, password_matches

def main():
    total_empty = []
    total_risky = []
    total_pw = []

    for fp in sorted(set(FILES_TO_SCAN)):
        ee, rs, pw = scan_file(fp)
        if ee:
            for ln, snippet in ee:
                total_empty.append((str(fp), ln, snippet))
        if rs:
            for ln, snippet in rs:
                total_risky.append((str(fp), ln, snippet))
        if pw:
            for ln, snippet in pw:
                total_pw.append((str(fp), ln, snippet))

    # Report empty except blocks (fatal)
    if total_empty:
        print("ERROR: Found empty/swallowing except blocks (forbidden). Please replace with specific exception handling and logging.")
        for file, ln, snippet in total_empty:
            print(f"  {file}:{ln}: {snippet}")
        print("\nFailing security check due to empty except blocks.")
        sys.exit(2)

    # Report risky SQL patterns (advisory)
    if total_risky:
        print("WARNING: Detected possible SQL string interpolation patterns (inspect manually).")
        for file, ln, snippet in total_risky[:200]:
            print(f"  {file}:{ln}: {snippet}")
        print("These are advisory warnings; ensure SQL statements use parameterized bindings and do not interpolate user input.")
    else:
        print("No risky SQL f-string/.format patterns detected (quick scan).")

    # Report password assignment hints (advisory)
    if total_pw:
        print("INFO: Found assignments referencing password-like identifiers (advisory - review to ensure hashing prior to storage).")
        for file, ln, snippet in total_pw[:200]:
            print(f"  {file}:{ln}: {snippet}")

    # Exit 0 when only advisories present
    print("Security checks passed (no fatal issues found).")
    sys.exit(0)

if __name__ == "__main__":
    main()