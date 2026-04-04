"""tests/test_purge_old_clicks.py - Unit tests for scripts/purge_old_clicks.purge_old_clicks (KAN-142)"""

import pytest
import time
from datetime import datetime, timedelta

# Import the purge function directly
try:
    from scripts.purge_old_clicks import purge_old_clicks, _compute_cutoff
except Exception:
    purge_old_clicks = None

import models

def _make_event(session, short_url_id: int, occurred_at: datetime, anonymized_ip="203.0.113.0"):
    ev = models.ClickEvent(short_url_id=short_url_id, occurred_at=occurred_at, anonymized_ip=anonymized_ip, user_agent="pytest", referrer=None, country=None)
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return ev

@pytest.mark.skipif(purge_old_clicks is None, reason="purge_old_clicks module not available")
def test_purge_dry_run_and_apply(db_session):
    """
    Create a set of ClickEvent rows: some older than retention (90 days), some newer.
    Verify that dry_run reports correct counts and that apply actually removes the old rows.
    """
    # Create a user & shorturl to satisfy FK constraint
    user = models.User(email="purgert@example.com", password_hash="pw", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    short = models.ShortURL(user_id=user.id, target_url="http://example.com", slug="purge-test", is_custom=True)
    db_session.add(short)
    db_session.commit()
    db_session.refresh(short)

    now = datetime.utcnow()
    # Old events: 3 events older than 90 days
    old_dates = [now - timedelta(days=100), now - timedelta(days=200), now - timedelta(days=365)]
    old_ev_ids = []
    for od in old_dates:
        ev = _make_event(db_session, short.id, od)
        old_ev_ids.append(ev.id)

    # Recent events: 2 events inside retention
    recent_dates = [now - timedelta(days=10), now - timedelta(days=1)]
    recent_ev_ids = []
    for rd in recent_dates:
        ev = _make_event(db_session, short.id, rd)
        recent_ev_ids.append(ev.id)

    # Dry run: retention 90 days
    res = purge_old_clicks(session=db_session, retention_days=90, dry_run=True, batch_size=0, limit=0)
    assert "found_total" in res
    assert res["found_total"] >= 3, f"Expected at least 3 candidates, got {res['found_total']}"
    # No deletion occurred
    # Count in DB should still show all events
    all_count = db_session.query(models.ClickEvent).filter_by(short_url_id=short.id).count()
    assert all_count == 5

    # Now apply deletion with batch_size to exercise batched deletion logic
    res_apply = purge_old_clicks(session=db_session, retention_days=90, dry_run=False, batch_size=2, limit=0)
    assert res_apply["deleted"] >= 3
    # After apply, only recent events remain
    remaining = db_session.query(models.ClickEvent).filter_by(short_url_id=short.id).all()
    remaining_ids = [r.id for r in remaining]
    for oid in old_ev_ids:
        assert oid not in remaining_ids
    for rid in recent_ev_ids:
        assert rid in remaining_ids

def test_compute_cutoff():
    # Basic sanity for cutoff computation
    c = _compute_cutoff(90)
    assert isinstance(c, datetime)
    # cutoff should be roughly 90 days in the past (with small tolerance)
    delta = datetime.utcnow() - c
    assert abs(delta.days - 90) <= 1
# --- END FILE: tests/test_purge_old_clicks.py ---