"""
tools/importer.py - Bulk importer for legacy shortlinks (KAN-148 - US-050)

Surgical summary:
 - New standalone CLI tool to import legacy shortlinks from CSV or JSON into the existing ShortURL model.
 - File to add: tools/importer.py (single-file surgical addition).
 - This module uses the project's models.Session / models.ShortURL and obeys the "ID Filter" rule
   when creating rows (we always attach an explicit owner_id supplied by the import source or via CLI default).
 - Writes an architectural trace to trace_KAN-148.txt for every invocation and important decisions.
 - Provides a dry-run mode that prints a detailed plan (no DB changes).
 - Live mode supports three conflict resolution modes:
     * skip  : do nothing when slug already exists (report conflict).
     * suffix: attempt deterministic suffixing (slug-1, slug-2, ...) until a free slug found.
     * remap : if existing slug points to the same normalized target -> treat as remapped (no DB change).
               if different target -> fall back to suffix strategy to produce a new slug and record mapping.
 - Uses click for CLI; when click is unavailable, falls back to a compatible argparse-based runner.
 - Validates and normalizes URLs using utils.validation.validate_and_normalize_url when available,
   else falls back to the shortener module's validator or an internal conservative validator.
 - Operates in transactional batches (configurable --batch-size), committing every batch in live mode.
 - Attempts to be idempotent: repeated runs will not create duplicate rows for identical slug+owner+target.
 - Produces a human-friendly summary and a JSON-formatted summary file when requested.
 - Defensive imports & fallbacks and non-raising trace writes are used per project guardrails.
"""

from __future__ import annotations

import os
import sys
import json
import time
import csv
import typing as t
from datetime import datetime
from pathlib import Path

# Dependency-tolerant CLI import (preference: click)
try:
    import click  # type: ignore
    _HAS_CLICK = True
except Exception:
    click = None  # type: ignore
    _HAS_CLICK = False

# SQLAlchemy / models
import models
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

# Defensive imports of helpers
try:
    from utils.validation import validate_and_normalize_url
except Exception:
    validate_and_normalize_url = None

try:
    from utils.shortener import validate_custom_slug
except Exception:
    validate_custom_slug = None

# Trace file for Architectural Memory
TRACE_FILE = "trace_KAN-148.txt"


def _trace(msg: str):
    """Best-effort trace writer (non-blocking)."""
    try:
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # Never raise from tracing
        pass


