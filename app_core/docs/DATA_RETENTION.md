DATA RETENTION & PURGE POLICY (KAN-142)
======================================

Purpose
-------
This document explains the ClickEvent data retention policy and operator guidance for scheduling and running purge jobs.

Policy
------
- Configurable retention window: DATA_RETENTION_DAYS (default 90 days).
- ClickEvent rows with occurred_at strictly older than (UTC now - DATA_RETENTION_DAYS) are eligible for purge.
- Purge script: scripts/purge_old_clicks.py
  - Supports dry-run (--dry-run or omit --apply) to preview deletions without modifying the DB.
  - Supports batching (--batch-size N) and total limit (--limit M) to control deletion throughput.
  - Writes trace_KAN-142.txt entries for auditing.

Scheduling guidance
-------------------
- Production cron (example, daily at 03:00 UTC):
    0 3 * * * /usr/bin/python3 /srv/smartlink/scripts/purge_old_clicks.py --apply --batch-size 5000 >> /var/log/smartlink/purge.log 2>&1

- Safer incremental approach (recommended for large tables):
  - Run nightly with a moderate --limit (e.g., 50k) until backlog cleared:
      python3 scripts/purge_old_clicks.py --apply --batch-size 2000 --limit 50000
  - Monitor DB I/O & replication lag. Adjust batch_size and schedule to maintain acceptable load.

- Run --dry-run before first apply and periodically to audit:
    python3 scripts/purge_old_clicks.py --dry-run

Backups & safety
----------------
- Always ensure DB backups/snapshots exist prior to destructive maintenance.
- For PostgreSQL: take a base backup or snapshot and consider running VACUUM/ANALYZE after large deletes.
- The script uses DELETE; operators may prefer to move old rows to an archive table before deletion for extra safety.

Performance considerations
--------------------------
- Large-table deletes can bloat WAL (Postgres) and require VACUUM to reclaim disk.
- Use batch deletions to keep transactions small.
- After a major purge, schedule a VACUUM FULL or VACUUM + REINDEX if disk usage is an issue.

Audit & trace
-------------
- The script writes trace_KAN-142.txt with details of each invocation and key events for Architectural Memory.
- Ensure trace files are collected with other operational logs (e.g., into central logging or retained per policy).

Developer notes
---------------
- The purge implementation uses SQLAlchemy .delete(synchronize_session=False) for bulk delete where possible.
- The purge function is testable and imported by unit tests (tests/test_purge_old_clicks.py).
- Default retention can be overridden via:
    - app config: app.config["DATA_RETENTION_DAYS"]
    - environment: DATA_RETENTION_DAYS
    - CLI flag: --days

Contact
-------
- Privacy Officer / Data Protection contact: internal team (add appropriate contact as required).
--- END FILE: docs/DATA_RETENTION.md ---