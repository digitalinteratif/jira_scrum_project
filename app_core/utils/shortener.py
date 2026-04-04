"""utils/shortener.py - Short slug generation utilities (KAN-112 / KAN-118)

Surgical update for KAN-118 (US-020):
 - Enforce reserved-word and blacklist checks when validating custom slugs.
 - Improve suggestion generation to avoid reserved/blacklisted/existing slugs.
 - Maintain deterministic and non-deterministic slug generation semantics.
 - Best-effort trace to trace_KAN-118.txt.

Notes:
 - This file follows the project's dependency-tolerance style and will not raise
   on absence of a Flask app context; it will fall back to environment vars or
   sensible defaults.
"""

from typing import Optional, Callable, Dict, Any, List, Set
import secrets
import re
import time
import hmac
import hashlib
import os

# SQLAlchemy / models imports
import models
from sqlalchemy.exc import IntegrityError

# Defensive mapping of DuplicateSlugError from models (may already exist)
try:
    DuplicateSlugError = models.DuplicateSlugError
except Exception:
    class DuplicateSlugError(Exception):
        pass

# Try importing Flask current_app for configuration overrides; tolerate absence.
try:
    from flask import current_app
except Exception:
    current_app = None  # type: ignore

# Constants
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
MAX_DB_SLUG_LENGTH = 255  # matches models.ShortURL.slug column
DEFAULT_SLUG_LENGTH = 8
CUSTOM_SLUG_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,255}$")  # exported validator uses this

# Default reserved and blacklisted words (module-level fallback)
_DEFAULT_RESERVED = {
    "admin", "login", "logout", "register", "dashboard", "api", "s", "shorten",
    "static", "auth", "verify-email", "health", "favicon.ico", "well-known"
}
_DEFAULT_BLACKLIST = {
    "null", "none", "undefined", "favicon.ico"
}

# Exceptions
class UniqueSlugGenerationError(Exception):
    """Raised when generate/find attempts exceed max_retries without finding a unique slug."""
    pass


