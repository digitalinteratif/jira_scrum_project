#!/usr/bin/env bash
set -euo pipefail

# Lightweight security scan wrapper used locally and in CI.
# - Runs ruff
# - Runs pytest (fast)
# - Runs python security checker for forbidden patterns (empty except blocks -> FAIL)
# - Prints advisory grep results for f-string SQL usage for manual review

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"

echo "Running ruff..."
if ! command -v ruff >/dev/null 2>&1; then
  echo "ruff not found in PATH; install with 'pip install ruff' or run in CI environment." >&2
else
  ruff check "${ROOT_DIR}"
fi

echo "Running pytest (fast) ..."
if ! command -v pytest >/dev/null 2>&1; then
  echo "pytest not found in PATH; install with 'pip install pytest' or run in CI environment." >&2
else
  pytest -q || { echo "pytest failed"; exit 1; }
fi

echo "Running python security checker (scripts/check_security.py)..."
if ! python3 "${ROOT_DIR}/scripts/check_security.py"; then
  echo "Security checker failed (forbidden patterns found). See output above." >&2
  exit 2
fi

echo "Scanning for f-string SQL usage (advisory - inspect matches)..."
# This is advisory: do not fail CI on these, but list matches for manual review
grep -nR -P "execute\(\s*f[\"']" app_core || true
grep -nR -P "execute\([^)]*\.format\(" app_core || true
grep -nR -E "(SELECT|INSERT|UPDATE|DELETE).*\{.*\}" app_core || true

echo "Scanning for obvious direct password assignments (advisory)..."
grep -nR -E "(password|passwd|pwd)\s*(=|:)" --exclude-dir=.git || true

echo "Security scan finished successfully."
exit 0
--- END FILE: scripts/security_scan.sh ---