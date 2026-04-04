"""utils/security.py - Security helpers (CSRF helpers, IP anonymization, etc.).

Updated for KAN-116:
 - The high-level helper anonymize_ip(remote_addr, x_forwarded_for, trust_xff) now delegates to the
   new core utils.ip.anonymize_ip(ip: str) which implements canonical masking rules and config overrides.
 - Backwards-compatible behavior retained: on parsing/invalid cases the wrapper returns an empty string
   (so callers that treat empty string as "no IP" remain unchanged). The core returns None on invalid.
 - Fallback: if utils.ip cannot be imported (e.g., transient test environment), a conservative local
   masking implementation is used (keeps last octet/hextet zeroed).
"""

from typing import Optional
import ipaddress

# Try to import the new core anonymizer; tolerate import failures per dependency tolerance.
try:
    from utils.ip import anonymize_ip as _core_anonymize_ip  # type: ignore
except Exception:
    _core_anonymize_ip = None  # fallback will be used


def anonymize_ip(remote_addr: Optional[str] = None, x_forwarded_for: Optional[str] = None, trust_xff: bool = False) -> str:
    """
    High-level wrapper used by routes. Accepts either a request.remote_addr + X-Forwarded-For header pair
    (or a single ip string) and returns a privacy-preserving anonymized IP string.

    Returns:
      - Masked IP string on success (e.g., "203.0.113.0" or "2001:db8:85a3::").
      - Empty string "" when there is no usable client IP or parsing failed (callers treat as NULL).
    """
    ip_candidate = None
    if trust_xff and x_forwarded_for:
        try:
            ip_candidate = x_forwarded_for.split(",")[0].strip()
        except Exception:
            ip_candidate = x_forwarded_for.strip() if x_forwarded_for else None
    if not ip_candidate:
        ip_candidate = remote_addr or ""

    if not ip_candidate:
        return ""

    # Try to use the canonical core anonymizer first
    if _core_anonymize_ip is not None:
        try:
            masked = _core_anonymize_ip(ip_candidate)
            # core returns None for invalid inputs; wrapper should return empty string to preserve prior semantics
            return masked if (masked is not None) else ""
        except Exception:
            # Fallthrough to defensive local implementation below
            pass

    # Defensive fallback anonymizer (conservative)
    try:
        candidate = ip_candidate.strip()
        # Strip IPv6 zone if present
        if "%" in candidate:
            candidate = candidate.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(candidate)
            if isinstance(ip_obj, ipaddress.IPv4Address):
                parts = candidate.split(".")
                if len(parts) == 4:
                    parts[-1] = "0"
                    return ".".join(parts)
                return candidate
            if isinstance(ip_obj, ipaddress.IPv6Address):
                # Zero the last hextet (very conservative fallback)
                exploded = ip_obj.exploded
                parts = exploded.split(":")
                parts[-1] = "0000"
                return ":".join(parts)
        except Exception:
            # Try string-based masking fallbacks
            if "." in candidate:
                parts = candidate.split(".")
                if len(parts) >= 4:
                    parts[-1] = "0"
                    return ".".join(parts[:4])
                return ""
            if ":" in candidate:
                parts = candidate.split(":")
                parts[-1] = "0000"
                return ":".join(parts)
    except Exception:
        pass

    return ""
# --- END FILE: utils/security.py ---