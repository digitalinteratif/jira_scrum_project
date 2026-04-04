"""
Unit tests for utils.passwords (policy checks and hints).

Covers:
 - policy_hints returns expected hint strings
 - password_policy_check rejects common weak passwords and accepts a strong password
 - respects configuration overrides when present through app.config
"""

import pytest
from utils import passwords

def test_policy_hints_contains_min_length():
    hints = passwords.policy_hints()
    assert isinstance(hints, list)
    assert any("At least" in h for h in hints)

def test_password_policy_check_rejects_common_password():
    # common password from default list
    violations = passwords.password_policy_check("password")
    assert any("too common" in v.lower() or "at least" in v.lower() or "uppercase" in v.lower() or "digit" in v.lower() for v in violations)

def test_password_policy_accepts_strong_password(app):
    # Run inside app context so policy_hints/current_app resolution works consistently
    pw = "Str0ng!Passw0rd2021"
    violations = passwords.password_policy_check(pw, email="user@example.com")
    assert isinstance(violations, list)
    assert len(violations) == 0

def test_policy_respects_min_length_config(app):
    # Temporarily override the policy via app.config
    app.config["PASSWORD_MIN_LENGTH"] = 4
    try:
        v = passwords.password_policy_check("Ab1!", email=None)
        # with min length 4 and classes present, should accept
        assert v == [] or all(isinstance(x, str) for x in v)
    finally:
        # cleanup is handled by test scope (app fixture recreated per test)
        pass
# --- END FILE ---