#!/usr/bin/env python3
"""
AST-based checker to detect empty except blocks.

Usage:
  python tools/check_empty_except.py [paths...]

If no paths are provided, it scans the repository files ending with .py.
Returns non-zero exit code if any empty except blocks are found.
"""

import os
import sys
import ast
from typing import List, Tuple

def _find_python_files(paths: List[str]) -> List[str]:
    files = []
    if paths:
        for p in paths:
            if os.path.isfile(p) and p.endswith(".py"):
                files.append(p)
            elif os.path.isdir(p):
                for root, _, fnames in os.walk(p):
                    for fn in fnames:
                        if fn.endswith(".py"):
                            files.append(os.path.join(root, fn))
            else:
                # globs not supported; ignore
                pass
    else:
        # default: scan current repo
        cwd = os.getcwd()
        for root, _, fnames in os.walk(cwd):
            # skip virtual envs and hidden dirs
            if any(part.startswith(".") for part in os.path.relpath(root, cwd).split(os.sep)):
                # still allow top-level hidden? skip hidden directories
                continue
            for fn in fnames:
                if fn.endswith(".py"):
                    files.append(os.path.join(root, fn))
    return files

def _is_pass_like(node: ast.stmt) -> bool:
    # Pass statement or constant string only (docstring), or a bare Ellipsis
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) and isinstance(node.value.value, str):
        # docstring-like expression
        return True
    if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) and node.value.value is ...:
        return True
    return False

def check_file(path: str) -> List[Tuple[int, str]]:
    results = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        # Unable to read file: treat as no issues
        return results
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        # skip files with syntax errors (do not block commit here)
        return results
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = getattr(node, "body", []) or []
            if not body:
                lineno = getattr(node, "lineno", 0)
                results.append((lineno, "Empty except body"))
            else:
                # if all statements in body are pass-like, flag it
                all_pass_like = True
                for stmt in body:
                    if not _is_pass_like(stmt):
                        all_pass_like = False
                        break
                if all_pass_like:
                    lineno = getattr(node, "lineno", 0)
                    results.append((lineno, "Except body contains only pass/docstring/ellipsis"))
    return results

def main(argv=None):
    args = argv or sys.argv[1:]
    paths = args
    files = _find_python_files(paths)
    issues_found = 0
    for f in files:
        rel = os.path.relpath(f)
        issues = check_file(f)
        for lineno, msg in issues:
            print(f"{rel}:{lineno}: {msg}")
            issues_found += 1
    if issues_found:
        print(f"\nFound {issues_found} empty/placeholder except blocks. See POLICIES.md for remediation guidance.")
        return 2
    print("No empty except blocks found.")
    return 0

if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
--- END FILE ---