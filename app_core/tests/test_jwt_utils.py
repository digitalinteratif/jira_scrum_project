"""
Unit tests for utils.jwt helpers: create_access_token, decode_access_token, is_revoked, revoke_token.

Covers:
 - create_access_token with create_session=True persists a SessionToken row
 - decode_access_token enforces expiry
 - revoke_token marks session revoked and inserts RevokedToken audit
 - is_revoked returns expected boolean before/after revoke
"""

import pytest
import time

from utils import jwt as jwt_utils
import models
from datetime import datetime

def test_create_access_token_persists_session(db_session, create_user):
    user = create_user(email="jwt-user@example.com")
    secret = "test-secret-xyz"
    # create_access_token returns (token, jti)
    token, jti = jwt_utils.create_access_token({"user_id": user.id}, secret=secret, expires_seconds=60, create_session=True, session_info={"user_id": user.id, "ip": "1.2.3.4", "user_agent": "pytest"})
    assert token is not None
    assert jti is not None
    # Verify SessionToken exists in DB
    st = db_session.query(models.SessionToken).filter_by(jti=jti).first()
    assert st is not None
    assert st.user_id == user.id
    # is_revoked should be False initially
    assert jwt_utils.is_revoked(jti) is False

def test_decode_access_token_expiry_raises(db_session, create_user):
    user = create_user(email="jwt-exp@example.com")
    secret = "secret-exp"
    # short expiry
    token, jti = jwt_utils.create_access_token({"user_id": user.id}, secret=secret, expires_seconds=1, create_session=False)
    assert token is not None
    # Immediately decode should succeed
    body = jwt_utils.decode_access_token(token, secret=secret)
    assert "jti" in body or "payload" in body
    # Wait for expiry
    time.sleep(1.1)
    with pytest.raises(Exception):
        jwt_utils.decode_access_token(token, secret=secret)

def test_revoke_token_and_is_revoked(db_session, create_user):
    user = create_user(email="jwt-revoke@example.com")
    secret = "secret-revoke"
    token, jti = jwt_utils.create_access_token({"user_id": user.id}, secret=secret, expires_seconds=60, create_session=True, session_info={"user_id": user.id})
    assert jwt_utils.is_revoked(jti) is False
    # Revoke
    ok = jwt_utils.revoke_token(jti, reason="unit test revoke")
    assert ok is True
    # After revoke, is_revoked should be True
    assert jwt_utils.is_revoked(jti) is True
    # There should be at least one RevokedToken entry with this jti
    rv = db_session.query(models.RevokedToken).filter_by(jti=jti).first()
    assert rv is not None
--- END FILE ---