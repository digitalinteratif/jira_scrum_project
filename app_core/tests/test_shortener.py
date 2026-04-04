"""
Unit tests for utils.shortener utilities.

Covers:
 - deterministic generate_slug produces same output for same source+secret
 - validate_custom_slug rejects reserved words (using default reserved set)
 - find_unique_slug retries on duplicate reservation by simulating a collision via an actual DB insert
"""

import pytest
from utils import shortener
import models

def test_generate_slug_deterministic_equivalent():
    secret = "det-secret"
    source = "https://example.com/target"
    a = shortener.generate_slug(length=8, deterministic_source=source, secret=secret)
    b = shortener.generate_slug(length=8, deterministic_source=source, secret=secret)
    assert a == b
    assert len(a) == 8

def test_validate_custom_slug_rejects_reserved_word():
    # Use known default reserved word "admin"
    assert shortener.validate_custom_slug("admin") is False
    # Accept a valid non-reserved slug
    assert shortener.validate_custom_slug("my-custom_slug-1") is True

def test_find_unique_slug_with_reservation_and_collision(db_session, create_user, monkeypatch):
    """
    Simulate a collision on the first generated candidate by:
      - monkeypatching generate_slug to return 'collide' then 'uniqueX'
      - reserve_callback will call models.create_shorturl; pre-insert a ShortURL with slug 'collide' to cause DuplicateSlugError
    """
    user = create_user(email="s-user@example.com")
    # Pre-insert a row with slug 'collide' to force duplicate on first attempt
    existing = models.ShortURL(user_id=user.id, target_url="http://example.com/ex", slug="collide", is_custom=True)
    db_session.add(existing)
    db_session.commit()

    # Prepare generator sequence
    seq = ["collide", "unique42"]
    def fake_generate_slug(length=8, deterministic_source=None, secret=None):
        return seq.pop(0)

    monkeypatch.setattr(shortener, "generate_slug", fake_generate_slug)

    # Use reserve_callback that calls models.create_shorturl (maps to DuplicateSlugError on duplicate)
    def reserve_cb(candidate_slug, **kwargs):
        return models.create_shorturl(db_session, user_id=user.id, target_url="http://example.com/new", slug=candidate_slug, is_custom=False)

    result = shortener.find_unique_slug(session=db_session, length=8, max_retries=5, deterministic_source=None, secret=None, reserve_callback=reserve_cb)
    assert result == "unique42"
    # Ensure DB has new row for unique42
    row = db_session.query(models.ShortURL).filter_by(slug="unique42").first()
    assert row is not None
    assert row.slug == "unique42"
--- END FILE ---