"""utils/ip.py - Core IP anonymization utility for KAN-116.

Responsibilities:
 - Provide anonymize_ip(ip: str) -> Optional[str] that:
    - Uses ipaddress.ip_address to parse input.
    - For IPv4: zero the last octet (mask 0xFFFFFF00) and return normalized dotted-quad string.
    - For IPv6: zero the lower N bits (policy default: lower 80 bits), return normalized compressed IPv6 string.
    - Returns None for invalid inputs.
 - Allow configuration override for IPv6 mask width via:
    - Flask current_app.config["IPV6_ANON_MASK_LOWER_BITS"] (if Flask app context available)
    - Environment variable IPV6_ANON_MASK_LOWER_BITS
    - Function parameter ipv6_mask_lower_bits (argument wins if provided)
 - Best-effort non-blocking trace written to trace_KAN-116.txt for Architectural Memory.
 - Defensive: strips IPv6 scope zones (e.g., "%eth0"), tolerant of minor formatting differences.

Notes:
 - Default behavior masks lower 80 bits for IPv6 (i.e., retains top 48 bits).
 - Returns canonical string representation (IPv4 dotted-quad; IPv6 compressed form from ipaddress).
"""

from typing import Optional
import os
import time
import ipaddress

# Try to import Flask's current_app for configuration override if present at runtime; tolerate absence.
try:
    from flask import current_app
except Exception:
    current_app = None


def _write_trace(entry: str) -> None:
    """Best-effort, non-blocking write to trace_KAN-116.txt for Architectural Memory."""
    try:
        with open("trace_KAN-116.txt", "a") as f:
            f.write(f"{time.time():.6f} {entry}\n")
    except Exception:
        # Trace writes must not raise in production or tests
        pass


def _get_ipv6_lower_bits_config(explicit: Optional[int] = None) -> int:
    """
    Determine how many lower bits to zero for IPv6 anonymization.

    Precedence:
      1) explicit argument passed to anonymize_ip()
      2) Flask app config current_app.config["IPV6_ANON_MASK_LOWER_BITS"] (if available)
      3) Environment variable IPV6_ANON_MASK_LOWER_BITS
      4) Default policy: 80
    """
    if explicit is not None:
        try:
            v = int(explicit)
            return max(0, min(128, v))
        except Exception:
            pass

    # Flask config if available
    try:
        if current_app is not None:
            v = current_app.config.get("IPV6_ANON_MASK_LOWER_BITS", None)
            if v is not None:
                return max(0, min(128, int(v)))
    except Exception:
        pass

    # Environment variable fallback
    try:
        env_v = os.environ.get("IPV6_ANON_MASK_LOWER_BITS", None)
        if env_v is not None:
            return max(0, min(128, int(env_v)))
    except Exception:
        pass

    # Default policy: zero lower 80 bits
    return 80


def anonymize_ip(ip: str, ipv6_mask_lower_bits: Optional[int] = None) -> Optional[str]:
    """
    Anonymize a single IP string.

    Parameters:
      - ip: input IP address string (IPv4 or IPv6). May include an IPv6 zone ("%eth0"); that will be stripped.
      - ipv6_mask_lower_bits: optional override for how many lower bits to zero for IPv6 (integer 0..128).
        If None, configuration / environment / default will apply.

    Returns:
      - Normalized masked IP string (IPv4 dotted-quad or IPv6 compressed) on success.
      - None on invalid input (parsing failure).
    """
    if not ip or not isinstance(ip, str):
        _write_trace(f"ANON_INPUT_INVALID ip={repr(ip)}")
        return None

    candidate = ip.strip()
    # Strip IPv6 zone identifiers (e.g., fe80::1%eth0 -> fe80::1)
    if "%" in candidate:
        try:
            candidate = candidate.split("%", 1)[0]
        except Exception:
            candidate = candidate.replace("%", "")

    # Defensive: empty after stripping?
    if not candidate:
        _write_trace(f"ANON_INPUT_EMPTY_AFTER_STRIP original={repr(ip)}")
        return None

    try:
        ip_obj = ipaddress.ip_address(candidate)
    except Exception:
        _write_trace(f"ANON_PARSE_FAILED ip={repr(candidate)}")
        return None

    # IPv4: mask last octet (/24)
    if isinstance(ip_obj, ipaddress.IPv4Address):
        try:
            ip_int = int(ip_obj)
            masked_int = ip_int & 0xFFFFFF00  # zero lower 8 bits (last octet)
            result = str(ipaddress.IPv4Address(masked_int))
            _write_trace(f"ANON_IPV4 original={candidate} masked={result}")
            return result
        except Exception:
            _write_trace(f"ANON_IPV4_ERROR ip={repr(candidate)}")
            return None

    # IPv6: zero lower N bits (default N = 80)
    if isinstance(ip_obj, ipaddress.IPv6Address):
        try:
            lower_bits = _get_ipv6_lower_bits_config(ipv6_mask_lower_bits)
            if not (0 <= lower_bits <= 128):
                lower_bits = 80
            # mask out lower 'lower_bits' bits:
            ip_int = int(ip_obj)
            if lower_bits == 0:
                # zero nothing
                masked_int = ip_int
            elif lower_bits >= 128:
                # zero everything; keep 0:: representation
                masked_int = 0
            else:
                mask = (~((1 << lower_bits) - 1)) & ((1 << 128) - 1)
                masked_int = ip_int & mask
            result = str(ipaddress.IPv6Address(masked_int))
            _write_trace(f"ANON_IPV6 original={candidate} lower_bits={lower_bits} masked={result}")
            return result
        except Exception:
            _write_trace(f"ANON_IPV6_ERROR ip={repr(candidate)}")
            return None

    # Unhandled type: defensive
    _write_trace(f"ANON_UNHANDLED_TYPE ip={repr(candidate)} type={type(ip_obj)}")
    return None
# --- END FILE: utils/ip.py ---