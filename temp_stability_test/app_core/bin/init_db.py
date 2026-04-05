#!/usr/bin/env python3
"""
Standalone DB initializer script (invokes app_core.db.init_db).

Usage:
  python app_core/bin/init_db.py [--db-path PATH_OR_URI] [--dry-run]

Exit codes:
  0 - success
  2 - initialization failure
"""

from __future__ import annotations

import argparse
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize local sqlite DB (idempotent).")
    parser.add_argument("--db-path", type=str, default=None, help="Optional sqlite path or sqlite URI (overrides DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run: report actions without applying them")
    args = parser.parse_args(argv)

    try:
        # Local import to avoid importing heavy app modules
        from app_core import db as db_mod
    except Exception as e:
        print("ERROR: Failed to import app_core.db:", e, file=sys.stderr)
        return 2

    try:
        db_mod.init_db(db_path=args.db_path, dry_run=bool(args.dry_run))
        print("INFO: Database initialized at", args.db_path or __import__("os").environ.get("DATABASE_URL", "sqlite:///shortener.db"))
        return 0
    except Exception as e:
        print("ERROR: init_db failed:", str(e), file=sys.stderr)
        return 2

if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
--- END FILE ---