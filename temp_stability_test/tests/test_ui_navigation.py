import os
import pytest

# This test suite uses pytest-playwright fixtures (the `page` fixture).
# It performs simple navigation checks against a local running instance (http://localhost:5000 by default).

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

@pytest.mark.order(1)
def test_root_loads(page):
    """
    Navigate to the root URL (/) and assert the response loads successfully (HTTP 200).
    """
    resp = page.goto(f"{BASE_URL}/", timeout=15000)
    assert resp is not None, "No response from root URL"
    # Some servers may redirect (3xx) to a landing page; treat 200 or 2xx as success for the landing content.
    status = resp.status
    assert status == 200 or (200 <= status < 400), f"Unexpected status for / : {status}"

@pytest.mark.order(2)
def test_login_and_register_render(page):
    """
    Visit /login and /register and assert they render without 404/500.
    The test uses Playwright navigation responses to assert status codes.
    """
    login_resp = page.goto(f"{BASE_URL}/login", timeout=10000)
    assert login_resp is not None, "No response for /login"
    assert login_resp.status is not None and login_resp.status < 500 and login_resp.status != 404, \
        f"/login returned unexpected status {login_resp.status}"

    register_resp = page.goto(f"{BASE_URL}/register", timeout=10000)
    assert register_resp is not None, "No response for /register"
    assert register_resp.status is not None and register_resp.status < 500 and register_resp.status != 404, \
        f"/register returned unexpected status {register_resp.status}"

# Minimal Playwright-only smoke to ensure pages produced HTML content
def test_pages_have_html(page):
    r = page.goto(f"{BASE_URL}/", timeout=10000)
    assert "<html" in (r.text() if hasattr(r, "text") else page.content()).lower()
--- END FILE: tests/test_ui_navigation.py ---