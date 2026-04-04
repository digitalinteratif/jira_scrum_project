"""utils/logging.py - Structured logging init + rotation recommendations for KAN-141

Responsibilities:
 - Initialize a structured JSON logger for the application (stdout + optional rotating file).
 - Provide a small JSONFormatter to ensure consistent JSON-serializable log records.
 - Be defensive: missing modules or file-system issues must not raise during app.init.
 - Expose get_logger(name) for importers to use the configured logger.

Configuration (via Flask app.config or environment):
 - LOG_LEVEL (default: "INFO")
 - LOG_FILE (optional): if set, a RotatingFileHandler is attached to write logs to this file.
 - LOG_MAX_BYTES (int, default 10*1024*1024) - rotate when file exceeds this many bytes
 - LOG_BACKUP_COUNT (int, default 5) - number of rotated files to keep
 - LOG_JSON (bool, default True) - whether to emit JSON lines (set False to use plain-text formatter)
 - LOG_INCLUDE_APPNAME (bool, default True) - include app name in records
 - SENTRY_DSN handled in app.create_app (this module does not initialize Sentry)

Notes:
 - This module writes a best-effort trace to trace_KAN-141.txt indicating initialization success/failure.
"""

from __future__ import annotations
import os
import sys
import json
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

_TRACE_FILE = "trace_KAN-141.txt"


def _trace(msg: str):
    try:
        with open(_TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # best-effort only
        pass


class JSONFormatter(logging.Formatter):
    """
    Minimal JSON formatter for structured logs.

    It serializes a dictionary containing:
      - timestamp (RFC3339-ish via isoformat)
      - level
      - logger
      - message
      - request_id (if present on record)
      - extra fields (if dict in record.args or record.__dict__ entries)
    """

    def __init__(self, include_appname: bool = True):
        super().__init__()
        self.include_appname = include_appname

    def format(self, record: logging.LogRecord) -> str:
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            payload = {
                "timestamp": ts,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # include request_id if set on record
            rid = getattr(record, "request_id", None)
            if rid:
                payload["request_id"] = rid

            # Attach any extra keys in record.__dict__ that are not standard
            standard = {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "stack_info",
                "exc_info",
                "exc_text",
            }
            extras = {}
            for k, v in record.__dict__.items():
                if k not in standard and not k.startswith("_"):
                    try:
                        # ensure JSON serializable
                        json.dumps({k: v})
                        extras[k] = v
                    except Exception:
                        try:
                            extras[k] = str(v)
                        except Exception:
                            extras[k] = None
            if extras:
                payload["extra"] = extras

            return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            # Fallback to plain message on failure
            try:
                return super().format(record)
            except Exception:
                return record.getMessage()


_default_logger = None


def init_logging(app=None):
    """
    Initialize structured logging for the given Flask app (or global env if app is None).
    Safe to call multiple times (idempotent-ish).
    """
    global _default_logger
    try:
        cfg = getattr(app, "config", {}) if app is not None else {}
    except Exception:
        cfg = {}

    # Resolve configuration with fallbacks
    LOG_LEVEL = str(cfg.get("LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO"))).upper()
    LOG_FILE = cfg.get("LOG_FILE", os.environ.get("LOG_FILE", ""))
    try:
        LOG_MAX_BYTES = int(cfg.get("LOG_MAX_BYTES", int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024))))
    except Exception:
        LOG_MAX_BYTES = 10 * 1024 * 1024
    try:
        LOG_BACKUP_COUNT = int(cfg.get("LOG_BACKUP_COUNT", int(os.environ.get("LOG_BACKUP_COUNT", 5))))
    except Exception:
        LOG_BACKUP_COUNT = 5
    LOG_JSON = bool(cfg.get("LOG_JSON", os.environ.get("LOG_JSON", "true").lower() in ("1", "true", "yes")))
    INCLUDE_APPNAME = bool(cfg.get("LOG_INCLUDE_APPNAME", True))

    try:
        logger = logging.getLogger(cfg.get("LOG_NAME", "smartlink"))
        logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        # Avoid duplicate handlers if called multiple times
        if not logger.handlers:
            fmt = JSONFormatter(include_appname=INCLUDE_APPNAME) if LOG_JSON else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            # Stream handler -> stdout for container-friendly logs
            stream = logging.StreamHandler(sys.stdout)
            stream.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
            stream.setFormatter(fmt)
            logger.addHandler(stream)

            # Optional file handler with rotation
            if LOG_FILE:
                try:
                    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
                    fh.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
                    fh.setFormatter(fmt)
                    logger.addHandler(fh)
                except Exception as e:
                    _trace(f"LOG_INIT_FILEHANDLER_FAILED err={str(e)}")

        _default_logger = logger
        _trace("LOG_INIT_SUCCESS")
    except Exception as e:
        # Give up gracefully: fall back to basic logging to stderr
        try:
            logging.basicConfig(level=logging.INFO)
            _default_logger = logging.getLogger("smartlink_fallback")
            _trace(f"LOG_INIT_FALLBACK err={str(e)}")
        except Exception:
            pass

    return _default_logger


def get_logger(name: Optional[str] = None):
    """
    Return the configured logger. If init_logging has not been called, attempt to initialize with defaults.
    """
    global _default_logger
    if _default_logger is None:
        init_logging(None)
    if name:
        return logging.getLogger(name)
    return _default_logger
--- END FILE: utils/logging.py ---