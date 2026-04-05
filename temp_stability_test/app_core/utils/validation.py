"""app_core.utils.validation - input validation helpers and safe delegates.

Provides:
 - validate_and_normalize_url(raw_url, *, allow_private=None, enable_dns=None) -> str
     Delegates to app_core.utils.url_utils.normalize_url when available.
 - validate_url alias
 - validate_short_code(short_code: str) -> bool
 - log_suspicious_input(field: str, value: str, route: Optional[str] = None, extra: Optional[dict] = None)
"""

from __future__ import annotations

import re
import logging
from typing import Optional, Any
from html import escape

# Defensive Flask imports
try:
    from flask import request, g
    _has_flask = True
except Exception:
    request = None  # type: ignore
    g = None  # type: ignore
    _has_flask = False

# Project logger (defensive)
try:
    from app_core.app_logging import get_logger
    _logger = get_logger(__name__)
except Exception:
    _logger = logging.getLogger(__name__)

# Default strict pattern for path-level short codes (KAN-180)
_DEFAULT_STRICT_SLUG_REGEX = r"^[A-Za-z0-9]{1,16}$"

# ---------------------------------------------------------------------
# URL validation delegation (KAN-177 compatibility)
# ---------------------------------------------------------------------
# If url_utils available, delegate to that canonical implementation to preserve
# normalization & policy semantics across the codebase.
try:
    from app_core.utils import url_utils as _url_utils  # type: ignore

    def validate_and_normalize_url(raw_url: str, *, allow_private: Optional[bool] = None, enable_dns: Optional[bool] = None) -> str:
        """
        Compatibility wrapper delegating to url_utils.normalize_url.

        Parameters:
          - allow_private: if True allows private IPs (maps to reject_private_ips inversely)
          - enable_dns: when True triggers DNS resolution during normalization
        """
        # Map allow_private -> reject_private_ips (invert semantics)
        if allow_private is None:
            reject_private = None
        else:
            try:
                reject_private = not bool(allow_private)
            except Exception:
                reject_private = None

        return _url_utils.normalize_url(raw_url, reject_private_ips=reject_private, enable_dns=enable_dns)

    validate_url = validate_and_normalize_url
except Exception:
    # If url_utils unavailable, fall back to a minimal placeholder that raises
    def validate_and_normalize_url(raw_url: str, *, allow_private: Optional[bool] = None, enable_dns: Optional[bool] = None) -> str:
        raise RuntimeError("URL validation backend unavailable")

    validate_url = validate_and_normalize_url

# ---------------------------------------------------------------------
# Short code / slug validation and suspicious input logging
# ---------------------------------------------------------------------
def validate_short_code(short_code: str, *, pattern: Optional[str] = None) -> bool:
    """
    Strict path-level short_code validator used for public redirect path params.

    Default pattern: r'^[A-Za-z0-9]{1,16}$' (alphanumeric only, 1-16 chars).
    Returns True when short_code fully matches the pattern.
    """
    if not isinstance(short_code, str):
        return False
    pat = pattern or _DEFAULT_STRICT_SLUG_REGEX
    try:
        return bool(re.fullmatch(pat, short_code))
    except Exception:
        # On malformed regex fallback to safe conservative check: alnum 1..16
        try:
            return bool(re.fullmatch(_DEFAULT_STRICT_SLUG_REGEX, short_code))
        except Exception:
            return False


def _truncate_for_log(val: Optional[str], max_len: int = 256) -> str:
    """
    Truncate values for logging to avoid leaking large or sensitive payloads.
    """
    if val is None:
        return ""
    s = str(val)
    if len(s) <= max_len:
        return s
    # Keep prefix + suffix hash-like snippet
    try:
        import hashlib
        h = hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return s[: max_len // 2] + "...[" + h + "]"
    except Exception:
        return s[:max_len] + "..."

def log_suspicious_input(field: str, value: Any, route: Optional[str] = None, extra: Optional[dict] = None) -> None:
    """
    Log suspicious or malformed user input at WARNING level with structured extras.

    Do not include entire values; truncate to avoid logging secrets (passwords).
    """
    try:
        value_snip = _truncate_for_log(str(value))
        req_path = None
        req_id = None
        remote = None
        try:
            if _has_flask and request is not None:
                req_path = getattr(request, "path", None)
                try:
                    remote = request.remote_addr
                except Exception:
                    remote = None
            if _has_flask and g is not None:
                req_id = getattr(g, "request_id", None)
        except Exception:
            pass

        payload = {
            "message_key": "security.suspicious_input",
            "field": field,
            "value_snip": value_snip,
            "route": route or req_path,
            "remote_addr": remote,
            "request_id": req_id,
        }
        if extra:
            # attach non-sensitive extras conservatively
            try:
                payload["extra"] = dict(extra)
            except Exception:
                payload["extra"] = str(extra)

        try:
            _logger.warning(f"Suspicious input detected on field '{field}'", extra=payload)
        except Exception:
            # fallback plain logging
            _logger.warning("Suspicious input detected: field=%s route=%s", field, route or req_path)
    except Exception:
        # Never raise from logging helper
        try:
            _logger.debug("log_suspicious_input failed for field=%s", field)
        except Exception:
            pass

# Exported names
__all__ = [
    "validate_and_normalize_url",
    "validate_url",
    "validate_short_code",
    "log_suspicious_input",
]
--- END FILE ---