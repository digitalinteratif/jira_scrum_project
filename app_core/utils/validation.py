"""utils/validation.py - URL validation and safe-target enforcement for KAN-123

Responsibilities (surgical, single-file):
 - Provide validate_and_normalize_url(raw_url: str, *,
     allow_private: Optional[bool]=None,
     enable_dns: Optional[bool]=None) -> str
     which:
       - Parses & normalizes an absolute URL (http/https only).
       - IDNA-encodes hostnames (punycode).
       - Ensures a parseable netloc is present.
       - Rejects private/internal IP addresses unless allowed by config.
       - Applies domain whitelist/blacklist rules from config/env.
       - Optionally performs DNS resolution (rate-limited) to check resolved IPs for private ranges.
       - Raises ValueError on invalid/unsafe URLs.
       - Returns a normalized absolute URL string (safe to redirect to).
 - Provide is_safe_target(host_or_netloc: str, *,
     allow_private: Optional[bool]=None,
     enable_dns: Optional[bool]=None) -> bool
     which:
       - Accepts a host string (possibly host:port or [ipv6]:port or userinfo@host:port)
       - Determines whether the host is acceptable according to policy.
 - Be defensive about external dependencies and Flask app context (follow project "dependency tolerance").
 - Provide best-effort trace lines to trace_KAN-123.txt for Architectural Memory.
 - Implement simple in-process DNS rate-limiting to avoid being abused for heavy resolution loads.

Notes about config keys (lookups are defensive and fall back to env/defaults):
 - ALLOW_PRIVATE_TARGETS (bool)            -> allow private/internal IP targets (default: False)
 - VALIDATION_ENABLE_DNS (bool)            -> whether DNS resolution is enabled (default: False)
 - VALIDATION_DNS_PER_SEC (int)            -> DNS lookups per second allowed (rate-limit; default: 1)
 - VALIDATION_WHITELIST_DOMAINS (list/str) -> comma-separated or list of allowed domains (optional)
 - VALIDATION_BLACKLIST_DOMAINS (list/str) -> comma-separated or list of blocked domains (optional)

This file is intentionally self-contained; other modules import validate_and_normalize_url
(or validate_url / is_safe_target) and will get robust validation/re-validation on redirect
to mitigate SSRF / open-redirect risks.

Surgical rule: Only this file is added/modified for KAN-123.
"""

from typing import Optional, Tuple, Iterable
import time
import threading
import os
import re

# Standard library imports (defensive)
try:
    from urllib.parse import urlparse, urlunparse, quote, unquote
except Exception:
    # Extremely defensive fallback (should not happen in normal Python)
    raise

try:
    import ipaddress
except Exception:
    ipaddress = None  # will raise if used; keep defensive

try:
    import socket
except Exception:
    socket = None

# Try to import Flask current_app for config access; tolerate absence.
try:
    from flask import current_app
except Exception:
    current_app = None  # type: ignore

# Trace file for architectural memory
_TRACE_FILE = "trace_KAN-123.txt"


