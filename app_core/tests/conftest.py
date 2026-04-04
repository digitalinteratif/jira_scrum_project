"""
pytest fixtures for KAN-131 tests.

Provides:
 - app: Flask app created with in-memory sqlite (sqlite:///:memory:) for isolation
 - client: Flask test_client bound to app
 - db_session: SQLAlchemy session (models.Session) for direct DB interactions in tests
"""

import pytest
import os
import time

from app import create_app
import models

@pytest.fixture
def app():
    """
    Create a fresh Flask app instance configured to use in-memory SQLite.
    The app factory will call models.init_db and Base.metadata.create_all(engine).
    """
    test_config = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt-secret",
        # Keep small defaults for test determinism
        "JWT_COOKIE_SECURE": False,
        "JWT_SAMESITE": "Lax",
    }
    app = create_app(test_config=test_config)
    # Push app context where necessary for utilities that reference current_app
    ctx = app.app_context()
    ctx.push()
    yield app
    try:
        ctx.pop()
    except Exception:
        pass

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def db_session(app):
    """
    Yield a SQLAlchemy session bound to the app's in-memory engine.

    Tests should commit as necessary. This fixture ensures session is closed when test finishes.
    """
    sess = models.Session()
    try:
        yield sess
    finally:
        try:
            sess.close()
        except Exception:
            pass

# Small helper fixture to create a user row for tests
@pytest.fixture
def create_user(db_session):
    def _create(email="user@example.com", password_hash="pw", is_active=True):
        u = models.User(email=email, password_hash=password_hash, is_active=is_active)
        db_session.add(u)
        db_session.commit()
        db_session.refresh(u)
        return u
    return _create
--- END FILE ---