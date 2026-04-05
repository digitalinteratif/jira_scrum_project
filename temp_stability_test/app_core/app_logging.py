"""
Centralized logging initialization and helpers (KAN-159)

Provides:
 - init_logging(env_or_app=None) -> logging.Logger
 - get_logger(name=None) -> logging.Logger
 - module-level 'logger' variable (initialized lazily)
 - RequestIdFilter and attach_request_id_middleware(app) to attach per-request UUIDs
 - RedactFilter to mask known secret fields on LogRecord prior to formatting
"""

from __future__ import annotations

import logging
import logging.handlers
import json
import sys
import os
import time
import uuid
from typing import Optional, Any

# Defensive Flask imports (may run in CLI/test contexts)
try:
    from flask import g, request
    _has_flask = True
except Exception:
    g = None  # type: ignore
    request = None  # type: ignore
    _has_flask = False

_DEFAULT_LOG_NAME = os.environ.get("LOG_NAME", "smartlink")
_default_level = os.environ.get("LOG_LEVEL", "INFO").upper()


class JSONFormatter(logging.Formatter):
    """
    Minimal JSON formatter for structured logs.

    Serializes:
      - timestamp (ISO8601 UTC)
      - level
      - logger
      - message
      - message_key (if provided in record.__dict__ or record.args)
      - request_id (if present on record)
      - extra (other non-standard attributes)
      - exc_text when exc_info present (string)
    """

    def __init__(self, include_appname: bool = True, pretty: bool = False):
        super().__init__()
        self.include_appname = include_appname
        self.pretty = pretty

    def formatTime(self, record, datefmt=None):
        # ISO8601 UTC
        t = time.gmtime(record.created)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)

    def _extract_extra(self, record: logging.LogRecord):
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
            "message",
        }
        extras = {}
        for k, v in record.__dict__.items():
            if k in standard:
                continue
            if k.startswith("_"):
                continue
            try:
                # Attempt JSON serialization
                json.dumps({k: v}, default=str)
                extras[k] = v
            except Exception:
                try:
                    extras[k] = str(v)
                except Exception:
                    extras[k] = None
        return extras

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = {
                "timestamp": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }

            # message_key is commonly passed in extra
            message_key = getattr(record, "message_key", None)
            if not message_key:
                # also check args if dict-like pattern used
                try:
                    if isinstance(record.args, dict):
                        message_key = record.args.get("message_key")
                except Exception:
                    pass
            if message_key:
                payload["message_key"] = message_key

            # request id
            request_id = getattr(record, "request_id", None)
            if request_id:
                payload["request_id"] = request_id

            extras = self._extract_extra(record)
            if extras:
                payload["extra"] = extras

            # include exception text if present
            if record.exc_info:
                try:
                    payload["exc_text"] = self.formatException(record.exc_info)
                except Exception:
                    try:
                        payload["exc_text"] = str(record.exc_info)
                    except Exception:
                        payload["exc_text"] = "exception_serialization_failure"

            if self.pretty:
                return json.dumps(payload, indent=2, default=str, ensure_ascii=False)
            return json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)
        except Exception:
            # Fallback to default formatter behavior
            try:
                return super().format(record)
            except Exception:
                return record.getMessage()


class PlainFormatter(logging.Formatter):
    """
    Simple human-friendly plain text formatter that includes request_id/message_key when available.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
        parts = [ts, record.levelname, record.name, record.getMessage()]
        message_key = getattr(record, "message_key", None)
        if message_key:
            parts.append(f"key={message_key}")
        request_id = getattr(record, "request_id", None)
        if request_id:
            parts.append(f"rid={request_id}")
        s = " ".join(str(p) for p in parts)
        if record.exc_info:
            try:
                s = s + "\n" + self.formatException(record.exc_info)
            except Exception:
                s = s + "\n<exc-info>"
        return s


class RequestIdFilter(logging.Filter):
    """
    Logging filter that attaches request_id from flask.g (if available) to LogRecord.request_id.
    Defensive: safe when Flask is not present or outside request context.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if _has_flask:
                rid = None
                try:
                    rid = getattr(g, "request_id", None)
                except Exception:
                    rid = None
                record.request_id = rid
            else:
                record.request_id = None
        except Exception:
            record.request_id = None
        return True


