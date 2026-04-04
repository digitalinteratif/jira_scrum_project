#!/usr/bin/env bash
#
# start_gunicorn.sh
#
# Lightweight, portable wrapper to start Gunicorn for wsgi.prod_wsgi:app
# - Execs Gunicorn so that the process replaces the shell (useful in containers; becomes PID 1)
# - Respects environment variables used by gunicorn.conf.py
# - Appends a trace_KAN-135.txt entry on startup (best-effort) for troubleshooting
#
# Environment variables this script commonly surfaces (but which are consumed by gunicorn.conf.py):
#   WORKER_COUNT               - override default worker calculation (2*CPU + 1)
#   GUNICORN_WORKER_CLASS      - 'gthread' (default) or 'sync'
#   GUNICORN_THREADS           - threads per worker (default 4 when using gthread)
#   GUNICORN_TIMEOUT           - worker timeout seconds (default 30)
#   GUNICORN_PRELOAD           - 'true' or 'false' (default true)
#   GUNICORN_BIND              - bind address (default 0.0.0.0:8000)
#   GUNICORN_CONF              - path to gunicorn.conf.py (default: ../gunicorn.conf.py relative to this script)
#   GUNICORN_BIN               - path to gunicorn binary (default 'gunicorn' on PATH)
#   DATABASE_URL               - application database URL (example env used by app)
#   SQLALCHEMY_POOL_SIZE       - recommended DB pool sizing variable (operator-controlled)
#
# Example invocation:
#   WORKER_COUNT=5 GUNICORN_WORKER_CLASS=gthread GUNICORN_THREADS=4 \
#     DATABASE_URL="postgres://user:pw@db:5432/app" \
#     ./bin/start_gunicorn.sh
#
# The script will exec the configured gunicorn binary and replace itself with the Gunicorn process.
#
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Default config path (repo root expected)
DEFAULT_CONF="$SCRIPT_DIR/../gunicorn.conf.py"
GUNICORN_CONF="${GUNICORN_CONF:-$DEFAULT_CONF}"

# Default gunicorn binary (allow override to point at venv binary)
GUNICORN_BIN="${GUNICORN_BIN:-gunicorn}"

# Decide which app module to run
WSGI_APP_MODULE="${WSGI_APP_MODULE:-wsgi.prod_wsgi:app}"

# Best-effort trace file append locations (tries in order)
TRACE_PATHS=( \
  "/var/log/trace_KAN-135.txt" \
  "/tmp/trace_KAN-135.txt" \
  "./trace_KAN-135.txt" \
)

_trace_file=""
for p in "${TRACE_PATHS[@]}"; do
  # Try to create/append to the file; if successful use it.
  if (mkdir -p "$(dirname "$p")" >/dev/null 2>&1 || true) && touch "$p" >/dev/null 2>&1; then
    _trace_file="$p"
    break
  fi
done

# Append a trace line (best-effort; failures are ignored)
{
  if [[ -n "$_trace_file" ]]; then
    printf '%s\n' "[$(date --rfc-3339=seconds 2>/dev/null || date)] START KAN-135 trace: host=$(hostname 2>/dev/null || echo unknown) user=${USER:-$(whoami || echo unknown)} pid=$$ cwd=$(pwd) wsgi=$WSGI_APP_MODULE conf=$GUNICORN_CONF cmd=$GUNICORN_BIN" >> "$_trace_file" 2>/dev/null || true
  fi
} || true

# Print a short startup summary to stdout for operator convenience
cat <<-SUMMARY
Starting Gunicorn:
  gunicorn binary : ${GUNICORN_BIN}
  wsgi app        : ${WSGI_APP_MODULE}
  config file     : ${GUNICORN_CONF}
  env (sample)    : WORKER_COUNT=${WORKER_COUNT:-<unset>} GUNICORN_WORKER_CLASS=${GUNICORN_WORKER_CLASS:-<unset>} GUNICORN_THREADS=${GUNICORN_THREADS:-<unset>} GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-<unset>}
  Note: Gunicorn will read other env vars (DATABASE_URL, SQLALCHEMY_POOL_SIZE, etc.) from the environment.
SUMMARY

# Validate config exists (error if missing)
if [ ! -f "$GUNICORN_CONF" ]; then
  echo "ERROR: Gunicorn configuration not found at: $GUNICORN_CONF" >&2
  echo "If your repository places gunicorn.conf.py elsewhere, set GUNICORN_CONF to the absolute path." >&2
  exit 2
fi

# Exec gunicorn so it becomes PID 1 (use --config to let gunicorn.conf.py read env vars)
# Any runtime args can be appended by setting GUNICORN_ARGS environment variable, e.g.:
#   export GUNICORN_ARGS="--log-level debug"
# Make sure the wsgi module is "wsgi.prod_wsgi:app" as required.
GUNICORN_ARGS="${GUNICORN_ARGS:-}"

exec "$GUNICORN_BIN" --config "$GUNICORN_CONF" $GUNICORN_ARGS "$WSGI_APP_MODULE"
--- END FILE ---