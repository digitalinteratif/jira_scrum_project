#!/usr/bin/env python3
"""
Manual migration: add UNIQUE constraint to Urls.short_code for SQLite DBs.

WARNING: This script performs a table-rebuild which can be destructive if duplicates exist.
Always run with --dry-run first; create a filesystem backup before applying.

Usage:
    python app_core/migrations/kan_175_add_unique_short_code.py --db-path <sqlite-uri-or-path> [--dry-run] [--no-backup]

Returns structured result when invoked programmatically (see add_unique()).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import traceback

try:
    from app_core import db as db_mod
except Exception:
    db_mod = None

TRACE_FILE = "trace_KAN-175.txt"
TABLE_NAME = "Urls"
COLUMN_NAME = "short_code"
INDEX_NAME = "idx_urls_short_code"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

def _fetch_table_sql(conn: sqlite3.Connection, table: str) -> str | None:
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    row = cur.fetchone()
    return row[0] if row else None

def _find_duplicates(conn: sqlite3.Connection, column: str) -> list:
    cur = conn.execute(f"SELECT {column}, COUNT(*) as cnt FROM {TABLE_NAME} WHERE {column} IS NOT NULL AND {column} != '' GROUP BY {column} HAVING cnt > 1")
    rows = cur.fetchall()
    # convert to list of tuples
    return [(r[0], r[1]) for r in rows]

def _rows_for_value(conn: sqlite3.Connection, column: str, value: str, limit: int = 10) -> list:
    cur = conn.execute(f"SELECT id, {column}, owner_user_id FROM {TABLE_NAME} WHERE {column} = ? ORDER BY id LIMIT ?", (value, limit))
    return [tuple(r) for r in cur.fetchall()]

def add_unique(db_path: str | None = None, dry_run: bool = True, make_backup: bool = True) -> dict:
    """
    Attempt to add UNIQUE constraint to Urls.short_code.

    Returns a dict:
      {
        "status": "ok" | "aborted" | "error",
        "message": str,
        "duplicates": [ (short_code, count), ... ]  # when aborted
      }

    dry_run=True: perform checks, do not modify DB.
    """
    result = {"status": "error", "message": "", "duplicates": []}
    if db_mod is None:
        result["message"] = "app_core.db module unavailable"
        _trace(result["message"])
        return result

    try:
        resolved = db_mod._resolve_db_path(db_path)
    except Exception as e:
        result["message"] = f"Failed to resolve DB path: {e}"
        _trace(result["message"])
        return result

    # Only support sqlite paths
    if resolved == ":memory:":
        # operate on in-memory DB using connection context from get_db_connection
        conn_ctx = db_mod.get_db_connection(resolved)
        is_file_db = False
    else:
        is_file_db = True

    try:
        # Use sqlite3 directly for DDL sequence to avoid row_factory surprises
        if is_file_db:
            conn = sqlite3.connect(resolved, check_same_thread=False)
            conn.row_factory = None
        else:
            conn = None

        # Use get_db_connection for PRAGMA application when possible
        with db_mod.get_db_connection(resolved) as ctx_conn:
            # For easier operations, get an underlying sqlite3.Connection
            sql_conn = ctx_conn  # row_factory set to sqlite.Row but OK for our queries
            # Check table exists
            if not db_mod._table_exists(sql_conn, TABLE_NAME):
                msg = f"Table '{TABLE_NAME}' does not exist in DB ({resolved}); nothing to do."
                _trace(msg)
                return {"status": "ok", "message": msg, "duplicates": []}

            # Check if column already has UNIQUE constraint
            if db_mod._column_has_unique_constraint(sql_conn, TABLE_NAME, COLUMN_NAME):
                # Ensure index exists as well
                if not db_mod._index_exists(sql_conn, INDEX_NAME):
                    sql_conn.execute(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE_NAME}({COLUMN_NAME});")
                    try:
                        sql_conn.commit()
                    except Exception:
                        try:
                            sql_conn.rollback()
                        except Exception:
                            pass
                msg = f"Column {TABLE_NAME}.{COLUMN_NAME} already has UNIQUE constraint/index in DB ({resolved})."
                _trace(msg)
                return {"status": "ok", "message": msg, "duplicates": []}

            # Check for NULL or empty values
            cur = sql_conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE {COLUMN_NAME} IS NULL OR {COLUMN_NAME} = ''")
            bad_count = cur.fetchone()[0]
            if bad_count and bad_count > 0:
                msg = f"Found {bad_count} rows where {COLUMN_NAME} is NULL or empty. Please fix these before applying UNIQUE constraint."
                _trace(msg)
                return {"status": "aborted", "message": msg, "duplicates": []}

            # Check duplicates
            duplicates = _find_duplicates(sql_conn, COLUMN_NAME)
            if duplicates:
                # Return duplicate list and abort
                dup_info = []
                for dup_val, cnt in duplicates:
                    sample_rows = _rows_for_value(sql_conn, COLUMN_NAME, dup_val, limit=10)
                    dup_info.append({"short_code": dup_val, "count": cnt, "sample_rows": sample_rows})
                msg = f"Duplicate short_code values detected ({len(duplicates)}). Aborting migration."
                _trace(msg)
                return {"status": "aborted", "message": msg, "duplicates": dup_info}

            # No duplicates and no nulls: safe to proceed (subject to dry_run)
            if dry_run:
                msg = f"Dry-run: No duplicates found and {COLUMN_NAME} can be made UNIQUE on table {TABLE_NAME} in DB ({resolved})."
                _trace(msg)
                return {"status": "ok", "message": msg, "duplicates": []}

            # Apply migration: backup file if requested and file-based
            backup_path = None
            if is_file_db and make_backup:
                try:
                    ts = int(time.time())
                    backup_path = f"{resolved}.kan175.bak.{ts}"
                    shutil.copy2(resolved, backup_path)
                    _trace(f"Backup created: {backup_path}")
                except Exception as e:
                    msg = f"Failed to create backup of DB file {resolved}: {e}"
                    _trace(msg)
                    return {"status": "error", "message": msg, "duplicates": []}

            # Execute table rebuild sequence
            try:
                # Start exclusive transaction
                sql_conn.execute("PRAGMA foreign_keys = OFF;")
                sql_conn.execute("BEGIN EXCLUSIVE;")
                # Create new table with UNIQUE short_code
                sql_conn.execute(
                    """
                    CREATE TABLE Urls_new (
                        id INTEGER PRIMARY KEY,
                        short_code TEXT NOT NULL UNIQUE,
                        original_url TEXT NOT NULL,
                        owner_user_id INTEGER,
                        created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                        FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
                    );
                    """
                )
                # Copy data
                sql_conn.execute(
                    "INSERT INTO Urls_new (id, short_code, original_url, owner_user_id, created_at) SELECT id, short_code, original_url, owner_user_id, created_at FROM Urls;"
                )
                # Drop old table
                sql_conn.execute("DROP TABLE Urls;")
                # Rename
                sql_conn.execute("ALTER TABLE Urls_new RENAME TO Urls;")
                # Recreate index if necessary
                sql_conn.execute(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE_NAME}({COLUMN_NAME});")
                sql_conn.execute("PRAGMA foreign_keys = ON;")
                sql_conn.execute("COMMIT;")
                msg = f"Applied UNIQUE constraint to {TABLE_NAME}.{COLUMN_NAME} in DB ({resolved})."
                _trace(msg)
                return {"status": "ok", "message": msg, "duplicates": []}
            except Exception as e:
                try:
                    sql_conn.execute("ROLLBACK;")
                except Exception:
                    pass
                # Attempt to restore from backup if available
                if backup_path and os.path.exists(backup_path):
                    try:
                        shutil.copy2(backup_path, resolved)
                        _trace(f"Restored DB from backup after failure: {backup_path}")
                    except Exception:
                        _trace(f"Failed to restore DB from backup: {backup_path}")
                tb = traceback.format_exc()
                _trace(f"Migration failed: {str(e)}")
                _trace(tb)
                return {"status": "error", "message": f"Migration failed: {e}", "duplicates": []}
    except Exception as e:
        tb = traceback.format_exc()
        _trace(f"Top-level error during add_unique: {str(e)}")
        _trace(tb)
        return {"status": "error", "message": str(e), "duplicates": []}
    finally:
        try:
            if is_file_db and conn:
                conn.close()
        except Exception:
            pass

def main(argv=None):
    parser = argparse.ArgumentParser(description="Add UNIQUE constraint to Urls.short_code for sqlite DB (KAN-175).")
    parser.add_argument("--db-path", type=str, default=None, help="Optional DB path or sqlite URI (overrides DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Dry run: only perform checks")
    parser.add_argument("--no-backup", action="store_true", default=False, help="When applying, skip creating a backup copy of the DB file")
    args = parser.parse_args(argv)

    res = add_unique(db_path=args.db_path, dry_run=args.dry_run, make_backup=(not args.no_backup))
    if res["status"] == "ok":
        print(res["message"])
        sys.exit(0)
    elif res["status"] == "aborted":
        print("ABORTED:", res["message"])
        if res.get("duplicates"):
            print("Duplicates (sample):")
            for d in res["duplicates"]:
                print(f"  short_code={d['short_code']} count={d['count']} sample_rows={d['sample_rows']}")
        sys.exit(2)
    else:
        print("ERROR:", res["message"])
        sys.exit(3)

if __name__ == "__main__":
    main()