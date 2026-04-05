from __future__ import annotations

import os
from datetime import timedelta
from typing import Dict, Any, Optional


def _coerce_bool(val: Optional[str], default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    try:
        s = str(val).strip().lower()
        return s in ("1", "true", "yes", "y", "on")
    except Exception:
        return default


def load_session_config(env: Optional[dict] = None) -> Dict[str, Any]:
    """
    Resolve session-related configuration from the given env mapping (defaults to os.environ).

    Returns dict suitable for app.config.update(...)
    Keys:
      - SESSION_COOKIE_HTTPONLY: bool
      - SESSION_COOKIE_SECURE: bool (default true when FLASK_ENV == 'production')
      - SESSION_COOKIE_SAMESITE: str (default 'Lax')
      - PERMANENT_SESSION_LIFETIME: datetime.timedelta
      - SESSION_SLIDING_EXPIRATION: bool
      - SESSION_ALLOW_LEGACY_SESSIONS: bool (transitional)
      - SESSION_WARN_BEFORE_EXPIRY_SECONDS: int
      - SESSION_PROTECTED_PREFIXES: list[str] (optional)
    """
    env = env or os.environ

    cfg: Dict[str, Any] = {}
    cfg["SESSION_COOKIE_HTTPONLY"] = _coerce_bool(env.get("SESSION_COOKIE_HTTPONLY"), True)

    if env.get("SESSION_COOKIE_SECURE") not in (None, ""):
        cfg["SESSION_COOKIE_SECURE"] = _coerce_bool(env.get("SESSION_COOKIE_SECURE"), False)
    else:
        cfg["SESSION_COOKIE_SECURE"] = (str(env.get("FLASK_ENV", "")).lower() == "production")

    cfg["SESSION_COOKIE_SAMESITE"] = env.get("SESSION_COOKIE_SAMESITE", "Lax")

    # Lifetime: allow legacy key PERMANENT_SESSION_LIFETIME (seconds) or PERMANENT_SESSION_LIFETIME_SECONDS
    try:
        lifetime_secs = int(env.get("PERMANENT_SESSION_LIFETIME", env.get("PERMANENT_SESSION_LIFETIME_SECONDS", 7 * 24 * 3600)))
    except Exception:
        lifetime_secs = 7 * 24 * 3600
    cfg["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=int(lifetime_secs))

    cfg["SESSION_SLIDING_EXPIRATION"] = _coerce_bool(env.get("SESSION_SLIDING_EXPIRATION"), True)
    cfg["SESSION_ALLOW_LEGACY_SESSIONS"] = _coerce_bool(env.get("SESSION_ALLOW_LEGACY_SESSIONS"), False)

    try:
        cfg["SESSION_WARN_BEFORE_EXPIRY_SECONDS"] = int(env.get("SESSION_WARN_BEFORE_EXPIRY_SECONDS", 60))
    except Exception:
        cfg["SESSION_WARN_BEFORE_EXPIRY_SECONDS"] = 60

    # Optional prefix list for protected endpoints; default common prefixes
    prefixes = env.get("SESSION_PROTECTED_PREFIXES", None)
    if prefixes:
        try:
            if isinstance(prefixes, str):
                # comma-separated
                cfg["SESSION_PROTECTED_PREFIXES"] = [p.strip() for p in prefixes.split(",") if p.strip()]
            else:
                cfg["SESSION_PROTECTED_PREFIXES"] = list(prefixes)
        except Exception:
            cfg["SESSION_PROTECTED_PREFIXES"] = ["/dashboard", "/shorten", "/analytics", "/domains", "/sessions"]
    else:
        cfg["SESSION_PROTECTED_PREFIXES"] = ["/dashboard", "/shorten", "/analytics", "/domains", "/sessions"]

    return cfg