# -------------------------
# Trace helper (best-effort; non-blocking)
# -------------------------
def _write_trace(msg: str):
    try:
        with open("trace_KAN-118.txt", "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # trace failures must not break runtime
        pass


# -------------------------
# Configuration helpers
# -------------------------
def _load_list_config(key: str, env_name: str, default: Set[str]) -> Set[str]:
    """
    Load a set of strings from:
      1) current_app.config[key] (if available)
      2) environment variable env_name (comma-separated)
      3) default set
    Returns a set of lowercased strings for case-insensitive comparisons.
    """
    # 1) Flask config
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is not None:
                if isinstance(v, (list, set, tuple)):
                    return set(str(x).lower() for x in v if x)
                if isinstance(v, str):
                    # allow comma-separated string from config
                    parts = [p.strip() for p in v.split(",") if p.strip()]
                    return set(p.lower() for p in parts)
    except Exception:
        pass

    # 2) environment variable
    try:
        env_v = os.environ.get(env_name, None)
        if env_v:
            parts = [p.strip() for p in env_v.split(",") if p.strip()]
            return set(p.lower() for p in parts)
    except Exception:
        pass

    # 3) default
    return set(x.lower() for x in default)


def _reserved_slugs() -> Set[str]:
    return _load_list_config("RESERVED_SLUGS", "RESERVED_SLUGS", _DEFAULT_RESERVED)


def _blacklisted_slugs() -> Set[str]:
    return _load_list_config("BLACKLISTED_SLUGS", "BLACKLISTED_SLUGS", _DEFAULT_BLACKLIST)


def _disallow_numeric_slugs() -> bool:
    """
    Configurable toggle for rejecting purely-numeric slugs.
    Default is True (reject numeric-only slugs).
    """
    try:
        if current_app is not None and ("DISALLOW_NUMERIC_SLUGS" in current_app.config):
            return bool(current_app.config.get("DISALLOW_NUMERIC_SLUGS"))
    except Exception:
        pass
    try:
        env_v = os.environ.get("DISALLOW_NUMERIC_SLUGS", None)
        if env_v is not None:
            return env_v.lower() not in ("0", "false", "no", "")
    except Exception:
        pass
    return True


# -------------------------
# Core utilities
# -------------------------
def generate_slug(length: int = DEFAULT_SLUG_LENGTH,
                  alphabet: str = DEFAULT_ALPHABET,
                  deterministic_source: Optional[str] = None,
                  secret: Optional[str] = None) -> str:
    """
    Generate a URL-safe slug.

    - By default uses cryptographically secure randomness via secrets.choice over 'alphabet'.
    - If deterministic_source (string) and secret are provided, the slug is derived
      deterministically by HMAC-SHA256(deterministic_source, secret) and mapped into the alphabet.

    Returns a string of requested length. Does NOT enforce DB uniqueness or reserved/blacklist constraints.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if length > MAX_DB_SLUG_LENGTH:
        # keep callers from creating slugs longer than DB column
        length = MAX_DB_SLUG_LENGTH

    if deterministic_source is not None:
        if secret is None:
            raise ValueError("deterministic_source provided but secret is None")
        # HMAC-SHA256 for deterministic, secret-keyed digest
        try:
            digest = hmac.new(secret.encode("utf-8"), deterministic_source.encode("utf-8"), hashlib.sha256).digest()
        except Exception:
            # Fallback: use simple sha256 without secret if hmac fails (defensive)
            digest = hashlib.sha256((deterministic_source + (secret or "")).encode("utf-8")).digest()

        # Map bytes to alphabet deterministically
        alpha_len = len(alphabet)
        out_chars = []
        idx = 0
        while len(out_chars) < length:
            # Expand digest deterministically if needed
            if idx >= len(digest):
                # re-digest using previous digest to get more deterministic bytes
                digest = hashlib.sha256(digest).digest()
                idx = 0
            val = digest[idx]
            idx += 1
            out_chars.append(alphabet[val % alpha_len])
        slug = "".join(out_chars)
        return slug

    # Non-deterministic: use secrets.choice
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_custom_slug(slug: str) -> bool:
    """
    Validate a user-provided custom slug.

    Rules:
      - Allowed chars: A-Z a-z 0-9 _ -
      - Length: 1..255 (DB constraint)
      - Not present in RESERVED_SLUGS (case-insensitive)
      - Not present in BLACKLISTED_SLUGS (case-insensitive)
      - Optionally reject pure-numeric slugs (configurable)

    Returns True if valid, False otherwise.
    """
    # Basic type/length/charset validation
    if not isinstance(slug, str):
        _write_trace(f"VALIDATE_REJECT type_not_str slug={repr(slug)}")
        return False
    if len(slug) == 0 or len(slug) > MAX_DB_SLUG_LENGTH:
        _write_trace(f"VALIDATE_REJECT length slug={repr(slug)}")
        return False
    if not CUSTOM_SLUG_REGEX.match(slug):
        _write_trace(f"VALIDATE_REJECT regex_mismatch slug={repr(slug)}")
        return False

    lower = slug.lower()

    # Reserved words
    try:
        reserved = _reserved_slugs()
        if lower in reserved:
            _write_trace(f"VALIDATE_REJECT reserved slug={repr(slug)}")
            return False
    except Exception:
        # Fail conservative: if config read fails, proceed (do not block) but log trace
        _write_trace(f"VALIDATE_WARN reserved_lookup_failed slug={repr(slug)}")

    # Blacklist words
    try:
        black = _blacklisted_slugs()
        if lower in black:
            _write_trace(f"VALIDATE_REJECT blacklisted slug={repr(slug)}")
            return False
    except Exception:
        _write_trace(f"VALIDATE_WARN blacklist_lookup_failed slug={repr(slug)}")

    # Optionally disallow pure-numeric slugs
    try:
        if _disallow_numeric_slugs() and slug.isdigit():
            _write_trace(f"VALIDATE_REJECT numeric_only slug={repr(slug)}")
            return False
    except Exception:
        # Conservative: if config lookup fails, assume True behavior (as default) already applied
        pass

    _write_trace(f"VALIDATE_ACCEPT slug={repr(slug)}")
    return True


def find_unique_slug(session,
                     length: int = DEFAULT_SLUG_LENGTH,
                     max_retries: int = 5,
                     deterministic_source: Optional[str] = None,
                     secret: Optional[str] = None,
                     reserve_callback: Optional[Callable[[str], Any]] = None,
                     reserve_kwargs: Optional[Dict[str, Any]] = None) -> str:
    """
    Attempt to find (and optionally reserve) a unique slug.

    See original module docstring for detailed behavior.

    Additions:
      - If reserve_callback is not provided we will also avoid returning reserved/blacklisted slugs.
    """
    reserve_kwargs = reserve_kwargs or {}

    for attempt in range(1, max_retries + 1):
        # Generate candidate
        try:
            candidate = generate_slug(length=length, deterministic_source=deterministic_source, secret=secret)
        except Exception as e:
            _write_trace(f"GEN_SLUG_ERROR attempt={attempt} err={str(e)}")
            continue

        _write_trace(f"GEN_SLUG_ATTEMPT attempt={attempt} candidate={candidate}")

        # If candidate collides with reserved/blacklist, skip quickly (non-DB check)
        try:
            lower = candidate.lower()
            if lower in _reserved_slugs() or lower in _blacklisted_slugs():
                _write_trace(f"GEN_SLUG_SKIPPED_RESERVED attempt={attempt} candidate={candidate}")
                continue
        except Exception:
            # If config/library fails, don't block generation; log and proceed
            _write_trace(f"GEN_SLUG_WARN reserved_lookup_err candidate={candidate}")

        if reserve_callback:
            try:
                reserve_callback(candidate, **(reserve_kwargs or {}))
                _write_trace(f"GEN_SLUG_RESERVED attempt={attempt} slug={candidate}")
                return candidate
            except DuplicateSlugError:
                _write_trace(f"GEN_SLUG_COLLISION_DUPLICATE attempt={attempt} slug={candidate}")
                continue
            except IntegrityError as ie:
                _write_trace(f"GEN_SLUG_COLLISION_INTEGRITY attempt={attempt} slug={candidate} err={str(ie)}")
                try:
                    session.rollback()
                except Exception:
                    pass
                continue
            except Exception as e:
                _write_trace(f"GEN_SLUG_RESERVE_ERROR attempt={attempt} slug={candidate} err={str(e)}")
                raise
        else:
            # Non-atomic existence check; also avoid reserved/blacklist
            try:
                exists = session.query(models.ShortURL).filter_by(slug=candidate).first()
                if exists:
                    _write_trace(f"GEN_SLUG_EXISTS attempt={attempt} slug={candidate}")
                    continue
                # Candidate not present and not reserved: return it
                _write_trace(f"GEN_SLUG_UNIQUE_CHECK attempt={attempt} slug={candidate}")
                return candidate
            except Exception as e:
                _write_trace(f"GEN_SLUG_DB_READ_ERROR attempt={attempt} err={str(e)}")
                try:
                    session.rollback()
                except Exception:
                    pass
                continue

    # Exhausted retries
    msg = f"Unable to generate unique slug after {max_retries} attempts (length={length})."
    _write_trace(f"GEN_SLUG_FAILED {msg}")
    raise UniqueSlugGenerationError(msg)


def suggest_alternatives(base_slug: str,
                         count: int = 5,
                         length: int = DEFAULT_SLUG_LENGTH,
                         session=None) -> List[str]:
    """
    Suggest alternative slugs derived from base_slug.

    Strategies employed:
      - Numeric suffixes: base-1, base-2, ...
      - Random tail suffixes: base-<random>
      - Purely random slugs as last resort

    Filters:
      - Exclude reserved and blacklisted slugs.
      - If session provided, exclude slugs that already exist in DB.
      - Ensure returned suggestions pass validate_custom_slug.

    Returns up to 'count' suggestions.
    """
    suggestions: List[str] = []
    tried: Set[str] = set()

    # Lowercased sets for quick checks
    try:
        reserved = _reserved_slugs()
    except Exception:
        reserved = set(x.lower() for x in _DEFAULT_RESERVED)
    try:
        black = _blacklisted_slugs()
    except Exception:
        black = set(x.lower() for x in _DEFAULT_BLACKLIST)

    def is_available(s: str) -> bool:
        ls = s.lower()
        if ls in tried:
            return False
        if not validate_custom_slug(s):
            return False
        if ls in reserved or ls in black:
            return False
        if session is not None:
            try:
                exists = session.query(models.ShortURL).filter_by(slug=s).first()
                if exists:
                    return False
            except Exception:
                # On DB check failure, be conservative: avoid recommending (do not raise)
                _write_trace(f"SUGGEST_DB_CHECK_FAILED candidate={s}")
                return False
        return True

    # 1) numeric suffixes up to a reasonable limit
    i = 1
    numeric_attempts_limit = max(count * 10, 50)
    while len(suggestions) < count and i <= numeric_attempts_limit:
        candidate = f"{base_slug}-{i}"
        if len(candidate) > MAX_DB_SLUG_LENGTH:
            trim_len = MAX_DB_SLUG_LENGTH - len(f"-{i}")
            candidate = f"{base_slug[:trim_len]}-{i}"
        if is_available(candidate):
            suggestions.append(candidate)
            tried.add(candidate.lower())
        i += 1

    # 2) short random tails
    attempts = 0
    random_tail_len = min(4, max(2, length // 4))
    while len(suggestions) < count and attempts < count * 20:
        tail = generate_slug(length=random_tail_len)
        candidate = f"{base_slug}-{tail}"
        if len(candidate) > MAX_DB_SLUG_LENGTH:
            trim_len = MAX_DB_SLUG_LENGTH - (1 + len(tail))
            candidate = f"{base_slug[:trim_len]}-{tail}"
        if is_available(candidate):
            suggestions.append(candidate)
            tried.add(candidate.lower())
        attempts += 1

    # 3) purely random fallback
    attempts = 0
    while len(suggestions) < count and attempts < count * 50:
        candidate = generate_slug(length=length)
        if is_available(candidate):
            suggestions.append(candidate)
            tried.add(candidate.lower())
        attempts += 1

    _write_trace(f"SUGGEST_ALTERNATIVES base={base_slug} returned={suggestions}")
    return suggestions[:count]

# End of utils/shortener.py