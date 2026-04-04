#!/usr/bin/env python3
"""
bin/cleanup_expired.py - Administrative cleanup/flagging tool for expired ShortURL rows.

Usage:
  - Dry run (show counts / sample rows):
      python bin/cleanup_expired.py --dry-run

  - Flag expired rows as inactive (soft flag; sets is_active=False):
      python bin/cleanup_expired.py --flag

  - Purge expired rows (hard delete):
      python bin/cleanup_expired.py --purge

  - Limit by older-than seconds (optional), e.g. only purge links expired > 86400s ago:
      python bin/cleanup_expired.py --purge --older-than 86400

Notes:
  - This script uses DATABASE_URL environment variable (same as app) to connect.
  - It writes a non-blocking trace to trace_KAN-119.txt for Architectural Memory.
  - It performs operations using server UTC time (datetime.utcnow()).
  - It will not proceed with destructive actions when --dry-run is specified.
"""

import os
import argparse
from datetime import datetime, timedelta
import time
import sys

# SQLAlchemy imports
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker, scoped_session

# Import models module (expects models.py to define init_db, Session)
import models

TRACE_FILE = "trace_KAN-119.txt"

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
    # For sqlite file-based in tests, ensure check_same_thread false semantics similar to app.create_app
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, connect_args=connect_args)
    SessionLocal = scoped_session(sessionmaker(bind=engine))
    # initialize models module runtime references so models.Session/Engine are available
    try:
        models.init_db(engine, SessionLocal)
    except Exception:
        pass
    return engine, SessionLocal

def main():
    parser = argparse.ArgumentParser(description="Cleanup expired ShortURL rows (purge or flag).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--purge", action="store_true", help="Hard delete expired ShortURL rows.")
    group.add_argument("--flag", action="store_true", help="Soft-flag expired rows by setting is_active=False.")
    parser.add_argument("--dry-run", action="store_true", help="Do not modify DB; only report what would happen.")
    parser.add_argument("--older-than", type=int, default=0, help="Only operate on rows that expired more than N seconds ago.")
    parser.add_argument("--limit", type=int, default=0, help="Batch limit (0 = no limit).")
    args = parser.parse_args()

    _trace(f"CLEANUP_STARTED args={vars(args)}")

    engine, SessionLocal = init_db_session()
    session = SessionLocal()

    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=max(0, args.older_than))

        # Build base query: expire_at != None AND expire_at < cutoff
        from sqlalchemy import func
        q = session.query(models.ShortURL).filter(models.ShortURL.expire_at != None, models.ShortURL.expire_at < cutoff)

        # Get count
        try:
            total = q.count()
        except Exception as e:
            _trace(f"CLEANUP_ERROR counting expired rows: {str(e)}")
            print("Error counting expired rows:", str(e))
            return 2

        _trace(f"CLEANUP_FOUND total_expired={total} cutoff={cutoff.isoformat()}")

        # Show a small sample for operator
        sample_rows = []
        try:
            sample_q = q.order_by(models.ShortURL.expire_at.asc())
            if args.limit and args.limit > 0:
                sample_q = sample_q.limit(min(10, args.limit))
            sample_rows = sample_q.all()
        except Exception:
            sample_rows = []

        print(f"Found {total} expired ShortURL rows (expire_at < {cutoff.isoformat()}).")
        if sample_rows:
            print("Sample expired rows:")
            for r in sample_rows[:10]:
                print(f"  - id={r.id} slug={r.slug} user_id={r.user_id} expire_at={r.expire_at} is_active={getattr(r, 'is_active', None)}")

        if args.dry_run:
            _trace("CLEANUP_DRYRUN exiting without changes")
            print("Dry run; no changes applied.")
            return 0

        if total == 0:
            _trace("CLEANUP_NOOP nothing to do")
            print("Nothing to do.")
            return 0

        # Apply requested action
        if args.purge:
            # Delete rows in batches if limit specified
            try:
                if args.limit and args.limit > 0:
                    deleted = 0
                    # fetch ids to delete to avoid locking differences across DBs
                    ids = [r.id for r in q.limit(args.limit).all()]
                    if ids:
                        deleted = session.query(models.ShortURL).filter(models.ShortURL.id.in_(ids)).delete(synchronize_session=False)
                        session.commit()
                        _trace(f"CLEANUP_PURGED batch_count={deleted} ids={ids}")
                        print(f"Purged {deleted} rows (batch).")
                    else:
                        print("No rows selected for purge with the provided limit.")
                else:
                    deleted = q.delete(synchronize_session=False)
                    session.commit()
                    _trace(f"CLEANUP_PURGED total_deleted={deleted}")
                    print(f"Purged {deleted} expired rows.")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"CLEANUP_ERROR purge_failed: {str(e)}")
                print("Error purging expired rows:", str(e))
                return 3
        else:
            # Default or --flag: soft-flag by setting is_active=False (requires models.ShortURL.is_active column)
            try:
                update_values = {"is_active": False}
                if args.limit and args.limit > 0:
                    # limit update by selecting IDs first
                    ids = [r.id for r in q.limit(args.limit).all()]
                    if ids:
                        updated = session.query(models.ShortURL).filter(models.ShortURL.id.in_(ids)).update(update_values, synchronize_session=False)
                        session.commit()
                        _trace(f"CLEANUP_FLAGGED batch_updated={updated} ids={ids}")
                        print(f"Flagged {updated} rows as inactive (batch).")
                    else:
                        print("No rows selected for flagging with the provided limit.")
                else:
                    updated = q.update(update_values, synchronize_session=False)
                    session.commit()
                    _trace(f"CLEANUP_FLAGGED total_updated={updated}")
                    print(f"Flagged {updated} rows as inactive.")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"CLEANUP_ERROR flag_failed: {str(e)}")
                print("Error flagging expired rows:", str(e))
                return 4

        _trace("CLEANUP_COMPLETED success")
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())