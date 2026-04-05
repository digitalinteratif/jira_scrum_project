from __future__ import annotations

import re
from typing import Optional
import pytest

from app import create_app
import models

from app_core.db import get_db_connection
from app_core.utils.validation import validate_and_normalize_url
import app_core.short_code_service as scs


_CSRF_RE = re.compile(r'name\s*=\s*["\']csrf_token["\']\s+value\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_csrf(html: str) -> str:
    if not html:
        return ""
    m = _CSRf := re.search(_CSRF_RE, html)
    return m.group(1) if m else ""


def _extract_slug_from_html(html: str) -> Optional[str]:
    """
    Try several patterns to extract the generated slug from the shorten response HTML.
    """
    if not html:
        return None
    # Pattern 1: Slug: <strong>abcd</strong>
    m = re.search(r"Slug:\s*<strong>([^<]+)</strong>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Pattern 2: Short Link: <a href=".../slug">
    m = re.search(r"Short Link:\s*<a\s+href=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
    if m:
        href = m.group(1)
        # last path segment
        return href.rstrip("/").split("/")[-1]
    # Pattern 3: common anchor with short url
    m = re.search(r'href=[\'\"]([^\'\"]+/( [A-Za-z0-9\-_]+))[\'\"]', html, re.IGNORECASE)
    if m:
        return m.group(2)
    return None


def _find_slug_in_db(sqlite_uri: str, owner_user_id: int, normalized_target: str) -> Optional[str]:
    """
    Query Urls table for a mapping matching owner and normalized target.
    Returns short_code or None.
    """
    try:
        with get_db_connection(sqlite_uri) as conn:
            cur = conn.execute(
                "SELECT short_code FROM Urls WHERE original_url = ? AND owner_user_id = ? ORDER BY id DESC LIMIT 1",
                (normalized_target, owner_user_id),
            )
            row = cur.fetchone()
            if row:
                return row["short_code"]
    except Exception:
        # Fall back: try SQLAlchemy models.ShortURL lookup
        try:
            sess = models.Session()
            try:
                r = sess.query(models.ShortURL).filter_by(user_id=owner_user_id, target_url=normalized_target).order_by(models.ShortURL.id.desc()).first()
                if r:
                    return getattr(r, "slug", None)
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
        except Exception:
            pass
    return None


@pytest.mark.integration
def test_shorten_and_redirect_happy_path(tmp_path):
    """
    Full end-to-end: create short link, assert DB mapping, and assert redirect response Location equals normalized URL.
    """
    db_file = tmp_path / "shortener_integ.db"
    sqlite_uri = f"sqlite:///{str(db_file)}"

    app = create_app(
        test_config={
            "DATABASE_URL": sqlite_uri,
            "SECRET_KEY": "test-secret",
            "JWT_SECRET": "test-jwt",
            "REDIRECT_CODE": 302,
        }
    )
    client = app.test_client()

    # Create a user directly using SQLAlchemy models
    s = models.Session()
    try:
        user = models.User(email="integ-user@example.com", password_hash="pwhash", is_active=True)
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id
    finally:
        try:
            s.close()
        except Exception:
            pass

    # Authenticate test client via server-side session
    with client.session_transaction() as sess:
        sess["user_id"] = int(user_id)

    # GET form to obtain CSRF
    get_resp = client.get("/shorten")
    assert get_resp.status_code == 200
    html = get_resp.get_data(as_text=True)
    m = re.search(_CSRF_RE, html)
    csrf = m.group(1) if m else ""

    target = "https://example.com/smoke?x=1"
    post = client.post("/shorten", data={"target_url": target, "csrf_token": csrf})
    assert post.status_code in (200, 201, 302)

    body = post.get_data(as_text=True)
    slug = _extract_slug_from_html(body)

    # Fallback: query DB by normalized target
    normalized_target = validate_and_normalize_url(target)
    if not slug:
        slug = _find_slug_in_db(sqlite_uri, user_id, normalized_target)
    assert slug, "Created slug could not be determined from response or DB"

    # Verify DB mapping exists with parameterized query
    found = None
    with get_db_connection(sqlite_uri) as conn:
        cur = conn.execute("SELECT short_code, original_url, owner_user_id FROM Urls WHERE short_code = ?", (slug,))
        found = cur.fetchone()
    assert found is not None, "Expected Urls table to contain mapping for created slug"
    assert int(found["owner_user_id"]) == int(user_id)
    # original_url stored may be normalized; compare to normalization
    assert found["original_url"] == normalized_target

    # Verify redirect works and Location header equals normalized URL
    resp = client.get(f"/{slug}", follow_redirects=False)
    expected_status = app.config.get("REDIRECT_CODE", 302)
    assert resp.status_code == expected_status
    assert resp.headers.get("Location") == normalized_target


@pytest.mark.integration
def test_persistence_across_restart(tmp_path):
    """
    Create a short code using one app instance and confirm another app instance with same DB resolves it.
    """
    db_file = tmp_path / "shortener_persist.db"
    sqlite_uri = f"sqlite:///{str(db_file)}"

    # App instance A
    app1 = create_app(test_config={"DATABASE_URL": sqlite_uri, "SECRET_KEY": "s", "JWT_SECRET": "j", "REDIRECT_CODE": 302})
    c1 = app1.test_client()
    s = models.Session()
    try:
        u = models.User(email="persist@example.com", password_hash="pw", is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    finally:
        try:
            s.close()
        except Exception:
            pass

    with c1.session_transaction() as sess:
        sess["user_id"] = int(uid)

    get = c1.get("/shorten")
    html = get.get_data(as_text=True)
    m = re.search(_CSRF_RE, html)
    csrf = m.group(1) if m else ""

    target = "http://example.com/restart"
    post = c1.post("/shorten", data={"target_url": target, "csrf_token": csrf})
    assert post.status_code in (200, 201, 302)

    normalized_target = validate_and_normalize_url(target)
    slug = _find_slug_in_db(sqlite_uri, uid, normalized_target)
    assert slug, "Slug not found after creation in app1"

    # App instance B (new process-like app)
    app2 = create_app(test_config={"DATABASE_URL": sqlite_uri, "SECRET_KEY": "s", "JWT_SECRET": "j", "REDIRECT_CODE": 302})
    c2 = app2.test_client()
    resp = c2.get(f"/{slug}", follow_redirects=False)
    assert resp.status_code == app2.config.get("REDIRECT_CODE", 302)
    assert resp.headers.get("Location") == normalized_target


@pytest.mark.integration
def test_collision_retry_path(tmp_path, monkeypatch):
    """
    Force a collision by pre-inserting a slug and monkeypatching the random generator to return that slug first,
    then a unique slug on retry. Assert final created slug is the unique one.
    """
    db_file = tmp_path / "shortener_collision.db"
    sqlite_uri = f"sqlite:///{str(db_file)}"
    app = create_app(test_config={"DATABASE_URL": sqlite_uri, "SECRET_KEY": "s", "JWT_SECRET": "j"})
    client = app.test_client()

    # Pre-insert existing slug
    collision_slug = "COLLIDE123"
    s = models.Session()
    try:
        u = models.User(email="col@example.com", password_hash="pw", is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
        existing = models.ShortURL(user_id=uid, target_url="http://existing/", slug=collision_slug, is_custom=True)
        s.add(existing)
        s.commit()
    finally:
        try:
            s.close()
        except Exception:
            pass

    # Prepare generator to return collision then unique
    seq = [collision_slug, "UNIQUE987"]

    def fake_generate(length, alphabet=None):
        return seq.pop(0)

    # Patch internal generator used by short_code_service
    monkeypatch.setattr(scs, "_generate_random_code", fake_generate)

    with client.session_transaction() as sess:
        sess["user_id"] = int(uid)

    get = client.get("/shorten")
    html = get.get_data(as_text=True)
    m = re.search(_CSRF_RE, html)
    csrf = m.group(1) if m else ""

    target = "http://example.com/collision"
    post = client.post("/shorten", data={"target_url": target, "csrf_token": csrf})
    assert post.status_code in (200, 201, 302)

    normalized_target = validate_and_normalize_url(target)
    slug = _find_slug_in_db(sqlite_uri, uid, normalized_target)
    assert slug is not None
    assert slug != collision_slug, "Expected collision to be resolved to a different slug"


@pytest.mark.integration
def test_invalid_url_rejected(tmp_path):
    """
    Submitting an invalid/unsafe URL should not create a DB row and should return a client error (400) or render form with error.
    """
    db_file = tmp_path / "shortener_invalid.db"
    sqlite_uri = f"sqlite:///{str(db_file)}"
    app = create_app(test_config={"DATABASE_URL": sqlite_uri, "SECRET_KEY": "s", "JWT_SECRET": "j"})
    client = app.test_client()

    s = models.Session()
    try:
        u = models.User(email="inv@example.com", password_hash="pw", is_active=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    finally:
        try:
            s.close()
        except Exception:
            pass

    with client.session_transaction() as sess:
        sess["user_id"] = int(uid)

    get = client.get("/shorten")
    html = get.get_data(as_text=True)
    m = re.search(_CSRF_RE, html)
    csrf = m.group(1) if m else ""

    bad_target = "javascript:alert(1)"
    post = client.post("/shorten", data={"target_url": bad_target, "csrf_token": csrf})
    # Accept 400 or a 200 with inline validation; ensure DB not updated
    assert post.status_code in (400, 200)

    # If validator normalizes the bad target (unlikely), it will raise; handle gracefully
    try:
        normalized_attempt = validate_and_normalize_url(bad_target)
    except Exception:
        normalized_attempt = None

    with get_db_connection(sqlite_uri) as conn:
        if normalized_attempt:
            cur = conn.execute("SELECT 1 FROM Urls WHERE original_url = ? AND owner_user_id = ?", (normalized_attempt, uid))
            assert cur.fetchone() is None
        else:
            # ensure no rows with javascript scheme stored
            cur = conn.execute("SELECT original_url FROM Urls WHERE owner_user_id = ?", (uid,))
            rows = cur.fetchall()
            for r in rows:
                assert not (str(r["original_url"]).lower().startswith("javascript:"))