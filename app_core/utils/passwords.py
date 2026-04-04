"""utils/passwords.py - Password policy checks and helpers for KAN-130 (US-032)

Responsibilities:
 - Expose password_policy_check(password: str, email: Optional[str] = None) -> List[str]
     which returns a list of human-friendly violation messages (empty list => password accepted).
 - Provide a helper policy_hints() -> list[str] that returns short hint strings suitable for client-side UX.
 - Be defensive about Flask current_app access so configuration can be tuned via app.config or env vars.
 - Provide lightweight list of common passwords to reject.
 - Must not raise for ordinary inputs; return violations list.
 - Best-effort trace to trace_KAN-130.txt for Architectural Memory.
"""

from typing import List, Optional
import os
import time
import re

# Try to import Flask current_app for runtime configuration (tolerant)
try:
    from flask import current_app
except Exception:
    current_app = None

TRACE_FILE = "trace_KAN-130.txt"

def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


# Minimal set of "common passwords" used for lightweight server-side rejection.
# This list is intentionally small and conservative; operators can override via config key COMMON_PASSWORDS.
_DEFAULT_COMMON = {
    "password",
    "123456",
    "12345678",
    "qwerty",
    "abc123",
    "letmein",
    "iloveyou",
    "admin",
    "welcome",
    "monkey",
    "dragon",
    "password1",
    "123456789",
}


def _get_config(name: str, default):
    """
    Resolve configuration from Flask current_app (if available), environment variable (uppercase name),
    or fallback to provided default.
    """
    try:
        if current_app is not None:
            v = current_app.config.get(name, None)
            if v is not None:
                return v
    except Exception:
        pass
    try:
        env_v = os.environ.get(name, None)
        if env_v is not None:
            # try to coerce booleans/ints where appropriate in callers
            return env_v
    except Exception:
        pass
    return default


def _load_common_passwords() -> List[str]:
    """
    Return configured common password set (lowercased strings).
    """
    cfg = _get_config("COMMON_PASSWORDS", None)
    if cfg:
        try:
            if isinstance(cfg, (list, set, tuple)):
                return [str(x).lower() for x in cfg if x]
            if isinstance(cfg, str):
                # comma-separated
                parts = [p.strip() for p in cfg.split(",") if p.strip()]
                return [p.lower() for p in parts]
        except Exception:
            pass
    return list(_DEFAULT_COMMON)


# Helper to present the policy as short hint strings (useful for client-side UI)
def policy_hints() -> List[str]:
    """
    Returns a list of short human-friendly hints describing the active server-side password policy.
    Example:
      ["At least 12 characters", "Include uppercase and lowercase letters", "Include a digit", "Include a symbol"]
    """
    try:
        min_len = int(_get_config("PASSWORD_MIN_LENGTH", 12))
    except Exception:
        min_len = 12
    require_upper = bool(_get_config("PASSWORD_REQUIRE_UPPER", True))
    require_lower = bool(_get_config("PASSWORD_REQUIRE_LOWER", True))
    require_digit = bool(_get_config("PASSWORD_REQUIRE_DIGIT", True))
    require_symbol = bool(_get_config("PASSWORD_REQUIRE_SYMBOL", True))
    disallow_common = bool(_get_config("PASSWORD_DISALLOW_COMMON", True))

    hints = [f"At least {min_len} characters"]
    char_reqs = []
    if require_upper:
        char_reqs.append("uppercase letter (A-Z)")
    if require_lower:
        char_reqs.append("lowercase letter (a-z)")
    if require_digit:
        char_reqs.append("a digit (0-9)")
    if require_symbol:
        char_reqs.append("a symbol (e.g. !@#$%)")
    if char_reqs:
        hints.append("Include " + ", ".join(char_reqs))
    if disallow_common:
        hints.append("Avoid common passwords (e.g. 'password', '123456')")
    return hints


def _contains_sequence_of_same_characters(s: str, max_run: int = 3) -> bool:
    """
    Return True if s contains a run of the same character longer than max_run.
    """
    if not s:
        return False
    run = 1
    last = s[0]
    for ch in s[1:]:
        if ch == last:
            run += 1
            if run > max_run:
                return True
        else:
            run = 1
            last = ch
    return False


