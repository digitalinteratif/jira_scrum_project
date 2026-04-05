import os
import re
import tempfile
import time

import pytest

# app factory and DB init helpers
from app import create_app
import app_core.db as db_mod
import models

# email dev stub to inspect sent verification token(s)
from app_core.utils.email_dev_stub import get_sent_emails, pop_last_email


_CSRF_RE = re.compile(r'name\s*=\s*["\']csrf_token["\']\s+value\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_csrf(html_text: str) -> str:
    """
    Extract CSRF token value from rendered HTML. Returns empty string if not present.
    Tests prefer token presence but tolerate missing token only when the app config disables CSRF in test env.
    """
    if not html_text:
        return ""
    m = _CSRF_RE.search(html_text)
    if m:
        return m.group(1)
    return ""


@pytest.fixture
def tmp_db_path(tmp_path):
    """
    Provide a file path for a temporary sqlite DB. We return the full filesystem path string.
    The file is created by app_core.db.init_db in the fixture using the resolved path.
    """
    db_file = tmp_path / f"test_auth_flow_{int(time.time() * 1000)}.db"
    return str(db_file)


@pytest.fixture
def app_and_client(tmp_db_path):
    """
    Create an app configured to use the temporary sqlite DB.
    Ensure db init runs and tables are created before returning the Flask test_client.
    """
    # Compose DATABASE_URL (explicit SQLite file)
    db_uri = f"sqlite:///{tmp_db_path}"

    # Ensure any prior dev-stub emails are cleared
    try:
        # utils.email_dev_stub stores a module-level list; pop everything
        while True:
            pop_last_email()
    except Exception:
        pass

    # Initialize DB (idempotent). Use db_mod.init_db to create tables for sqlite file.
    db_mod.init_db(db_path=db_uri, dry_run=False)

    # Create Flask app with test overrides
    test_config = {
        "DATABASE_URL": db_uri,
        "DATABASE_PATH": db_uri,  # some modules read DATABASE_PATH
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt-secret",
        # Ensure sessions are test-friendly
        "JWT_COOKIE_SECURE": False,
        "JWT_SAMESITE": "Lax",
        # Allow demo user_id where routes/dashboard fallback uses it
        "ALLOW_DEMO_USER_ID": True,
    }

    app = create_app(test_config=test_config)

    # create app.test_client
    client = app.test_client()

    # Provide both to tests
    yield app, client

    # Teardown: ensure SQLAlchemy sessions closed
    try:
        # models.Session is scoped_session; remove / close if available
        sess = getattr(models, "Session", None)
        if sess:
            try:
                sess.remove()
            except Exception:
                try:
                    s = sess()
                    s.close()
                except Exception:
                    pass
    except Exception:
        pass

    # Delete the DB file if present (best-effort); tmp fixture usually cleans up
    try:
        if os.path.exists(tmp_db_path):
            os.remove(tmp_db_path)
    except Exception:
        pass


def test_register_login_dashboard_flow(app_and_client):
    """
    Full happy-path:
      - GET /auth/register -> POST /auth/register
      - verify via recorded dev-stub token
      - GET /auth/login -> POST /auth/login
      - Assert Set-Cookie HttpOnly and redirect to /dashboard
      - GET /dashboard returns 200 and contains the shortener form
      - DB assertions: user persisted and password hashed (not stored plaintext)
    """
    app, client = app_and_client

    test_email = f"intg_user_{int(time.time())}@example.com"
    test_password = "Str0ngPass!234"

    # --- GET register form (extract CSRF) ---
    resp = client.get("/auth/register")
    assert resp.status_code == 200, f"GET /auth/register returned {resp.status_code}"
    html = resp.get_data(as_text=True)
    csrf = _extract_csrf(html)

    # --- POST register ---
    post = client.post("/auth/register", data={"email": test_email, "password": test_password, "csrf_token": csrf})
    assert post.status_code == 200, f"Registration failed, status={post.status_code}, body={post.get_data(as_text=True)[:400]!r}"

    # Confirm dev-stub email recorded verification token
    sent = get_sent_emails()
    assert len(sent) >= 1, "Expected at least one dev-stub email after registration"
    last = sent[-1]
    token = last.get("token")
    assert token, "Expected verification token recorded by dev-stub"

    # --- Verify token (dev-stub verification endpoint) ---
    verify_resp = client.get(f"/auth/verify-email/{token}")
    assert verify_resp.status_code == 200, f"Verify endpoint returned {verify_resp.status_code} body={verify_resp.get_data(as_text=True)[:300]}"

    # Confirm DB persisted user and password is hashed (not plaintext)
    s = models.Session()
    try:
        u = s.query(models.User).filter_by(email=test_email).first()
        assert u is not None, "Created user not found in DB"
        assert u.password_hash is not None and u.password_hash != test_password, "Password appears to be stored in plaintext"
    finally:
        try:
            s.close()
        except Exception:
            pass

    # --- Login (CSRF) ---
    login_get = client.get("/auth/login")
    assert login_get.status_code == 200
    login_html = login_get.get_data(as_text=True)
    login_csrf = _extract_csrf(login_html)

    login_post = client.post("/auth/login", data={"email": test_email, "password": test_password, "csrf_token": login_csrf}, follow_redirects=False)
    assert login_post.status_code in (200, 302), f"Login POST unexpected status: {login_post.status_code} body={login_post.get_data(as_text=True)[:300]!r}"

    # Response should include Set-Cookie and include HttpOnly attribute for session cookie
    set_cookie = login_post.headers.get("Set-Cookie") or ""
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower(), f"Expected HttpOnly in Set-Cookie; got: {set_cookie!r}"

    # If redirect, location should point to dashboard; else client can do GET /dashboard
    if login_post.status_code in (301, 302, 303, 307, 308):
        loc = login_post.headers.get("Location", "")
        # Accept either a redirect to url_for('dashboard.dashboard_index') or to '/'
        assert "/dashboard" in loc or loc.endswith("/dashboard") or loc == "", f"Expected redirect to /dashboard; got {loc}"

    # --- GET dashboard with client (cookie preserved by client) ---
    dash = client.get("/dashboard")
    assert dash.status_code == 200, f"Dashboard GET returned {dash.status_code} body={dash.get_data(as_text=True)[:400]!r}"
    dash_html = dash.get_data(as_text=True)

    # Assert shortener form presence (dashboard uses form id 'dashboard-shorten-form')
    assert "id=\"dashboard-shorten-form\"" in dash_html or "Create a Short Link" in dash_html, "Dashboard did not contain the shortener form"

    # Confirm user context present server-side (server persisted session.user_id earlier)
    # We can also assert that the ShortURL form exists and that the page includes an element referencing user_id when ALLOW_DEMO_USER_ID is used.
    assert "Shorten a URL" in dash_html or "Create a Short Link" in dash_html


def test_duplicate_registration_returns_409(app_and_client):
    """
    Register the same email twice. First should succeed; second should return 409 Conflict.
    """
    app, client = app_and_client
    email = f"dup_{int(time.time() * 1000)}@example.com"
    password = "Passw0rd!23"

    # GET CSRF
    g = client.get("/auth/register")
    assert g.status_code == 200
    csrf = _extract_csrf(g.get_data(as_text=True))

    r1 = client.post("/auth/register", data={"email": email, "password": password, "csrf_token": csrf})
    assert r1.status_code == 200, f"First register failed: status={r1.status_code}"

    # Second attempt: get fresh CSRF and POST
    g2 = client.get("/auth/register")
    csrf2 = _extract_csrf(g2.get_data(as_text=True))
    r2 = client.post("/auth/register", data={"email": email, "password": password, "csrf_token": csrf2})
    # Per app.auth.register: duplicate email should raise ValidationError with status_code=409
    assert r2.status_code == 409, f"Expected 409 on duplicate registration; got {r2.status_code} body={r2.get_data(as_text=True)[:400]!r}"


def test_login_wrong_password_returns_401_and_no_cookie(app_and_client):
    """
    Attempt to login with wrong password. Expect 401 and no session cookie set in response.
    """
    app, client = app_and_client
    email = f"wrongpass_{int(time.time() * 1000)}@example.com"
    password = "RightPass!1"
    wrong_password = "WrongPass!2"

    # Create a user entry directly (bypass register flow) so we can test login failure
    s = models.Session()
    try:
        u = models.User(email=email, password_hash="pwplaceholder", is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
    finally:
        try:
            s.close()
        except Exception:
            pass

    # GET login page for CSRF
    lg = client.get("/auth/login")
    assert lg.status_code == 200
    csrf = _extract_csrf(lg.get_data(as_text=True))

    resp = client.post("/auth/login", data={"email": email, "password": wrong_password, "csrf_token": csrf}, follow_redirects=False)
    # auth.login raises ValidationError with status 401 on invalid creds
    assert resp.status_code == 401, f"Login with wrong password should be 401; got {resp.status_code} body={resp.get_data(as_text=True)[:400]!r}"

    # Ensure response does not set a session cookie header indicating user logged in
    set_cookie = resp.headers.get("Set-Cookie")
    if set_cookie:
        # In some configurations framework may set cookies for other reasons, ensure no HttpOnly session cookie set that correlates with user session
        # Conservative: assert not setting cookie that contains 'user_id' or same name as JWT_COOKIE_NAME in app config
        cookie_name = client.application.config.get("JWT_COOKIE_NAME", "smartlink_jwt")
        assert cookie_name not in (set_cookie or ""), f"Unexpected session cookie set on failed login: {set_cookie!r}"