"""app_core.utils.web_helpers - helpers to render templates with standardized error reporting (KAN-171)"""

from __future__ import annotations
from typing import Callable, Optional, Dict, Any, Iterable
from flask import request
from html import escape

# Defensive import of project logger
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)


# Allowed prefill keys that may be echoed back to the template and logged (safe fields only)
_ALLOWED_PREFILL_KEYS = {"email", "name", "username"}


def _sanitize_prefill_for_template(prefill: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return a copy of prefill containing only allowed keys. Values are left as-is for templates
    (templates should HTML-escape). This function ensures sensitive keys (like 'password') are not returned.
    """
    out = {}
    if not prefill:
        return out
    try:
        for k, v in prefill.items():
            if k in _ALLOWED_PREFILL_KEYS:
                out[k] = v
    except Exception:
        # Defensive: return empty on unexpected structure
        return {}
    return out


def _sanitize_prefill_keys_for_log(prefill: Optional[Dict[str, Any]]) -> Iterable[str]:
    if not prefill:
        return []
    try:
        return [k for k in prefill.keys() if k in _ALLOWED_PREFILL_KEYS]
    except Exception:
        return []


def render_with_error(template_renderer: Callable[..., str],
                      message: str,
                      prefill: Optional[Dict[str, Any]] = None,
                      status_code: int = 400,
                      message_key: Optional[str] = None) -> (str, int):
    """
    Render a template (via template_renderer) and prepare a standardized response for validation failures.

    Parameters:
      - template_renderer: callable used to render template. Expected signature accepts kwargs including:
          * prefill (dict) OR explicit named safe params like 'email', and 'error' (string)
        The helper will attempt to call template_renderer(prefill=safe_prefill, error=message)
        and fall back to calling with named safe keys (email=..., error=...).
      - message: user-facing message (safe to display)
      - prefill: dict of safe fields to re-populate the form (only keys in _ALLOWED_PREFILL_KEYS are preserved)
      - status_code: HTTP status code to return
      - message_key: optional logging message_key for structured logs

    Returns:
      (rendered_html, status_code)
    """
    safe_prefill = _sanitize_prefill_for_template(prefill)
    log_prefill_keys = _sanitize_prefill_keys_for_log(prefill)

    # Structured logging of validation failure (non-sensitive)
    try:
        extra = {
            "message_key": message_key or "validation.failure",
            "route": getattr(request, "path", None),
            "method": getattr(request, "method", None),
            "remote_addr": getattr(request, "remote_addr", None),
            "prefill_keys": log_prefill_keys,
        }
        logger.warning(f"Validation failure: {message}", extra=extra)
    except Exception:
        try:
            logger.warning("Validation failure (logging failed to include structured extras): %s", message)
        except Exception:
            pass

    # Attempt rendering with two common calling styles:
    # 1) template_renderer(prefill=safe_prefill, error=message)
    # 2) template_renderer(email=safe_prefill.get('email',''), name=..., error=message)
    try:
        rendered = template_renderer(prefill=safe_prefill, error=message)
        return rendered, int(status_code)
    except TypeError:
        # Try fallback invocation with explicit fields
        try:
            kwargs = dict(error=message)
            # inject allowed keys as named args if supported
            for k in _ALLOWED_PREFILL_KEYS:
                if k in safe_prefill:
                    kwargs[k] = safe_prefill[k]
            rendered = template_renderer(**kwargs)
            return rendered, int(status_code)
        except Exception:
            # Last resort: try calling without prefill and just pass error
            try:
                rendered = template_renderer(error=message)
                return rendered, int(status_code)
            except Exception:
                # Give up: return plain text fallback
                try:
                    fallback = f"<p style='color:#b00020;'>{escape(str(message))}</p>"
                    return fallback, int(status_code)
                except Exception:
                    return str(message), int(status_code)
    except Exception:
        # Unexpected render error: return safe fallback
        try:
            fallback = f"<p style='color:#b00020;'>{escape(str(message))}</p>"
            return fallback, int(status_code)
        except Exception:
            return str(message), int(status_code)
--- END FILE ---