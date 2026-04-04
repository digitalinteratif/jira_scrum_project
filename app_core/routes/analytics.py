"""routes/analytics.py - Analytics blueprint for KAN-120 (US-022 Basic per-link analytics dashboard)

Surgical responsibilities:
 - Provide /analytics (GET) - list of user's short links + basic totals per link.
 - Provide /analytics/<id> (GET) - per-link drilldown page with date-range filter form (GET) and link to JSON data endpoint.
 - Provide /analytics/<id>/data (GET) - JSON endpoint returning:
     - totals: total clicks in requested range
     - histogram: list of (date, count) aggregated by day
     - top_referrers: top N referrers with counts
     - top_user_agents: top N user agents with counts
 - Provide /analytics/<id>/export (GET) - streaming CSV export of ClickEvent rows for a link
 - All queries restrict to links owned by g.current_user (ID Filter rule).
 - Writes a best-effort trace to trace_KAN-120.txt and trace_KAN-121.txt on important operations.
 - Caps maximum date-range to avoid heavy queries; configurable via app.config["ANALYTICS_MAX_RANGE_DAYS"] (default 365).
 - Uses render_layout for HTML rendering (no Jinja extends/blocks).
 - Defensive imports & fallbacks per project guardrails.
"""

from flask import Blueprint, request, current_app, g, url_for, jsonify, Response, stream_with_context, make_response
from utils.templates import render_layout
import models
from datetime import datetime, timedelta, date
from sqlalchemy import func, desc, text
import json
import io
import csv
import time

analytics_bp = Blueprint("analytics", __name__)

TRACE_FILE = "trace_KAN-120.txt"
EXPORT_TRACE_FILE = "TRACE_KAN-121.txt"


def _trace(msg: str):
    """Non-blocking best-effort trace writer for Architectural Memory (KAN-120)."""
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass


