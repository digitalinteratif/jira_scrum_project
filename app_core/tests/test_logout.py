"""tests/test_logout.py - unit & integration tests for logout endpoint (KAN-170)"""

import re
import pytest

# Reuse fixtures from conftest.py: client, app
# The client fixture provides a Flask test_client bound to an app using in-memory sqlite.

_CSRF_RE = re.compile(r'name\s*=\s*[\'"]csrf_token[\'"]\s+value\s*=\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE)


def _extract_csrf(html: str) -> str:
    if not html:
        return ""
    m = _CSRF_RE.search(html)
    if m:
        return m.group(1)
    return ""


def test_logout_clears_session_and_redirects(client):
    """
    Set a session key, fetch a page to obtain a CSRF tied to session, POST /auth/logout,
    and assert session cleared and response redirects to /login.
    """
    # Establish a session and user_id
    with client.session_transaction() as sess:
        sess['user_id'] = 1

    # GET login page to obtain a CSRF token (uses same session)
    get_resp = client.get("/auth/login")
    assert get_resp.status_code == 200
    html = get_resp.get_data(as_text=True)
    csrf = _extract_csrf(html)
    assert csrf != "", "CSRF token not present on login page"

    # POST to logout with csrf
    post = client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert post.status_code in (302, 301)
    # Location header should contain /login
    loc = post.headers.get("Location", "")
    assert loc.endswith("/login") or "/login" in loc

    # Verify session no longer contains user_id
    with client.session_transaction() as sess2:
        assert sess2.get("user_id") is None


def test_logout_no_session_still_redirects(client):
    """
    When no user session is present, logout should still redirect to /login without error.
    """
    # Ensure no session user_id
    with client.session_transaction() as sess:
        sess.pop("user_id", None)

    # Obtain CSRF from login page (fresh session)
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    csrf = _extract_csrf(resp.get_data(as_text=True))
    assert csrf != ""

    post = client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert post.status_code in (302, 301)
    loc = post.headers.get("Location", "")
    assert loc.endswith("/login") or "/login" in loc


def test_logout_invalid_csrf_fails(client):
    """
    Posting without a CSRF token should result in CSRF failure (400).
    """
    # Perform POST without csrf_token
    post = client.post("/auth/logout", data={}, follow_redirects=False)
    # Expect a 400 response from CSRF protection. Accept 400 explicit.
    assert post.status_code == 400


def test_double_logout_idempotent(client):
    """
    Logout once, then perform logout again (with new CSRF), both should redirect to /login and not error.
    """
    # Setup session
    with client.session_transaction() as sess:
        sess['user_id'] = 55

    # First logout
    resp1 = client.get("/auth/login")
    csrf1 = _extract_csrf(resp1.get_data(as_text=True))
    assert csrf1 != ""
    p1 = client.post("/auth/logout", data={"csrf_token": csrf1}, follow_redirects=False)
    assert p1.status_code in (302, 301)
    assert (p1.headers.get("Location") or "").endswith("/login") or "/login" in (p1.headers.get("Location") or "")

    # Second logout: get fresh CSRF (session cleared, but login page still returns token for anonymous session)
    resp2 = client.get("/auth/login")
    csrf2 = _extract_csrf(resp2.get_data(as_text=True))
    assert csrf2 != ""
    p2 = client.post("/auth/logout", data={"csrf_token": csrf2}, follow_redirects=False)
    assert p2.status_code in (302, 301)
    assert (p2.headers.get("Location") or "").endswith("/login") or "/login" in (p2.headers.get("Location") or "")
--- END FILE ---