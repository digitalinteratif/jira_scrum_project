"""tests/test_db_users.py - Unit tests for app_core.db user helpers (KAN-161)"""

import sqlite3
import pytest

from app_core import db as db_mod


def test_create_users_table_and_basic_ops():
    # Use in-memory sqlite connection
    with db_mod.get_db_connection(":memory:") as conn:
        # create table (idempotent)
        db_mod._create_users_table(conn)

        # verify table exists
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'users'")
        assert cur.fetchone() is not None

        # insert user
        uid = db_mod.create_user(conn, "u@example.com", "h1")
        assert isinstance(uid, int) and uid > 0

        row = db_mod.get_user_by_email(conn, "u@example.com")
        assert row is not None
        assert row["email"] == "u@example.com"
        assert row["password_hash"] == "h1"


def test_duplicate_email_raises_integrityerror():
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        db_mod.create_user(conn, "dup@example.com", "h1")
        with pytest.raises(sqlite3.IntegrityError):
            db_mod.create_user(conn, "dup@example.com", "h2")


def test_get_user_by_email_not_found_returns_none():
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        row = db_mod.get_user_by_email(conn, "missing@example.com")
        assert row is None