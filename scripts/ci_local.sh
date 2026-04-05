#!/usr/bin/env bash
# Local helper to run the same checks as CI for development use.
# Usage: scripts/ci_local.sh [venv-path]
# Example: ./scripts/ci_local.sh .venv

set -euo pipefail
VENV_DIR="${1:-.venv}"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

echo "CI local runner — create venv at ${VENV_DIR}"
if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi

# Upgrade pip and install dependencies into venv
"${PIP}" install --upgrade pip
if [ -f requirements.txt ]; then
  "${PIP}" install -r requirements.txt
fi

# Ensure linters/test tools available in venv
"${PIP}" install --upgrade ruff flake8 pytest

echo "Running linters..."
"${VENV_DIR}/bin/ruff" check .
"${VENV_DIR}/bin/flake8" .

echo "Running unit tests..."
# use a temp DB for unit tests if needed; default DB preserved if project not using DB.
TMP_UNIT_DB="$(mktemp -u)/ci_local_unit.db"
export DATABASE_URL="sqlite:///$TMP_UNIT_DB"
"${VENV_DIR}/bin/pytest" -q -m "not integration" --maxfail=1

echo "Running integration tests (uses sqlite file in repo/tmp_ci_integration.db)"
TMP_INT_DB="./tmp_ci_integration.db"
rm -f "$TMP_INT_DB" || true
export DATABASE_URL="sqlite:///${PWD}/tmp_ci_integration.db"
"${VENV_DIR}/bin/pytest" -q -m integration --maxfail=1

echo "Local CI checks complete."

# make script executable suggestion (cannot set file system mode here)
# After adding the file run: chmod +x scripts/ci_local.sh