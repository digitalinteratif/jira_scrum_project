"""Edge-case tests for data containing question marks."""
import pytest
from app_core.utils import sql_param as sp
from app_core import db as db_mod

def test_question_mark_in_data_is_preserved():
    url = "https://example.com/search?q=who?"
    with db_mod.get_db_connection(":memory:") as conn:
        sp.execute_query(conn, "CREATE TABLE urls (id INTEGER PRIMARY KEY, original_url TEXT);", ())
        sp.execute_query(conn, "INSERT INTO urls(original_url) VALUES(?)", (url,))
        cur = sp.execute_query(conn, "SELECT original_url FROM urls WHERE original_url = ?", (url,))
        row = cur.fetchone()
        assert row is not None
        assert row["original_url"] == url

def test_placeholder_count_advisory_does_not_crash():
    q = "INSERT INTO t(val) VALUES(?)"
    # calling count_placeholders on a standard query should not raise
    assert sp.count_placeholders(q) == 1
--- END FILE ---