def _trace(msg: str) -> None:
    """Best-effort non-blocking trace writer; must not raise."""
    try:
        with open(_TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


# Simple in-process rate limiter for DNS resolutions (sliding window per-second counter).
_DNS_STATS = {
    "last_reset": 0.0,
    "count": 0,
}
_DNS_LOCK = threading.Lock()


# Helper: load config with defensive fallbacks
def _get_config_bool(key: str, default: bool) -> bool:
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is not None:
                return bool(v)
    except Exception:
        pass
    try:
        env_v = os.environ.get(key, None)
        if env_v is not None:
            return env_v.lower() not in ("0", "false", "no", "")
    except Exception:
        pass
    return default


def _get_config_int(key: str, default: int) -> int:
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


def _get_config_list(key: str, env_name: str) -> Optional[Iterable[str]]:
    """
    Return a lowercased iterable of strings if found in Flask config (list/str) or env var (comma-separated).
    Returns None if not configured.
    """
    try:
        if current_app is not None:
            v = current_app.config.get(key, None)
            if v is None:
                pass
            elif isinstance(v, (list, tuple, set)):
                return [str(x).lower() for x in v if x]
            elif isinstance(v, str):
                parts = [p.strip().lower() for p in v.split(",") if p.strip()]
                return parts
    except Exception:
        pass
    try:
        env_v = os.environ.get(env_name, None)
        if env_v:
            parts = [p.strip().lower() for p in env_v.split(",") if p.strip()]
            return parts
    except Exception:
        pass
    return None


# Domain matching helpers
def _domain_matches(domain: str, pattern: str) -> bool:
    """
    Case-insensitive match where pattern may be:
      - exact domain (example.com)
      - leading-dot means subdomains allowed (.example.com) or we accept suffix match
    We'll accept either exact match or suffix match with '.' boundary to allow subdomains,
    i.e., pattern 'example.com' will match 'example.com' and 'sub.example.com'.
    """
    domain = domain.lower().strip(".")
    pattern = pattern.lower().strip(".")
    if domain == pattern:
        return True
    # suffix match (.pattern)
    if domain.endswith("." + pattern):
        return True
    return False


# Private/internal networks per RFCs
def _is_private_ip(ip_str: str) -> bool:
    """
    Return True if ip_str is in private / link-local / loopback / unspecified ranges.
    Uses ipaddress module if available.
    """
    if ipaddress is None:
        # Conservative fallback: treat addresses that look like RFC1918 prefixes as private
        if ip_str.startswith("10.") or ip_str.startswith("192.168.") or ip_str.startswith("172."):
            return True
        if ip_str.startswith("127.") or ip_str.startswith("169.254."):
            return True
        if ":" in ip_str:
            # Do not attempt to be clever; return True conservatively only for loopback ::
            if ip_str == "::1":
                return True
        return False

    try:
        obj = ipaddress.ip_address(ip_str)
        # ipaddress exposes properties: is_private, is_loopback, is_link_local, is_reserved, is_multicast
        if getattr(obj, "is_loopback", False):
            return True
        if getattr(obj, "is_link_local", False):
            return True
        if getattr(obj, "is_private", False):
            return True
        # Treat unspecified/reserved as "not allowed" for safety
        if getattr(obj, "is_reserved", False):
            return True
        # IPv6 unique local addresses (fc00::/7) are covered by is_private for ipaddress module
        return False
    except Exception:
        # Parsing failed -> conservative True (treat as unsafe)
        return True


def _rate_limit_dns(allowed_per_sec: int) -> bool:
    """
    Simple in-process rate limiter.
    Returns True when a DNS resolution is allowed; False when rate limit exceeded.
    """
    now = time.time()
    with _DNS_LOCK:
        last = _DNS_STATS.get("last_reset", 0.0)
        if now - last >= 1.0:
            # reset
            _DNS_STATS["last_reset"] = now
            _DNS_STATS["count"] = 0
        if _DNS_STATS["count"] >= allowed_per_sec:
            return False
        _DNS_STATS["count"] += 1
        return True


def _resolve_host_ips(hostname: str) -> Tuple[bool, Optional[Iterable[str]]]:
    """
    Resolve hostname to IP addresses (both IPv4 and IPv6) using socket.getaddrinfo.
    Returns (success, iterable_of_ip_strings) or (False, None) on failure.
    Defensive: must not raise to callers.
    """
    if socket is None:
        return False, None
    try:
        # getaddrinfo may return many results; collect unique IPs
        infos = socket.getaddrinfo(hostname, None)
        ips = []
        for info in infos:
            # info[4] can be (addr, port) for IPv4 or (addr, port, flowinfo, scopeid) for IPv6
            addr = info[4][0]
            if addr not in ips:
                ips.append(addr)
        return True, ips
    except Exception:
        return False, None


def _extract_host_from_netloc(netloc: str) -> Tuple[str, Optional[str]]:
    """
    Given a netloc string (possibly with userinfo and port), extract (host, port_or_None).
    Examples:
      - "user:pass@host:123" -> ("host", "123")
      - "[::1]:8080" -> ("::1", "8080")
      - "example.com" -> ("example.com", None)
    This function does not perform IDNA conversion.
    """
    if "@" in netloc:
        try:
            _, hostport = netloc.rsplit("@", 1)
        except Exception:
            hostport = netloc
    else:
        hostport = netloc

    # IPv6 literal in brackets?
    if hostport.startswith("["):
        # Expect form [ipv6addr]:port or [ipv6addr]
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", hostport)
        if m:
            return m.group(1), m.group(2)
        # fallback: strip brackets
        stripped = hostport.strip("[]")
        if ":" in stripped:
            # treat entire as IPv6
            return stripped, None

    # Otherwise split by last ':'
    if ":" in hostport:
        # Could be IPv6 without brackets (rare); try to detect numeric colons.
        if hostport.count(":") == 1:
            host, port = hostport.rsplit(":", 1)
            return host, (port if port else None)
        else:
            # Many colons -> probably IPv6 without brackets; return as-is
            return hostport, None

    return hostport, None


def is_safe_target(host_or_netloc: str, *,
                   allow_private: Optional[bool] = None,
                   enable_dns: Optional[bool] = None) -> bool:
    """
    Determine whether the given host/netloc is an acceptable redirect target.

    Parameters:
      - host_or_netloc: host portion (netloc) extracted from URL.parse(). Can include userinfo and port.
      - allow_private: explicit override for allowing private/Internal IPs (None -> consult config/env -> default False).
      - enable_dns: explicit override whether to perform DNS resolution (None -> consult config/env -> default False).

    Returns:
      - True if host is considered safe.
      - False otherwise.

    Behavior:
      - If host is an IP literal (v4 or v6), check it is not private/reserved unless allow_private True.
      - If host is a hostname:
          - apply blacklist/whitelist domain policy (config/env).
          - if enable_dns True, resolve DNS (rate-limited) and ensure resolved IPs are not private/reserved unless allow_private True.
    """
    try:
        # Resolve effective config
        if allow_private is None:
            allow_private = _get_config_bool("ALLOW_PRIVATE_TARGETS", False)
        if enable_dns is None:
            enable_dns = _get_config_bool("VALIDATION_ENABLE_DNS", False)

        # Extract host (strip userinfo/port)
        host, port = _extract_host_from_netloc(host_or_netloc or "")
        if not host:
            _trace(f"IS_SAFE_REJECT empty_host input={repr(host_or_netloc)}")
            return False

        # Strip surrounding whitespace and possible brackets for IPv6
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]

        # Attempt to detect IP-literal quickly
        is_ip_literal = False
        try:
            if ipaddress is not None:
                ipaddress.ip_address(host)
                is_ip_literal = True
            else:
                # conservative textual heuristic: IPv4 dotted-quads or presence of ":"
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
                    is_ip_literal = True
                elif ":" in host:
                    is_ip_literal = True
        except Exception:
            is_ip_literal = False

        if is_ip_literal:
            # Validate IP
            if _is_private_ip(host):
                if allow_private:
                    _trace(f"IS_SAFE_ACCEPT ip_private_but_allowed host={host}")
                    return True
                else:
                    _trace(f"IS_SAFE_REJECT ip_private host={host}")
                    return False
            # Otherwise public IP -> ok
            _trace(f"IS_SAFE_ACCEPT ip_public host={host}")
            return True

        # Host is a DNS name; normalize via IDNA for comparisons
        try:
            # Use Python's idna through encode/decode; defensive try/except
            idna_host = host.encode("idna").decode("ascii")
        except Exception:
            # Bad hostname
            _trace(f"IS_SAFE_REJECT idna_fail host={host}")
            return False

        # Check domain blacklist/whitelist
        # Whitelist takes precedence: if configured and non-empty, only allow domains matching whitelist.
        whitelist = _get_config_list("VALIDATION_WHITELIST_DOMAINS", "VALIDATION_WHITELIST_DOMAINS")
        blacklist = _get_config_list("VALIDATION_BLACKLIST_DOMAINS", "VALIDATION_BLACKLIST_DOMAINS")

        if whitelist:
            allowed = any(_domain_matches(idna_host, w) for w in whitelist)
            if not allowed:
                _trace(f"IS_SAFE_REJECT whitelist_miss host={idna_host} whitelist={whitelist}")
                return False
            else:
                _trace(f"IS_SAFE_ACCEPT whitelist_match host={idna_host} whitelist={whitelist}")
                # even if whitelisted, optionally we might want to skip DNS check; but continue below to perform resolution if enabled.

        if blacklist:
            blocked = any(_domain_matches(idna_host, b) for b in blacklist)
            if blocked:
                _trace(f"IS_SAFE_REJECT blacklist_match host={idna_host} blacklist={blacklist}")
                return False

        # Optionally perform DNS resolution to ensure name does not resolve to private IPs
        if enable_dns:
            allowed_per_sec = _get_config_int("VALIDATION_DNS_PER_SEC", 1)
            if not _rate_limit_dns(allowed_per_sec):
                _trace(f"IS_SAFE_REJECT dns_rate_limited host={idna_host}")
                # Conservative: if we cannot safely perform DNS resolution due to rate limiting, reject
                return False

            success, ips = _resolve_host_ips(idna_host)
            if not success or not ips:
                _trace(f"IS_SAFE_REJECT dns_resolve_failed host={idna_host}")
                return False

            for ip in ips:
                if _is_private_ip(ip):
                    if not allow_private:
                        _trace(f"IS_SAFE_REJECT dns_resolved_private host={idna_host} ip={ip}")
                        return False
            _trace(f"IS_SAFE_ACCEPT dns_resolved_public host={idna_host} ips={ips}")
            return True

        # If DNS not enabled, accept based on domain lists and syntactic checks (IDNA succeeded)
        _trace(f"IS_SAFE_ACCEPT no_dns host={idna_host}")
        return True

    except Exception as e:
        # On unexpected internal errors, be conservative and reject
        _trace(f"IS_SAFE_ERROR host={repr(host_or_netloc)} err={str(e)}")
        return False


