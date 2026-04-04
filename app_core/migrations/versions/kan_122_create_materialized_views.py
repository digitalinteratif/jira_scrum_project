"""Alembic migration: KAN-122 create materialized views and summary table for analytics.

This migration will:
 - On Postgres:
    - create materialized view clicks_per_shorturl_day (short_url_id int, day date, clicks bigint)
    - create unique index for concurrent refresh and regular index for query performance
    - create analytics_summary_shorturl_daily table (short_url_id int, day date, clicks bigint) PRIMARY KEY(short_url_id, day)
    - populate summary table from the materialized view
 - On non-Postgres (e.g., SQLite): this migration is a no-op (safe dev fallback).

Note: REFRESH logic is implemented in bin/refresh_analytics.py (scheduled job). This migration ensures the objects exist initially.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "kan_122_create_materialized_views"
down_revision = None  # adjust if you have an existing revision graph
branch_labels = None
depends_on = None

def _is_postgres():
    bind = op.get_bind()
    try:
        name = getattr(bind.dialect, "name", "")
        return name and name.lower().startswith("postgres")
    except Exception:
        return False

def upgrade():
    if not _is_postgres():
        # Non-postgres environment (SQLite dev) -> skip creation
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write("UPGRADE_SKIPPED non-postgres environment\n")
        except Exception:
            pass
        return

    # Create materialized view and indexes
    conn = op.get_bind()
    try:
        # Create materialized view aggregating clicks per shorturl by day
        op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS clicks_per_shorturl_day AS
        SELECT
          short_url_id::integer AS short_url_id,
          date_trunc('day', occurred_at)::date AS day,
          COUNT(*)::bigint AS clicks
        FROM clickevents
        GROUP BY short_url_id, day;
        """)

        # Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY
        op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_clicks_per_shorturl_day_shorturl_day
        ON clicks_per_shorturl_day (short_url_id, day);
        """)

        # Additional non-unique index for fast short_url_id lookups by day ordering
        op.execute("""
        CREATE INDEX IF NOT EXISTS ix_clicks_per_shorturl_day_shorturl
        ON clicks_per_shorturl_day (short_url_id, day);
        """)

        # Create summary table to be used for fast totals (can be populated nightly from MV)
        op.execute("""
        CREATE TABLE IF NOT EXISTS analytics_summary_shorturl_daily (
          short_url_id integer NOT NULL,
          day date NOT NULL,
          clicks bigint NOT NULL,
          PRIMARY KEY (short_url_id, day)
        );
        """)

        # Populate summary table initially from materialized view (idempotent-ish via upsert)
        # Use INSERT ... ON CONFLICT to upsert in case migration is re-run
        op.execute("""
        INSERT INTO analytics_summary_shorturl_daily (short_url_id, day, clicks)
        SELECT short_url_id, day, clicks FROM clicks_per_shorturl_day
        ON CONFLICT (short_url_id, day) DO UPDATE SET clicks = EXCLUDED.clicks;
        """)

        # Index to accelerate queries by short_url_id
        op.execute("""
        CREATE INDEX IF NOT EXISTS ix_analytics_summary_shorturl_daily_shorturl
        ON analytics_summary_shorturl_daily (short_url_id);
        """)
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write("UPGRADE_APPLIED created materialized view and analytics summary table\n")
        except Exception:
            pass
    except Exception as e:
        # Try to log and re-raise to make migration failure visible to operators
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write(f"UPGRADE_ERROR {str(e)}\n")
        except Exception:
            pass
        raise

def downgrade():
    if not _is_postgres():
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write("DOWNGRADE_SKIPPED non-postgres environment\n")
        except Exception:
            pass
        return

    try:
        # Drop indexes/tables/view if present
        op.execute("DROP INDEX IF EXISTS ix_analytics_summary_shorturl_daily_shorturl;")
        op.execute("DROP TABLE IF EXISTS analytics_summary_shorturl_daily;")
        op.execute("DROP INDEX IF EXISTS ix_clicks_per_shorturl_day_shorturl;")
        op.execute("DROP INDEX IF EXISTS ux_clicks_per_shorturl_day_shorturl_day;")
        op.execute("DROP MATERIALIZED VIEW IF EXISTS clicks_per_shorturl_day;")
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write("DOWNGRADE_APPLIED dropped materialized view and summary table\n")
        except Exception:
            pass
    except Exception as e:
        try:
            with open("trace_KAN-122.txt", "a") as f:
                f.write(f"DOWNGRADE_ERROR {str(e)}\n")
        except Exception:
            pass
        raise
--- END FILE: migrations/versions/kan_122_create_materialized_views.py ---