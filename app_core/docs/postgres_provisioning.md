---
Postgres Production Provisioning Guide
Ticket: KAN-136 (US-038)
Status: Draft (apply to production after validation & staging tests)
Purpose: Provide production-ready Postgres provisioning guidance for the Smart Link service: connection pooling, pgbouncer guidance, crucial indices, VACUUM/ANALYZE schedule, materialized view maintenance, example psql tuning commands, migration notes, and testing guidance.

Table of Contents
1) Executive summary
2) Connection pool sizing & application server guidance
3) PgBouncer recommendation & sample configuration
4) Indices to create (DDL)
5) Materialized view & summary-table maintenance (US-024 / KAN-122)
6) VACUUM / ANALYZE recommendations & schedules
7) Postgres tuning sample commands (psql snippets)
8) Migrations & index creation notes (CONCURRENTLY)
9) Testing & validation (integration & synthetic load)
10) Cloud-specific considerations (RDS, Render, Heroku, managed Postgres)
11) Quick checklist for deployers
12) References & pointers to repository artifacts

1) Executive summary
- Use a connection pooling strategy rather than letting each web process exhaust DB connections.
- For high concurrency, front Postgres with PgBouncer in transaction pooling mode.
- Size SQLAlchemy/Gunicorn pools conservatively: avoid exceeding the database max_connections.
- Create indices required for query performance (clickevents(short_url_id, occurred_at), shorturls(slug), users(email)), prefer CREATE INDEX CONCURRENTLY in production.
- Maintain materialized views used by analytics with periodic REFRESH CONCURRENTLY and populate summary tables to accelerate totals.
- Use sensible autovacuum + targeted VACUUM/ANALYZE schedules for click-events heavy workloads.

2) Connection pool sizing & application server guidance
Goal: Avoid exhausting Postgres max_connections while providing enough pooled connections per application worker/thread.

Key formulas:
- If using Gunicorn worker model with no threads (sync workers):
    recommended_sqlalchemy_pool_size >= workers + 2
- If using Gunicorn gthread worker class (threads per worker):
    recommended_sqlalchemy_pool_size >= (workers * threads) + 2

Rationale:
- Each worker thread may concurrently need a DB connection; add +2 for admin/healthchecks and headroom.
- In the codebase we already document a QueuePool sizing formula in gunicorn.conf.py; align SQLALCHEMY_POOL_SIZE with that guidance.

Operator examples:
- Example A: CPU=2, workers = (2 * CPU) + 1 = 5; GUNICORN_WORKER_CLASS=gthread; threads=4
  - Worst-case concurrent DB connections = workers * threads = 5 * 4 = 20
  - Recommended SQLAlchemy pool_size >= 20 + 2 = 22
  - Recommendation: set environment variable SQLALCHEMY_POOL_SIZE=22 (or set pool_size in SQLAlchemy create_engine params)

- Simpler guidance per ticket: connection pool = gunicorn_workers * 2
  - If you choose that simplified approach (useful when using sync workers), set SQLALCHEMY_POOL_SIZE = workers * 2

Where to set:
- Set SQLALCHEMY_POOL_SIZE via environment variable or application config. Adjust gunicorn.conf.py (comments already present) and the app's create_engine usage via DATABASE_URL params or engine options.

Important:
- If Postgres max_connections is tight (e.g., small cloud instances or managed services), prefer PgBouncer to multiplex many app connections onto fewer DB server connections.

3) PgBouncer recommendation & sample configuration
Why PgBouncer:
- For high concurrency (many short-lived requests), PgBouncer dramatically reduces Postgres connection overhead and memory pressure.
- Use PgBouncer in pool_mode = transaction for the most throughput with Flask+SQLAlchemy typical patterns.

Suggested topology:
- App containers -> PgBouncer (in same VPC, ideally co-located in same AZ) -> Postgres
- For managed services (RDS), run PgBouncer in the same network (e.g., sidecar, separate instance, or ECS task).

