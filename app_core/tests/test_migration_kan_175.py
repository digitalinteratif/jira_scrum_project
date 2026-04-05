"""Integration tests for KAN-175 migrations (ensure index & add UNIQUE constraint)."""

import os
import tempfile
import sqlite3
import shutil
import time

import pytest

from app_core import db as db_mod

try:
    from app_core.migrations import kan_175_ensure_idx_urls_short_code as ensure_idx_mod
    from app_core.migrations import kan_175_add_unique_short_code as add_unique_mod
except Exception:
    ensure_idx_mod = None
    add_unique_mod = None

def _create_db_file():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path

def _drop_db_file(path):
    try:
        os.remove(path)
    except Exception:
        pass

def test_migration_index_creation_when_missing(tmp_path):
    # Create a fresh sqlite DB file with Urls table but without index
    db_file = tmp_path / "test_idx.db"
    db_path = str(db_file)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Urls (
                id INTEGER PRIMARY KEY,
                short_code TEXT NOT NULL,
                original_url TEXT NOT NULL,
                owner_user_id INTEGER,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Ensure module available
    assert ensure_idx_mod is not None, "ensure index module not available"

    res = ensure_idx_mod.ensure_index(db_path=db_path)
    assert res["ok"] is True
    # Verify index now exists
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name = ?", ("idx_urls_short_code",))
        assert cur.fetchone() is not None
    finally:
        conn.close()

def test_unique_migration_aborts_on_duplicates(tmp_path):
    db_file = tmp_path / "test_dup.db"
    db_path = str(db_file)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Urls (
                id INTEGER PRIMARY KEY,
                short_code TEXT NOT NULL,
                original_url TEXT NOT NULL,
                owner_user_id INTEGER,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        # Insert duplicate short_code values
        conn.execute("INSERT INTO Urls (short_code, original_url, owner_user_id) VALUES (?, ?, ?)", ("dupcode", "http://a", 1))
        conn.execute("INSERT INTO Urls (short_code, original_url, owner_user_id) VALUES (?, ?, ?)", ("dupcode", "http://b", 2))
        conn.commit()
    finally:
        conn.close()

    assert add_unique_mod is not None, "add_unique module not available"

    res = add_unique_mod.add_unique(db_path=db_path, dry_run=True, make_backup=False)
    assert res["status"] == "aborted"
    assert res["duplicates"], "Expected duplicates reported"

    # Ensure schema unchanged (no UNIQUE on short_code)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", ("Urls",))
        row = cur.fetchone()
        assert row is not None
        table_sql = row[0]
        assert "UNIQUE" not in table_sql.upper()
    finally:
        conn.close()

def test_unique_migration_success_on_clean_db(tmp_path):
    db_file = tmp_path / "test_clean.db"
    db_path = str(db_file)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS Urls (
                id INTEGER PRIMARY KEY,
                short_code TEXT NOT NULL,
                original_url TEXT NOT NULL,
                owner_user_id INTEGER,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        # Insert unique entries
        conn.execute("INSERT INTO Urls (short_code, original_url, owner_user_id) VALUES (?, ?, ?)", ("a1", "http://a", 1))
        conn.execute("INSERT INTO Urls (short_code, original_url, owner_user_id) VALUES (?, ?, ?)", ("b2", "http://b", 2))
        conn.commit()
    finally:
        conn.close()

    res = add_unique_mod.add_unique(db_path=db_path, dry_run=False, make_backup=True)
    assert res["status"] == "ok"

    # Verify table SQL contains UNIQUE
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", ("Urls",))
        row = cur.fetchone()
        assert row is not None
        table_sql = row[0]
        assert "UNIQUE" in table_sql.upper()

        # Attempt to insert duplicate should raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO Urls (short_code, original_url, owner_user_id) VALUES (?, ?, ?)", ("a1", "http://c", 3))
            conn.commit()
    finally:
        conn.close()