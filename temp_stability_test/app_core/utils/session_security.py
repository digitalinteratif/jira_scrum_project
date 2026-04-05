from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, Optional

from flask import current_app, redirect, request, session, url_for, jsonify

# Defensive logger import
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)


def _now_epoch() -> int:
    return int(time.time())


def _get_lifetime_seconds() -> int:
    """
    Resolve PERMANENT_SESSION_LIFETIME to integer seconds defensively.
    """
    try:
        lifetime = current_app.config.get("PERMANENT_SESSION_LIFETIME")
        if hasattr(lifetime, "total_seconds"):
            return int(lifetime.total_seconds())
        return int(lifetime)
    except Exception:
        # default 7 days
        return 7 * 24 * 3600


def _is_json_request() -> bool:
    accept = request.headers.get("Accept", "") or ""
    return "application/json" in accept.lower()


def _logout_and_respond(expired_reason: str = "session_expired"):
    """
    Clear session and return a redirect to login for HTML or JSON 401 when Accept header indicates JSON.
    """
    try:
        user_id = session.get("user_id")
    except Exception:
        user_id = None
    try:
        session.clear()
    except Exception:
        try:
            logger.exception("Failed to clear session during expiry handling", extra={"message_key": "session.clear_failed"})
        except Exception:
            pass

    try:
        logger.info("Session expired/cleared", extra={"message_key": "session.expired", "reason": expired_reason, "user_id": user_id, "path": request.path})
    except Exception:
        pass

    if _is_json_request():
        return jsonify({"error": expired_reason}), 401
    # fallback to redirect to login page
    try:
        return redirect(url_for("auth.login"))
    except Exception:
        # safe fallback
        return redirect("/login")


def init_session_security(app) -> None:
    """
    Attach a before_request handler that enforces server-side session inactivity expiry and
    implements optional sliding expiration.

    Must be called with the Flask app instance during app initialization.
    """
    if not hasattr(app, "before_request"):
        return

    protected_prefixes = app.config.get("SESSION_PROTECTED_PREFIXES", ["/dashboard", "/shorten", "/analytics", "/domains", "/sessions"])

    @app.before_request
    def _session_activity_check():
        """
        Enforce session lifetime for protected endpoints.

        Behavior:
          - If request path does not start with any protected prefix, skip check.
          - If session['user_id'] missing: skip (not authenticated).
          - If last_activity missing and SESSION_ALLOW_LEGACY_SESSIONS False -> expire immediately.
          - If now - last_activity > PERMANENT_SESSION_LIFETIME -> clear and respond (redirect or 401).
          - If not expired and SESSION_SLIDING_EXPIRATION True -> update session['last_activity'] and mark session.modified = True.
        Defensive: any internal errors are logged and do not block request processing.
        """
        try:
            path = (request.path or "")
            # Skip static and health endpoints, etc. Only enforce for configured protected prefixes.
            if not any(path.startswith(p) for p in protected_prefixes):
                return None

            # Only enforce for requests that carry session-based auth
            try:
                user_present = bool(session.get("user_id"))
            except Exception:
                user_present = False

            if not user_present:
                # Not authenticated via server-side session: nothing to enforce here.
                return None

            now = _now_epoch()
            last = session.get("last_activity")

            allow_legacy = bool(app.config.get("SESSION_ALLOW_LEGACY_SESSIONS", False))
            if last is None:
                if not allow_legacy:
                    # Fail-closed: expire session
                    try:
                        logger.info("Session missing last_activity -> expired", extra={"message_key": "session.missing_last_activity", "user_id": session.get("user_id"), "path": path})
                    except Exception:
                        pass
                    return _logout_and_respond("session_missing_last_activity")
                else:
                    # Transitional: set last_activity to now and continue
                    try:
                        session["last_activity"] = now
                        session.modified = True
                    except Exception:
                        pass
                    return None

            try:
                last_int = int(last)
            except Exception:
                last_int = now

            lifetime_secs = _get_lifetime_seconds()
            if (now - last_int) > int(lifetime_secs):
                # expired
                return _logout_and_respond("session_inactive_timeout")

            # sliding expiration: refresh last_activity on activity if enabled
            try:
                sliding = bool(app.config.get("SESSION_SLIDING_EXPIRATION", True))
            except Exception:
                sliding = True

            if sliding:
                # Optionally limit which methods refresh activity; keep conservative set
                try:
                    refresh_on = app.config.get("SESSION_SLIDING_REFRESH_METHODS", ("GET", "POST", "PUT", "DELETE"))
                    if request.method in refresh_on:
                        try:
                            session["last_activity"] = now
                            session.modified = True
                            try:
                                logger.debug("Session last_activity refreshed", extra={"message_key": "session.refreshed", "user_id": session.get("user_id"), "path": path})
                            except Exception:
                                pass
                        except Exception:
                            # if setting session fails, continue (do not block)
                            pass
                except Exception:
                    # In case of config error, attempt default refresh
                    try:
                        session["last_activity"] = now
                        session.modified = True
                    except Exception:
                        pass

            return None
        except Exception:
            # Non-fatal: log and allow request to proceed to avoid breaking user flow on misbehaving session middleware
            try:
                logger.exception("Session security before_request encountered an unexpected error", extra={"message_key": "session.before_request_error", "path": request.path})
            except Exception:
                pass
            return None


def login_required(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to guard endpoints that require a valid server-side session.

    If session is missing or expired, behaves similarly to before_request handler:
      - Returns JSON 401 for Accept: application/json requests
      - Redirects to /login for HTML
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            if not session.get("user_id"):
                # Not authenticated
                if _is_json_request():
                    return jsonify({"error": "unauthenticated"}), 401
                return redirect(url_for("auth.login"))
            # If last_activity missing and legacy sessions not allowed, expire now
            if session.get("last_activity") is None and not bool(current_app.config.get("SESSION_ALLOW_LEGACY_SESSIONS", False)):
                return _logout_and_respond("session_missing_last_activity")
            # rely on before_request to have enforced expiry; call handler if needed
            return func(*args, **kwargs)
        except Exception:
            try:
                logger.exception("login_required decorator error", extra={"message_key": "session.login_required_error"})
            except Exception:
                pass
            # Fail open: do not block access in case of unexpected decorator error
            return func(*args, **kwargs)
    return wrapper
--- END FILE: app_core/utils/session_security.py ---