pgbouncer.ini example (annotated):
(Adjust values to your environment and DB max_connections)

[databases]
# Connection string for your DB (PgBouncer will connect using this)
smartlink = host=<db-host> port=5432 dbname=<db-name> user=<db-user> password=<secret>

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction         # transaction pooling recommended
max_client_conn = 1000         # allow many clients to connect to PgBouncer
default_pool_size = 20         # per-database pooled server connections (adjust per DB capacity)
reserve_pool_size = 5
reserve_pool_timeout = 5
server_lifetime = 3600
server_idle_timeout = 600
ignore_startup_parameters = extra_float_digits
log_connections = 1
log_disconnections = 1
log_pooler_errors = 1
tcp_keepalive = 1

Sizing guidance:
- default_pool_size should be set so that sum(default_pool_size across applications) <= Postgres max_connections - reserved_for_admin
- If you run N application processes clusters, ensure the aggregated default_pool_size does not exceed DB capacity.
- Example: DB max_connections = 200. Reserve 20 for admins/replication. Remaining 180. If you will run 3 app clusters, set default_pool_size≈60 and max_client_conn accordingly.

Pool mode:
- transaction: best for connection multiplexing; requires that sessions do not rely on session-level state (e.g., temp tables, session variables).
- If your app relies on session-local state across multiple statements in a single request, use session pooling, but this reduces multiplexing.

PgBouncer advice for SQLAlchemy:
- Point SQLALCHEMY DATABASE_URL to pgbouncer (port 6432). In the app, continue to use SQLAlchemy pooling; PgBouncer manages backend connections.
- When PgBouncer is used, reduce application pool_size to default_pool_size to avoid excessive internal waiting. Example:
  - App: SQLALCHEMY_POOL_SIZE = 20 (matches default_pool_size)
  - PgBouncer default_pool_size = 20
  - Postgres max_connections must be >= number_of_pgbouncer_server_connections + reserved

4) Indices to create (DDL)
Acceptance requires indices:
- click_events(short_url_id, occurred_at)
- short_urls(slug)
- users(email)

Use CONCURRENTLY in production to avoid blocking writes/readers.

Recommended DDL (Postgres):

-- 1) Composite index for click event lookups by link+time (fast histogram/totals)
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_clickevents_short_url_id_occurred_at
  ON clickevents (short_url_id, occurred_at);

-- 2) ShortURLs slug unique index (slug already has unique constraint in models; ensure index exists)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_shorturls_slug
  ON shorturls (slug);

-- 3) Users email index (unique)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_users_email
  ON users (email);

Notes:
- Our models.py already creates indices/unique constraints (SQLAlchemy metadata + naming_convention). Use the DDL above only if migrations left gaps (i.e., older deployments).
- For large tables (clickevents) use CONCURRENTLY so index creation does not lock writes.
- If the table is too large for a single index build to succeed within maintenance windows, consider creating a partial index or partitioning (see below).

Optional advanced considerations:
- Partition clickevents by month/year if you anticipate huge write volumes (multi-millions/day). Partitioning reduces index maintenance cost and improves partition-local vacuuming. If partitioned, create local indexes per partition and a partitioned index if necessary.
- For analytics histograms that only query counts by day, consider creating a materialized view (already present in migrations) with a unique index (see KAN-122). This can speed reads significantly.

5) Materialized view & summary-table maintenance (KAN-122 / US-024)
- Migration included: migrations/versions/kan_122_create_materialized_views.py creates:
  - clicks_per_shorturl_day (materialized view)
  - unique index ux_clicks_per_shorturl_day_shorturl_day (required for REFRESH MATERIALIZED VIEW CONCURRENTLY)
  - analytics_summary_shorturl_daily table (short_url_id, day) with upserts.

Maintenance recommendations:
- Refresh materialized view concurrently at a schedule that meets your data freshness SLA.
  - Use REFRESH MATERIALIZED VIEW CONCURRENTLY clicks_per_shorturl_day;
  - Requires a unique index on (short_url_id, day) — migration creates ux_clicks_per_shorturl_day_shorturl_day.
