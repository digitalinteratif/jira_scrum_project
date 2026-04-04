"""
tests/test_geoip_enrichment.py - Unit & integration tests for GeoIP enrichment (KAN-147)

Notes:
 - Tests mock maxminddb reader access; do not require real GeoIP DB.
 - These are best-effort unit/integration tests to validate enrichment logic end-to-end.
"""

import os
import time
import pytest
from datetime import datetime
import models

# import enrichment helpers (we will import internal functions for unit testing)
import importlib
enrich_mod = importlib.import_module("bin.enrich_geoip")

# Helper: create user and short + click
def _create_click(db_session, anonymized_ip="203.0.113.0"):
    user = models.User(email=f"geo-{int(time.time())}@example.com", password_hash="pw", is_active=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    short = models.ShortURL(user_id=user.id, target_url="http://example.com/", slug=f"geo-{int(time.time())}", is_custom=True)
    db_session.add(short)
    db_session.commit()
    db_session.refresh(short)
    click = models.ClickEvent(short_url_id=short.id, anonymized_ip=anonymized_ip, user_agent="pytest", referrer=None, occurred_at=datetime.utcnow())
    db_session.add(click)
    db_session.commit()
    db_session.refresh(click)
    return user, short, click

class FakeReader:
    def __init__(self, mapping=None):
        # mapping: ip -> iso code
        self.mapping = mapping or {}

    def get(self, ip):
        code = self.mapping.get(ip)
        if not code:
            return None
        return {"country": {"iso_code": code}}

    def close(self):
        pass

def test_lookup_country_unit():
    reader = FakeReader({"203.0.113.0": "US", "198.51.100.0": "GB"})
    assert enrich_mod._lookup_country(reader, "203.0.113.0") == "US"
    assert enrich_mod._lookup_country(reader, "unknown") is None

@pytest.mark.usefixtures("db_session")
def test_process_batch_integration(db_session, monkeypatch):
    # Ensure env config allows enrichment
    monkeypatch.setenv("GEOIP_ENABLED", "1")
    # create sample click
    user, short, click = _create_click(db_session, anonymized_ip="203.0.113.0")

    # Create fake reader and patch _open_reader to return it
    fake = FakeReader({"203.0.113.0": "US"})
    monkeypatch.setattr(enrich_mod, "_open_reader", lambda path=None: fake)
    # Call _process_batch directly
    session = db_session
    updated = enrich_mod._process_batch(session=session, reader=fake, batch_limit=10, allow_enrich_from_anon=True, trace_file="trace_KAN-147_test.txt")
    assert updated >= 1

    # Fetch updated click
    c = session.query(models.ClickEvent).filter_by(id=click.id).first()
    assert c is not None
    assert c.country == "US"

    # Clean up trace
    try:
        os.remove("trace_KAN-147_test.txt")
    except Exception:
        pass
--- END FILE: tests/test_geoip_enrichment.py ---