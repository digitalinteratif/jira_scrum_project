"""
app_core.utils.url_utils - URL normalization & validation utilities (KAN-177)

Provides:
 - normalize_url(raw_url: str, *, prepend_scheme: Optional[bool]=None, reject_private_ips: Optional[bool]=None,
                 enable_dns: Optional[bool]=None, normalize_trailing_slash: Optional[str]=None) -> str
 - is_private_ip(ip_str: str) -> bool

Behavior and configuration follow project-spec for KAN-177.

This module is defensive and logs actionable events with structured extras.
"""

from __future__ import annotations

import threading
import time
import os
import socket
import ipaddress
from typing import Optional, Iterable, Tuple
from urllib.parse import urlparse, urlunparse, quote, unquote

# Project logger and ValidationError
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)

try:
    from app_core.utils.errors import ValidationError
except Exception:
    # Fallback: use built-in Exception if imports fail (should not happen)
    class ValidationError(Exception):
        pass

# Try Flask current_app for config; tolerant if not present
try:
    from flask import current_app
except Exception:
    current_app = None  # type: ignore

# Trace file helper
_TRACE_FILE = "trace_KAN-177.txt"


def _trace(msg: str) -> None:
    try:
        with open(_TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # best-effort; do not raise
        pass


# Simple config resolution helpers
def _coerce_bool(val, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    try:
        s = str(val).strip().lower()
        return s in ("1", "true", "yes", "y", "on")
    except Exception:
        return default


def _get_config(key: str, default):
    # Prefer Flask current_app.config when available
    try:
        if current_app is not None and hasattr(current_app, "config"):
            v = current_app.config.get(key, None)
            if v is not None:
                return v
    except Exception:
        pass
    try:
        env = os.environ.get(key)
        if env is not None:
            return env
    except Exception:
        pass
    return default


# DNS rate-limiter state (very small window)
_DNS_LOCK = threading.Lock()
_DNS_STATE = {"last_reset": 0.0, "count": 0}


def _allow_dns_lookup(allowed_per_sec: int) -> bool:
    """
    Simple per-process rate limit for DNS lookups. Returns True if allowed.
    """
    now = time.time()
    with _DNS_LOCK:
        last = _DNS_STATE.get("last_reset", 0.0)
        if now - last >= 1.0:
            _DNS_STATE["last_reset"] = now
            _DNS_STATE["count"] = 0
        if _DNS_STATE["count"] >= int(allowed_per_sec):
            return False
        _DNS_STATE["count"] += 1
        return True


def _resolve_host_ips(hostname: str) -> Tuple[bool, Optional[Iterable[str]]]:
    """
    Resolve the hostname to a list of IP strings using socket.getaddrinfo.
    Returns (success, ips) or (False, None) on failure.
    """
    try:
        if not socket:
            return False, None
        infos = socket.getaddrinfo(hostname, None)
        ips = []
        for info in infos:
            try:
                addr = info[4][0]
            except Exception:
                continue
            if addr not in ips:
                ips.append(addr)
        return True, ips
    except Exception as e:
        try:
            logger.debug("DNS resolution failed", extra={"message_key": "url.validation.dns_error", "host": hostname, "err": str(e)})
        except Exception:
            pass
        return False, None


def is_private_ip(ip_str: str) -> bool:
    """
    Decide if an IP string is private/loopback/link-local/reserved.
    Conservative: if parsing fails, treat as private/unsafe.
    """
    try:
        obj = ipaddress.ip_address(ip_str)
        # Treat loopback, link-local, private, reserved as unsafe
        if getattr(obj, "is_loopback", False):
            return True
        if getattr(obj, "is_link_local", False):
            return True
        if getattr(obj, "is_private", False):
            return True
        if getattr(obj, "is_reserved", False):
            return True
        return False
    except Exception:
        # conservative
        return True


def _strip_ipv6_brackets(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _normalize_percent_encoding(component: str, safe: str) -> str:
    """
    Normalize percent-encoding by unquoting then quoting again with safe chars.
    """
    try:
        return quote(unquote(component or ""), safe=safe)
    except Exception:
        # Best-effort fallback
        try:
            return quote(component or "", safe=safe)
        except Exception:
            return component or ""


def _split_userinfo_hostport(netloc: str) -> Tuple[Optional[str], str]:
    """
    Return (userinfo or None, hostport).
    """
    if "@" in netloc:
        try:
            ui, hp = netloc.rsplit("@", 1)
            return ui, hp
        except Exception:
            return None, netloc
    return None, netloc


def _extract_host_port(hostport: str) -> Tuple[str, Optional[int]]:
    """
    Extract host and integer port (if present). Supports IPv6 bracketed form.
    Returns (host, port_or_None)
    """
    host = hostport
    port = None
    # IPv6 bracketed
    if hostport.startswith("["):
        # [ipv6]:port or [ipv6]
        end = hostport.find("]")
        if end != -1:
            h = hostport[1:end]
            rest = hostport[end + 1:]
            if rest.startswith(":"):
                p = rest[1:]
                try:
                    port = int(p)
                except Exception:
                    port = None
            host = h
    else:
        # split last ":" for port, but avoid splitting IPv6 without brackets (rare)
        if hostport.count(":") == 1:
            h, p = hostport.rsplit(":", 1)
            try:
                port = int(p)
                host = h
            except Exception:
                # not a port
                host = hostport
        else:
            host = hostport
    return host, port


def normalize_url(
    raw_url: str,
    *,
    prepend_scheme: Optional[bool] = None,
    reject_private_ips: Optional[bool] = None,
    enable_dns: Optional[bool] = None,
    normalize_trailing_slash: Optional[str] = None,
) -> str:
    """
    Validate and normalize a URL. Returns normalized absolute URL string or raises ValidationError.

    Parameters map to config keys:
      - prepend_scheme -> URL_UTILS_PREPEND_SCHEME (default True)
      - reject_private_ips -> URL_UTILS_REJECT_PRIVATE_IPS (default True)
      - enable_dns -> URL_UTILS_ENABLE_DNS (default False)
      - normalize_trailing_slash -> URL_UTILS_NORMALIZE_TRAILING_SLASH (default 'preserve')
    """
    # Resolve config defaults
    try:
        cfg_prepend = _coerce_bool(prepend_scheme if prepend_scheme is not None else _get_config("URL_UTILS_PREPEND_SCHEME", True), True)
        cfg_default_scheme = str(_get_config("URL_UTILS_DEFAULT_SCHEME", "https") or "https").lower()
        cfg_reject_private = _coerce_bool(reject_private_ips if reject_private_ips is not None else _get_config("URL_UTILS_REJECT_PRIVATE_IPS", True), True)
        cfg_enable_dns = _coerce_bool(enable_dns if enable_dns is not None else _get_config("URL_UTILS_ENABLE_DNS", False), False)
        cfg_dns_rate = int(_get_config("URL_UTILS_DNS_RATE_PER_SEC", 1) or 1)
        max_len = int(_get_config("URL_UTILS_MAX_URL_LENGTH", 4096) or 4096)
        cfg_reject_local_names = _coerce_bool(_get_config("URL_UTILS_REJECT_LOCAL_HOSTNAMES", True), True)
        cfg_allow_userinfo = _coerce_bool(_get_config("URL_UTILS_ALLOW_USERINFO", False), False)
        cfg_trail = str(normalize_trailing_slash if normalize_trailing_slash is not None else _get_config("URL_UTILS_NORMALIZE_TRAILING_SLASH", "preserve") or "preserve").lower()
    except Exception:
        # On config parsing failure, fallback to safe defaults
        cfg_prepend = True
        cfg_default_scheme = "https"
        cfg_reject_private = True
        cfg_enable_dns = False
        cfg_dns_rate = 1
        max_len = 4096
        cfg_reject_local_names = True
        cfg_allow_userinfo = False
        cfg_trail = "preserve"

    # Basic type and size checks
    if not isinstance(raw_url, str):
        raise ValidationError("Invalid URL: must be a string.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "type"})

    s = raw_url.strip()
    if s == "":
        raise ValidationError("Invalid URL: empty.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "empty"})

    if len(s) > max_len:
        raise ValidationError("Invalid URL: too long.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "too_long"})

    # Reject CRLF / control characters
    if "\n" in s or "\r" in s or "\x00" in s:
        logger.warning("URL validation rejected control characters", extra={"message_key": "url.validation.invalid", "input_snip": s[:200]})
        _trace(f"reject_control_chars input_snip={s[:200]}")
        raise ValidationError("Invalid characters in URL.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "control_chars"})

    # Prepend scheme if missing and configured
    parsed = urlparse(s)
    if not parsed.scheme and cfg_prepend:
        s = f"{cfg_default_scheme}://{s}"
        parsed = urlparse(s)

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        logger.warning("URL validation rejected unsupported scheme", extra={"message_key": "url.validation.invalid", "scheme": scheme, "input_snip": s[:200]})
        _trace(f"reject_scheme scheme={scheme} input_snip={s[:200]}")
        raise ValidationError("Unsupported URL scheme.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "scheme"})

    if not parsed.netloc:
        logger.warning("URL validation rejected missing netloc", extra={"message_key": "url.validation.invalid", "input_snip": s[:200]})
        _trace(f"reject_netloc_missing input_snip={s[:200]}")
        raise ValidationError("URL missing host.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "netloc_missing"})

    # Parse netloc -> userinfo and hostport
    userinfo, hostport = _split_userinfo_hostport(parsed.netloc)
    if userinfo:
        # If userinfo contains password (username:password), reject by default
        if ":" in userinfo and not cfg_allow_userinfo:
            logger.warning("URL validation rejected embedded credentials", extra={"message_key": "url.validation.invalid", "input_snip": s[:200]})
            _trace(f"reject_userinfo input_netloc={parsed.netloc}")
            raise ValidationError("URLs with embedded credentials are not allowed.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "userinfo"})

    host_str, port = _extract_host_port(hostport)
    host_str = (host_str or "").strip()
    if host_str == "":
        logger.warning("URL validation rejected empty host after parse", extra={"message_key": "url.validation.invalid", "input_snip": s[:200]})
        _trace(f"reject_host_empty input_netloc={parsed.netloc}")
        raise ValidationError("Invalid host in URL.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "host_empty"})

    # Normalize host via IDNA
    try:
        # Strip brackets for IPv6 literal if present
        host_for_idna = _strip_ipv6_brackets(host_str)
        host_idna = host_for_idna.encode("idna").decode("ascii")
    except Exception:
        logger.warning("URL validation rejected invalid hostname (IDNA failure)", extra={"message_key": "url.validation.invalid", "host": host_str, "input_snip": s[:200]})
        _trace(f"reject_idna host={host_str}")
        raise ValidationError("Invalid hostname in URL.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "idna"})

    # Rebuild netloc, avoid re-inserting password
    netloc_parts = []
    if userinfo and cfg_allow_userinfo:
        # If userinfo contained password and allowance is true, strip password for safety
        if ":" in userinfo:
            user_only = userinfo.split(":", 1)[0]
            netloc_parts.append(user_only + "@")
        else:
            netloc_parts.append(userinfo + "@")

    # Handle IPv6 literal bracketed form when host contains colon
    try:
        if ":" in host_idna and not host_idna.startswith("["):
            host_netloc = f"[{host_idna}]"
        else:
            host_netloc = host_idna
    except Exception:
        host_netloc = host_idna

    # Remove default ports
    if port is not None:
        try:
            p_int = int(port)
            if (scheme == "http" and p_int == 80) or (scheme == "https" and p_int == 443):
                port = None
        except Exception:
            pass

    if port:
        netloc_parts.append(f"{host_netloc}:{port}")
    else:
        netloc_parts.append(host_netloc)
    normalized_netloc = "".join(netloc_parts)

    # Host safety checks
    # Is host an IP literal?
    is_ip_literal = False
    try:
        maybe_host = _strip_ipv6_brackets(host_idna)
        ipaddress.ip_address(maybe_host)
        is_ip_literal = True
    except Exception:
        is_ip_literal = False

    if is_ip_literal:
        try:
            # If IP literal, check private ranges
            if cfg_reject_private:
                if is_private_ip(maybe_host):
                    logger.warning("URL validation rejected private IP literal", extra={"message_key": "url.validation.invalid", "host": maybe_host, "input_snip": s[:200]})
                    _trace(f"reject_ip_literal_private host={maybe_host}")
                    raise ValidationError("Target host is not allowed by policy.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "private_ip"})
        except ValidationError:
            raise
        except Exception:
            # Any unexpected error treat as unsafe
            logger.exception("Error while evaluating IP literal safety", extra={"message_key": "url.validation.invalid", "host": host_idna})
            raise ValidationError("URL validation failed for operational reasons.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "ip_check_error"})
    else:
        # host is DNS name
        lower_host = host_idna.lower()
        if cfg_reject_local_names and lower_host in ("localhost", "ip6-localhost", "localhost.localdomain"):
            logger.warning("URL validation rejected local hostname", extra={"message_key": "url.validation.invalid", "host": lower_host, "input_snip": s[:200]})
            _trace(f"reject_local_name host={lower_host}")
            raise ValidationError("Target host is not allowed by policy.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "local_hostname"})

        # Optionally resolve and inspect resolved IPs
        if cfg_enable_dns:
            allowed = _allow_dns_lookup(cfg_dns_rate)
            if not allowed:
                logger.warning("URL validation DNS rate limit hit", extra={"message_key": "url.validation.invalid", "host": lower_host})
                _trace(f"dns_rate_limited host={lower_host}")
                raise ValidationError("URL validation temporarily unavailable.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "dns_rate_limit"})
            success, ips = _resolve_host_ips(lower_host)
            if not success or not ips:
                logger.warning("URL validation DNS resolution failed", extra={"message_key": "url.validation.invalid", "host": lower_host})
                _trace(f"dns_resolve_failed host={lower_host}")
                raise ValidationError("Unable to resolve host for URL.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "dns_fail"})
            if cfg_reject_private:
                for ip in ips:
                    if is_private_ip(ip):
                        logger.warning("URL validation rejected DNS-resolved private address", extra={"message_key": "url.validation.invalid", "host": lower_host, "resolved_ip": ip, "input_snip": s[:200]})
                        _trace(f"dns_resolve_private host={lower_host} ip={ip}")
                        raise ValidationError("Target host resolves to a private or disallowed address.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "dns_private"})

    # Normalize path/query/fragment percent-encoding
    path = _normalize_percent_encoding(parsed.path or "", safe="/%:@[]!$&'()*+,;=")
    query = _normalize_percent_encoding(parsed.query or "", safe="=&?/")
    fragment = _normalize_percent_encoding(parsed.fragment or "", safe="")

    # Trailing slash normalization
    try:
        if cfg_trail == "strip":
            if path not in ("", "/"):
                path = path.rstrip("/")
            if path == "":
                path = "/"
        elif cfg_trail == "ensure":
            if not path.endswith("/"):
                path = path + "/"
    except Exception:
        # ignore normalization failures, preserve path
        pass

    normalized = urlunparse((scheme, normalized_netloc, path, parsed.params or "", query, fragment))

    # Final safety net: ensure no CRLF in constructed URL
    if "\n" in normalized or "\r" in normalized or "\x00" in normalized:
        logger.error("Normalized URL contains control characters (unexpected)", extra={"message_key": "url.validation.invalid", "normalized": normalized})
        _trace(f"reject_post_normalize_control normalized={normalized[:200]}")
        raise ValidationError("Invalid URL after normalization.", status_code=400, extra={"message_key": "url.validation.invalid", "reason": "post_normalize_control"})

    # Log accept
    try:
        logger.info("URL validated and normalized", extra={"message_key": "url.validation.accept", "normalized": normalized})
    except Exception:
        pass
    _trace(f"accept normalized={normalized}")

    return normalized


# Backwards-compatible alias
__all__ = ["normalize_url", "is_private_ip"]
--- END FILE ---