- After MV refresh, upsert into analytics_summary_shorturl_daily from clicks_per_shorturl_day:
  - INSERT INTO analytics_summary_shorturl_daily (short_url_id, day, clicks)
    SELECT short_url_id, day, clicks FROM clicks_per_shorturl_day
    ON CONFLICT (short_url_id, day) DO UPDATE SET clicks = EXCLUDED.clicks;

- Prefer using bin/refresh_analytics.py:
  - It already attempts REFRESH MATERIALIZED VIEW CONCURRENTLY and handles a non-concurrent fallback.
  - Schedule bin/refresh_analytics.py to run at off-peak times (nightly) via cron or pg_cron.

Materialized view schedule examples:
- Near-real-time needs (low-scale): refresh every 5-15 minutes.
- Analytics snapshot for dashboards: nightly refresh + real-time partial updates for top links.
- For high-volume systems, recompute summary table incrementally rather than full MV refresh.

6) VACUUM / ANALYZE recommendations & schedule
- For click-events heavy insert tables, autovacuum must be tuned and periodic manual maintenance scheduled.

Autovacuum tuning (recommended starting values; adjust per load):
- autovacuum_max_workers = 5
- autovacuum_naptime = 60
- autovacuum_vacuum_scale_factor = 0.05  # lower for very active tables
- autovacuum_vacuum_threshold = 50
- autovacuum_vacuum_cost_delay = 20ms
- autovacuum_vacuum_cost_limit = -1 (use defaults; measure IO)

Table-specific autovacuum (recommended for clickevents):
ALTER TABLE clickevents SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_threshold = 1000);

Manual maintenance schedule suggestions:
- Frequently insert-only (clickevents):
  - Run ANALYZE clickevents daily (or every 4 hours if you need up-to-date planner stats for aggregations).
  - Run VACUUM (not FULL) periodically; FULL only when reclaiming space (rare) because FULL locks table.
- Weekly:
  - VACUUM VERBOSE ANALYZE on smaller tables (users, shorturls).
- Monthly:
  - REINDEX on heavily-updated indexes if bloat observed or after large bulk operations.

Cron example (run as postgres user or via pg_cron):
# Every 4 hours: analyze click events
0 */4 * * * psql $DATABASE_URL -c "ANALYZE VERBOSE clickevents;"

# Daily maintenance at 03:00: analyze DB, vacuum small tables
0 3 * * * psql $DATABASE_URL -c "ANALYZE VERBOSE;" && psql $DATABASE_URL -c "VACUUM VERBOSE ANALYZE users; VACUUM VERBOSE ANALYZE shorturls;"

# Weekly: check bloat and schedule reindex if needed
0 4 * * 0 psql $DATABASE_URL -c "REINDEX DATABASE public;"

Notes:
- Use pg_stat_user_tables and pg_stat_all_tables to monitor vacuum/analyze activity and detect autovacuum lag.
- Prefer incremental/autovacuum adjustments per-table rather than global changes where possible.

7) Postgres tuning sample commands (psql snippets)
- View current settings:
SHOW max_connections;
SHOW shared_buffers;
SHOW work_mem;
SHOW maintenance_work_mem;

- Adjust parameters (for immediate testing; for persistent changes use postgresql.conf or cloud parameter groups):
-- Example set via SQL (requires superuser):
ALTER SYSTEM SET max_connections = 300;
ALTER SYSTEM SET shared_buffers = '4GB';
ALTER SYSTEM SET effective_cache_size = '12GB';
ALTER SYSTEM SET work_mem = '8MB';
ALTER SYSTEM SET maintenance_work_mem = '512MB';
ALTER SYSTEM SET checkpoint_timeout = '15min';
ALTER SYSTEM SET max_wal_size = '2GB';
SELECT pg_reload_conf();

Note: For managed services (RDS/Aurora), use the parameter group UI / CLI to set parameters and reboot when required.

