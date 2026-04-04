"""tests/test_email_verification.py - Unit & integration tests for email verification flow."""

import pytest
import time

from app import create_app
import models
from utils.email_dev_stub import get_sent_emails, pop_last_email
from utils.crypto import create_verification_token, decode_verification_token

@pytest.fixture
def app():
    # Use in-memory SQLite for tests
    test_config = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt-secret",
        "EMAIL_VERIFY_EXPIRY_SECONDS": 2,  # short expiry for tests
    }
    app = create_app(test_config=test_config)
    yield app

@pytest.fixture
def client(app):
    return app.test_client()

def test_token_encode_decode_and_purpose():
    secret = "s3cr3t"
    token = create_verification_token({"user_id": 123}, purpose="email_verify", secret=secret, expires_seconds=60)
    payload = decode_verification_token(token, secret=secret, expected_purpose="email_verify")
    assert payload["user_id"] == 123

def test_token_expiry_handling():
    secret = "s3cr3t"
    token = create_verification_token({"user_id": 50}, purpose="email_verify", secret=secret, expires_seconds=1)
    # wait for expiry
    time.sleep(1.1)
    with pytest.raises(Exception):
        decode_verification_token(token, secret=secret, expected_purpose="email_verify")

def test_registration_and_verification_flow(client, app):
    # Register user
    resp = client.post("/auth/register", data={"email": "foo@example.com", "password": "password123"})
    assert resp.status_code == 200
    # Get last sent email
    sent = get_sent_emails()
    assert len(sent) >= 1
    last = sent[-1]
    token = last["token"]
    assert token is not None

    # Verify before expiry
    verify_resp = client.get("/auth/verify-email/{}".format(token))
    assert verify_resp.status_code == 200

    # Confirm user active in DB
    session = models.Session()
    user = session.query(models.User).filter_by(email="foo@example.com").first()
    assert user is not None
    assert user.is_active is True

def test_verification_with_expired_token_returns_error(client, app):
    # Create token with short expiry via create_verification_token
    secret = app.config["JWT_SECRET"]
    token = create_verification_token({"user_id": 9999}, purpose="email_verify", secret=secret, expires_seconds=1)
    time.sleep(1.1)
    resp = client.get("/auth/verify-email/{}".format(token))
    assert resp.status_code == 400
    assert b"Verification Failed" in resp.data or b"expired" in resp.data