#!/usr/bin/env python3
"""
bin/enrich_geoip.py - Async GeoIP enrichment worker for ClickEvent.country (KAN-147)

Usage:
  # One-shot pass (default): process up to GEOIP_BATCH_LIMIT rows (or --limit)
  python bin/enrich_geoip.py --once

  # Daemon mode: poll every N seconds
  python bin/enrich_geoip.py --daemon --interval 60

  Options:
    --database-url <URL>      (optional) override DATABASE_URL env
    --db-batch-size <N>       rows processed per DB transaction (default env/GEOIP_BATCH_SIZE or 200)
    --limit <N>               maximum total rows to process this invocation (default env/GEOIP_BATCH_LIMIT or 1000; 0 => no limit)
    --once                    run once and exit (default)
    --daemon                  run in daemon mode, polling every --interval seconds
    --interval <seconds>      daemon sleep interval (default 60)
    --trace-file <path>       override trace file (default trace_KAN-147.txt)
    --db-path <path>          path to MaxMind DB (overrides GEOIP_DB_PATH env)
    --help

Behavior:
  - Respects GEOIP_ENABLED (env or app config). If disabled, exits with message.
  - Uses maxminddb if available; if not, logs and exits (enrichment optional).
  - Scans ClickEvent rows with country IS NULL and anonymized_ip IS NOT NULL and attempts to resolve a country ISO code.
  - Writes to trace_KAN-147.txt (architectural memory) when actions occur.
  - Operates in best-effort manner: continues on row-level errors.
"""

from __future__ import annotations
import os
import sys
import time
import argparse
from datetime import datetime
from typing import Optional, List

# SQLAlchemy imports (defensive)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session

# Try importing maxminddb (dependency tolerant)
try:
    import maxminddb
    _has_maxminddb = True
except Exception:
    maxminddb = None
    _has_maxminddb = False

# models module
import models

DEFAULT_TRACE = "trace_KAN-147.txt"


def _trace(msg: str, trace_file: Optional[str] = None) -> None:
    try:
        tf = trace_file or DEFAULT_TRACE
        with open(tf, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///local_dev.db")


def init_db_engine(db_url: Optional[str]) -> (object, object):
    db_url = db_url or get_database_url()
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, connect_args=connect_args)
    SessionLocal = scoped_session(sessionmaker(bind=engine))
    # ensure models.Session/Engine set
    try:
        models.init_db(engine, SessionLocal)
    except Exception:
        pass
    return engine, SessionLocal


def _open_reader(db_path: Optional[str]):
    """
    Open MaxMind DB reader if available and path provided. If not available, return None.
    """
    if not _has_maxminddb:
        return None
    path = db_path or os.environ.get("GEOIP_DB_PATH", "") or ""
    if not path:
        return None
    try:
        reader = maxminddb.open_database(path)
        return reader
    except Exception as e:
        _trace(f"GEOIP_READER_OPEN_ERROR path={path} err={str(e)}")
        return None


def _lookup_country(reader, ip_str: str) -> Optional[str]:
    """
    Lookup country ISO code from the reader for a given IP string.
    Returns uppercase ISO code (e.g., 'US') or None if not found.
    """
    if not reader or not ip_str:
        return None
    try:
        rec = reader.get(ip_str)
        if not rec:
            return None
        # Various DB schemas: prefer country.iso_code, fallback to registered_country, country code keys
        country = None
        try:
            country = rec.get("country", {}).get("iso_code")
        except Exception:
            country = None
        if not country:
            try:
                country = rec.get("registered_country", {}).get("iso_code")
            except Exception:
                country = None
        if country:
            return str(country).upper()
    except Exception:
        # Lookup failure for this IP -> ignore
        return None
    return None