- Tune autovacuum:
ALTER SYSTEM SET autovacuum_max_workers = 5;
ALTER SYSTEM SET autovacuum_naptime = '1min';
SELECT pg_reload_conf();

- Enforce statement timeout for long-running queries (application-level or role-level):
ALTER ROLE smartlink_app_role SET statement_timeout = '30s';

- Inspect indexes and bloat:
-- Requires admin extensions such as pgstattuple or check manually
-- Example: list indexes for table
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'clickevents';

- Recreate index concurrently if bloat or missing:
CREATE INDEX CONCURRENTLY ix_clickevents_short_url_id_occurred_at ON clickevents (short_url_id, occurred_at);

8) Migrations & index creation notes
- Always use migrations (Alembic) in production. The repo contains a migration for materialized views (KAN-122).
- When adding indexes to large tables:
  - Use CREATE INDEX CONCURRENTLY to avoid locking writes.
  - In Alembic, use op.execute("CREATE INDEX CONCURRENTLY ...") in an upgrade function and guard for non-Postgres.
  - Note: Alembic migrations run inside transactions by default; CREATE INDEX CONCURRENTLY cannot run inside a transaction. To handle this:
    - Use the autocommit/execute outside of transaction pattern (op.get_bind().execute(...)) and ensure the migration script is executed with autocommit. Many teams choose to run index creation as a separate maintenance job instead of in migration to avoid transactional limitations.
- Materialized view CONCURRENTLY requirement:
  - REFRESH MATERIALIZED VIEW CONCURRENTLY requires the materialized view to have a unique index on the rows you expect to refresh. The repository's migration creates ux_clicks_per_shorturl_day_shorturl_day for this reason.
- Example alembic snippet for concurrent index creation:
from alembic import op
op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_clickevents_short_url_id_occurred_at ON clickevents (short_url_id, occurred_at);")

9) Testing & validation (integration & synthetic load)
Integration tests:
- Use bin/smoke_ci.py (already included in repo) to validate end-to-end interactions with a real Postgres instance in CI.
- CI smoke runs should exercise migrations, app startup (Gunicorn), register/verify/login, create short URL, and redirect flows. The smoke script already contains these steps.

Synthetic load testing:
- Use pgbench or a small custom script to generate concurrent redirect requests and creation requests.
- Sample pgbench workflow (measure insert rates to clickevents via simple SQL script):
-- Prepare custom script to execute INSERT into clickevents (simulate click tracking)
-- Run:
pgbench -h <db-host> -p 5432 -U <user> -c 50 -j 4 -T 60 -f scripts/insert_click.sql

- Alternative: Use a simple Python/locust script to hit /<slug> endpoints from many concurrent workers.

Metrics to monitor during tests:
- Postgres: connection_count, active connections, waiting connections, CPU, disk IO, wal write latency, autovacuum backlog.
- Application: request latency (p50/p95/p99), connection errors, 429/500 rates.
- PgBouncer: pool utilization (show pools), waiters, server connections.

Success criteria:
- No or minimal connection errors (connection refused/timeout) at expected concurrency.
- P99 request latency within acceptable bounds (established by product SLA).
- Postgres max_connections not exceeded, and autovacuum keeps up (no bloat growth).

Integration test for recommendations:
- Verify SQLALCHEMY_POOL_SIZE calculation:
  - Start app with workers W and threads T (if using gthread).
  - Confirm application DB pool size set to >= (W * T) + 2 (or gunicorn_workers * 2 if chosen simpler rule).
- Verify PgBouncer:
  - Point app DATABASE_URL to PgBouncer and ensure backend Postgres connections are reduced while total client connections increase.

Edge-case test:
- Simulate a sudden traffic spike and ensure PgBouncer does not starve server connections; monitor reserve_pool.

10) Cloud-specific considerations
A) Amazon RDS / Aurora:
- RDS often imposes a max_connections determined by instance class; tuned values are in RDS docs.
- Use PgBouncer when application concurrency would exceed RDS connection limits.
- In RDS, modify parameters via parameter groups. Some parameters require reboot.
- RDS monitoring via CloudWatch: track DatabaseConnections, CPUUtilization, FreeableMemory, WriteIOPS.