class RedactFilter(logging.Filter):
    """
    Logging filter that masks/redacts known sensitive keys on the LogRecord.

    Behavior:
      - Looks for common secret keys in the LogRecord.__dict__ and masks them.
      - Masks values inside an 'extra' dict if present.
      - Keeps records lightweight and avoids leaking secrets to log sinks.

    Masking strategy:
      - For strings: preserve a short prefix (6 chars) then replace rest with '...[REDACTED]'
      - For non-strings: replace with '[REDACTED]'
    """

    REDACT_KEYS = {
        "password",
        "passwd",
        "pwd",
        "token",
        "verification_token",
        "jwt",
        "access_token",
        "refresh_token",
        "secret",
        "api_key",
        "authorization",
        "authorization_header",
        "credentials",
    }

    def _mask_value(self, v: Any) -> Any:
        try:
            if isinstance(v, str):
                if len(v) <= 10:
                    return "[REDACTED]"
                # preserve small prefix for debugging
                prefix = v[:6]
                return f"{prefix}...[REDACTED]"
            # for bytes
            if isinstance(v, (bytes, bytearray)):
                try:
                    s = v.decode("utf-8", errors="ignore")
                    return self._mask_value(s)
                except Exception:
                    return "[REDACTED]"
        except Exception:
            pass
        return "[REDACTED]"

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Mask direct attributes on record
            for key in list(record.__dict__.keys()):
                lk = key.lower()
                if lk in self.REDACT_KEYS:
                    try:
                        record.__dict__[key] = self._mask_value(record.__dict__[key])
                    except Exception:
                        record.__dict__[key] = "[REDACTED]"

            # Also handle nested 'extra' dict if present
            extra = record.__dict__.get("extra", None)
            if isinstance(extra, dict):
                for k in list(extra.keys()):
                    if k.lower() in self.REDACT_KEYS:
                        try:
                            extra[k] = self._mask_value(extra[k])
                        except Exception:
                            extra[k] = "[REDACTED]"
                # write back
                record.__dict__["extra"] = extra

            # Also check common places where user-supplied data might be added: args, message etc are kept but not modified here
        except Exception:
            # Never raise from a logging filter
            pass
        return True


_default_logger: Optional[logging.Logger] = None


def _coerce_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    try:
        s = str(val).strip().lower()
        return s in ("1", "true", "yes", "y", "on")
    except Exception:
        return default


