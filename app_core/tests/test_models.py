"""
Unit tests for models & create_shorturl helper (KAN-131 model coverage).

Covers:
 - create_shorturl success path
 - create_shorturl duplicate slug maps to DuplicateSlugError
 - model relationships and to_dict helpers for basic coverage
"""

import pytest
from datetime import datetime
import models

def test_create_shorturl_success(db_session, create_user):
    user = create_user(email="m1@example.com")
    # Ensure no slug exists
    slug = "unique-slug-1"
    new = models.create_shorturl(db_session, user_id=user.id, target_url="http://example.com/page", slug=slug, is_custom=True)
    assert new is not None
    assert new.id is not None
    assert new.slug == slug
    assert new.user_id == user.id
    # to_dict should include expected keys
    d = new.to_dict()
    assert d["slug"] == slug
    assert d["user_id"] == user.id

def test_create_shorturl_duplicate_raises(db_session, create_user):
    user = create_user(email="m2@example.com")
    slug = "duplicate-slug"
    # Insert initial shorturl
    first = models.ShortURL(user_id=user.id, target_url="http://example.com/1", slug=slug, is_custom=True)
    db_session.add(first)
    db_session.commit()
    # Attempt to create same slug via create_shorturl -> DuplicateSlugError expected
    with pytest.raises(models.DuplicateSlugError):
        models.create_shorturl(db_session, user_id=user.id, target_url="http://example.com/2", slug=slug, is_custom=True)

def test_user_shorturl_relationship_and_clickevent_roundtrip(db_session, create_user):
    user = create_user(email="m3@example.com")
    short = models.ShortURL(user_id=user.id, target_url="http://example.com/a", slug="link-rel", is_custom=False)
    db_session.add(short)
    db_session.commit()
    db_session.refresh(short)
    # Relationship backref: user.short_urls should include the created short
    db_session.refresh(user)
    assert any(s.slug == "link-rel" for s in user.short_urls)
    # Create a ClickEvent referencing short
    click = models.ClickEvent(short_url_id=short.id, anonymized_ip="203.0.113.0", user_agent="pytest", referrer=None)
    db_session.add(click)
    db_session.commit()
    assert click.id is not None
    # back relationship short.click_events should contain the click
    db_session.refresh(short)
    assert any(c.id == click.id for c in short.click_events)
# --- END FILE ---