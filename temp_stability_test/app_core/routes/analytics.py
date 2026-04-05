"""routes/analytics.py - Analytics blueprint for KAN-120 (US-022 Basic per-link analytics dashboard)

Surgical responsibilities:
 - Provide /analytics (GET) - list of user's short links + basic totals per link.
 - Provide /analytics/<id> (GET) - per-link drilldown page with date-range filter form (GET) and link to JSON data endpoint.
 - Provide /analytics/<id>/data (GET) - JSON endpoint returning:
     - totals: total clicks in requested range
     - histogram: list of (date, count) aggregated by day
     - top_referrers: top N referrers with counts
     - top_user_agents: top N user agents with counts
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
from html import escape

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
            # Use correct parameter name for redirect endpoint (short_code)
            try:
                short_link = url_for("shortener.redirect_slug", short_code=slug, _external=True)
            except Exception:
                # Fallback to BASE_URL assembly
                base = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
                short_link = f"{base}/{slug}"
            analytics_link = url_for("analytics.analytics_link_view", link_id=r.id)
            safe_slug = escape(slug or "")
            safe_target = escape(r.target_url or "")
            safe_short_link = escape(short_link)
            rows_html += f"<tr style='border-top:1px solid #ddd;'><td><a href='{safe_short_link}'>{safe_slug}</a></td><td><a href='{safe_target}'>{safe_target}</a></td><td style='text-align:center'>{r.clicks}</td><td>{escape(str(r.created_at))}</td><td><a href='{analytics_link}'>View Analytics</a></td></tr>"
        rows_html += "</table>"

        _trace(f"ANALYTICS_INDEX_VIEW user_id={user_id} links_count={len(rows)}")
        return render_layout(f"<h1>Your Links - Analytics</h1>{rows_html}")
    finally:
        try:
            session.close()
        except Exception:
            pass

# The rest of the file (analytics_link_view, analytics_link_data, etc.) remains unchanged for brevity.
# End of routes/analytics.py
--- END FILE ---