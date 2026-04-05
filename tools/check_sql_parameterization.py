#!/usr/bin/env python3
"""
tools/check_sql_parameterization.py - lightweight heuristic checker for unsafe SQL string construction.

Usage:
  python tools/check_sql_parameterization.py [paths...]
  If no paths provided, scans the repository root.

Behavior:
 - Flags probable unsafe patterns: f-strings containing SQL keywords, ".format" usage on SQL-like strings,
   string concatenation involving SQL keywords, and percent-formatting of SQL strings.
 - Conservative: supports inline suppression via "# noqa:sql-param".
 - Skips known directories (migrations, alembic, node_modules, vendor).
 - Returns exit code 1 when violations found (CI should fail), 0 otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Tuple, Dict

# Directories to skip
DEFAULT_SKIP_DIRS = {"migrations", "alembic", "node_modules", "vendor", ".venv", "venv", "__pycache__"}

# SQL keywords to look for (word boundaries)
SQL_KEYWORDS = r"(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|REPLACE|MERGE)"

# Compile conservative regexes
PATTERNS = {
    "fstring_sql": re.compile(rf"""(?i)(f['"][^'"]*\b{SQL_KEYWORDS}\b[^'"]*['"])"""),
    "format_sql": re.compile(rf"""(?i)(['"][^'"]*\b{SQL_KEYWORDS}\b[^'"]*['"]\s*\.\s*format\s*\()"""),
    "concat_sql": re.compile(rf"""(?i)\b{SQL_KEYWORDS}\b[^"\n']*\+[^"\n']*"""),
    "percent_sql": re.compile(rf"""(?i)(['"][^'"]*\b{SQL_KEYWORDS}\b[^'"]*['"]\s*%[^#\n]*)"""),
}

SUPPRESS_TOKEN = "noqa:sql-param"


def _is_ignored_path(path: str, skip_dirs=DEFAULT_SKIP_DIRS) -> bool:
    parts = set(p for p in path.replace("\\", "/").split("/"))
    return bool(parts & skip_dirs)


def scan_file(path: str) -> List[Dict]:
    """
    Scan a single file for suspicious SQL-building patterns.

    Returns list of violations: { 'file': path, 'line_no': int, 'rule': str, 'snippet': str }
    """
    violations = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return violations

    for idx, line in enumerate(lines, start=1):
        text = line.rstrip("\n")
        # Skip suppressed lines (inline)
        if SUPPRESS_TOKEN in text:
            continue
        # Basic skip: if no SQL keyword present in the line, skip quickly
        if re.search(SQL_KEYWORDS, text, re.IGNORECASE) is None:
            continue
        # Check each pattern
        for rule, rx in PATTERNS.items():
            if rx.search(text):
                # Final check: skip if '?' placeholder present (likely parameterized) and execute with params on same line
                if "?" in text:
                    # If line contains 'execute(' and a comma after it and '(' -> heuristic that params provided; skip
                    if re.search(r"execute\s*\(.*\?.*\,", text):
                        continue
                violations.append({"file": path, "line_no": idx, "rule": rule, "snippet": text.strip()})
    return violations


def _collect_paths(inputs: List[str]) -> List[str]:
    paths = []
    if not inputs:
        # walk repo root
        for root, dirs, files in os.walk(os.getcwd()):
            # prune skip dirs
            dirs[:] = [d for d in dirs if d not in DEFAULT_SKIP_DIRS]
            for fn in files:
                if fn.endswith(".py") or fn.endswith(".sql") or fn.endswith(".html"):
                    full = os.path.join(root, fn)
                    paths.append(full)
    else:
        for p in inputs:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs if d not in DEFAULT_SKIP_DIRS]
                    for fn in files:
                        if fn.endswith(".py") or fn.endswith(".sql") or fn.endswith(".html"):
                            full = os.path.join(root, fn)
                            paths.append(full)
            elif os.path.isfile(p):
                paths.append(p)
    # filter common skip dirs
    filtered = [p for p in paths if not _is_ignored_path(p)]
    return filtered


def scan_paths(paths: List[str]) -> List[Dict]:
    violations = []
    for p in paths:
        if _is_ignored_path(p):
            continue
        v = scan_file(p)
        if v:
            violations.extend(v)
    return violations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check for unsafe SQL string construction (heuristic).")
    parser.add_argument("paths", nargs="*", help="Files or directories to scan (default: repo root).")
    parser.add_argument("--quiet", action="store_true", help="Only return exit code, minimize output.")
    parser.add_argument("--no-fail", action="store_true", help="Do not return non-zero exit code even if violations found.")
    parser.add_argument("--show-all", action="store_true", help="Show all scanned files (verbose).")
    args = parser.parse_args(argv)

    # Allow env override to skip check (CI may set)
    if os.environ.get("SKIP_SQL_PARAM_CHECK", "").lower() in ("1", "true", "yes"):
        if not args.quiet:
            print("SKIP_SQL_PARAM_CHECK set; skipping sql parameterization check.")
        return 0

    paths = _collect_paths(args.paths)
    if args.show_all and not args.quiet:
        print(f"Scanning {len(paths)} files...")

    violations = scan_paths(paths)

    if violations and not args.quiet:
        print("Potential SQL parameterization issues detected:")
        for v in violations:
            print(f"{v['file']}:{v['line_no']}  [{v['rule']}]  {v['snippet']}")
            print(f"    -> add '# {SUPPRESS_TOKEN}' on the line to suppress with SURGICAL_RATIONALE in PR if intentional")
        print("")
        print(f"Total potential issues: {len(violations)}")

    exit_code = 1 if violations and not args.no_fail else 0
    return exit_code


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
--- END FILE ---