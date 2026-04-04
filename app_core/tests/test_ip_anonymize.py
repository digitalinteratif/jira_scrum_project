"""tests/test_ip_anonymize.py - Unit tests for the new utils.ip.anonymize_ip (KAN-116)."""

import os
import ipaddress
import pytest

from utils.ip import anonymize_ip

def _mask_ipv6_expected(ip_str: str, lower_bits: int) -> str:
    """Helper to compute expected masked IPv6 as the core implementation would."""
    ip_obj = ipaddress.ip_address(ip_str)
    ip_int = int(ip_obj)
    if lower_bits == 0:
        masked_int = ip_int
    elif lower_bits >= 128:
        masked_int = 0
    else:
        mask = (~((1 << lower_bits) - 1)) & ((1 << 128) - 1)
        masked_int = ip_int & mask
    return str(ipaddress.IPv6Address(masked_int))


def test_ipv4_masking():
    ip = "203.0.113.45"
    masked = anonymize_ip(ip)
    assert masked == "203.0.113.0"


def test_ipv6_masking_default_80_bits():
    ip = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    # default lower_bits=80 -> keep top 48 bits
    expected = _mask_ipv6_expected(ip, 80)
    masked = anonymize_ip(ip)
    assert masked == expected


def test_ipv6_masking_override_via_env(monkeypatch):
    ip = "2001:0db8:85a3:1234:5678:9abc:def0:1111"
    # set env override to zero lower 64 bits
    monkeypatch.setenv("IPV6_ANON_MASK_LOWER_BITS", "64")
    try:
        expected = _mask_ipv6_expected(ip, 64)
        masked = anonymize_ip(ip)  # no explicit param -> env config used
        assert masked == expected
    finally:
        monkeypatch.delenv("IPV6_ANON_MASK_LOWER_BITS", raising=False)


def test_malformed_input_returns_none():
    assert anonymize_ip("not-an-ip") is None
    assert anonymize_ip("") is None
    assert anonymize_ip(None) is None


def test_ipv6_with_zone_stripping():
    ip_with_zone = "fe80::1%eth0"
    masked = anonymize_ip(ip_with_zone)
    # Should succeed (zone stripped) and return a string (not None)
    assert masked is not None
    # Ensure it's a valid IPv6 string by parsing
    ipaddress.ip_address(masked)
--- END FILE: tests/test_ip_anonymize.py ---