B) Render / Heroku / Platform-managed Postgres:
- Many managed platforms limit concurrent connections and recommend PgBouncer as well.
- On Heroku, consider PgBouncer buildpacks or managed PgBouncer offering.
- On Render, use private services for PgBouncer or use connection pooling add-ons.

C) Containerized deployments:
- Co-locate PgBouncer as a sidecar (per-node) or a central pooler in the same VPC to reduce network latency.
- Ensure health checks and readiness probes point to the application through PgBouncer when relevant.

D) Max connections & pool sizing practical example:
- Database max_connections = 500
- Reserve 20 for replication/admin = 480
- If you have 4 app clusters connecting via PgBouncer: default_pool_size per cluster <= 120
- Then allocate application SQLAlchemy pool_size to match PgBouncer default_pool_size to avoid over-committing.

11) Quick checklist for deployers (actionable)
- [ ] Determine Gunicorn worker topology (workers and threads)
- [ ] Compute SQLAlchemy pool_size = (workers * threads) + 2 (or workers * 2 simplified)
- [ ] Ensure Postgres max_connections is >= (sum of PgBouncer server connections + reserved)
- [ ] Deploy PgBouncer in front of Postgres with pool_mode = transaction
- [ ] Apply required indices via migrations or run CREATE INDEX CONCURRENTLY if needed:
      - ix_clickevents_short_url_id_occurred_at on clickevents (short_url_id, occurred_at)
      - uq_shorturls_slug on shorturls (slug)
      - uq_users_email on users (email)
- [ ] Deploy KAN-122 migration to create materialized view and unique index for CONCURRENT refresh
- [ ] Schedule bin/refresh_analytics.py to refresh MV and rebuild summary nightly (or per SLA)
- [ ] Set autovacuum and schedule ANALYZE for clickevents
- [ ] Run synthetic load tests; monitor metrics
- [ ] Adjust Postgres configuration (work_mem, shared_buffers, maintenance_work_mem) via parameter groups

12) Example commands (psql) for operators
# Check DB connection setting:
psql <DATABASE_URL> -c "SHOW max_connections;"

# Create composite index concurrently (safe for production):
psql <DATABASE_URL> -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_clickevents_short_url_id_occurred_at ON clickevents (short_url_id, occurred_at);"

# Create unique index for shorturls.slug if missing
psql <DATABASE_URL> -c "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_shorturls_slug ON shorturls (slug);"

# Vacuum & Analyze
psql <DATABASE_URL> -c "ANALYZE VERBOSE clickevents;"
psql <DATABASE_URL> -c "VACUUM VERBOSE ANALYZE shorturls;"

# Refresh materialized view concurrently (requires unique index)
psql <DATABASE_URL> -c "REFRESH MATERIALIZED VIEW CONCURRENTLY clicks_per_shorturl_day;"

# Rebuild summary table (idempotent upsert)
psql <DATABASE_URL> -c "
INSERT INTO analytics_summary_shorturl_daily (short_url_id, day, clicks)
SELECT short_url_id, day, clicks FROM clicks_per_shorturl_day
ON CONFLICT (short_url_id, day) DO UPDATE SET clicks = EXCLUDED.clicks;
"

# Set a role-level statement timeout (defensive guard)
psql <DATABASE_URL> -c "ALTER ROLE smartlink_app_role SET statement_timeout = '30s';"

# Inspect PgBouncer (on the PgBouncer host)
# Connect to pgbouncer using psql: psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer
# Then:
SHOW POOLS;

13) Notes on partitioning & long-term scaling
- If the click volume grows into multi-millions per day, consider:
  - Partition clickevents by date (monthly) to limit index size and vacuum scope.
  - Keep older partitions on cheaper storage/colder nodes if architecture supports it.
  - Use COPY for bulk imports if replaying click logs.

