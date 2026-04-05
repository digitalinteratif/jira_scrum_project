#!/usr/bin/env python3
"""
Migration helper (safe): ensure idx_urls_short_code exists on the Urls table.

Usage:
    python app_core/migrations/kan_175_ensure_idx_urls_short_code.py [--db-path <sqlite-uri-or-path>]

Behavior:
    - Resolves DB path using app_core.db._resolve_db_path semantics.
    - If Urls table exists and index missing, creates it (CREATE INDEX IF NOT EXISTS ...).
    - Idempotent and safe to run at startup.
    - Writes a small trace to trace_KAN-175.txt.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback

try:
    from app_core import db as db_mod
except Exception:
    db_mod = None

TRACE_FILE = "trace_KAN-175.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

def ensure_index(db_path: str | None = None) -> dict:
    """
    Ensure idx_urls_short_code exists. Returns dict with result.
    """
    result = {"ok": False, "created": False, "message": ""}
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

    try:
        with db_mod.get_db_connection(resolved) as conn:
            if not db_mod._table_exists(conn, "Urls"):
                msg = f"Table 'Urls' not found in DB ({resolved}); nothing to do."
                result.update({"ok": True, "created": False, "message": msg})
                _trace(msg)
                return result

            if db_mod._index_exists(conn, "idx_urls_short_code"):
                msg = f"Index 'idx_urls_short_code' already exists in DB ({resolved})."
                result.update({"ok": True, "created": False, "message": msg})
                _trace(msg)
                return result

            # Create index
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_short_code ON Urls(short_code);")
            try:
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            msg = f"Created index 'idx_urls_short_code' on Urls(short_code) in DB ({resolved})."
            result.update({"ok": True, "created": True, "message": msg})
            _trace(msg)
            return result
    except Exception as e:
        tb = traceback.format_exc()
        result.update({"ok": False, "created": False, "message": f"Error ensuring index: {str(e)}", "trace": tb})
        _trace(f"Error ensuring index: {str(e)}")
        _trace(tb)
        return result

def main(argv=None):
    parser = argparse.ArgumentParser(description="Ensure idx_urls_short_code index exists on Urls table (KAN-175).")
    parser.add_argument("--db-path", type=str, default=None, help="Optional DB path or sqlite URI (overrides DATABASE_URL)")
    args = parser.parse_args(argv)

    res = ensure_index(db_path=args.db_path)
    if not res.get("ok"):
        print("ERROR:", res.get("message"))
        if "trace" in res:
            print(res["trace"])
        sys.exit(2)
    else:
        print(res.get("message"))
        sys.exit(0)

if __name__ == "__main__":
    main()