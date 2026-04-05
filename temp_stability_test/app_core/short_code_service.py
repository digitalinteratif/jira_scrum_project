"""
app_core.short_code_service - Short code generation & allocation service (KAN-174)

Provides:
 - ShortCodeGenerationError: raised when unique code cannot be produced after configured attempts
 - generate_unique_short_code(conn_or_session, app=None) -> str
     * Optimistic existence-checking generator (non-atomic).
 - allocate_short_code_and_insert(conn_or_session, original_url, owner_user_id=None, *, app=None, is_custom=False)
     * Recommended atomic insert-with-retry allocation that catches uniqueness collisions and retries.

Behavior:
 - Configurable via Flask app.config keys (preferred) or environment variables:
     SHORT_CODE_LENGTH (default 8)
     SHORT_CODE_ATTEMPTS (default 8)
     SHORT_CODE_ALPHABET (default alphanumeric + - _)
 - Uses Python's secrets for cryptographically secure randomness.
 - Works with either sqlite3.Connection (from app_core.db.get_db_connection) or SQLAlchemy Session (models.Session).
 - Logs structured events via app_core.app_logging.get_logger(__name__).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import traceback
from typing import Optional, Any

# Defensive Flask import for config access
try:
    from flask import current_app
except Exception:
    current_app = None  # type: ignore

# Project logging
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)

# DB helpers and models (dependency-tolerant)
try:
    # sqlite helper
    from app_core.db import create_url_mapping
except Exception:
    create_url_mapping = None

try:
    import models
except Exception:
    models = None

# Defaults
_DEFAULT_LENGTH = 8
_DEFAULT_ATTEMPTS = 8
_DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


class ShortCodeGenerationError(Exception):
    """Raised when the service cannot produce a unique short code after configured attempts."""

    def __init__(self, message: str = "unable to generate unique short code", attempts: int = 0):
        super().__init__(message)
        self.message = message
        self.attempts = int(attempts)


def _get_config(app=None) -> dict:
    """Resolve configuration from provided app or Flask current_app or environment with safe defaults."""
    cfg = {}
    # prefer explicit app
    try:
        cfg_src = None
        if app is not None and hasattr(app, "config"):
            cfg_src = dict(app.config)
        elif current_app is not None:
            cfg_src = dict(current_app.config)
        else:
            cfg_src = {}
    except Exception:
        cfg_src = {}

    def _cfg_get(key: str, default):
        try:
            if key in cfg_src and cfg_src.get(key) is not None:
                return cfg_src.get(key)
        except Exception:
            pass
        try:
            env = os.environ.get(key)
            if env is not None:
                # try cast to int where appropriate for known keys
                if key in ("SHORT_CODE_LENGTH", "SHORT_CODE_ATTEMPTS"):
                    return int(env)
                return env
        except Exception:
            pass
        return default

    cfg["SHORT_CODE_LENGTH"] = int(_cfg_get("SHORT_CODE_LENGTH", _DEFAULT_LENGTH))
    cfg["SHORT_CODE_ATTEMPTS"] = int(_cfg_get("SHORT_CODE_ATTEMPTS", _DEFAULT_ATTEMPTS))
    cfg["SHORT_CODE_ALPHABET"] = str(_cfg_get("SHORT_CODE_ALPHABET", _DEFAULT_ALPHABET))
    return cfg


def _generate_random_code(length: int, alphabet: str) -> str:
    """Generate a cryptographically secure random string of given length using provided alphabet."""
    if not alphabet:
        alphabet = _DEFAULT_ALPHABET
    if length <= 0:
        raise ValueError("length must be positive")
    # Use secrets.choice for CSPRNG
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _is_sqlalchemy_session(obj: Any) -> bool:
    """
    Heuristic to decide if the object is a SQLAlchemy Session.
    We expect a session to have 'add' and 'query' attributes (sqlite3.Connection does not).
    """
    try:
        return hasattr(obj, "add") and hasattr(obj, "query")
    except Exception:
        return False


def generate_unique_short_code(conn_or_session: Any, *, app: Optional[Any] = None) -> str:
    """
    Generate a candidate short code that is not currently present according to the provided DB handle.

    Note: This is an optimistic existence-check; it is NOT atomic. A later insert may still conflict.
    For race-resistant allocation use allocate_short_code_and_insert().

    Parameters:
      - conn_or_session: sqlite3.Connection or SQLAlchemy Session-like
      - app: optional Flask app for config overrides

    Returns:
      - unique short code string

    Raises:
      - ShortCodeGenerationError when attempts exhausted
    """
    cfg = _get_config(app)
    length = int(cfg["SHORT_CODE_LENGTH"])
    attempts = int(cfg["SHORT_CODE_ATTEMPTS"])
    alphabet = str(cfg["SHORT_CODE_ALPHABET"])

    for attempt in range(1, attempts + 1):
        candidate = _generate_random_code(length, alphabet)
        try:
            if _is_sqlalchemy_session(conn_or_session):
                # SQLAlchemy path
                if models is None:
                    # Can't check: assume candidate unique (but caller should use atomic allocation)
                    logger.debug("models unavailable; returning candidate without DB existence check", extra={"message_key": "shortcode.generate.attempt", "candidate": candidate, "attempt": attempt})
                    return candidate
                exists = conn_or_session.query(models.ShortURL).filter_by(slug=candidate).first()
                if not exists:
                    logger.debug("generated candidate not found in SQLAlchemy DB", extra={"message_key": "shortcode.generate.attempt", "candidate": candidate, "attempt": attempt})
                    return candidate
                logger.debug("candidate exists in SQLAlchemy DB; retrying", extra={"message_key": "shortcode.generate.collision", "candidate": candidate, "attempt": attempt})
            else:
                # Assume sqlite3.Connection-like
                cur = conn_or_session.cursor()
                cur.execute("SELECT 1 FROM Urls WHERE short_code = ?", (candidate,))
                row = cur.fetchone()
                if row is None:
                    logger.debug("generated candidate not found in sqlite DB", extra={"message_key": "shortcode.generate.attempt", "candidate": candidate, "attempt": attempt})
                    return candidate
                logger.debug("candidate exists in sqlite DB; retrying", extra={"message_key": "shortcode.generate.collision", "candidate": candidate, "attempt": attempt})
        except Exception as e:
            # On unexpected errors, log and raise to surface operational issues
            logger.exception("Error checking candidate existence", extra={"message_key": "shortcode.generate.check_error", "candidate": candidate})
            raise

    logger.error("Exhausted attempts in generate_unique_short_code", extra={"message_key": "shortcode.generate.exhausted", "attempts": attempts})
    raise ShortCodeGenerationError("exhausted attempts generating unique short code", attempts=attempts)


def allocate_short_code_and_insert(conn_or_session: Any, original_url: str, owner_user_id: Optional[int] = None, *, app: Optional[Any] = None, is_custom: bool = False):
    """
    Atomically allocate a short code by attempting to insert candidate codes and catching uniqueness collisions.

    Parameters:
      - conn_or_session: sqlite3.Connection or SQLAlchemy Session-like
      - original_url: target URL (string) to store
      - owner_user_id: optional owner FK (int)
      - app: optional Flask app for config
      - is_custom: boolean hint; kept for parity with models.create_shorturl API

    Returns:
      - On SQLAlchemy Session input: returns the created models.ShortURL instance
      - On sqlite3.Connection input: returns the chosen short_code (string)

    Raises:
      - ShortCodeGenerationError if attempts exhausted
      - Propagates other unexpected DB errors
    """
    if not original_url or not isinstance(original_url, str):
        raise ValueError("original_url must be a non-empty string")

    cfg = _get_config(app)
    length = int(cfg["SHORT_CODE_LENGTH"])
    attempts = int(cfg["SHORT_CODE_ATTEMPTS"])
    alphabet = str(cfg["SHORT_CODE_ALPHABET"])

    # SQLAlchemy session path
    if _is_sqlalchemy_session(conn_or_session):
        if models is None:
            raise RuntimeError("models module not available; cannot perform SQLAlchemy insert")
        session = conn_or_session
        for attempt in range(1, attempts + 1):
            candidate = _generate_random_code(length, alphabet)
            try:
                # Use models.create_shorturl helper which raises DuplicateSlugError on conflict
                new = models.create_shorturl(session, user_id=owner_user_id, target_url=original_url, slug=candidate, is_custom=bool(is_custom))
                try:
                    logger.info("Allocated short code via SQLAlchemy", extra={"message_key": "shortcode.generate.success", "short_code": candidate, "attempts": attempt})
                except Exception:
                    logger.info("Allocated short code (SQLAlchemy) %s after %d attempts", candidate, attempt)
                return new
            except models.DuplicateSlugError:
                # Collision: retry
                try:
                    logger.warning("SQLAlchemy slug collision; retrying", extra={"message_key": "shortcode.generate.collision", "candidate": candidate, "attempt": attempt})
                except Exception:
                    logger.warning("SQLAlchemy slug collision candidate=%s attempt=%d", candidate, attempt)
                # Ensure any attempted transaction is rolled back (models.create_shorturl uses session.begin())
                try:
                    session.rollback()
                except Exception:
                    pass
                continue
            except Exception:
                logger.exception("Unexpected DB error during SQLAlchemy slug insertion", extra={"message_key": "shortcode.generate.db_error"})
                # propagate
                raise

        logger.error("Exhausted attempts allocating short code (SQLAlchemy)", extra={"message_key": "shortcode.generate.exhausted", "attempts": attempts})
        raise ShortCodeGenerationError("exhausted attempts allocating unique short code (SQLAlchemy)", attempts=attempts)

    # sqlite3.Connection path (fallback)
    else:
        conn = conn_or_session
        for attempt in range(1, attempts + 1):
            candidate = _generate_random_code(length, alphabet)
            try:
                if create_url_mapping is None:
                    # Attempt a raw parameterized insert into Urls table
                    cur = conn.cursor()
                    cur.execute("INSERT INTO Urls(short_code, original_url, owner_user_id) VALUES(?, ?, ?)", (candidate, original_url, owner_user_id))
                    conn.commit()
                else:
                    # Preferred helper which handles logging and rollback on IntegrityError
                    create_url_mapping(conn, candidate, original_url, owner_user_id)
                try:
                    logger.info("Allocated short code via sqlite insert", extra={"message_key": "shortcode.generate.success", "short_code": candidate, "attempts": attempt})
                except Exception:
                    logger.info("Allocated short code (sqlite) %s after %d attempts", candidate, attempt)
                return candidate
            except sqlite3.IntegrityError:
                # Likely duplicate short_code; retry
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    logger.warning("Sqlite slug collision; retrying", extra={"message_key": "shortcode.generate.collision", "candidate": candidate, "attempt": attempt})
                except Exception:
                    logger.warning("Sqlite slug collision candidate=%s attempt=%d", candidate, attempt)
                continue
            except Exception:
                logger.exception("Unexpected DB error during sqlite slug insertion", extra={"message_key": "shortcode.generate.db_error"})
                raise

        logger.error("Exhausted attempts allocating short code (sqlite)", extra={"message_key": "shortcode.generate.exhausted", "attempts": attempts})
        raise ShortCodeGenerationError("exhausted attempts allocating unique short code (sqlite)", attempts=attempts)


__all__ = ["ShortCodeGenerationError", "generate_unique_short_code", "allocate_short_code_and_insert", "_generate_random_code"]

--- END FILE: app_core/short_code_service.py ---