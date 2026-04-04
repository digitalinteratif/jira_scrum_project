# gunicorn.conf.py
# Standalone Gunicorn configuration for production.
#
# Designed for safe import by Gunicorn (no heavy app imports).
# Inline operator notes and DB sizing guidance (US-038) are included below.
#
# Behavior highlights:
#  - workers: default (2 x CPU) + 1, override with WORKER_COUNT
#  - worker_class: default 'gthread' override with GUNICORN_WORKER_CLASS (supports 'gthread' or 'sync')
#  - threads: when using gthread, from GUNICORN_THREADS (default 4)
#  - timeout: default 30s, override with GUNICORN_TIMEOUT
#  - preload_app: default True, override with GUNICORN_PRELOAD
#  - sensible keepalive/graceful_timeout/max_requests/max_requests_jitter with env overrides
#  - logging defaults to stdout/stderr (accesslog/errorlog = '-') and loglevel override via GUNICORN_LOG_LEVEL
#  - bind default 0.0.0.0:8000 override via GUNICORN_BIND
#  - capture_output option and process name
#  - lightweight lifecycle hooks that log via server.log (do not import app or models in master process)
#
# IMPORTANT:
# - This file is intentionally dependency-tolerant and defensive so Gunicorn can safely import it.
# - Do NOT import your Flask app or SQLAlchemy models here; doing so would run application code in the master process.
#
# -----------------------
# SQLAlchemy DB pool sizing notes (US-038) — operator guidance
#
# Relationship of Gunicorn workers/threads to DB connection pool sizing:
#  - Recommendation: SQLALCHEMY_POOL_SIZE >= (workers * threads) + 2
#    Rationale:
#      * Each worker can have up to `threads` concurrent request-handling threads.
#      * In the worst steady-state each of those threads could require one DB connection.
#      * Add +2 for short-lived admin/healthcheck/management connections and a small safety headroom.
#  - Example:
#      If WORKER_COUNT=5 and GUNICORN_WORKER_CLASS=gthread with GUNICORN_THREADS=4:
#        pool_size >= (5 * 4) + 2 = 22
#  - Alternatives:
#      * For short-lived, completely isolated processes (e.g., typical serverless) consider NullPool.
#      * For connection-reuse in long-running processes, use QueuePool (SQLAlchemy default). Set pool_size via
#        your app's SQLALCHEMY_POOL_SIZE config (or DATABASE_URL params) as needed.
#  - ENV guidance example: export SQLALCHEMY_POOL_SIZE=22
#  - Monitoring: track DB pool checkout metrics and connection errors (timeout/overflow). Adjust workers/threads or DB pool accordingly.
#
# Operator notes:
#  - preload_app=True (default) will call create_app() in the master process. This surfaces import/runtime failures early.
#    Ensure create_app() and any code it imports are safe to run in the master process (i.e., do not start background threads or open DB connections
#    unconditionally at import time).
#  - If your create_app() opens DB connections during app initialization you may want to use a lazy connection pattern or set preload_app=False.
# -----------------------

import os
import multiprocessing

# -----------------------
# Helper utilities (defensive parsing)
# -----------------------
def _safe_int(env_name, default):
    val = os.getenv(env_name)
    if val is None or val == "":
        return default
    try:
        ival = int(float(val))
        if ival < 0:
            return default
        return ival
    except Exception:
        # fallback to default for any parse error
        return default

def _safe_bool(env_name, default):
    val = os.getenv(env_name)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "t", "yes", "y", "on"):
        return True
    if v in ("0", "false", "f", "no", "n", "off"):
        return False
    return default

def _cpu_count_or_default(fallback=1):
    try:
        # Prefer multiprocessing.cpu_count (robust). Fall back to os.cpu_count.
        return max(1, int(multiprocessing.cpu_count()))
    except Exception:
        try:
            return max(1, int(os.cpu_count() or fallback))
        except Exception:
            return fallback

# -----------------------
# Core Gunicorn settings (tunable via env)
# -----------------------
_CPUS = _cpu_count_or_default(1)

# workers default: (2 x CPU) + 1
workers = _safe_int("WORKER_COUNT", (2 * _CPUS) + 1)

# worker class: default gthread, support 'gthread' and 'sync' (others will default to 'gthread')
_worker_class_env = os.getenv("GUNICORN_WORKER_CLASS", "gthread").strip().lower()
if _worker_class_env not in ("gthread", "sync"):
    # fallback to 'gthread' for compatibility/safety
    worker_class = "gthread"
else:
    worker_class = _worker_class_env

# threads only used for gthread workers
if worker_class == "gthread":
    threads = _safe_int("GUNICORN_THREADS", 4)
else:
    # threads is ignored for sync worker class, but defining it as 1 avoids surprises
    threads = 1

# timeouts and graceful shutdowns
timeout = _safe_int("GUNICORN_TIMEOUT", 30)  # seconds before a worker is killed
graceful_timeout = _safe_int("GUNICORN_GRACEFUL_TIMEOUT", 30)  # graceful shutdown period
keepalive = _safe_int("GUNICORN_KEEPALIVE", 2)  # seconds to hold keep-alive connections

# worker recycle settings to reduce memory bloat; jitter spreads restarts
max_requests = _safe_int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _safe_int("GUNICORN_MAX_REQUESTS_JITTER", 50)

# preload application in master by default (surfaces app import errors early). Override with env.
preload_app = _safe_bool("GUNICORN_PRELOAD", True)

# logging - send access/error logs to stdout/stderr by default ('-')
accesslog = os.getenv("GUNICORN_ACCESS_LOG", "-")
errorlog = os.getenv("GUNICORN_ERROR_LOG", "-")
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info").lower()

# bind address
bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# capture stdout/stderr and redirect to errorlog (True recommended for container logs)
capture_output = _safe_bool("GUNICORN_CAPTURE_OUTPUT", True)

# process name (useful for ps/systemd)
proc_name = os.getenv("GUNICORN_PROC_NAME", "gunicorn_app")

# worker temporary dir (use /dev/shm when available to reduce disk IO on temp files)
worker_tmp_dir = os.getenv("GUNICORN_WORKER_TMP_DIR", "/dev/shm")

# Other tunables (exposed via env as needed)
# Note: Gunicorn will only see top-level variables defined above that it recognizes.
# -----------------------
# Lifecycle hooks (lightweight, only logging via server.log)
# -----------------------
def on_starting(server):
    try:
        server.log.info("gunicorn.on_starting: pid=%s cpus=%s configured_workers=%s worker_class=%s threads=%s",
                        os.getpid(), _CPUS, workers, worker_class, threads)
    except Exception:
        # don't raise during import/starting
        pass

def when_ready(server):
    try:
        server.log.info("gunicorn.when_ready: master is ready. bind=%s preload_app=%s", bind, bool(preload_app))
        server.log.info("gunicorn.when_ready: logging accesslog=%s errorlog=%s level=%s", accesslog, errorlog, loglevel)
    except Exception:
        pass

def pre_fork(server, worker):
    # This runs in the master process just before forking a worker.
    # Do NOT import the application or models here.
    try:
        server.log.info("gunicorn.pre_fork: forking worker pid=%s ppid=%s worker_pid=%s", os.getpid(),
                        worker.pid if hasattr(worker, "pid") else "unknown", getattr(worker, "pid", "unknown"))
    except Exception:
        pass

def post_fork(server, worker):
    # This runs in the worker process after fork. Logging is safe.
    try:
        server.log.info("gunicorn.post_fork: worker spawned pid=%s", os.getpid())
    except Exception:
        pass

# End of gunicorn.conf.py
# --- END FILE ---