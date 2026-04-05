"""
Centralized error handlers and ValidationError integration (KAN-181)

Registers HTML error pages for 400, 401, 404, 500 and a ValidationError handler
that delegates to app_core.utils.web_helpers.render_with_error when endpoints
supply a template_renderer via ValidationError.extra.

This module provides:
 - register_error_handlers(app)
 - small get_prefill_from_request(request) utility to produce an allowlisted prefill dict
"""

from __future__ import annotations

import typing as t
from flask import request, render_template, current_app
from werkzeug.exceptions import HTTPException

# Defensive imports for logging and helpers
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)

# Use the existing sanitize + render helper when available
try:
    from app_core.utils.web_helpers import render_with_error, _sanitize_prefill_for_template  # type: ignore
except Exception:
    # Fallback: implement a minimal sanitizer and a stub render_with_error
    def _sanitize_prefill_for_template(prefill: t.Optional[dict]) -> dict:
        allowed = {"email", "name", "username"}
        out = {}
        if not prefill or not isinstance(prefill, dict):
            return out
        for k, v in prefill.items():
            if k in allowed:
                out[k] = v
        return out

    def render_with_error(template_renderer, message: str, prefill: t.Optional[dict] = None, status_code: int = 400, message_key: t.Optional[str] = None):
        # Best-effort fallback: try to call template_renderer, else render basic template page
        safe_prefill = _sanitize_prefill_for_template(prefill)
        try:
            rendered = template_renderer(prefill=safe_prefill, error=message)
            return rendered, int(status_code)
        except Exception:
            try:
                return render_template(f"errors/{status_code}.html", message=message, prefill=safe_prefill), int(status_code)
            except Exception:
                return (f"<h1>Error</h1><p>{message}</p>"), int(status_code)


# ValidationError from project
try:
    from app_core.utils.errors import ValidationError  # type: ignore
except Exception:
    class ValidationError(Exception):  # type: ignore
        def __init__(self, message="validation error", status_code=400, field=None, extra=None):
            super().__init__(message)
            self.message = message
            self.status_code = status_code
            self.field = field
            self.extra = extra or {}

# Allowed keys for prefill returned to templates
_ALLOWED_PREFILL_KEYS = {"email", "name", "username", "target_url", "url"}


def _get_prefill_from_request() -> dict:
    """
    Build a small prefill dict from request context using an allow-list.
    Avoid returning sensitive fields (passwords, tokens).
    """
    prefill = {}
    try:
        # Prefer any explicit 'prefill' set on request (endpoints may set request._prefill)
        rp = getattr(request, "_prefill", None)
        if isinstance(rp, dict):
            for k in _ALLOWED_PREFILL_KEYS:
                if k in rp:
                    prefill[k] = rp.get(k)
            return prefill
    except Exception:
        # ignore and proceed to form fallback
        pass

    try:
        form = request.form or {}
        if hasattr(form, "items"):
            for k in _ALLOWED_PREFILL_KEYS:
                v = form.get(k)
                if v is not None:
                    prefill[k] = v
    except Exception:
        pass

    return prefill


def _log_client_error(status_code: int, message: str = "", exc: Exception | None = None) -> None:
    try:
        extra = {
            "message_key": f"http.{status_code}",
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
        }
        # Avoid logging user-submitted values; log only keys present
        try:
            keys = list(request.form.keys()) if request and request.form else []
            extra["form_keys"] = keys
        except Exception:
            extra["form_keys"] = None
        if status_code >= 500:
            logger.exception(message or f"HTTP {status_code} error", extra=extra)
        else:
            logger.info(message or f"HTTP {status_code} response", extra=extra)
    except Exception:
        # never raise from logging helper
        try:
            logger.info("Logging client error failed for status %s", status_code)
        except Exception:
            pass


def handle_400(error):
    """
    Handler for 400 Bad Request.
    Prefer to use a template errors/400.html with message and prefill.
    """
    message = "Bad request. Please correct the highlighted fields and try again."
    _log_client_error(400, "Bad Request returned to client", error)
    safe_prefill = _get_prefill_from_request()
    try:
        return render_template("errors/400.html", message=message, prefill=safe_prefill), 400
    except Exception:
        # Fallback simple response
        return f"<h1>400 Bad Request</h1><p>{message}</p>", 400


