#!/usr/bin/env python3
"""
scripts/purge_old_clicks.py - Purge ClickEvent rows older than configured retention.

Usage (CLI):
  # dry-run (preview deletions, no DB modification)
  python scripts/purge_old_clicks.py --dry-run

  # purge using configured days (app config or env), destructive
  python scripts/purge_old_clicks.py --apply

  # override retention days
  python scripts/purge_old_clicks.py --days 90 --apply

  # perform deletions in batches (safer for large tables)
  python scripts/purge_old_clicks.py --apply --batch-size 1000

  # limit total rows to delete (useful for incremental runs)
  python scripts/purge_old_clicks.py --apply --limit 5000 --batch-size 500

Notes:
 - This script is defensive: it uses models.Session via a local engine if executed standalone.
 - It will not delete user accounts, sessions, or other tables — only models.ClickEvent rows are targeted.
 - Always run with --dry-run first to preview deletions.
 - Architectural Memory: writes to trace_KAN-142.txt for all major steps.
"""

from __future__ import annotations
import os
import sys
import argparse
import time
from datetime import datetime, timedelta
from typing import Optional

# SQLAlchemy imports
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, scoped_session

# Defensive import of models module
try:
    import models
except Exception:
    models = None  # will be handled later

TRACE_FILE = "trace_KAN-142.txt"

def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # best-effort only; never raise
        pass


def init_db_session_from_env(database_url: Optional[str] = None):
    """
    Initialize an engine + scoped_session using DATABASE_URL env (or provided db_url).
    Also initializes models.init_db so models.Session/Engine are available if models module was imported.
    """
    db_url = database_url or os.environ.get("DATABASE_URL", "sqlite:///local_dev.db")
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    try:
        engine = create_engine(db_url, connect_args=connect_args)
    except TypeError:
        # Some create_engine signatures in older SQLAlchemy expect connect_args differently
        try:
            engine = create_engine(db_url)
        except Exception as e:
            _trace(f"INIT_DB_FAILED create_engine_error err={str(e)} db_url={db_url}")
            raise

    SessionLocal = scoped_session(sessionmaker(bind=engine))
    # Initialize models module runtime references so models.Session/Engine are available to callers.
    try:
        if models is not None:
            models.init_db(engine, SessionLocal)
    except Exception:
        try:
            _trace("INIT_DB_WARNING models.init_db failed (continuing)")
        except Exception:
            pass
    return engine, SessionLocal


