"""
services/auth_service - Centralized password hashing & verification (KAN-168)

Public API:
 - hash_password(plaintext: str) -> str
 - verify_password(plaintext: str, stored_hash: str) -> bool
 - HashError(Exception)

Behavior:
 - Prefer Werkzeug.generate_password_hash/check_password_hash (pbkdf2:sha256)
 - Fallback to bcrypt if requested and available
 - Final fallback to utils.crypto.hash_password/verify_password if libraries are not present
 - Configurable via app.config or environment variables:
     * PASSWORD_HASH_ALGO: 'werkzeug' (default) or 'bcrypt'
     * PASSWORD_WORK_FACTOR: int (pbkdf2 iterations or bcrypt rounds)
     * PASSWORD_SALT_LENGTH: int (werkzeug salt length)
"""

from __future__ import annotations

import os
import logging
from typing import Optional

# Defensive Flask import for config preference
try:
    from flask import current_app
except Exception:
    current_app = None  # type: ignore

# Prefer project logger
try:
    from app_core.app_logging import get_logger
    _logger = get_logger(__name__)
except Exception:
    _logger = logging.getLogger(__name__)

# Try Werkzeug
try:
    from werkzeug.security import generate_password_hash, check_password_hash  # type: ignore
    _has_werkzeug = True
except Exception:
    generate_password_hash = None  # type: ignore
    check_password_hash = None  # type: ignore
    _has_werkzeug = False

# Try bcrypt
try:
    import bcrypt  # type: ignore
    _has_bcrypt = True
except Exception:
    bcrypt = None  # type: ignore
    _has_bcrypt = False

# Fallback crypto (project provided)
try:
    from utils import crypto as crypto_fallback  # type: ignore
    _has_crypto_fallback = True
except Exception:
    crypto_fallback = None  # type: ignore
    _has_crypto_fallback = False


class HashError(Exception):
    """Operational error while hashing/verifying passwords (not an auth mismatch)."""
    pass


def _get_config(key: str, default=None):
    """
    Resolve config value from Flask current_app (if available), then environment, else default.
    """
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is not None:
                return v
    except Exception:
        pass
    try:
        env_v = os.environ.get(key, None)
        if env_v is not None:
            return env_v
    except Exception:
        pass
    return default


def _get_int_config(key: str, default: int) -> int:
    val = _get_config(key, None)
    if val is None:
        return default
    try:
        return int(val)
    except Exception:
        # Log and raise operational error
        try:
            _logger.exception("Invalid integer config for %s", key, extra={"message_key": "auth.invalid_config", "config_key": key, "value": str(val)})
        except Exception:
            pass
        raise HashError(f"Invalid integer configuration for {key}")


def _get_algo() -> str:
    """
    Return configured algorithm name: 'werkzeug' or 'bcrypt' (lowercased).
    Default: 'werkzeug'
    """
    algo = _get_config("PASSWORD_HASH_ALGO", "werkzeug")
    try:
        return str(algo).strip().lower()
    except Exception:
        return "werkzeug"


# Safe defaults
_DEFAULT_PBKDF2_ITERATIONS = 260000
_DEFAULT_SALT_LENGTH = 16
_DEFAULT_BCRYPT_ROUNDS = 12