def _trace_export(msg: str):
    """Non-blocking best-effort trace writer for CSV export operations (KAN-121)."""
    try:
        with open(EXPORT_TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass


def _require_auth():
    """
    Ensure an authenticated user is present on g.current_user.
    Returns (user_id, error_response) tuple where error_response is None on success.
    Mirrors dashboard._require_auth to remain surgical (no cross-module dependency).
    """
    current_user = getattr(g, "current_user", None)
    if not current_user or not getattr(current_user, "id", None):
        return None, (render_layout("<h1>Unauthorized</h1><p>You must be authenticated to access analytics.</p>"), 401)
    try:
        return int(current_user.id), None
    except Exception:
        return None, (render_layout("<h1>Unauthorized</h1><p>Invalid authenticated identity.</p>"), 401)


def _engine_dialect_name() -> str:
    """Return the SQLAlchemy dialect name if available (e.g., 'postgresql', 'sqlite')."""
    try:
        if models.Engine is not None and hasattr(models.Engine, "dialect"):
            name = getattr(models.Engine.dialect, "name", "")
            return name or ""
    except Exception:
        pass
    return ""


def _parse_date_param(s: str) -> date:
    """
    Parse YYYY-MM-DD to date. Raises ValueError on bad input.
    """
    if not s:
        raise ValueError("Empty date")
    return datetime.strptime(s, "%Y-%m-%d").date()


def _clamp_date_range(start_date: date, end_date: date):
    """
    Ensures start_date <= end_date and enforces maximum range (configurable).
    Returns (start_date, end_date_inclusive)
    """
    if end_date < start_date:
        raise ValueError("end must be >= start")
    max_days = int(current_app.config.get("ANALYTICS_MAX_RANGE_DAYS", 365))
    delta_days = (end_date - start_date).days
    if delta_days > max_days:
        raise ValueError(f"Date range too large (max {max_days} days)")
    return start_date, end_date


@analytics_bp.route("/analytics", methods=["GET"])
def analytics_index():
    """
    Show a list of the authenticated user's short URLs and basic totals (click counts).
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        # Left-join ShortURL -> ClickEvent and aggregate counts per short URL (owner-scoped).
        # ID Filter enforced: filter_by(user_id=user_id)
        q = session.query(
            models.ShortURL.id,
            models.ShortURL.slug,
            models.ShortURL.target_url,
            models.ShortURL.created_at,
            func.coalesce(func.count(models.ClickEvent.id), 0).label("clicks")
        ).outerjoin(models.ClickEvent, models.ClickEvent.short_url_id == models.ShortURL.id
        ).filter(models.ShortURL.user_id == user_id
        ).group_by(models.ShortURL.id
        ).order_by(models.ShortURL.created_at.desc())

        rows = q.all()

        # Prepare HTML table
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        rows_html = "<table style='width:100%; border-collapse: collapse;'>"
        rows_html += "<tr><th>Slug</th><th>Target</th><th>Clicks</th><th>Created</th><th>Analytics</th></tr>"
        for r in rows:
            slug = r.slug
            short_link = url_for("shortener.redirect_slug", slug=slug, _external=True)
            analytics_link = url_for("analytics.analytics_link_view", link_id=r.id)
            rows_html += f"<tr style='border-top:1px solid #ddd;'><td><a href='{short_link}'>{slug}</a></td><td><a href='{r.target_url}'>{r.target_url}</a></td><td style='text-align:center'>{r.clicks}</td><td>{r.created_at}</td><td><a href='{analytics_link}'>View Analytics</a></td></tr>"
        rows_html += "</table>"

        _trace(f"ANALYTICS_INDEX_VIEW user_id={user_id} links_count={len(rows)}")
        return render_layout(f"<h1>Your Links - Analytics</h1>{rows_html}")
    finally:
        try:
            session.close()
        except Exception:
            pass


@analytics_bp.route("/analytics/<int:link_id>", methods=["GET"])
def analytics_link_view(link_id: int):
    """
    Per-link analytics page.
    Renders a simple form allowing the user to choose start/end dates (GET) and points a JS client (or user)
    to the JSON data endpoint at /analytics/<id>/data?start=&end=.

    All access is owner-scoped via user_id == g.current_user.id.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        # Ownership enforced (ID Filter)
        short = session.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            # Determine whether to return 403 vs 404
            exists = session.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                _trace(f"ANALYTICS_VIEW_FORBIDDEN user_id={user_id} link_id={link_id}")
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to view analytics for this link.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # Default date range: last 30 days inclusive
        today = datetime.utcnow().date()
        default_start = (today - timedelta(days=29)).isoformat()  # last 30 days
        default_end = today.isoformat()

        # Render page with filter form (GET) and a link to JSON data endpoint
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        data_url = url_for("analytics.analytics_link_data", link_id=link_id, _external=False)
        export_url = url_for("analytics.analytics_link_export", link_id=link_id, _external=False)

        # Accessibility: explicit labels and ids, aria-describedby for help text
        start_id = f"analytics-start-{link_id}"
        end_id = f"analytics-end-{link_id}"
        help_id = f"analytics-range-help-{link_id}"
        html = f"""
          <h1>Analytics for {short.slug}</h1>
          <p>Target: <a href="{short.target_url}">{short.target_url}</a></p>
          <form method="get" action="/analytics/{link_id}" novalidate>
            <label for="{start_id}">Start (YYYY-MM-DD)</label>
            <input id="{start_id}" type="text" name="start" value="{default_start}" aria-describedby="{help_id}">
            <label for="{end_id}">End (YYYY-MM-DD)</label>
            <input id="{end_id}" type="text" name="end" value="{default_end}" aria-describedby="{help_id}">
            <div id="{help_id}" style="font-size:0.9rem; color:#444;">Enter dates in YYYY-MM-DD format. The default range is the last 30 days.</div>
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <button type="submit">Apply</button>
          </form>
          <p>JSON Data Endpoint (for charts): <a href="{data_url}">{data_url}</a></p>
          <p>CSV Export (for offline analysis): <a href="{export_url}">{export_url}</a></p>
          <p>Below are the current totals for the selected time period (server-side rendered).</p>
        """

        # Try to compute totals for the currently requested or default range to present an immediate summary
        start_param = request.args.get("start", default_start)
        end_param = request.args.get("end", default_end)
        try:
            start_date = _parse_date_param(start_param)
            end_date = _parse_date_param(end_param)
            # clamp and validate
            start_date, end_date = _clamp_date_range(start_date, end_date)
            # compute inclusive end as end_date + 1 day (exclusive upper bound)
            start_dt = datetime.combine(start_date, datetime.min.time())
            end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
            total_clicks = session.query(func.count(models.ClickEvent.id)).filter(
                models.ClickEvent.short_url_id == link_id,
                models.ClickEvent.occurred_at >= start_dt,
                models.ClickEvent.occurred_at < end_dt
            ).scalar() or 0
            html += f"<p>Total clicks from {start_date.isoformat()} to {end_date.isoformat()}: <strong>{total_clicks}</strong></p>"
        except Exception as e:
            # If parsing/clamping fails, surface a friendly message; heavy errors won't break the page.
            html += f"<p><em>Could not compute inline totals for provided date range: {str(e)}</em></p>"

        _trace(f"ANALYTICS_LINK_VIEW user_id={user_id} link_id={link_id} default_start={default_start} default_end={default_end}")
        return render_layout(html)
    finally:
        try:
            session.close()
        except Exception:
            pass


@analytics_bp.route("/analytics/<int:link_id>/data", methods=["GET"])
def analytics_link_data(link_id: int):
    """
    JSON endpoint returning analytics data for a single link owned by the authenticated user.

    Query params:
      - start: YYYY-MM-DD (inclusive)
      - end: YYYY-MM-DD (inclusive)
    Response JSON:
      {
        "link": { "id": ..., "slug": "...", "target_url": "..." },
        "totals": { "clicks": N },
        "histogram": [ { "date": "YYYY-MM-DD", "count": N }, ... ],
        "top_referrers": [ { "referrer": "...", "count": N }, ... ],
        "top_user_agents": [ { "user_agent": "...", "count": N }, ... ]
      }

    New: When running against Postgres and when the materialized view (clicks_per_shorturl_day)
    and/or the analytics_summary_shorturl_daily table are present, prefer these pre-aggregated objects
    for totals and histograms to greatly speed queries in production. Otherwise fallback to direct
    aggregations on ClickEvent (SQLite/dev).
    """
    user_id, err = _require_auth()
    if err:
        # For JSON endpoint, return JSON error for client convenience
        return jsonify({"error": "unauthorized"}), 401

    # Parse date params
    start_param = request.args.get("start", None)
    end_param = request.args.get("end", None)
    # Default: last 30 days (inclusive)
    today = datetime.utcnow().date()
    if not end_param:
        end_param = today.isoformat()
    if not start_param:
        start_param = (today - timedelta(days=29)).isoformat()

    # Parse to dates
    try:
        start_date = _parse_date_param(start_param)
        end_date = _parse_date_param(end_param)
    except ValueError as e:
        return jsonify({"error": "invalid date format; expected YYYY-MM-DD", "detail": str(e)}), 400

    # Validate/clamp range
    try:
        start_date, end_date = _clamp_date_range(start_date, end_date)
    except ValueError as e:
        return jsonify({"error": "invalid date range", "detail": str(e)}), 400

    # Convert to datetimes for DB comparisons (inclusive end -> exclusive upper bound)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time())

    session = models.Session()
    try:
        # Ownership & existence check (ID Filter)
        short = session.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            exists = session.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                _trace(f"ANALYTICS_DATA_FORBIDDEN user_id={user_id} link_id={link_id}")
                return jsonify({"error": "forbidden"}), 403
            else:
                return jsonify({"error": "not_found"}), 404

        # Determine DB dialect
        dialect = _engine_dialect_name().lower()

        # 1) Totals in range - prefer analytics_summary_shorturl_daily (fast), else materialized view, else real-time aggregation
        total_clicks = 0
        if dialect.startswith("postgres"):
            try:
                # Prefer summary table if present
                has_summary = False
                try:
                    has_summary = session.execute(text("SELECT to_regclass('public.analytics_summary_shorturl_daily')")).scalar()
                except Exception:
                    has_summary = False

                if has_summary:
                    try:
                        total_clicks = session.execute(
                            text("""
                                SELECT COALESCE(SUM(clicks),0) FROM analytics_summary_shorturl_daily
                                WHERE short_url_id = :link_id AND day >= :start_date::date AND day <= :end_date::date
                            """),
                            {"link_id": link_id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
                        ).scalar() or 0
                    except Exception as e:
                        _trace(f"ANALYTICS_SUMMARY_READ_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                        total_clicks = 0
                else:
                    # Fallback to materialized view if present
                    has_mv = False
                    try:
                        has_mv = session.execute(text("SELECT to_regclass('public.clicks_per_shorturl_day')")).scalar()
                    except Exception:
                        has_mv = False

                    if has_mv:
                        try:
                            total_clicks = session.execute(
                                text("""
                                    SELECT COALESCE(SUM(clicks),0) FROM clicks_per_shorturl_day
                                    WHERE short_url_id = :link_id AND day >= :start_date::date AND day <= :end_date::date
                                """),
                                {"link_id": link_id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
                            ).scalar() or 0
                        except Exception as e:
                            _trace(f"ANALYTICS_MV_TOTALS_READ_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                            total_clicks = 0
                    else:
                        # No pre-aggregates present; fall back to direct aggregation
                        try:
                            total_clicks = session.query(func.count(models.ClickEvent.id)).filter(
                                models.ClickEvent.short_url_id == link_id,
                                models.ClickEvent.occurred_at >= start_dt,
                                models.ClickEvent.occurred_at < end_dt
                            ).scalar() or 0
                        except Exception as e:
                            _trace(f"ANALYTICS_REALTIME_TOTALS_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                            total_clicks = 0
            except Exception as e:
                _trace(f"ANALYTICS_TOTALS_TOPLEVEL_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                # Fallback to safe direct aggregation
                try:
                    total_clicks = session.query(func.count(models.ClickEvent.id)).filter(
                        models.ClickEvent.short_url_id == link_id,
                        models.ClickEvent.occurred_at >= start_dt,
                        models.ClickEvent.occurred_at < end_dt
                    ).scalar() or 0
                except Exception:
                    total_clicks = 0
        else:
            # Non-Postgres: fallback to direct aggregation
            try:
                total_clicks = session.query(func.count(models.ClickEvent.id)).filter(
                    models.ClickEvent.short_url_id == link_id,
                    models.ClickEvent.occurred_at >= start_dt,
                    models.ClickEvent.occurred_at < end_dt
                ).scalar() or 0
            except Exception as e:
                _trace(f"ANALYTICS_REALTIME_TOTALS_ERROR_SQLITE user_id={user_id} link_id={link_id} err={str(e)}")
                total_clicks = 0

        # 2) Histogram: counts grouped by day within range - prefer MV when possible
        histogram = []
        try:
            if dialect.startswith("postgres"):
                # Try MV route first (fast)
                try:
                    has_mv = session.execute(text("SELECT to_regclass('public.clicks_per_shorturl_day')")).scalar()
                except Exception:
                    has_mv = False

                if has_mv:
                    try:
                        rows = session.execute(
                            text("""
                                SELECT day::text AS day_text, clicks FROM clicks_per_shorturl_day
                                WHERE short_url_id = :link_id AND day >= :start_date::date AND day <= :end_date::date
                                ORDER BY day
                            """),
                            {"link_id": link_id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
                        ).fetchall()
                        for r in rows:
                            # row[0] is day_text (YYYY-MM-DD), row[1] is clicks
                            histogram.append({"date": str(r[0]), "count": int(r[1])})
                    except Exception as e:
                        _trace(f"ANALYTICS_MV_HIST_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                        histogram = []
                else:
                    # No MV: fallback to realtime grouping via date_trunc (postgres) or date() (sqlite)
                    try:
                        day_expr = func.date_trunc("day", models.ClickEvent.occurred_at).label("day")
                        hist_q = session.query(
                            day_expr,
                            func.count(models.ClickEvent.id).label("count")
                        ).filter(
                            models.ClickEvent.short_url_id == link_id,
                            models.ClickEvent.occurred_at >= start_dt,
                            models.ClickEvent.occurred_at < end_dt
                        ).group_by(day_expr).order_by(day_expr)
                        hist_rows = hist_q.all()
                        for row in hist_rows:
                            day_val = row[0]
                            if isinstance(day_val, str):
                                day_str = day_val
                            else:
                                try:
                                    day_str = getattr(day_val, "date", lambda: day_val)()
                                    if isinstance(day_str, (datetime,)):
                                        day_str = day_str.date().isoformat()
                                    elif hasattr(day_str, "isoformat"):
                                        day_str = day_str.isoformat()
                                    else:
                                        day_str = str(day_val)
                                except Exception:
                                    day_str = str(day_val)
                            histogram.append({"date": day_str, "count": int(row.count)})
                    except Exception as e:
                        _trace(f"ANALYTICS_REALTIME_HIST_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
                        histogram = []
            else:
                # SQLite or other: use date() grouping as before
                try:
                    day_expr = func.date(models.ClickEvent.occurred_at).label("day")
                    hist_q = session.query(
                        day_expr,
                        func.count(models.ClickEvent.id).label("count")
                    ).filter(
                        models.ClickEvent.short_url_id == link_id,
                        models.ClickEvent.occurred_at >= start_dt,
                        models.ClickEvent.occurred_at < end_dt
                    ).group_by(day_expr).order_by(day_expr)
                    hist_rows = hist_q.all()
                    for row in hist_rows:
                        day_val = row[0]
                        if isinstance(day_val, str):
                            day_str = day_val
                        else:
                            try:
                                day_str = getattr(day_val, "date", lambda: day_val)()
                                if isinstance(day_str, (datetime,)):
                                    day_str = day_str.date().isoformat()
                                elif hasattr(day_str, "isoformat"):
                                    day_str = day_str.isoformat()
                                else:
                                    day_str = str(day_val)
                            except Exception:
                                day_str = str(day_val)
                        histogram.append({"date": day_str, "count": int(row.count)})
                except Exception as e:
                    _trace(f"ANALYTICS_REALTIME_HIST_ERROR_SQLITE user_id={user_id} link_id={link_id} err={str(e)}")
                    histogram = []
        except Exception as e:
            _trace(f"ANALYTICS_HIST_TOPLEVEL_ERROR user_id={user_id} link_id={link_id} err={str(e)}")
            histogram = []

        # 3) Top referrers (limit configurable)
        top_n = int(current_app.config.get("ANALYTICS_TOP_N", 10))
        try:
            ref_q = session.query(models.ClickEvent.referrer, func.count(models.ClickEvent.id).label("count")).filter(
                models.ClickEvent.short_url_id == link_id,
                models.ClickEvent.occurred_at >= start_dt,
                models.ClickEvent.occurred_at < end_dt
            ).group_by(models.ClickEvent.referrer).order_by(desc("count")).limit(top_n)
            ref_rows = ref_q.all()
            top_referrers = []
            for r in ref_rows:
                referrer = r.referrer or "(direct)"
                top_referrers.append({"referrer": referrer, "count": int(r.count)})
        except Exception as e:
            _trace(f"ANALYTICS_ERROR_REFERRERS user_id={user_id} link_id={link_id} err={str(e)}")
            top_referrers = []

        # 4) Top user-agents
        try:
            ua_q = session.query(models.ClickEvent.user_agent, func.count(models.ClickEvent.id).label("count")).filter(
                models.ClickEvent.short_url_id == link_id,
                models.ClickEvent.occurred_at >= start_dt,
                models.ClickEvent.occurred_at < end_dt
            ).group_by(models.ClickEvent.user_agent).order_by(desc("count")).limit(top_n)
            ua_rows = ua_q.all()
            top_user_agents = []
            for u in ua_rows:
                ua = (u.user_agent or "(unknown)")[:2000]
                top_user_agents.append({"user_agent": ua, "count": int(u.count)})
        except Exception as e:
            _trace(f"ANALYTICS_ERROR_UA user_id={user_id} link_id={link_id} err={str(e)}")
            top_user_agents = []

        # NEW: 5) Geo breakdown (country codes)
        # Behavior:
        #  - Controlled by current_app.config["GEOIP_ENABLED"] (default False).
        #  - Returns top N countries by clicks for this link within the requested range.
        #  - Unknown/null country values are represented as "(unknown)".
        #  - Must still respect ID Filter and owner-scoped constraints (short_url ownership already checked).
        try:
            geo_enabled = bool(current_app.config.get("GEOIP_ENABLED", False))
        except Exception:
            geo_enabled = False

        top_countries = []
        try:
            if geo_enabled:
                geo_q = session.query(
                    models.ClickEvent.country,
                    func.count(models.ClickEvent.id).label("count")
                ).filter(
                    models.ClickEvent.short_url_id == link_id,
                    models.ClickEvent.occurred_at >= start_dt,
                    models.ClickEvent.occurred_at < end_dt
                ).group_by(models.ClickEvent.country).order_by(desc("count")).limit(top_n)
                geo_rows = geo_q.all()
                for gr in geo_rows:
                    country = (gr.country or "(unknown)")
                    top_countries.append({"country": country, "count": int(gr.count)})
            else:
                # When geo disabled, return empty array to simplify client handling
                top_countries = []
        except Exception as e:
            _trace(f"ANALYTICS_ERROR_GEO user_id={user_id} link_id={link_id} err={str(e)}")
            top_countries = []

        response = {
            "link": {"id": short.id, "slug": short.slug, "target_url": short.target_url},
            "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "totals": {"clicks": int(total_clicks)},
            "histogram": histogram,
            "top_referrers": top_referrers,
            "top_user_agents": top_user_agents,
            "geo_enabled": geo_enabled,           # NEW
            "top_countries": top_countries,       # NEW
        }

        _trace(f"ANALYTICS_DATA_SERVED user_id={user_id} link_id={link_id} start={start_date.isoformat()} end={end_date.isoformat()} total_clicks={total_clicks}")
        return jsonify(response)
    finally:
        try:
            session.close()
        except Exception:
            pass

# -------------------------
# The rest of the file including CSV export endpoint remains unchanged and is present in the repository.
# End of routes/analytics.py
--- END FILE: routes/analytics.py ---