def validate_and_normalize_url(raw_url: str, *,
                               allow_private: Optional[bool] = None,
                               enable_dns: Optional[bool] = None) -> str:
    """
    Validate and return a normalized absolute URL string suitable for safe redirects.

    Raises:
      - ValueError on invalid/unsafe URL.

    Normalization steps:
      - Strip whitespace and reject CRLF injection.
      - Parse with urllib.parse.urlparse.
      - Enforce scheme in ('http','https').
      - Ensure netloc present.
      - Extract host from netloc, IDNA-encode hostname portion.
      - Rebuild normalized netloc preserving userinfo and port if present.
      - Quote path/query/fragment appropriately.
      - Ensure host passes is_safe_target checks.
      - Return urlunparse([...]) string.

    Parameters:
      - allow_private: override for ALLOW_PRIVATE_TARGETS config.
      - enable_dns: override for VALIDATION_ENABLE_DNS config.
    """
    if not isinstance(raw_url, str):
        _trace(f"VALIDATE_REJECT type_not_str input={repr(raw_url)}")
        raise ValueError("URL must be a string.")

    s = raw_url.strip()
    if not s:
        _trace("VALIDATE_REJECT empty_input")
        raise ValueError("Empty URL provided.")

    # Defend against CRLF injection
    if "\n" in s or "\r" in s:
        _trace("VALIDATE_REJECT crlf_in_url")
        raise ValueError("Invalid characters in URL.")

    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        _trace(f"VALIDATE_REJECT scheme_missing_or_unsupported scheme={parsed.scheme}")
        raise ValueError("URL must start with http:// or https://")

    if not parsed.netloc:
        _trace("VALIDATE_REJECT netloc_missing")
        raise ValueError("URL must include a network location (host).")

    # Extract netloc components
    netloc = parsed.netloc
    userinfo = ""
    hostport = netloc
    if "@" in netloc:
        try:
            userinfo, hostport = netloc.rsplit("@", 1)
        except Exception:
            # defensive fallback
            hostport = netloc

    # Extract host and port (preserve port if numeric)
    host, port = _extract_host_from_netloc(hostport)
    if not host:
        _trace(f"VALIDATE_REJECT host_extract_failed netloc={netloc}")
        raise ValueError("Invalid host in URL.")

    # IDNA-encode hostname
    try:
        host_idna = host.encode("idna").decode("ascii")
    except Exception:
        _trace(f"VALIDATE_REJECT idna_failed host={host}")
        raise ValueError("Invalid hostname in URL.")

    # Reconstruct normalized netloc
    port_part = f":{port}" if port else ""
    if ":" in host_idna and not host_idna.startswith("["):
        # IPv6 literal should be bracketed
        host_idna_for_netloc = f"[{host_idna}]"
    else:
        host_idna_for_netloc = host_idna

    normalized_netloc = host_idna_for_netloc + port_part
    if userinfo:
        normalized_netloc = f"{userinfo}@{normalized_netloc}"

    # Check safety of host/netloc
    if not is_safe_target(normalized_netloc, allow_private=allow_private, enable_dns=enable_dns):
        _trace(f"VALIDATE_REJECT unsafe_target url={raw_url} normalized_netloc={normalized_netloc}")
        raise ValueError("Target host is not allowed by policy.")

    # Normalize path/query/fragment safely
    path = quote(unquote(parsed.path or ""), safe="/%:@[]!$&'()*+,;=")
    query = quote(unquote(parsed.query or ""), safe="=&?/")
    fragment = quote(unquote(parsed.fragment or ""), safe="")

    normalized = urlunparse((scheme, normalized_netloc, path, parsed.params or "", query, fragment))
    _trace(f"VALIDATE_ACCEPT url={raw_url} normalized={normalized}")
    return normalized


# Backwards-compatible aliases as requested by various modules:
validate_url = validate_and_normalize_url
# Keep original name mapping to be safe for imports that may reference either name.
__all__ = [
    "validate_and_normalize_url",
    "validate_url",
    "is_safe_target",
]