def hash_password(plaintext: str) -> str:
    """
    Hash plaintext password and return the stored hash string.

    Raises HashError on operational failures (invalid input, misconfiguration, library failures).
    """
    if not isinstance(plaintext, str) or plaintext == "":
        try:
            _logger.warning("hash_password called with invalid input type/empty", extra={"message_key": "auth.invalid_input"})
        except Exception:
            pass
        raise HashError("Invalid password input")

    algo = _get_algo()

    # Work factor and salt length resolution
    try:
        if algo == "bcrypt":
            rounds = _get_int_config("PASSWORD_WORK_FACTOR", _DEFAULT_BCRYPT_ROUNDS)
        else:
            # default to werkzeug pbkdf2
            rounds = _get_int_config("PASSWORD_WORK_FACTOR", _DEFAULT_PBKDF2_ITERATIONS)
    except HashError:
        # Already logged in _get_int_config
        raise
    except Exception:
        try:
            _logger.exception("Failed to read PASSWORD_WORK_FACTOR", extra={"message_key": "auth.config_read_failed"})
        except Exception:
            pass
        raise HashError("Failed to read password work factor configuration")

    salt_length = _get_int_config("PASSWORD_SALT_LENGTH", _DEFAULT_SALT_LENGTH)

    # Try primary: Werkzeug
    if algo == "werkzeug" and _has_werkzeug:
        try:
            method = f"pbkdf2:sha256:{rounds}"
            # Werkzeug's generate_password_hash accepts method and salt_length
            hashed = generate_password_hash(plaintext, method=method, salt_length=int(salt_length))
            try:
                _logger.info("Password hashed using werkzeug pbkdf2", extra={"message_key": "auth.hash_success", "algo": "pbkdf2:sha256", "work_factor": rounds})
            except Exception:
                pass
            return hashed
        except Exception as e:
            try:
                _logger.exception("Failed to hash password with werkzeug", extra={"message_key": "auth.hash_failed", "algo": "pbkdf2:sha256", "work_factor": rounds})
            except Exception:
                pass
            raise HashError("Failed to hash password")

    # If algorithm requested is bcrypt
    if algo == "bcrypt":
        if _has_bcrypt:
            try:
                rounds_safe = int(rounds)
                salt = bcrypt.gensalt(rounds_safe)
                hashed_bytes = bcrypt.hashpw(plaintext.encode("utf-8"), salt)
                hashed = hashed_bytes.decode("utf-8")
                try:
                    _logger.info("Password hashed using bcrypt", extra={"message_key": "auth.hash_success", "algo": "bcrypt", "work_factor": rounds_safe})
                except Exception:
                    pass
                return hashed
            except Exception:
                try:
                    _logger.exception("Failed to hash password with bcrypt", extra={"message_key": "auth.hash_failed", "algo": "bcrypt"})
                except Exception:
                    pass
                raise HashError("Failed to hash password")
        else:
            # bcrypt requested but not available -> fallback to werkzeug if present
            if _has_werkzeug:
                try:
                    method = f"pbkdf2:sha256:{_DEFAULT_PBKDF2_ITERATIONS}"
                    hashed = generate_password_hash(plaintext, method=method, salt_length=int(salt_length))
                    try:
                        _logger.warning("bcrypt requested but unavailable; falling back to werkzeug pbkdf2", extra={"message_key": "auth.fallback_werkzeug"})
                    except Exception:
                        pass
                    return hashed
                except Exception:
                    try:
                        _logger.exception("Failed to fallback-hash password with werkzeug after bcrypt unavailable", extra={"message_key": "auth.fallback_failed"})
                    except Exception:
                        pass
                    raise HashError("Failed to hash password (bcrypt unavailable, fallback failed)")
            # fallback to crypto_fallback if available
            if _has_crypto_fallback:
                try:
                    hashed = crypto_fallback.hash_password(plaintext)
                    try:
                        _logger.warning("bcrypt requested but unavailable; used crypto fallback", extra={"message_key": "auth.crypto_fallback"})
                    except Exception:
                        pass
                    return hashed
                except Exception:
                    try:
                        _logger.exception("crypto fallback failed for hashing", extra={"message_key": "auth.crypto_fallback_failed"})
                    except Exception:
                        pass
                    raise HashError("Failed to hash password (bcrypt unavailable, crypto fallback failed)")
            # nothing available
            try:
                _logger.error("bcrypt requested but no hashing backend available", extra={"message_key": "auth.no_backend"})
            except Exception:
                pass
            raise HashError("No hashing backend available")

    # If algo default is werkzeug but Werkzeug not present -> try bcrypt then crypto
    if not _has_werkzeug:
        if _has_bcrypt:
            try:
                rounds_safe = int(rounds) if rounds else _DEFAULT_BCRYPT_ROUNDS
                salt = bcrypt.gensalt(rounds_safe)
                hashed = bcrypt.hashpw(plaintext.encode("utf-8"), salt).decode("utf-8")
                try:
                    _logger.info("Password hashed using bcrypt (werkzeug missing)", extra={"message_key": "auth.hash_success", "algo": "bcrypt"})
                except Exception:
                    pass
                return hashed
            except Exception:
                try:
                    _logger.exception("Failed to hash password with bcrypt as werkzeug missing", extra={"message_key": "auth.hash_failed"})
                except Exception:
                    pass
                raise HashError("Failed to hash password")
        if _has_crypto_fallback:
            try:
                hashed = crypto_fallback.hash_password(plaintext)
                try:
                    _logger.warning("Werkzeug not available; used crypto fallback for hashing", extra={"message_key": "auth.crypto_fallback"})
                except Exception:
                    pass
                return hashed
            except Exception:
                try:
                    _logger.exception("crypto fallback failed for hashing when werkzeug missing", extra={"message_key": "auth.crypto_fallback_failed"})
                except Exception:
                    pass
                raise HashError("Failed to hash password (no werkzeug/bcrypt)")

    # Fallback safety net (shouldn't reach)
    try:
        _logger.error("Unexpected state in hash_password: no branch matched", extra={"message_key": "auth.unexpected"})
    except Exception:
        pass
    raise HashError("No hashing backend available")


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """
    Verify plaintext against stored_hash. Returns True on match, False on mismatch.
    Raises HashError on operational errors (misconfiguration, library failure).
    """
    if not isinstance(plaintext, str) or plaintext == "":
        try:
            _logger.warning("verify_password called with invalid input", extra={"message_key": "auth.invalid_input_verify"})
        except Exception:
            pass
        # For security treat invalid input as mismatch rather than operational error
        return False

    if not isinstance(stored_hash, str) or stored_hash == "":
        try:
            _logger.warning("verify_password called with invalid stored_hash", extra={"message_key": "auth.invalid_stored_hash"})
        except Exception:
            pass
        return False

    # Heuristics: bcrypt hashes start with $2
    try:
        is_bcrypt = stored_hash.startswith("$2")
    except Exception:
        is_bcrypt = False

    is_pbkdf2 = "pbkdf2:" in stored_hash.lower()

    # Prefer Werkzeug when pbkdf2 or Werkzeug present
    if (is_pbkdf2 or _has_werkzeug) and check_password_hash is not None:
        try:
            # check_password_hash returns True/False
            ok = check_password_hash(stored_hash, plaintext)
            return bool(ok)
        except Exception:
            # If Werkzeug fails, fallthrough to bcrypt or crypto
            try:
                _logger.exception("Werkzeug check_password_hash failed", extra={"message_key": "auth.verify_werkzeug_failed"})
            except Exception:
                pass

    # If bcrypt-detected or Werkzeug didn't handle, try bcrypt if available
    if is_bcrypt or _has_bcrypt:
        if _has_bcrypt:
            try:
                ok = bcrypt.checkpw(plaintext.encode("utf-8"), stored_hash.encode("utf-8"))
                return bool(ok)
            except Exception:
                try:
                    _logger.exception("bcrypt.checkpw failed", extra={"message_key": "auth.verify_bcrypt_failed"})
                except Exception:
                    pass
                # fallthrough to crypto fallback
        else:
            try:
                _logger.warning("bcrypt format detected but bcrypt not available", extra={"message_key": "auth.bcrypt_missing"})
            except Exception:
                pass

    # Final fallback: use project's crypto module if available
    if _has_crypto_fallback:
        try:
            # crypto_fallback.verify_password signature: verify_password(password, stored_hash, pepper=None)
            ok = crypto_fallback.verify_password(plaintext, stored_hash)
            return bool(ok)
        except Exception:
            try:
                _logger.exception("crypto fallback verify_password failed", extra={"message_key": "auth.verify_crypto_failed"})
            except Exception:
                pass
            raise HashError("Password verification failed due to internal error")

    # If none matched, treat as mismatch (conservative)
    try:
        _logger.warning("No verification backend matched; treating as mismatch", extra={"message_key": "auth.verify_no_backend"})
    except Exception:
        pass
    return False
--- END FILE ---