# ---------- Input parsing helpers ----------
def _iter_input_rows(path: str):
    """
    Yield normalized dicts for each input row.

    Expected keys accepted (case-insensitive):
      - slug
      - target_url (or url)
      - owner_id
      - is_custom
      - created_at
      - expire_at

    Supports:
      - CSV files (first row header)
      - JSON:
         * JSON array of objects
         * newline-delimited JSON (jsonlines)
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    lower = p.suffix.lower()
    # Prefer CSV if .csv extension
    if lower == ".csv":
        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for r in reader:
                yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
        return

    # Try to parse as JSON array
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read()
            text = text.strip()
            if not text:
                return
            # If it looks like a JSON array
            if text.startswith("["):
                arr = json.loads(text)
                for obj in arr:
                    if isinstance(obj, dict):
                        yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in obj.items()}
                return
            # If it's JSON-lines where each line is an object
            if "\n" in text:
                for ln in text.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                        if isinstance(obj, dict):
                            yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in obj.items()}
                    except Exception:
                        # skip malformed lines but trace
                        _trace(f"INPUT_PARSE_WARN jsonline_malformed line_snip={ln[:120]}")
                return
            # Otherwise try parsing as a single object (dict)
            obj = json.loads(text)
            if isinstance(obj, dict):
                yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in obj.items()}
                return
    except Exception as e:
        # Fall back to conservative CSV parse attempt (best-effort)
        _trace(f"INPUT_PARSE_ERROR path={path} err={str(e)} - attempting CSV fallback")
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    yield {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            return
        except Exception as e2:
            _trace(f"INPUT_PARSE_FALLBACK_FAILED path={path} err={str(e2)}")
            raise


# ---------- Normalization & Validation ----------
def _normalize_slug(raw_slug: t.Optional[str]) -> t.Optional[str]:
    if raw_slug is None:
        return None
    s = str(raw_slug).strip()
    if not s:
        return None
    # maximum length to match DB column
    if len(s) > 255:
        s = s[:255]
    # If we have a validator helper use it to check formatting; else perform conservative check
    if validate_custom_slug:
        try:
            if not validate_custom_slug(s):
                return None
        except Exception:
            # If validator errors, fall back to basic check
            pass
    else:
        # conservative charset check: allow alnum, -, _
        import re
        if not re.match(r"^[A-Za-z0-9_-]{1,255}$", s):
            return None
    return s


def _normalize_and_validate_target(raw_target: str):
    """
    Return normalized target URL using utils.validation.validate_and_normalize_url when available.
    On failure raise ValueError.
    """
    if not raw_target or not isinstance(raw_target, str):
        raise ValueError("Missing target_url")
    s = raw_target.strip()
    if not s:
        raise ValueError("Empty target_url")
    if validate_and_normalize_url:
        try:
            return validate_and_normalize_url(s)
        except Exception as e:
            raise ValueError(f"URL validation failed: {str(e)}") from e
    # Fallback: conservative check - require http/https prefix and a netloc
    try:
        from urllib.parse import urlparse, urlunparse, quote, unquote
        p = urlparse(s)
        if p.scheme.lower() not in ("http", "https"):
            raise ValueError("URL must start with http:// or https://")
        if not p.netloc:
            raise ValueError("URL missing host")
        # Minimal normalization: idna host + quoted path/query
        host = p.netloc
        try:
            # attempt IDNA as best-effort
            host_parts = host.split("@")[-1]
            if ":" in host_parts and host_parts.count(":") == 1 and "[" not in host_parts:
                # host:port
                host_only, port = host_parts.rsplit(":", 1)
                host_idna = host_only.encode("idna").decode("ascii")
                host = host.replace(host_only, host_idna)
            else:
                host_idna = host_parts.encode("idna").decode("ascii")
                host = host.replace(host_parts, host_idna)
        except Exception:
            pass
        path = quote(unquote(p.path or ""), safe="/%:@[]!$&'()*+,;=")
        query = quote(unquote(p.query or ""), safe="=&?/")
        frag = quote(unquote(p.fragment or ""), safe="")
        return urlunparse((p.scheme, host, path, p.params or "", query, frag))
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"URL normalization fallback failed: {str(e)}") from e


# ---------- Core import logic ----------
class ImportSummary:
    def __init__(self):
        self.total_rows = 0
        self.inserted = 0
        self.skipped = 0
        self.remapped = 0
        self.duplicated_in_input = 0
        self.invalid_rows = 0
        self.errors = []
        # mapping: legacy_slug -> action/result
        self.details: t.List[t.Dict[str, t.Any]] = []

    def to_dict(self):
        return {
            "total_rows": self.total_rows,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "remapped": self.remapped,
            "duplicated_in_input": self.duplicated_in_input,
            "invalid_rows": self.invalid_rows,
            "errors": list(self.errors),
            "details": list(self.details),
        }


def _attempt_insert(session, owner_id: int, slug: str, target_url: str, is_custom: bool, expire_at=None):
    """
    Try to insert ShortURL row using session.add + session.flush to allow batched commits.
    Returns the created ShortURL instance on success.
    Raises IntegrityError if slug uniqueness violated (caller will handle).
    """
    obj = models.ShortURL(
        user_id=int(owner_id),
        target_url=target_url,
        slug=slug,
        is_custom=bool(is_custom),
        expire_at=expire_at,
    )
    session.add(obj)
    # flush so DB checks uniqueness now (but don't commit)
    session.flush()
    # refresh to populate id / timestamps
    try:
        session.refresh(obj)
    except Exception:
        pass
    return obj


def _find_existing_by_slug(session, slug: str):
    try:
        return session.query(models.ShortURL).filter_by(slug=slug).first()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        return None


def _generate_suffix_slug(base_slug: str, session, max_attempts: int = 1000):
    """
    Deterministic incremental suffixing to find an available slug.
    It will try: base-1, base-2, ... ensuring length <= 255
    """
    base = base_slug
    for i in range(1, max_attempts + 1):
        candidate = f"{base}-{i}"
        if len(candidate) > 255:
            # trim base to make room for suffix
            trim_len = 255 - (len(f"-{i}"))
            candidate = f"{base[:trim_len]}-{i}"
        exists = _find_existing_by_slug(session, candidate)
        if not exists:
            return candidate
    return None


def run_import(
    input_path: str,
    dry_run: bool = True,
    default_owner_id: t.Optional[int] = None,
    conflict_mode: str = "skip",
    batch_size: int = 100,
    out_summary_json: t.Optional[str] = None,
):
    """
    Main import driver.

    conflict_mode: 'skip' | 'remap' | 'suffix'
    """
    start_ts = time.time()
    _trace(f"IMPORT_STARTED path={input_path} dry_run={dry_run} default_owner_id={default_owner_id} conflict_mode={conflict_mode} batch_size={batch_size}")

    # Validate conflict_mode
    if conflict_mode not in ("skip", "remap", "suffix"):
        raise ValueError("conflict-mode must be one of: skip, remap, suffix")

    # Prepare dedupe set for input slugs (to report duplicates inside the supplied file)
    seen_input_slugs = set()

    summary = ImportSummary()

    session = None
    try:
        # initialize DB session runtime references
        # models.init_db is already called by app.create_app in normal workflows; but ensure session exists
        try:
            session = models.Session()
        except Exception:
            # Try to re-init (rare)
            raise RuntimeError("Database session initialization failed (models.Session is unavailable)")

        # For dry-run, we will attempt to perform operations but rollback at the end
        # For live, we will commit periodically per batch_size
        row_iter = _iter_input_rows(input_path)

        batch_count = 0
        pending_actions = []

        for raw in row_iter:
            summary.total_rows += 1

            # Normalize keys by lowering them for predictable access
            # Accept different header names: 'url' or 'target_url', 'slug', 'owner_id'
            row = {k.lower(): v for k, v in (raw or {}).items()}

            raw_slug = row.get("slug") or row.get("short") or row.get("id")
            raw_target = row.get("target_url") or row.get("url") or row.get("destination")
            raw_owner = row.get("owner_id") or row.get("user_id")
            raw_is_custom = row.get("is_custom") or row.get("custom")

            # Deduplicate in input
            if raw_slug and raw_slug in seen_input_slugs:
                summary.duplicated_in_input += 1
                summary.details.append({"input": raw, "action": "duplicate_in_input", "reason": "slug repeated in input"})
                continue
            if raw_slug:
                seen_input_slugs.add(raw_slug)

            # Determine owner
            owner_id = None
            if raw_owner:
                try:
                    owner_id = int(raw_owner)
                except Exception:
                    owner_id = None
            if owner_id is None:
                if default_owner_id is not None:
                    owner_id = int(default_owner_id)
                else:
                    # No owner -> invalid row per acceptance criteria (we require an owner)
                    summary.invalid_rows += 1
                    summary.details.append({"input": raw, "action": "invalid", "reason": "missing_owner_id"})
                    continue

            # Normalize slug
            slug = _normalize_slug(raw_slug) if raw_slug is not None else None
            if slug is None:
                # Missing slug is allowed? Legacy systems may omit slug (auto-generate). For importer we require slug to preserve legacy mapping.
                summary.invalid_rows += 1
                summary.details.append({"input": raw, "action": "invalid", "reason": "invalid_or_missing_slug"})
                continue

            # Normalize target URL
            try:
                target_norm = _normalize_and_validate_target(raw_target)
            except Exception as e:
                summary.invalid_rows += 1
                summary.details.append({"input": raw, "slug": slug, "action": "invalid", "reason": f"target_url_invalid: {str(e)}"})
                continue

            is_custom = bool(raw_is_custom) if raw_is_custom is not None else True

            # At this point we have: owner_id, slug, target_norm
            # Check idempotency: Does a ShortURL already exist with same slug AND same owner_id AND same target?
            try:
                existing = session.query(models.ShortURL).filter_by(slug=slug).first()
            except Exception as e:
                # Try to recover
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"DB_READ_ERROR slug={slug} err={str(e)}")
                summary.errors.append(str(e))
                summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": f"db_read_error: {str(e)}"})
                continue

            if existing:
                # Candidate conflict handling
                # If existing has same owner & same target -> already migrated (idempotent)
                try:
                    same_owner = (int(existing.user_id) == int(owner_id))
                except Exception:
                    same_owner = False
                same_target = False
                try:
                    # compare normalized targets conservatively (exact match)
                    same_target = (getattr(existing, "target_url", None) or "").strip() == (target_norm or "").strip()
                except Exception:
                    same_target = False

                if same_owner and same_target:
                    summary.details.append({"input": raw, "slug": slug, "action": "noop", "reason": "already_exists_same_owner_target", "existing_id": existing.id})
                    # Nothing to apply
                    continue

                # Conflict present (slug exists but differs in owner and/or target)
                if conflict_mode == "skip":
                    summary.skipped += 1
                    summary.details.append({"input": raw, "slug": slug, "action": "skipped", "reason": "slug_conflict_exists", "existing_owner": getattr(existing, "user_id", None), "existing_target": getattr(existing, "target_url", None)})
                    continue

                if conflict_mode == "remap":
                    # If existing points to same target (even different owner), treat as remapped (no DB change).
                    if same_target:
                        summary.remapped += 1
                        summary.details.append({"input": raw, "slug": slug, "action": "remapped_to_existing", "existing_slug": slug, "existing_id": existing.id})
                        continue
                    # Otherwise, fall back to suffix strategy to create a new unique slug and record mapping
                    candidate_slug = _generate_suffix_slug(slug, session)
                    if not candidate_slug:
                        summary.errors.append(f"Unable to find suffix for slug {slug}")
                        summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "suffix_exhausted"})
                        continue
                    # Try to insert with candidate_slug
                    try:
                        if dry_run:
                            # Report planned remap -> created slug mapping
                            summary.details.append({"input": raw, "slug": slug, "action": "planned_remap_insert", "new_slug": candidate_slug})
                            summary.inserted += 1  # count as insertion planned
                            batch_count += 1
                        else:
                            try:
                                obj = _attempt_insert(session, owner_id, candidate_slug, target_norm, is_custom)
                                summary.inserted += 1
                                summary.details.append({"input": raw, "slug": slug, "action": "remap_inserted", "new_slug": candidate_slug, "new_id": obj.id})
                                batch_count += 1
                            except IntegrityError:
                                # Very unlikely (we checked candidate), but be safe and report
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                                summary.errors.append(f"IntegrityError while inserting remap candidate {candidate_slug}")
                                summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "integrity_on_remap_candidate"})
                                continue
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        summary.errors.append(str(e))
                        summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": str(e)})
                        continue

                elif conflict_mode == "suffix":
                    # Try suffixing repeatedly to obtain unique slug then insert
                    candidate_slug = _generate_suffix_slug(slug, session)
                    if not candidate_slug:
                        summary.errors.append(f"Suffix exhaustion for slug {slug}")
                        summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "suffix_exhausted"})
                        continue
                    try:
                        if dry_run:
                            summary.details.append({"input": raw, "slug": slug, "action": "planned_suffix_insert", "new_slug": candidate_slug})
                            summary.inserted += 1
                            batch_count += 1
                        else:
                            try:
                                obj = _attempt_insert(session, owner_id, candidate_slug, target_norm, is_custom)
                                summary.inserted += 1
                                summary.details.append({"input": raw, "slug": slug, "action": "suffix_inserted", "new_slug": candidate_slug, "new_id": obj.id})
                                batch_count += 1
                            except IntegrityError:
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                                summary.errors.append(f"IntegrityError while inserting suffix candidate {candidate_slug}")
                                summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "integrity_on_suffix_candidate"})
                                continue
                    except Exception as e:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        summary.errors.append(str(e))
                        summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": str(e)})
                        continue

                else:
                    # Unknown mode (shouldn't happen)
                    summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": f"unknown_conflict_mode_{conflict_mode}"})
                    continue
            else:
                # No existing: attempt to insert slug as-is
                try:
                    if dry_run:
                        summary.details.append({"input": raw, "slug": slug, "action": "planned_insert"})
                        summary.inserted += 1
                        batch_count += 1
                    else:
                        try:
                            obj = _attempt_insert(session, owner_id, slug, target_norm, is_custom)
                            summary.inserted += 1
                            summary.details.append({"input": raw, "slug": slug, "action": "inserted", "new_id": obj.id})
                            batch_count += 1
                        except IntegrityError:
                            # Race: slug inserted by another process between our check and flush
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            # Resolve per conflict_mode: re-enter loop logic by setting existing and letting conflict-path run
                            existing_now = _find_existing_by_slug(session, slug)
                            if existing_now:
                                # emulate conflict handling by re-processing the conflict branch next iteration
                                # For simplicity, handle similar to suffix
                                if conflict_mode == "skip":
                                    summary.skipped += 1
                                    summary.details.append({"input": raw, "slug": slug, "action": "skipped_race", "reason": "race_inserted_by_other"})
                                    continue
                                candidate_slug = None
                                if conflict_mode in ("suffix", "remap"):
                                    candidate_slug = _generate_suffix_slug(slug, session)
                                if not candidate_slug:
                                    summary.errors.append(f"Race and suffix exhaustion for slug {slug}")
                                    summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "race_suffix_exhaustion"})
                                    continue
                                try:
                                    obj = _attempt_insert(session, owner_id, candidate_slug, target_norm, is_custom)
                                    summary.inserted += 1
                                    summary.details.append({"input": raw, "slug": slug, "action": "suffix_inserted_after_race", "new_slug": candidate_slug, "new_id": obj.id})
                                    batch_count += 1
                                except IntegrityError:
                                    try:
                                        session.rollback()
                                    except Exception:
                                        pass
                                    summary.errors.append(f"IntegrityError after race while attempting candidate {candidate_slug}")
                                    summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": "integrity_after_race"})
                                    continue
                except Exception as e:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    summary.errors.append(str(e))
                    summary.details.append({"input": raw, "slug": slug, "action": "error", "reason": str(e)})
                    continue

            # Commit per batch if in live mode
            if not dry_run and batch_size and batch_count >= int(batch_size):
                try:
                    session.commit()
                    _trace(f"BATCH_COMMIT count={batch_count}")
                except Exception as e:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    summary.errors.append(f"Batch commit failed: {str(e)}")
                    summary.details.append({"batch_commit_error": str(e)})
                batch_count = 0

        # End for rows

        # Final commit of remaining in live mode
        if not dry_run:
            try:
                session.commit()
                if batch_count:
                    _trace(f"FINAL_COMMIT count={batch_count}")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                summary.errors.append(f"Final commit failed: {str(e)}")
                summary.details.append({"final_commit_error": str(e)})
        else:
            # Dry run: rollback any transactional changes (if any)
            try:
                session.rollback()
            except Exception:
                pass

        # Close session
        try:
            session.close()
        except Exception:
            pass

    except Exception as e:
        # Top-level failure: try to rollback & close session
        try:
            if session is not None:
                session.rollback()
                session.close()
        except Exception:
            pass
        summary.errors.append(str(e))
        summary.details.append({"action": "fatal_error", "reason": str(e)})

    end_ts = time.time()
    duration = end_ts - start_ts
    _trace(f"IMPORT_COMPLETED path={input_path} total={summary.total_rows} inserted={summary.inserted} skipped={summary.skipped} remapped={summary.remapped} invalid={summary.invalid_rows} duration={duration:.2f}s errors={len(summary.errors)}")

    # Print human summary
    print("\nIMPORT SUMMARY")
    print(f"  Input file: {input_path}")
    print(f"  Dry run: {dry_run}")
    print(f"  Conflict mode: {conflict_mode}")
    print(f"  Total rows processed: {summary.total_rows}")
    print(f"  Insertions planned/applied: {summary.inserted}")
    print(f"  Skipped (conflicts): {summary.skipped}")
    print(f"  Remapped: {summary.remapped}")
    print(f"  Invalid rows: {summary.invalid_rows}")
    print(f"  Duplicates in input: {summary.duplicated_in_input}")
    print(f"  Errors encountered: {len(summary.errors)}")
    if len(summary.errors) > 0:
        print("  Errors (sample):")
        for e in summary.errors[:10]:
            print("   -", e)

    # Optionally write summary JSON
    if out_summary_json:
        try:
            with open(out_summary_json, "w", encoding="utf-8") as of:
                json.dump(summary.to_dict(), of, indent=2, default=str)
            print(f"Wrote summary JSON to {out_summary_json}")
        except Exception as e:
            print("Failed to write summary JSON:", str(e))
            _trace(f"SUMMARY_WRITE_ERROR path={out_summary_json} err={str(e)}")

    return summary


# ---------- CLI Entrypoint ----------
def _run_click_cli():
    @click.command(help="Bulk import legacy shortlinks into ShortURL model (KAN-148)")
    @click.option("--input-file", "-i", required=True, help="Path to input CSV or JSON (json-array or jsonlines).")
    @click.option("--dry-run/--live", default=True, help="Run in dry-run mode (default). Use --live to apply changes.")
    @click.option("--default-owner-id", "-o", type=int, default=None, help="Default owner_id to assign when input rows lack an owner_id.")
    @click.option("--conflict-mode", "-c", type=click.Choice(["skip", "remap", "suffix"]), default="skip", help="Conflict resolution when slug exists.")
    @click.option("--batch-size", "-b", type=int, default=100, help="Commit to DB every N inserts (live mode).")
    @click.option("--summary-json", "-s", type=str, default=None, help="Optional path to write a JSON summary report.")
    def cli(input_file, dry_run, default_owner_id, conflict_mode, batch_size, summary_json):
        # Validate file exists early
        if not os.path.exists(input_file):
            click.echo(f"Input file not found: {input_file}", err=True)
            sys.exit(2)
        try:
            summary = run_import(
                input_path=input_file,
                dry_run=bool(dry_run),
                default_owner_id=default_owner_id,
                conflict_mode=conflict_mode,
                batch_size=batch_size,
                out_summary_json=summary_json,
            )
            # exit code: 0 if no errors, else 3
            if summary.errors:
                click.echo(f"Completed with {len(summary.errors)} errors. See {TRACE_FILE} for details.", err=True)
                sys.exit(3)
            click.echo("Import completed successfully.")
            sys.exit(0)
        except Exception as e:
            click.echo(f"Import failed: {str(e)}", err=True)
            _trace(f"CLI_FATAL_ERROR err={str(e)}")
            sys.exit(4)

    cli()


def _run_argparse_cli():
    # Basic argparse fallback if click is missing (still supports required flags)
    import argparse
    parser = argparse.ArgumentParser(description="Bulk import legacy shortlinks into ShortURL model (KAN-148)")
    parser.add_argument("--input-file", "-i", required=True)
    parser.add_argument("--dry-run", action="store_true", default=False, help="Dry-run mode (default False for argparse fallback). Use --live by omitting --dry-run in this mode.")
    parser.add_argument("--live", action="store_true", help="Apply changes (live). When both present live takes precedence.")
    parser.add_argument("--default-owner-id", "-o", type=int, default=None)
    parser.add_argument("--conflict-mode", "-c", choices=["skip", "remap", "suffix"], default="skip")
    parser.add_argument("--batch-size", "-b", type=int, default=100)
    parser.add_argument("--summary-json", "-s", type=str, default=None)

    args = parser.parse_args()
    # For compatibility with the click interface: treat --live presence as dry_run=False
    dry_run_flag = not args.live if args.live else args.dry_run
    try:
        summary = run_import(
            input_path=args.input_file,
            dry_run=dry_run_flag,
            default_owner_id=args.default_owner_id,
            conflict_mode=args.conflict_mode,
            batch_size=args.batch_size,
            out_summary_json=args.summary_json,
        )
        if summary.errors:
            print(f"Completed with {len(summary.errors)} errors. See {TRACE_FILE} for details.", file=sys.stderr)
            sys.exit(3)
        print("Import completed successfully.")
        sys.exit(0)
    except Exception as e:
        print("Import failed:", str(e), file=sys.stderr)
        _trace(f"ARGPARSE_FATAL_ERROR err={str(e)}")
        sys.exit(4)


if __name__ == "__main__":
    # Write invocation trace
    try:
        _trace(f"CLI_INVOKED argv={' '.join(sys.argv)} cwd={os.getcwd()}")
    except Exception:
        pass

    if _HAS_CLICK and click is not None:
        _run_click_cli()
    else:
        _trace("CLICK_UNAVAILABLE using argparse fallback")
        _run_argparse_cli()

# End of tools/importer.py
#
# Deployment notes / surgical checklist:
#  - Add exactly one file: tools/importer.py (this file). Do NOT modify other modules.
#  - The importer uses models.Session() and models.ShortURL exclusively; it will run against SQLite for dev and Postgres in production.
#  - Ensure the environment variable DATABASE_URL is set consistently with app.create_app when running in live mode.
#  - Tests:
#      * Unit tests should target the internal functions (e.g., _generate_suffix_slug, _normalize_and_validate_target).
#      * Integration tests should create an in-memory sqlite app (tests/conftest.py provides fixtures), prepare a sample CSV/JSON,
#        run the importer in dry-run and live modes, and assert DB state matches expected (idempotency & conflict-resolution).
#  - Trace logging:
#      * All invocations write to trace_KAN-148.txt. CI and other agents must include these trace files in Architectural Memory logs.
#  - Stability guarantee:
#      * Before enabling live imports in production pipelines, run the tool in dry-run and run the app._stability_check endpoint for 10s as required by the project governance.
#
# Example quick runs:
#   # dry-run, report only
#   python tools/importer.py --input-file legacy_links.csv --dry-run --default-owner-id 42 --conflict-mode suffix
#
#   # apply changes (live), commit in batches of 200
#   python tools/importer.py --input-file legacy_links.json --live --default-owner-id 42 --conflict-mode remap --batch-size 200
#
# End of file.