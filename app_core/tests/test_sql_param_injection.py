"""Integration test to ensure SQL meta-characters are treated as data, not executable SQL."""
import pytest
from app_core.utils import sql_param as sp
from app_core import db as db_mod

def test_injection_string_stored_literal():
    malicious = "Robert'); DROP TABLE users;--"
    with db_mod.get_db_connection(":memory:") as conn:
        # create table users and verify it exists after insertion
        sp.execute_query(conn, "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);", ())
        sp.execute_query(conn, "INSERT INTO users(email) VALUES(?)", (malicious,))
        # select back
        cur = sp.execute_query(conn, "SELECT email FROM users WHERE email = ?", (malicious,))
        row = cur.fetchone()
        assert row is not None
        assert row["email"] == malicious
        # ensure table still exists by inserting another row
        sp.execute_query(conn, "INSERT INTO users(email) VALUES(?)", ("safe@example.com",))
        cur2 = sp.execute_query(conn, "SELECT COUNT(*) as c FROM users", ())
        count = cur2.fetchone()["c"]
        assert count == 2
--- END FILE ---