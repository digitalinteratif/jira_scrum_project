"""tests/test_redirect_route.py - unit & integration tests for KAN-176 redirect route"""

import re
import pytest

import models


def _create_short(db_session, user, slug="abc123", target="https://example.com/"):
    short = models.ShortURL(user_id=user.id, target_url=target, slug=slug, is_custom=True)
    db_session.add(short)
    db_session.commit()
    db_session.refresh(short)
    return short


def test_redirect_happy_path(client, db_session, create_user):
    """
    Happy path: existing short_code redirects with configured code and Location header.
    """
    user = create_user(email="redir_user@example.com", password_hash="pw", is_active=True)
    short = _create_short(db_session, user, slug="happy123", target="https://example.com/target")

    # Ensure default config present
    resp = client.get("/happy123", follow_redirects=False)
    assert resp.status_code in (301, 302), f"Unexpected redirect code: {resp.status_code}"
    assert resp.headers.get("Location") == "https://example.com/target"


def test_missing_short_code_returns_404(client):
    """
    Nonexistent short code returns 404 friendly page.
    """
    resp = client.get("/no-such-slug", follow_redirects=False)
    assert resp.status_code == 404
    data = resp.get_data(as_text=True)
    assert "Not Found" in data or "not exist" in data.lower()


def test_malformed_short_code_returns_400(client):
    """
    Slug containing illegal characters (e.g., '!') should be rejected as malformed (400).
    Use URL-encoded representation to hit the route.
    """
    # 'bad!' encoded as %21
    resp = client.get("/bad%21", follow_redirects=False)
    assert resp.status_code == 400
    assert "Bad Request" in resp.get_data(as_text=True)


def test_redirect_code_configurable(client, db_session, create_user):
    """
    Changing REDIRECT_CODE in app config should change issued response code.
    """
    # modify config on the running app instance
    client.application.config["REDIRECT_CODE"] = 301

    user = create_user(email="redir_cfg@example.com", password_hash="pw", is_active=True)
    _create_short(db_session, user, slug="cfg301", target="https://example.com/cfg")

    resp = client.get("/cfg301", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers.get("Location") == "https://example.com/cfg"


def test_invalid_stored_destination_returns_500(client, db_session, create_user):
    """
    If the stored original_url is malformed/unsafe (e.g., javascript:), handler should not redirect and should return 500.
    """
    user = create_user(email="redir_invalid@example.com", password_hash="pw", is_active=True)
    _create_short(db_session, user, slug="bad-dest", target="javascript:alert(1)")

    resp = client.get("/bad-dest", follow_redirects=False)
    assert resp.status_code == 500
    assert "Server Error" in resp.get_data(as_text=True) or "invalid" in resp.get_data(as_text=True).lower()