def _process_batch(session, reader, batch_limit: int, allow_enrich_from_anon: bool, trace_file: Optional[str] = None):
    """
    Process up to batch_limit ClickEvent rows where country IS NULL and anonymized_ip IS NOT NULL.
    Returns number of rows updated (int).
    """
    if batch_limit <= 0:
        return 0

    updated = 0
    try:
        # Query candidate rows in order of oldest occurrences (best-effort)
        q = session.query(models.ClickEvent).filter(models.ClickEvent.country == None, models.ClickEvent.anonymized_ip != None).order_by(models.ClickEvent.occurred_at.asc()).limit(batch_limit)
        rows = q.all()
    except Exception as e:
        _trace(f"GEOIP_DB_QUERY_ERROR err={str(e)}", trace_file)
        return 0

    if not rows:
        return 0

    for r in rows:
        try:
            ip = (r.anonymized_ip or "").strip()
            if not ip:
                continue

            if not allow_enrich_from_anon:
                _trace(f"GEOIP_SKIP_PRIVACY row_id={getattr(r,'id',None)} ip_present=1", trace_file)
                continue

            country = None
            # try simple lookup using reader (if available)
            if reader:
                country = _lookup_country(reader, ip)
            # If no reader or lookup failed, try best-effort heuristics (very conservative: use leading octets)
            if not country:
                # do not attempt network calls; instead attempt simple heuristics for IPv4 (map common blocks)
                # heuristic: if ip is IPv4 masked like '203.0.113.0' -> use first two octets to guess country if mapping configured.
                # By default, do not guess; maintain None. This preserves privacy.
                country = None

            if country:
                r.country = country
                # write a small last-updated trace field (not persisted to DB other than country)
                session.add(r)
                try:
                    session.commit()
                    updated += 1
                    _trace(f"GEOIP_UPDATED row_id={r.id} ip={ip} country={country}", trace_file)
                except Exception as e:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    _trace(f"GEOIP_COMMIT_ERROR row_id={getattr(r,'id', None)} err={str(e)}", trace_file)
            else:
                # no country resolved; mark as attempted to avoid repeated futile lookups?
                # Do nothing here to allow future DB/reader improvements; optionally operator can set a cutoff.
                _trace(f"GEOIP_NO_RESULT row_id={getattr(r,'id', None)} ip={ip}", trace_file)
                # Optional future improvement: set a flag or last_attempt timestamp
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            _trace(f"GEOIP_ROW_ERROR row_id={getattr(r,'id',None)} err={str(e)}", trace_file)
            continue

    return updated


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enrich ClickEvent.country using MaxMind DB (KAN-147)")
    parser.add_argument("--database-url", type=str, default=None)
    parser.add_argument("--db-path", type=str, default=None, help="Path to MaxMind DB (overrides GEOIP_DB_PATH env)")
    parser.add_argument("--limit", type=int, default=0, help="Total rows to process this invocation (0 => use config or 1000)")
    parser.add_argument("--db-batch-size", type=int, default=0, help="Rows processed per DB batch transaction (0 => use config or 200)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (default)")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode, polling every --interval seconds")
    parser.add_argument("--interval", type=int, default=0, help="Polling interval in seconds for daemon mode (default: from ENV/GEOIP_DAEMON_INTERVAL or 60)")
    parser.add_argument("--trace-file", type=str, default=DEFAULT_TRACE)
    args = parser.parse_args(argv)

    trace_file = args.trace_file or DEFAULT_TRACE

    # Check operator-config: GEOIP_ENABLED must be truthy
    try:
        geo_enabled_env = os.environ.get("GEOIP_ENABLED", "")
        geo_enabled_cfg = str(geo_enabled_env).lower() in ("1", "true", "yes")
    except Exception:
        geo_enabled_cfg = False

    if not geo_enabled_cfg:
        _trace("GEOIP_DISABLED by env; exiting", trace_file)
        print("GEOIP enrichment disabled (GEOIP_ENABLED is false or unset). Exiting.")
        return 0

    engine, SessionLocal = init_db_engine(args.database_url)
    reader = None
    if _has_maxminddb:
        dbpath = args.db_path or os.environ.get("GEOIP_DB_PATH", "")
        if not dbpath:
            _trace("GEOIP_NO_DB_PATH configured; enrichment disabled", trace_file)
            print("No GEOIP_DB_PATH configured; enrichment disabled.")
            return 1
        try:
            reader = _open_reader(dbpath)
            if reader is None:
                _trace(f"GEOIP_READER_UNAVAILABLE path={dbpath}", trace_file)
                print(f"Could not open MaxMind DB at {dbpath}. Exiting.")
                return 1
        except Exception as e:
            _trace(f"GEOIP_READER_OPEN_FAILED err={str(e)}", trace_file)
            print("Failed to open GeoIP DB:", e)
            return 1
    else:
        _trace("GEOIP_NO_MAXMINDDB_INSTALLED", trace_file)
        print("maxminddb not installed; cannot enrich. Exiting.")
        return 1

    # runtime params
    total_limit = args.limit if args.limit and args.limit > 0 else int(os.environ.get("GEOIP_BATCH_LIMIT", 1000))
    batch_size = args.db_batch_size if args.db_batch_size and args.db_batch_size > 0 else int(os.environ.get("GEOIP_BATCH_SIZE", 200))
    interval = args.interval if args.interval and args.interval > 0 else int(os.environ.get("GEOIP_DAEMON_INTERVAL", 60))
    allow_enrich_from_anon = os.environ.get("GEOIP_ALLOW_ENRICH_FROM_ANON", "true").lower() in ("1", "true", "yes")

    _trace(f"GEOIP_WORKER_START db={get_database_url()} db_path={args.db_path or os.environ.get('GEOIP_DB_PATH','<unset>')} batch_size={batch_size} total_limit={total_limit} daemon={args.daemon} interval={interval} anon_ok={allow_enrich_from_anon}", trace_file)

    processed_total = 0
    try:
        while True:
            # Respect global total limit if set
            remaining = None
            if total_limit and total_limit > 0:
                remaining = total_limit - processed_total
                if remaining <= 0:
                    _trace(f"GEOIP_TOTAL_LIMIT_REACHED processed_total={processed_total}", trace_file)
                    break
            cur_batch = batch_size if (remaining is None or remaining >= batch_size) else remaining

            session = SessionLocal()
            try:
                updated = _process_batch(session=session, reader=reader, batch_limit=cur_batch, allow_enrich_from_anon=allow_enrich_from_anon, trace_file=trace_file)
                processed_total += int(updated)
                _trace(f"GEOIP_BATCH_COMPLETED updated={updated} processed_total={processed_total}", trace_file)
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            # If run-once, break after one batch
            if args.once or not args.daemon:
                break

            # If daemon: sleep then continue
            time.sleep(interval)
    except KeyboardInterrupt:
        _trace("GEOIP_WORKER_INTERRUPTED_BY_USER", trace_file)
    finally:
        try:
            if reader:
                reader.close()
        except Exception:
            pass
        _trace(f"GEOIP_WORKER_STOP processed_total={processed_total}", trace_file)
        print("Enrichment finished. total updated:", processed_total)
    return 0


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
--- END FILE: bin/enrich_geoip.py ---