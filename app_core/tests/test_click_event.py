"""tests/test_click_event.py - unit & integration tests for ClickEvent model and redirect tracking (KAN-115)"""

import pytest
import time
from datetime import datetime
from app import create_app
import models
from sqlalchemy import inspect

@pytest.fixture
def app():
    # Use in-memory SQLite for tests
    test_config = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt-secret",
        # Keep redirects as 302 for tests
    }
    app = create_app(test_config=test_config)
    yield app

@pytest.fixture
def client(app):
    return app.test_client()

def _create_user_and_short(session, email="clicker@example.com", slug="testslug", target="http://example.com/"):
    # Create user
    user = models.User(email=email, password_hash="pw", is_active=True)
    session.add(user)
    session.commit()
    # Create shorturl
    short = models.ShortURL(user_id=user.id, target_url=target, slug=slug, is_custom=True)
    session.add(short)
    session.commit()
    session.refresh(short)
    return user, short

def test_clickevent_model_persistence_and_index_exists(app):
    session = models.Session()
    try:
        user, short = _create_user_and_short(session)
        # Create ClickEvent
        click = models.ClickEvent(
            short_url_id=short.id,
            anonymized_ip="203.0.113.0",
            user_agent="pytest-agent",
            referrer="http://referrer.example/",
            country="US",
            occurred_at=datetime.utcnow(),
        )
        session.add(click)
        session.commit()
        # Refresh and assert persisted
        assert click.id is not None

        # Inspect indexes for composite (short_url_id, occurred_at)
        inspector = inspect(models.Engine)
        idxs = inspector.get_indexes("clickevents")
        # There should be an index with those column names
        found = False
        for idx in idxs:
            cols = idx.get("column_names", []) or idx.get("column_names")
            if set(cols) == set(["short_url_id", "occurred_at"]):
                found = True
                break
        assert found, f"Expected composite index on (short_url_id, occurred_at). Indexes: {idxs}"
    finally:
        try:
            session.close()
        except Exception:
            pass

def test_redirect_flow_persists_clickevent(client, app):
    session = models.Session()
    try:
        user, short = _create_user_and_short(session, slug="redir-sample", target="http://example.com/")
    finally:
        session.close()

    # Simulate a client request to the public slug
    resp = client.get("/redir-sample")
    assert resp.status_code in (302, 301)  # redirect occurred

    # Check DB for ClickEvent
    session = models.Session()
    try:
        clicks = session.query(models.ClickEvent).filter_by(short_url_id=short.id).all()
        assert len(clicks) >= 1
        c = clicks[-1]
        assert c.user_agent is not None
        assert c.occurred_at is not None
    finally:
        try:
            session.close()
        except Exception:
            pass

def test_missing_ip_results_in_null_anonymized_ip(client, app):
    session = models.Session()
    try:
        user, short = _create_user_and_short(session, slug="no-ip", target="http://example.com/")
    finally:
        session.close()

    # Do not set X-Forwarded-For; test_client requests typically don't include remote_addr -> expect anonymized_ip NULL
    resp = client.get("/no-ip")
    assert resp.status_code in (302, 301)

    session = models.Session()
    try:
        c = session.query(models.ClickEvent).filter_by(short_url_id=short.id).order_by(models.ClickEvent.occurred_at.desc()).first()
        # Per acceptance criteria: when IP missing/malformed store NULL
        assert c is not None
        assert c.anonymized_ip is None
    finally:
        try:
            session.close()
        except Exception:
            pass

# End of tests/test_click_event.py