def _compute_cutoff(retention_days: int) -> datetime:
    """
    Compute UTC cutoff datetime: events strictly older than cutoff will be purged.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=int(retention_days))
    return cutoff


def purge_old_clicks(session, retention_days: int, dry_run: bool = True, batch_size: int = 0, limit: int = 0):
    """
    Purge ClickEvent rows older than retention_days.

    Parameters:
      - session: an active SQLAlchemy Session instance (caller manages session lifecycle)
      - retention_days: integer number of days to retain (rows older than now - retention_days are deleted)
      - dry_run: if True, do not modify DB; instead return counts and preview ids
      - batch_size: when > 0, perform deletions in batches of this size (recommended for large tables)
      - limit: optional cap on total rows to delete (0 => no limit)

    Returns:
      dict with keys:
        - found_total: number of candidate rows matching cutoff (approximate if DB backend counts differently)
        - planned_delete: number of rows that would be/are deleted (subject to limit)
        - deleted: actual deleted rows (0 when dry_run is True)
        - sample_ids: list of up to 10 sample ClickEvent ids (for operator review)
    """
    if session is None:
        raise ValueError("session must be provided")

    cutoff = _compute_cutoff(retention_days)
    _trace(f"PURGE_INVOCATION retention_days={retention_days} cutoff={cutoff.isoformat()} dry_run={dry_run} batch_size={batch_size} limit={limit}")

    # Build base query for candidate rows
    try:
        q = session.query(models.ClickEvent).filter(models.ClickEvent.occurred_at < cutoff)
    except Exception as e:
        _trace(f"PURGE_ERROR building_query err={str(e)}")
        raise

    # Count candidates (best-effort)
    try:
        found_total = q.count()
    except Exception as e:
        # Some DBs may have expensive counts; fall back to scanning limited sample
        _trace(f"PURGE_COUNT_ERROR err={str(e)} - will attempt approximate count via limited scan")
        try:
            # approximate via selecting limited ids in batches
            found_total = len(session.query(models.ClickEvent.id).filter(models.ClickEvent.occurred_at < cutoff).limit(10000).all())
        except Exception:
            found_total = -1

    # Sample ids for operator preview
    sample_ids = []
    try:
        sample_q = q.with_entities(models.ClickEvent.id).order_by(models.ClickEvent.occurred_at.asc()).limit(10)
        sample_ids = [r.id for r in sample_q.all()]
    except Exception:
        sample_ids = []

    planned_delete = found_total if (found_total >= 0 and limit == 0) else (min(found_total, limit) if found_total >= 0 and limit > 0 else limit)

    result = {
        "found_total": int(found_total) if isinstance(found_total, int) and found_total >= 0 else found_total,
        "planned_delete": int(planned_delete) if isinstance(planned_delete, int) else planned_delete,
        "deleted": 0,
        "sample_ids": sample_ids,
        "cutoff": cutoff.isoformat(),
    }

    if dry_run:
        _trace(f"PURGE_DRYRUN found_total={found_total} sample_ids={sample_ids}")
        return result

    # Destructive path: perform deletions. Two strategies:
    #  - If batch_size <= 0 and limit == 0: single bulk delete q.delete(synchronize_session=False)
    #  - Otherwise: iterate selecting ids limited by min(batch_size, remaining_limit) and delete by id in batches
    deleted_total = 0

    try:
        if (batch_size <= 0) and (limit == 0):
            # single bulk delete
            try:
                deleted = q.delete(synchronize_session=False)
                session.commit()
                deleted_total = int(deleted)
                _trace(f"PURGE_BULK_DELETED count={deleted_total}")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"PURGE_BULK_ERROR err={str(e)}")
                raise
        else:
            # Batch delete loop
            remaining = limit if (limit and limit > 0) else None
            bsize = int(batch_size) if batch_size and batch_size > 0 else 1000
            while True:
                # determine current batch size considering remaining limit
                cur_limit = bsize if remaining is None else min(bsize, remaining)
                # fetch candidate ids
                try:
                    ids = [r.id for r in session.query(models.ClickEvent.id).filter(models.ClickEvent.occurred_at < cutoff).order_by(models.ClickEvent.occurred_at.asc()).limit(cur_limit).all()]
                except Exception as e:
                    _trace(f"PURGE_BATCH_SELECT_ERROR err={str(e)}")
                    break

                if not ids:
                    break

                try:
                    deleted = session.query(models.ClickEvent).filter(models.ClickEvent.id.in_(ids)).delete(synchronize_session=False)
                    session.commit()
                    deleted_count = int(deleted)
                    deleted_total += deleted_count
                    _trace(f"PURGE_BATCH_DELETED batch={len(ids)} deleted={deleted_count} remaining_before={remaining}")
                except Exception as e:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    _trace(f"PURGE_BATCH_DELETE_ERROR ids={ids[:10]} err={str(e)}")
                    # stop on repeated failures to avoid loops
                    break

                if remaining is not None:
                    remaining -= deleted_count
                    if remaining <= 0:
                        break

                # stop if last batch smaller than requested -> no more rows
                if len(ids) < cur_limit:
                    break

            _trace(f"PURGE_BATCH_COMPLETED deleted_total={deleted_total}")

    except Exception as e:
        _trace(f"PURGE_ERROR_TOPLEVEL err={str(e)}")
        raise

    result["deleted"] = deleted_total
    return result


def _cli_main(argv=None):
    parser = argparse.ArgumentParser(description="Purge old ClickEvent rows (KAN-142).")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--apply", action="store_true", help="Apply deletions (destructive). If omitted, script runs in dry-run mode.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run (no changes); same as omitting --apply.")
    parser.add_argument("--days", type=int, default=None, help="Retention in days (overrides app/config or DATA_RETENTION_DAYS env).")
    parser.add_argument("--batch-size", type=int, default=0, help="Batch size for deletions (0 = use single bulk delete).")
    parser.add_argument("--limit", type=int, default=0, help="Maximum total rows to delete (0 = no limit).")
    parser.add_argument("--database-url", type=str, default=None, help="Optional DATABASE_URL (overrides env).")
    args = parser.parse_args(argv)

    # Resolve retention days: CLI -> env/DATABASE_URL -> fallback to app default if available
    retention_days = None
    if args.days is not None:
        retention_days = int(args.days)
    else:
        # Try to fetch from Flask-style app config by importing app.py create_app (best-effort), else env
        try:
            # Avoid importing whole Flask app; try to import app.create_app if present
            from app import create_app  # type: ignore
            # create a minimal app to read config if environment available (but don't push app context heavy operations)
            try:
                app = create_app(test_config={})  # will use envs if present
                retention_days = int(app.config.get("DATA_RETENTION_DAYS", os.environ.get("DATA_RETENTION_DAYS", 90)))
            except Exception:
                retention_days = int(os.environ.get("DATA_RETENTION_DAYS", 90))
        except Exception:
            retention_days = int(os.environ.get("DATA_RETENTION_DAYS", 90))

    dry_run = (not args.apply) or args.dry_run
    batch_size = int(args.batch_size or 0)
    limit = int(args.limit or 0)

    _trace(f"CLI_START apply={args.apply} dry_run={dry_run} retention_days={retention_days} batch_size={batch_size} limit={limit}")

    # Init DB session
    try:
        engine, SessionLocal = init_db_session_from_env(args.database_url)
    except Exception as e:
        print("Failed to initialize DB session:", str(e))
        _trace(f"CLI_ERROR init_db err={str(e)}")
        return 2

    session = SessionLocal()
    try:
        result = purge_old_clicks(session=session, retention_days=retention_days, dry_run=dry_run, batch_size=batch_size, limit=limit)
        # Human-friendly output
        print("Purge job summary:")
        print("  Retention days:", retention_days)
        print("  Cutoff (UTC):", result.get("cutoff"))
        print("  Candidate ClickEvent rows found:", result.get("found_total"))
        print("  Planned deletions:", result.get("planned_delete"))
        print("  Sample ids (up to 10):", result.get("sample_ids"))
        print("  Dry run:", dry_run)
        print("  Deleted rows (applied):", result.get("deleted"))
        _trace(f"CLI_COMPLETED result={result}")
        return 0
    except Exception as e:
        print("Error during purge:", str(e))
        _trace(f"CLI_FAILED err={str(e)}")
        return 3
    finally:
        try:
            session.close()
        except Exception:
            pass


if __name__ == "__main__":
    rc = _cli_main()
    sys.exit(rc)
--- END FILE: scripts/purge_old_clicks.py ---