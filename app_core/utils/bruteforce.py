"""utils/bruteforce.py - Brute-force protection helpers (KAN-127)

Responsibilities:
 - Track failed login attempts per-account and per-IP using models.FailedLoginCounter.
 - Apply threshold-based lockout and configurable exponential backoff.
 - Provide helpers:
     - check_lockout(user_id, ip) -> (blocked:bool, retry_after_seconds:int, details:dict)
     - register_failed_attempt(user_id, ip) -> dict with per-key results
     - reset_failed_login_state(user_id, ip) -> clears counters on successful login
 - Use app config or environment variables to control thresholds and durations.
 - Best-effort trace to trace_KAN-127.txt for Architectural Memory.
 - Defensive imports and DB session handling per project guardrails.
"""

from datetime import datetime, timedelta
import time
import os

# Defensive Flask import
try:
    from flask import current_app
except Exception:
    current_app = None

# Models and DB session
import models

TRACE_FILE = "trace_KAN-127.txt"

def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass

def _get_config_int(key: str, default: int) -> int:
    """
    Resolve integer configuration from Flask current_app (if available) or environment variable or default.
    """
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is not None:
                return int(v)
    except Exception:
        pass
    try:
        env_v = os.environ.get(key, None)
        if env_v is not None:
            return int(env_v)
    except Exception:
        pass
    return default

def _get_config_float(key: str, default: float) -> float:
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is not None:
                return float(v)
    except Exception:
        pass
    try:
        env_v = os.environ.get(key, None)
        if env_v is not None:
            return float(env_v)
    except Exception:
        pass
    return default

# Configuration defaults (tunable via current_app.config or env vars)
DEFAULTS = {
    "FAILED_LOGIN_THRESHOLD_PER_IP": 20,
    "FAILED_LOGIN_THRESHOLD_PER_ACCOUNT": 5,
    "LOCKOUT_DURATION_SECONDS": 300,       # base lockout (5 minutes)
    "BACKOFF_MULTIPLIER": 2,               # exponential multiplier
    "MAX_BACKOFF_SECONDS": 3600,           # 1 hour max
    # Small response delay (backoff) ramp for slowing attacker rather than immediate lockout
    "BACKOFF_RESPONSE_BASE_SECONDS": 0.25, # multiply by attempts to add small delays
    "BACKOFF_RESPONSE_MAX_SECONDS": 5.0,
}

def _resolve_conf(name: str) -> int:
    return _get_config_int(name, DEFAULTS.get(name))

def _resolve_conf_float(name: str) -> float:
    return _get_config_float(name, DEFAULTS.get(name))

# Helper to build keys for DB rows
def _user_key(user_id: int) -> str:
    return f"user:{user_id}"

def _ip_key(ip: str) -> str:
    return f"ip:{ip}"

def _now() -> datetime:
    return datetime.utcnow()

def _get_or_create_row(session, key: str):
    """
    Return models.FailedLoginCounter row for key, creating if absent.
    """
    row = None
    try:
        row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
    except Exception:
        # tolerate DB quirks
        row = None

    if row is None:
        try:
            row = models.FailedLoginCounter(key=key, count=0, lockout_until=None, lockout_count=0, last_failed_at=None)
            session.add(row)
            session.commit()
            # refresh
            session.refresh(row)
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            # attempt to load again
            try:
                row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
            except Exception:
                row = None
    return row

def check_lockout(user_id=None, ip=None):
    """
    Check whether the provided account (user_id) or IP is currently locked out.

    Returns:
      (blocked: bool, retry_after_seconds: int, details: dict)
    where details contains per-key info like {'user:key': {...}, 'ip:key': {...}}
    """
    session = None
    details = {}
    try:
        session = models.Session()
        now = _now()

        # Check account lock (if user_id provided)
        if user_id is not None:
            try:
                key = _user_key(user_id)
                row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
                if row and row.lockout_until is not None and row.lockout_until > now:
                    retry_after = int((row.lockout_until - now).total_seconds())
                    details[key] = {"locked": True, "retry_after": retry_after, "lockout_until": row.lockout_until, "lockout_count": row.lockout_count}
                    _trace(f"CHECK_LOCKOUT user_key={key} locked retry_after={retry_after}")
                    return True, retry_after, details
                else:
                    if row:
                        details[key] = {"locked": False, "count": row.count, "lockout_until": row.lockout_until, "lockout_count": row.lockout_count}
                    else:
                        details[key] = {"locked": False}
            except Exception as e:
                _trace(f"CHECK_LOCKOUT_USER_ERROR user_id={user_id} err={str(e)}")

        # Check IP lock
        if ip:
            try:
                key = _ip_key(ip)
                row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
                if row and row.lockout_until is not None and row.lockout_until > now:
                    retry_after = int((row.lockout_until - now).total_seconds())
                    details[key] = {"locked": True, "retry_after": retry_after, "lockout_until": row.lockout_until, "lockout_count": row.lockout_count}
                    _trace(f"CHECK_LOCKOUT ip_key={key} locked retry_after={retry_after}")
                    return True, retry_after, details
                else:
                    if row:
                        details[key] = {"locked": False, "count": row.count, "lockout_until": row.lockout_until, "lockout_count": row.lockout_count}
                    else:
                        details[key] = {"locked": False}
            except Exception as e:
                _trace(f"CHECK_LOCKOUT_IP_ERROR ip={ip} err={str(e)}")

        # No lock detected
        return False, 0, details
    except Exception as e:
        _trace(f"CHECK_LOCKOUT_TOPLEVEL_ERROR err={str(e)}")
        # Conservative: do not block on internal errors; return not blocked
        return False, 0, details
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

