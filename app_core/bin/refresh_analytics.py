#!/usr/bin/env python3
"""
bin/refresh_analytics.py - Refresh materialized views and rebuild summary tables for analytics (KAN-122)

Usage:
  - Dry run (show actions):
      python bin/refresh_analytics.py --dry-run

  - Refresh materialized views and rebuild summary tables:
      python bin/refresh_analytics.py

  - Refresh only materialized views:
      python bin/refresh_analytics.py --refresh-mv-only

  - Rebuild only summary tables (from MV):
      python bin/refresh_analytics.py --rebuild-summary-only

Notes:
 - Writes a best-effort trace to trace_KAN-122.txt.
 - Skips Postgres-specific operations when DATABASE_URL is sqlite://... to preserve dev fallback.
 - In Postgres it will attempt REFRESH MATERIALIZED VIEW CONCURRENTLY to avoid blocking readers.
   That requires the materialized view to have a UNIQUE index (created by migration).
"""

import os
import sys
import argparse
import time
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
import models

TRACE_FILE = "trace_KAN-122.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass

def get_database_url():
    return os.environ.get("DATABASE_URL", "sqlite:///local_dev.db")

def init_db_session():
    db_url = get_database_url()
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, connect_args=connect_args)
    SessionLocal = scoped_session(sessionmaker(bind=engine))
    try:
        models.init_db(engine, SessionLocal)
    except Exception:
        pass
    return engine, SessionLocal

def _is_postgres(engine):
    try:
        name = getattr(engine.dialect, "name", "")
        return name and name.lower().startswith("postgres")
    except Exception:
        return False

def refresh_materialized_views(engine, dry_run=False):
    """
    Refresh materialized views in Postgres (if present). Use CONCURRENTLY when possible (requires unique index).
    """
    if not _is_postgres(engine):
        _trace("REFRESH_MV_SKIPPED non-postgres environment")
        return {"skipped": True, "refreshed": []}

    refreshed = []
    conn = engine.connect()
    try:
        # Check if MV exists
        res = conn.execute(text("SELECT to_regclass('public.clicks_per_shorturl_day') AS exists")).scalar()
        if not res:
            _trace("REFRESH_MV_NONE clicks_per_shorturl_day not found")
            return {"skipped": False, "refreshed": []}

        if dry_run:
            _trace("REFRESH_MV_DRYRUN would refresh clicks_per_shorturl_day")
            return {"skipped": False, "refreshed": ["clicks_per_shorturl_day"]}

        # Try CONCURRENTLY first; fallback to plain REFRESH if it fails
        try:
            conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY clicks_per_shorturl_day"))
            refreshed.append("clicks_per_shorturl_day (concurrent)")
            _trace("REFRESH_MV_CONCURRENT clicks_per_shorturl_day success")
        except Exception as e:
            # Concurrent refresh may fail if unique index missing or inside transaction
            _trace(f"REFRESH_MV_CONCURRENT_FAILED clicks_per_shorturl_day err={str(e)}; falling back to non-concurrent")
            try:
                conn.execute(text("REFRESH MATERIALIZED VIEW clicks_per_shorturl_day"))
                refreshed.append("clicks_per_shorturl_day")
                _trace("REFRESH_MV_NONCONCURRENT clicks_per_shorturl_day success")
            except Exception as e2:
                _trace(f"REFRESH_MV_FAILED clicks_per_shorturl_day err={str(e2)}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {"skipped": False, "refreshed": refreshed}

def rebuild_summary_tables(engine, dry_run=False):
    """
    Rebuild analytics_summary_shorturl_daily from clicks_per_shorturl_day (Postgres only).
    This operation does a single transactional replace (INSERT ... ON CONFLICT) to avoid window where
    table is missing. In heavy-load systems a more sophisticated streaming delta approach is recommended.
    """
    if not _is_postgres(engine):
        _trace("REBUILD_SUMMARY_SKIPPED non-postgres environment")
        return {"skipped": True, "rebuilt": False}

    conn = engine.connect()
    trans = None
    try:
        # Confirm MV presence
        mv_exists = conn.execute(text("SELECT to_regclass('public.clicks_per_shorturl_day')")).scalar()
        if not mv_exists:
            _trace("REBUILD_SUMMARY_ABORT clicks_per_shorturl_day not found")
            return {"skipped": False, "rebuilt": False}

        if dry_run:
            _trace("REBUILD_SUMMARY_DRYRUN would rebuild analytics_summary_shorturl_daily from clicks_per_shorturl_day")
            return {"skipped": False, "rebuilt": False}

        # Rebuild summary by upserting from MV
        trans = conn.begin()
        conn.execute(text("""
            INSERT INTO analytics_summary_shorturl_daily (short_url_id, day, clicks)
            SELECT short_url_id, day, clicks FROM clicks_per_shorturl_day
            ON CONFLICT (short_url_id, day) DO UPDATE SET clicks = EXCLUDED.clicks;
        """))
        trans.commit()
        _trace("REBUILD_SUMMARY_APPLIED analytics_summary_shorturl_daily updated from MV")
        return {"skipped": False, "rebuilt": True}
    except Exception as e:
        if trans is not None:
            try:
                trans.rollback()
            except Exception:
                pass
        _trace(f"REBUILD_SUMMARY_FAILED err={str(e)}")
        return {"skipped": False, "rebuilt": False}
    finally:
        try:
            conn.close()
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description="Refresh analytics materialized views and rebuild summary tables (KAN-122).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--refresh-mv-only", action="store_true")
    parser.add_argument("--rebuild-summary-only", action="store_true")
    args = parser.parse_args()

    _trace(f"REFRESH_STARTED args={vars(args)}")

    engine, SessionLocal = init_db_session()

    results = {"mv": None, "summary": None}
    try:
        if args.rebuild_summary_only:
            results["summary"] = rebuild_summary_tables(engine, dry_run=args.dry_run)
        elif args.refresh_mv_only:
            results["mv"] = refresh_materialized_views(engine, dry_run=args.dry_run)
        else:
            results["mv"] = refresh_materialized_views(engine, dry_run=args.dry_run)
            results["summary"] = rebuild_summary_tables(engine, dry_run=args.dry_run)
        _trace(f"REFRESH_COMPLETED results={results}")
        if args.dry_run:
            print("Dry run results:", results)
        else:
            print("Refresh completed:", results)
        return 0
    except Exception as e:
        _trace(f"REFRESH_ERROR err={str(e)}")
        print("Error during refresh:", str(e))
        return 2

if __name__ == "__main__":
    sys.exit(main())
--- END FILE: bin/refresh_analytics.py ---