def init_logging(env_or_app: Optional[Any] = None) -> logging.Logger:
    """
    Initialize structured logging.

    env_or_app: Flask app instance or dict-like config, or None to read from environment variables.

    Idempotent: safe to call multiple times.
    """
    global _default_logger

    # Resolve config
    cfg = {}
    if env_or_app is not None:
        try:
            # Flask app
            if hasattr(env_or_app, "config"):
                cfg = dict(env_or_app.config)
            elif isinstance(env_or_app, dict):
                cfg = env_or_app
        except Exception:
            cfg = {}

    # Env fallback
    def cfg_get(key: str, default=None):
        if key in cfg:
            return cfg.get(key)
        return os.environ.get(key, default)

    log_name = cfg_get("LOG_NAME", os.environ.get("LOG_NAME", _DEFAULT_LOG_NAME))
    log_level = str(cfg_get("LOG_LEVEL", os.environ.get("LOG_LEVEL", _default_level))).upper()
    json_mode = _coerce_bool(cfg_get("LOG_JSON", os.environ.get("LOG_JSON", "true")), True)
    pretty = _coerce_bool(cfg_get("LOG_PRETTY_PRINT", os.environ.get("LOG_PRETTY_PRINT", "false")), False)
    file_path = cfg_get("LOG_FILE", os.environ.get("LOG_FILE", ""))
    try:
        max_bytes = int(cfg_get("LOG_MAX_BYTES", os.environ.get("LOG_MAX_BYTES", str(10 * 1024 * 1024))))
    except Exception:
        max_bytes = 10 * 1024 * 1024
    try:
        backup_count = int(cfg_get("LOG_BACKUP_COUNT", os.environ.get("LOG_BACKUP_COUNT", "5")))
    except Exception:
        backup_count = 5
    include_appname = _coerce_bool(cfg_get("LOG_INCLUDE_APPNAME", os.environ.get("LOG_INCLUDE_APPNAME", "true")), True)
    level_no = getattr(logging, log_level, logging.INFO)

    # If previously initialized, update level and return
    if _default_logger is not None:
        try:
            _default_logger.setLevel(level_no)
            for h in _default_logger.handlers:
                h.setLevel(level_no)
            return _default_logger
        except Exception:
            pass

    logger = logging.getLogger(str(log_name))
    logger.setLevel(level_no)
    logger.propagate = False

    # Create handlers if none exist
    try:
        if not logger.handlers:
            # Stream handler
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setLevel(level_no)
            if json_mode:
                fmt = JSONFormatter(include_appname=include_appname, pretty=pretty)
            else:
                fmt = PlainFormatter()
            stream_handler.setFormatter(fmt)
            # Add RequestIdFilter to handler
            try:
                stream_handler.addFilter(RequestIdFilter())
            except Exception:
                pass
            # Add RedactFilter to handler
            try:
                stream_handler.addFilter(RedactFilter())
            except Exception:
                pass
            logger.addHandler(stream_handler)

            # Optional rotating file handler
            if file_path:
                try:
                    fh = logging.handlers.RotatingFileHandler(file_path, maxBytes=max_bytes, backupCount=backup_count)
                    fh.setLevel(level_no)
                    fh.setFormatter(fmt)
                    try:
                        fh.addFilter(RequestIdFilter())
                    except Exception:
                        pass
                    try:
                        fh.addFilter(RedactFilter())
                    except Exception:
                        pass
                    logger.addHandler(fh)
                except Exception:
                    # File handler failures should not prevent app from starting
                    logger.warning("LOG_INIT_WARNING: failed to attach file handler", extra={"message_key": "app.log_file_handler_failed", "path": file_path})
    except Exception:
        # Fallback to basic config if something goes wrong
        try:
            logging.basicConfig(stream=sys.stdout, level=level_no)
        except Exception:
            pass

    _default_logger = logger

    # If a Flask app was provided, optionally attach middleware
    if env_or_app is not None and hasattr(env_or_app, "before_request") and hasattr(env_or_app, "after_request"):
        try:
            attach_request_id_middleware(env_or_app)
        except Exception:
            # Do not raise on middleware attach failure
            logger.debug("LOG_INIT: request id middleware attach failed", extra={"message_key": "app.request_id_middleware_failed"})

    return _default_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Return the configured logger or initialize with defaults.
    """
    global _default_logger
    if _default_logger is None:
        init_logging(None)
    if name:
        return logging.getLogger(name)
    return _default_logger  # type: ignore


def attach_request_id_middleware(app) -> None:
    """
    Attach simple request-id middleware to Flask app.

    Behavior:
      - Reads header LOG_REQUEST_ID_HEADER (default X-Request-ID)
      - If absent and LOG_GENERATE_REQUEST_ID true, generates a uuid4 hex
      - Stores value in flask.g.request_id and sets response header
    Defensive (no-op when Flask not present).
    """
    if not _has_flask:
        return

    header = app.config.get("LOG_REQUEST_ID_HEADER", os.environ.get("LOG_REQUEST_ID_HEADER", "X-Request-ID"))
    generate = app.config.get("LOG_GENERATE_REQUEST_ID", _coerce_bool(os.environ.get("LOG_GENERATE_REQUEST_ID", "true"), True))

    @app.before_request
    def _set_request_id():
        try:
            rid = None
            if header and request:
                rid = request.headers.get(header)
            if not rid and generate:
                rid = uuid.uuid4().hex
            try:
                setattr(g, "request_id", rid)
            except Exception:
                pass
        except Exception:
            # Best-effort only
            try:
                setattr(g, "request_id", None)
            except Exception:
                pass

    @app.after_request
    def _prop_request_id(response):
        try:
            rid = None
            try:
                rid = getattr(g, "request_id", None)
            except Exception:
                rid = None
            if rid and header:
                try:
                    response.headers[header] = rid
                except Exception:
                    pass
        except Exception:
            pass
        return response


# Module-level logger for convenience importers
try:
    logger = get_logger(None)
except Exception:
    # Final fallback to root logger
    logger = logging.getLogger(_DEFAULT_LOG_NAME)
    logger.setLevel(logging.INFO)

# End of app_core.app_logging.py