def register_failed_attempt(user_id=None, ip=None):
    """
    Record a failed login attempt for the given user_id and/or ip.

    Behavior:
      - Increment per-key counters.
      - If a counter reaches its configured threshold, set lockout_until to now + computed_duration.
      - lockout_count is incremented each time a lockout is applied for backoff computation.
      - Reset count to 0 after applying a lockout for that key (optional).
    Returns:
      dict of { key: { 'count': int, 'locked': bool, 'lockout_until': datetime|None, 'lockout_count': int } }
    """
    results = {}
    session = None
    try:
        session = models.Session()
        now = _now()

        # Configs
        per_ip_threshold = _resolve_conf("FAILED_LOGIN_THRESHOLD_PER_IP")
        per_account_threshold = _resolve_conf("FAILED_LOGIN_THRESHOLD_PER_ACCOUNT")
        lockout_base = _resolve_conf("LOCKOUT_DURATION_SECONDS")
        backoff_mul = _resolve_conf_float("BACKOFF_MULTIPLIER")
        max_backoff = _resolve_conf("MAX_BACKOFF_SECONDS")

        # Helper to process single key
        def _process_key(key: str, threshold: int):
            try:
                row = _get_or_create_row(session, key)
                # Reload fresh copy for safety
                try:
                    session.refresh(row)
                except Exception:
                    pass

                row.count = int((row.count or 0) + 1)
                row.last_failed_at = now
                locked = False
                lockout_until = None

                if row.count >= int(threshold):
                    # apply lockout, increment lockout_count
                    row.lockout_count = int((row.lockout_count or 0) + 1)
                    # exponential backoff: lockout_base * (backoff_mul ** (lockout_count - 1))
                    try:
                        # compute float seconds and clamp
                        exponent = max(0, int(row.lockout_count) - 1)
                        duration = float(lockout_base) * (float(backoff_mul) ** float(exponent))
                        if duration > float(max_backoff):
                            duration = float(max_backoff)
                        lockout_until = now + timedelta(seconds=int(duration))
                        row.lockout_until = lockout_until
                        locked = True
                        # reset count after lockout to require fresh attempts after expiry
                        row.count = 0
                        _trace(f"LOCK_APPLIED key={key} lockout_until={lockout_until.isoformat()} lockout_count={row.lockout_count} duration_sec={int(duration)}")
                    except Exception as e:
                        _trace(f"LOCK_APPLY_ERROR key={key} err={str(e)}")
                session.add(row)
                session.commit()
                try:
                    session.refresh(row)
                except Exception:
                    pass
                results[key] = {
                    "count": int(row.count or 0),
                    "locked": locked,
                    "lockout_until": getattr(row, "lockout_until", None),
                    "lockout_count": int(row.lockout_count or 0),
                }
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"REGISTER_FAILED_KEY_ERROR key={key} err={str(e)}")
                results[key] = {"error": str(e)}
            return

        # Process user key first (if provided)
        if user_id is not None:
            k = _user_key(user_id)
            _process_key(k, per_account_threshold)

        # Process IP key if provided
        if ip:
            k = _ip_key(ip)
            _process_key(k, per_ip_threshold)

        return results
    except Exception as e:
        _trace(f"REGISTER_FAILED_TOPLEVEL_ERROR user_id={user_id} ip={ip} err={str(e)}")
        return results
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

def reset_failed_login_state(user_id=None, ip=None):
    """
    Reset failed login counters and lockouts for the specified user_id and/or ip.
    Intended to be called on successful authentication or by admin unlock path.
    """
    session = None
    try:
        session = models.Session()
        if user_id is not None:
            try:
                key = _user_key(user_id)
                row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
                if row:
                    row.count = 0
                    row.lockout_until = None
                    row.lockout_count = 0
                    row.last_failed_at = None
                    session.add(row)
                    session.commit()
                    _trace(f"RESET_FAILED user_key={key}")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"RESET_FAILED_USER_ERROR user_id={user_id} err={str(e)}")

        if ip:
            try:
                key = _ip_key(ip)
                row = session.query(models.FailedLoginCounter).filter_by(key=key).first()
                if row:
                    row.count = 0
                    row.lockout_until = None
                    row.lockout_count = 0
                    row.last_failed_at = None
                    session.add(row)
                    session.commit()
                    _trace(f"RESET_FAILED ip_key={key}")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"RESET_FAILED_IP_ERROR ip={ip} err={str(e)}")
    except Exception as e:
        _trace(f"RESET_FAILED_TOPLEVEL_ERROR user_id={user_id} ip={ip} err={str(e)}")
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

# End of utils/bruteforce.py
--- END FILE: utils/bruteforce.py ---