def handle_401(error):
    """
    Handler for 401 Unauthorized.
    """
    message = "You must be signed in to access this resource."
    _log_client_error(401, "Unauthorized access attempted", error)
    safe_prefill = _get_prefill_from_request()
    try:
        return render_template("errors/401.html", message=message, prefill=safe_prefill), 401
    except Exception:
        return f"<h1>401 Unauthorized</h1><p>{message}</p>", 401


def handle_404(error):
    """
    Handler for 404 Not Found.
    """
    message = "We could not find that page or resource."
    _log_client_error(404, "Not Found served", error)
    safe_prefill = _get_prefill_from_request()
    try:
        return render_template("errors/404.html", message=message, prefill=safe_prefill), 404
    except Exception:
        return f"<h1>404 Not Found</h1><p>{message}</p>", 404


def handle_500(error):
    """
    Handler for 500 Internal Server Error.
    Logs exception stacktrace but returns a friendly page without exposing internals.
    """
    # Log full exception with stacktrace
    try:
        extra = {
            "message_key": "http.500",
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
        }
        # Truncate form keys only
        try:
            extra["form_keys"] = list(request.form.keys()) if request and request.form else []
        except Exception:
            extra["form_keys"] = None
        logger.exception("Unhandled exception in request, returning 500", extra=extra)
    except Exception:
        try:
            logger.exception("Unhandled exception; logging failed")
        except Exception:
            pass

    user_message = "An unexpected server error occurred. Our team has been notified."
    try:
        return render_template("errors/500.html", message=user_message, prefill={}), 500
    except Exception:
        return f"<h1>500 Server Error</h1><p>{user_message}</p>", 500


def handle_validation_error(exc: ValidationError):
    """
    Catch project ValidationError and render using render_with_error when a template_renderer is provided.
    Fallback to a status-code specific errors/<status>.html.
    """
    # Compose safe prefill and message
    msg = getattr(exc, "message", str(exc))
    status = int(getattr(exc, "status_code", 400) or 400)
    extra = getattr(exc, "extra", {}) or {}
    # If template_renderer provided, use the centralized helper which will sanitize prefill
    template_renderer = None
    try:
        if isinstance(extra, dict):
            template_renderer = extra.get("template_renderer")
            prefill = extra.get("prefill")
            message_key = extra.get("message_key")
        else:
            prefill = None
            message_key = None
    except Exception:
        template_renderer = None
        prefill = None
        message_key = None

    # Log the validation failure (non-sensitive)
    try:
        log_extra = {
            "message_key": message_key or "validation.failure",
            "path": getattr(request, "path", None),
            "method": getattr(request, "method", None),
            "prefill_keys": list(prefill.keys()) if isinstance(prefill, dict) else []
        }
        logger.warning(f"Validation failure: {msg}", extra=log_extra)
    except Exception:
        try:
            logger.warning("Validation failure (logging fallback): %s", msg)
        except Exception:
            pass

    if callable(template_renderer):
        try:
            rendered, code = render_with_error(template_renderer, msg, prefill=prefill, status_code=status, message_key=message_key)
            return rendered, code
        except Exception as e:
            # Fall through to template fallback
            logger.exception("render_with_error failed while handling ValidationError", extra={"message_key": "validation.render_failed"})
    # Fallback
    try:
        safe_prefill = _get_prefill_from_request()
        return render_template(f"errors/{status}.html", message=msg, prefill=safe_prefill), status
    except Exception:
        return (f"<h1>{status} Error</h1><p>{msg}</p>"), status


def register_error_handlers(app):
    """
    Register our handlers onto the provided Flask app. Safe to call once after blueprint registration.
    """
    if not hasattr(app, "register_error_handler"):
        return

    # Register handlers
    try:
        app.register_error_handler(ValidationError, handle_validation_error)
    except Exception:
        # Be conservative: still attempt HTTP code handlers
        try:
            logger.exception("Failed to register ValidationError handler")
        except Exception:
            pass

    try:
        app.register_error_handler(400, handle_400)
        app.register_error_handler(401, handle_401)
        app.register_error_handler(404, handle_404)
        app.register_error_handler(500, handle_500)
    except Exception:
        try:
            logger.exception("Failed to register HTTP error handlers")
        except Exception:
            pass