from __future__ import annotations

import re
from typing import Optional

from flask import current_app, redirect, request

# Defensive logger import
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)

# DB helpers (sqlite helper preferred)
try:
    from app_core.db import get_db_connection, get_url_by_short_code
except Exception:
    get_db_connection = None
    get_url_by_short_code = None

# Validation helper for short_code and suspicious logging
try:
    from app_core.utils.validation import validate_short_code, log_suspicious_input
except Exception:
    validate_short_code = None  # type: ignore
    def log_suspicious_input(*args, **kwargs):  # type: ignore
        pass

# Fallback render helper
try:
    from app_core.routes.home import render_layout
except Exception:
    try:
        from utils.templates import render_layout  # type: ignore
    except Exception:
        # Minimal fallback
        def render_layout(content: str):
            return f"<html><body>{content}</body></html>"


def _validate_short_code(short_code: str) -> bool:
    """
    Validate short_code using app config REDIRECT_SLUG_REGEX and REDIRECT_SLUG_MAX_LEN.
    New: support strict enforcement via REDIRECT_ENFORCE_STRICT_SLUG and internal validate_short_code.

    Behavior:
      - If REDIRECT_ENFORCE_STRICT_SLUG true -> use validate_short_code() with strict default (alphanumeric only).
      - Else use configured REDIRECT_SLUG_REGEX if present (defensive).
    """
    if not isinstance(short_code, str) or short_code == "":
        return False
    try:
        max_len = int(current_app.config.get("REDIRECT_SLUG_MAX_LEN", 16))
    except Exception:
        max_len = 16
    if len(short_code) > max_len:
        return False

    try:
        enforce_strict = bool(current_app.config.get("REDIRECT_ENFORCE_STRICT_SLUG", False))
    except Exception:
        enforce_strict = False

    if enforce_strict and validate_short_code is not None:
        try:
            ok = validate_short_code(short_code)
            return bool(ok)
        except Exception:
            return False

    # Fallback to config regex if present
    pattern = current_app.config.get("REDIRECT_SLUG_REGEX", r"[A-Za-z0-9\-_]{1,16}")
    try:
        return bool(re.fullmatch(pattern, short_code))
    except Exception:
        # Defensive fallback: accept only ASCII alnum and -_
        try:
            return bool(re.fullmatch(r"[A-Za-z0-9\-_]{1,%d}" % max_len, short_code))
        except Exception:
            return False


def _validate_stored_destination(dest: Optional[str]) -> bool:
    """
    Sanity-check the stored original_url. Prefer using utils.validation.validate_and_normalize_url if available.
    Returns True if destination is acceptable for redirecting.
    """
    if not dest or not isinstance(dest, str):
        return False
    # Prefer canonical validator when available
    try:
        from utils.validation import validate_and_normalize_url  # type: ignore
        try:
            # validate; we ignore returned normalized value (we will redirect to stored string)
            validate_and_normalize_url(dest)
            return True
        except Exception:
            return False
    except Exception:
        # Fallback conservative check: must start with http:// or https://
        try:
            return dest.startswith("http://") or dest.startswith("https://")
        except Exception:
            return False


def redirect_short_code(short_code: str):
    """
    Public redirect handler for GET /<short_code>.

    Behavior:
      - Validate short_code pattern; return 400 on malformed.
      - Lookup Urls mapping using app_core.db.get_url_by_short_code (preferred).
      - If found: sanity-check stored original_url and issue redirect using app.config['REDIRECT_CODE'] (default 302).
      - If not found: render friendly 404 page.
      - All important events are logged with structured extras.
    """
    # Validate input slug
    if not _validate_short_code(short_code):
        try:
            log_suspicious_input(field="short_code", value=short_code, route=getattr(request, "path", None), extra={"note": "malformed_short_code"})
            logger.warning("Redirect malformed input", extra={"message_key": "redirector.malformed", "short_code": short_code})
        except Exception:
            pass
        return render_layout("<h1>Bad Request</h1><p>Invalid short link.</p>"), 400

    # Attempt DB lookup via sqlite helper first (preferred)
    row = None
    try:
        if get_db_connection is not None and get_url_by_short_code is not None:
            try:
                with get_db_connection() as conn:
                    row = get_url_by_short_code(conn, short_code)
            except ValueError:
                # Non-sqlite DATABASE_URL configured; fall through to SQLAlchemy fallback
                row = None
    except Exception:
        # Log and proceed to fallback
        try:
            logger.exception("Error during sqlite DB lookup for redirect", extra={"message_key": "redirector.db_lookup_error", "short_code": short_code})
        except Exception:
            pass
        row = None

    # SQLAlchemy fallback if sqlite helper unavailable or returned None due to non-sqlite env
    if row is None:
        try:
            import models  # type: ignore
            Session = getattr(models, "Session", None)
            if Session:
                s = Session()
                try:
                    r = s.query(models.ShortURL).filter_by(slug=short_code).first()
                    if r:
                        row = {"id": getattr(r, "id", None), "short_code": getattr(r, "slug", None), "original_url": getattr(r, "target_url", None), "owner_user_id": getattr(r, "user_id", None)}
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
        except Exception:
            # If SQLAlchemy fallback fails we log and surface a 500
            try:
                logger.exception("SQLAlchemy fallback lookup failed for redirect", extra={"message_key": "redirector.db_fallback_error", "short_code": short_code})
            except Exception:
                pass
            return render_layout("<h1>Server Error</h1><p>Unable to lookup short link. Please try again later.</p>"), 500

    if not row:
        try:
            logger.info("Redirect missing", extra={"message_key": "redirector.missing", "short_code": short_code})
        except Exception:
            pass
        return render_layout("<h1>Not Found</h1><p>The requested short link does not exist.</p>"), 404

    dest = row.get("original_url", "")
    # Validate stored destination to avoid open-redirects / unsafe schemes
    if not _validate_stored_destination(dest):
        try:
            logger.error("Invalid stored redirect destination", extra={"message_key": "redirector.invalid_destination", "short_code": short_code, "destination": dest})
        except Exception:
            pass
        # Operational data integrity issue -> return 500 (do not redirect)
        return render_layout("<h1>Server Error</h1><p>Destination for this short link is invalid. Please contact support.</p>"), 500

    # Issue redirect with configured code
    try:
        code = int(current_app.config.get("REDIRECT_CODE", 302))
    except Exception:
        code = 302

    try:
        logger.info("Redirecting short link", extra={"message_key": "redirector.redirect", "short_code": short_code, "destination": dest})
    except Exception:
        pass

    # Perform redirect (Flask will set Location header)
    return redirect(dest, code=code)
--- END FILE ---