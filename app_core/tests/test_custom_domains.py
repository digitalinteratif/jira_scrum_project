"""tests/test_custom_domains.py - Unit & integration tests for custom domain registration & verification (KAN-144)"""

import pytest
import time
from app import create_app
import models
import os
import socket

# Defensive import for dns resolver mocking
try:
    import dns.resolver as dns_resolver_mod
except Exception:
    dns_resolver_mod = None


def _make_app():
    test_config = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt",
        "ALLOW_DEMO_USER_ID": True,  # tests will pass user_id directly
    }
    app = create_app(test_config=test_config)
    return app


@pytest.fixture
def app():
    app = _make_app()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    sess = models.Session()
    try:
        yield sess
    finally:
        try:
            sess.close()
        except Exception:
            pass


def test_register_generates_token_and_persists(client, db_session):
    # Create a user that will own the domain
    user = models.User(email="domuser@example.com", password_hash="pw", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Register domain
    resp = client.post("/domains", data={"domain": "example-verify.test", "user_id": str(user.id)})
    assert resp.status_code == 200
    # Confirm DB row
    sess = models.Session()
    try:
        cd = sess.query(models.CustomDomain).filter_by(domain="example-verify.test").first()
        assert cd is not None
        assert cd.owner_id == user.id
        assert cd.verification_token and len(cd.verification_token) > 0
        assert cd.is_verified is False
    finally:
        try:
            sess.close()
        except Exception:
            pass


def test_verify_detects_txt_token_and_marks_verified(client, db_session, monkeypatch):
    # skip if dnspython not available in test environment, but ensure failure explicit
    # We will monkeypatch routes.domains._has_dns_resolver by mocking dns.resolver import indirectly via monkeypatching in module
    # Create user/domain
    user = models.User(email="domtxt@example.com", password_hash="pw", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Create domain record manually (simulate POST)
    from models import CustomDomain
    token = "test-token-abc123"
    cd = CustomDomain(owner_id=user.id, domain="txt-example.test", verification_token=token, is_verified=False)
    db_session.add(cd)
    db_session.commit()
    db_session.refresh(cd)

    # Mock dns.resolver.resolve to return a TXT-like object with .strings attribute containing token
    class MockTXT:
        def __init__(self, val):
            self.strings = [val.encode("utf-8")]

    def fake_resolve(name, rdtype):
        if rdtype == "TXT" and name == "txt-example.test":
            return [MockTXT(f"smartlink-verification={token}")]
        raise Exception("No records")

    # Patch the resolver used by routes/domains (module-level import)
    import routes.domains as dommod
    monkeypatch.setattr(dommod, "_has_dns_resolver", True)
    monkeypatch.setattr(dommod, "_dns_resolver", type("X", (), {"resolve": staticmethod(fake_resolve)}))

    # Call verify endpoint
    resp = client.get(f"/domains/verify/{cd.id}", query_string={"user_id": str(user.id)})
    assert resp.status_code == 200
    # Confirm DB updated
    s2 = models.Session()
    try:
        cd2 = s2.query(models.CustomDomain).filter_by(id=cd.id).first()
        assert cd2.is_verified is True
    finally:
        try:
            s2.close()
        except Exception:
            pass


def test_verify_rejects_private_ip_resolution(client, db_session, monkeypatch):
    # Create user/domain
    user = models.User(email="domprivate@example.com", password_hash="pw", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    from models import CustomDomain
    token = "private-token-xyz"
    cd = CustomDomain(owner_id=user.id, domain="private-example.test", verification_token=token, is_verified=False)
    db_session.add(cd)
    db_session.commit()
    db_session.refresh(cd)

    # Mock TXT record present but A record resolves to private IP
    class MockTXT:
        def __init__(self, val):
            self.strings = [val.encode("utf-8")]

    def fake_resolve(name, rdtype):
        if rdtype == "TXT":
            return [MockTXT(f"smartlink-verification={token}")]
        return []

    import routes.domains as dommod
    monkeypatch.setattr(dommod, "_has_dns_resolver", True)
    monkeypatch.setattr(dommod, "_dns_resolver", type("X", (), {"resolve": staticmethod(fake_resolve)}))

    # Monkeypatch socket.getaddrinfo in the domains module to return a private IP for the host
    def fake_getaddrinfo(host, *args, **kwargs):
        # Return a tuple whose [4][0] yields '10.0.0.5'
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 0))]

    import socket as _socket
    monkeypatch.setattr(dommod, "socket", _socket)
    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

    # Call verify endpoint - should reject (422)
    resp = client.get(f"/domains/verify/{cd.id}", query_string={"user_id": str(user.id)})
    assert resp.status_code == 422
    # Confirm DB not marked verified
    s2 = models.Session()
    try:
        cd2 = s2.query(models.CustomDomain).filter_by(id=cd.id).first()
        assert cd2.is_verified is False
    finally:
        try:
            s2.close()
        except Exception:
            pass
--- END FILE: tests/test_custom_domains.py ---