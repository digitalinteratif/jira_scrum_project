"""tests/test_accessibility.py - Accessibility smoke checks for KAN-143"""

import re
from app import create_app

def _has_label_for_pair(html, input_id):
    # Find label with for="input_id"
    return bool(re.search(rf'<label[^>]*for=["\']{re.escape(input_id)}["\']', html, re.IGNORECASE))

def _has_input_with_id(html, input_id):
    return bool(re.search(rf'<input[^>]*id=["\']{re.escape(input_id)}["\']', html, re.IGNORECASE))

def test_register_and_login_have_labels_and_aria():
    app = create_app(test_config={
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt",
    })
    client = app.test_client()

    # Register page
    resp = client.get("/auth/register")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Skip link present
    assert 'class="skip-link"' in html or 'id="main-content"' in html or 'href="#main-content"' in html

    # Responsive meta should be present
    assert '<meta name="viewport"' in html.lower()

    # Email input and label
    assert _has_input_with_id(html, "register-email")
    assert _has_label_for_pair(html, "register-email")

    # Password input and aria-describedby present
    assert _has_input_with_id(html, "register-password")
    assert 'aria-describedby="pw-strength-widget"' in html

    # Login page
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _has_input_with_id(html, "login-email")
    assert _has_label_for_pair(html, "login-email")
    assert _has_input_with_id(html, "login-password")
    assert _has_label_for_pair(html, "login-password")

def test_shorten_form_and_copy_control():
    # This tests that the shorten form uses labels and that created short page provides copy control
    app = create_app(test_config={
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt",
    })
    client = app.test_client()

    # Allow demo user id for tests
    client.application.config["ALLOW_DEMO_USER_ID"] = True

    # GET shorten page
    resp = client.get("/shorten")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # label for target url
    assert _has_input_with_id(html, "shorten-target_url")
    assert _has_label_for_pair(html, "shorten-target_url")
    assert _has_input_with_id(html, "shorten-slug")
    assert _has_label_for_pair(html, "shorten-slug")

    # Submit shorten POST to create short link (demo user)
    # Extract csrf token if present
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html)
    csrf = m.group(1) if m else ""
    post = client.post("/shorten", data={
        "user_id": "1",
        "target_url": "http://example.com/",
        "csrf_token": csrf,
    })
    assert post.status_code == 200
    created_html = post.get_data(as_text=True)
    # Copy control exists
    assert _has_input_with_id(created_html, "shortlink-input")
    assert 'id="copy-shortlink"' in created_html
    assert 'aria-label="Copy short link"' in created_html or 'aria-label=\'Copy short link\'' in created_html
    assert 'tabindex="0"' in created_html or "tabindex=0" in created_html
--- END FILE: tests/test_accessibility.py ---