14) Examples & recommended values (starter values; measure & tune)
- Small production:
  - DB instance: db.r5.large or equivalent
  - max_connections: 200
  - shared_buffers: 25% of RAM
  - work_mem: 4-16MB (increase for complex aggregations)
  - maintenance_work_mem: 256-1024MB
- Medium production:
  - DB instance: db.r5.xlarge
  - shared_buffers: 25% of RAM
  - work_mem: 8-32MB
  - maintenance_work_mem: 512MB

15) Rollout plan
- Stage environment:
  - Apply index creation in a staging DB with production-sized dataset if feasible.
  - Deploy PgBouncer in staging, configure app to use it, run smoke_ci and synthetic load tests.
- Production:
  - Create indexes CONCURRENTLY during low traffic windows or as separate maintenance job.
  - Deploy PgBouncer, test under controlled ramp-up.
  - Monitor DB connections, pgbouncer stats, and application latency.

16) Recording migrations and index setup
- Always document index creation in an Alembic migration (or in ops doc) so future schema evolution is traceable.
- For materialized view and analytics summary table, the repository includes migrations/versions/kan_122_create_materialized_views.py. Ensure it has been applied or that the create_all fallback covers dev-only environments.

17) Operations contact & runbook pointers
- Keep a short runbook for:
  - Emergency steps when DB runs out of connections (stop application instances, scale PgBouncer or DB).
  - Steps to gracefully rebuild an index with minimal downtime (CREATE INDEX CONCURRENTLY, or use logical replication for major changes).
  - How to increase max_connections safely (reboot required for some settings on managed services).

18) Appendix: Quick test scripts & examples
- Use bin/smoke_ci.py to run a short end-to-end verification in CI against an ephemeral Postgres (script exists in repo).
- For load testing click insertion:
  - write a small Python script that calls the public redirect (/<slug>) in many threads or use locust.
- Example pgbench custom script (pseudo):
\set n 10000
-- a custom SQL that inserts into clickevents (use prepared statement to avoid planner overhead)
INSERT INTO clickevents (short_url_id, anonymized_ip, user_agent, referrer, country) VALUES (1, '203.0.113.0', 'pgbench', '', 'US');

19) Final remarks & safety
- These recommendations are starting points. Monitor and iterate: production workloads and cloud platform behavior vary.
- Favor PgBouncer for connection consolidation if your app has large concurrency or many dynos/pods.
- Prefer index creation CONCURRENTLY and online operations whenever possible to avoid service disruption.

Relevant repository pointers
- gunicorn.conf.py — contains guidance on pool sizing and worker/thread relationships that should be used to compute pool sizes.
- migrations/versions/kan_122_create_materialized_views.py — migration that creates clicks_per_shorturl_day and analytics_summary_shorturl_daily.
- bin/refresh_analytics.py — supports REFRESH MATERIALIZED VIEW CONCURRENTLY and rebuilding summary tables.
- routes/analytics.py — prefers materialized view / summary table when present to speed analytics reads.
- tests/ — includes smoke and integration tests (bin/smoke_ci.py and tests/*) to validate migrations and redirections.

End of guide.
---

Developer notes for merging (surgical update)
- Add file docs/postgres_provisioning.md containing the exact content above.
- After adding, make sure to create a one-line trace file entry into trace_KAN-136.txt (best-effort) during the PR merge by appending:
  <UTC ISO8601> CREATED docs/postgres_provisioning.md for KAN-136
- No code changes required to pass initial review; however, operators should follow the rollout plan to apply indexes and PgBouncer changes in staged environments before production.

If you want, I can:
- 1) produce the exact git patch (diff) to add docs/postgres_provisioning.md, including a pre-populated trace_KAN-136.txt entry; or
- 2) create a short checklist runnable as a script that applies CREATE INDEX CONCURRENTLY commands in a safe order, with dry-run mode for operator review.

Which would you like me to prepare next (git patch, automated ops script, or both)?