# Main exported function
def password_policy_check(password: str, email: Optional[str] = None) -> List[str]:
    """
    Return a list of violation messages. If empty, password meets policy.

    Parameters:
      - password: candidate password string
      - email: optional user's email/address to detect trivial overlaps (e.g., password contains email or localpart)

    Policy defaults (configurable via app.config or env vars):
      - PASSWORD_MIN_LENGTH (int, default 12)
      - PASSWORD_REQUIRE_UPPER (bool, default True)
      - PASSWORD_REQUIRE_LOWER (bool, default True)
      - PASSWORD_REQUIRE_DIGIT (bool, default True)
      - PASSWORD_REQUIRE_SYMBOL (bool, default True)
      - PASSWORD_DISALLOW_COMMON (bool, default True)
      - PASSWORD_MAX_IDENTICAL_RUN (int, default 3)  # disallow runs longer than this
      - COMMON_PASSWORDS (list or comma-separated string) override for common password blacklist

    Behavior:
      - Does NOT perform heavy entropy checks or online lookups.
      - Returns human-friendly messages suitable for server-side validation responses.
    """
    violations: List[str] = []
    try:
        if password is None:
            violations.append("Password is required.")
            _trace("POLICY_CHECK missing_password")
            return violations
        pw = str(password)
        # Basic length
        try:
            min_len = int(_get_config("PASSWORD_MIN_LENGTH", 12))
        except Exception:
            min_len = 12

        if len(pw) < min_len:
            violations.append(f"Password must be at least {min_len} characters long.")

        # Character classes
        require_upper = bool(_get_config("PASSWORD_REQUIRE_UPPER", True))
        require_lower = bool(_get_config("PASSWORD_REQUIRE_LOWER", True))
        require_digit = bool(_get_config("PASSWORD_REQUIRE_DIGIT", True))
        require_symbol = bool(_get_config("PASSWORD_REQUIRE_SYMBOL", True))

        if require_upper and not re.search(r"[A-Z]", pw):
            violations.append("Password must include at least one uppercase letter (A-Z).")
        if require_lower and not re.search(r"[a-z]", pw):
            violations.append("Password must include at least one lowercase letter (a-z).")
        if require_digit and not re.search(r"\d", pw):
            violations.append("Password must include at least one digit (0-9).")
        if require_symbol and not re.search(r"[!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]", pw):
            # common printable symbol set; be conservative
            violations.append("Password should include at least one symbol (e.g. !@#$%).")

        # Reject overly repetitive sequences
        try:
            max_run = int(_get_config("PASSWORD_MAX_IDENTICAL_RUN", 3))
        except Exception:
            max_run = 3
        if _contains_sequence_of_same_characters(pw, max_run=max_run):
            violations.append(f"Avoid sequences of the same character longer than {max_run} characters.")

        # Reject too similar to email (if provided)
        if email:
            try:
                localpart = str(email).split("@", 1)[0]
                if localpart and localpart.lower() in pw.lower():
                    violations.append("Password is too similar to your email address.")
            except Exception:
                pass

        # Reject common passwords
        disallow_common = bool(_get_config("PASSWORD_DISALLOW_COMMON", True))
        if disallow_common:
            common_list = _load_common_passwords()
            if pw.lower() in set(common_list):
                violations.append("This password is too common. Choose a less unpredictable password.")

        # Minimal entropy hint: require at least 3 classes among upper/lower/digit/symbol for shorter passwords
        try:
            if len(pw) < max(min_len + 4, 16):
                classes = 0
                if re.search(r"[A-Z]", pw): classes += 1
                if re.search(r"[a-z]", pw): classes += 1
                if re.search(r"\d", pw): classes += 1
                if re.search(r"[!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]", pw): classes += 1
                if classes < 3:
                    violations.append("Try mixing letters, numbers and symbols for a stronger password.")
        except Exception:
            pass

        # Ensure not empty after trimming
        if pw.strip() == "":
            violations.append("Password cannot be empty or whitespace-only.")

        _trace(f"POLICY_CHECK completed len={len(pw)} violations={len(violations)}")
    except Exception as e:
        # On unexpected internal errors, be conservative: signal a generic violation
        try:
            _trace(f"POLICY_CHECK_ERROR err={str(e)}")
        except Exception:
            pass
        violations.append("Password validation unavailable (internal error); try a stronger password.")
    return violations

# Backwards-compatible alias
password_policy_violations = password_policy_check
# --- END FILE: utils/passwords.py ---