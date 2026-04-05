"""tests/test_db_urls.py - Unit tests for URLs schema and helpers (KAN-162)"""

import sqlite3
import pytest

from app_core import db as db_mod


def test_create_urls_table_and_index_exists():
    with db_mod.get_db_connection(":memory:") as conn:
        # create users table first (Urls has FK to users)
        db_mod._create_users_table(conn)
        # create urls table
        db_mod.create_urls_table(conn)

        # Verify table exists
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = 'Urls'")
        assert cur.fetchone() is not None

        # Verify index exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name = 'idx_urls_short_code'")
        assert cur.fetchone() is not None


def test_create_url_mapping_and_get_by_short_code():
    with db_mod.get_db_connection(":memory:") as conn:
        # prepare users table and insert a user
        db_mod._create_users_table(conn)
        uid = db_mod.create_user(conn, "u@example.com", "h1")

        # create urls table
        db_mod.create_urls_table(conn)

        # insert mapping
        sid = db_mod.create_url_mapping(conn, "abc123", "https://example.com/page", uid)
        assert isinstance(sid, int) and sid > 0

        # fetch mapping
        row = db_mod.get_url_by_short_code(conn, "abc123")
        assert row is not None
        assert row["short_code"] == "abc123"
        assert row["original_url"] == "https://example.com/page"
        assert row["owner_user_id"] == uid


def test_duplicate_short_code_raises_integrityerror():
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        uid = db_mod.create_user(conn, "dupuser@example.com", "pw")
        db_mod.create_urls_table(conn)

        _ = db_mod.create_url_mapping(conn, "dup", "http://a", uid)
        with pytest.raises(sqlite3.IntegrityError):
            db_mod.create_url_mapping(conn, "dup", "http://b", uid)


def test_list_urls_by_owner_limit_and_order():
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        uid1 = db_mod.create_user(conn, "owner1@example.com", "pw")
        uid2 = db_mod.create_user(conn, "owner2@example.com", "pw2")
        db_mod.create_urls_table(conn)

        # create multiple entries for uid1 and some for uid2
        codes = []
        for i in range(6):
            code = f"u1-{i}"
            db_mod.create_url_mapping(conn, code, f"http://example.com/{i}", uid1)
            codes.append(code)
        # create for other owner
        db_mod.create_url_mapping(conn, "other-1", "http://other", uid2)

        # list with limit 3 should return 3 most recent (created_at desc)
        result = db_mod.list_urls_by_owner(conn, uid1, limit=3)
        assert isinstance(result, list)
        assert len(result) == 3
        # ensure returned slugs are subset of codes and are ordered by created_at desc
        returned_codes = [r["short_code"] for r in result]
        assert all(c in codes for c in returned_codes)
        # most recent should be last created: u1-5 first in returned list
        assert returned_codes[0] == "u1-5"


def test_foreign_key_enforcement_on_vs_off():
    # When foreign_keys ON, insertion with non-existent owner should fail
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        db_mod.create_urls_table(conn)

        # Ensure pragma foreign_keys is ON (get_db_connection sets it)
        conn.execute("PRAGMA foreign_keys = ON;")
        with pytest.raises(sqlite3.IntegrityError):
            db_mod.create_url_mapping(conn, "fktest", "http://no-owner", 9999)

    # When foreign_keys OFF, insertion should succeed (demonstrates behavior)
    with db_mod.get_db_connection(":memory:") as conn:
        db_mod._create_users_table(conn)
        db_mod.create_urls_table(conn)

        conn.execute("PRAGMA foreign_keys = OFF;")
        # Should not raise
        new_id = db_mod.create_url_mapping(conn, "fktest2", "http://no-owner", 9999)
        assert isinstance(new_id, int) and new_id > 0

--- END FILE: app_core/tests/test_db_urls.py ---