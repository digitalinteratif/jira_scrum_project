"""routes/dashboard.py - Dashboard blueprint for short URL management (KAN-117)

Provides:
  - GET  /dashboard
  - GET  /dashboard/<id>/edit
  - POST /dashboard/<id>/edit
  - POST /dashboard/<id>/delete

All routes enforce authentication via g.current_user, apply the "ID Filter" rule for user-owned data,
include CSRF tokens in forms, and return HTML wrapped via utils.templates.render_layout.

This module is a surgical addition to register dashboard functionality as its own Blueprint.
"""

from flask import Blueprint, request, current_app, url_for, redirect, g
from utils.templates import render_layout
from sqlalchemy.exc import IntegrityError
import models
from datetime import datetime

# Utilities used for validation/suggestions
try:
    from utils.shortener import validate_custom_slug, suggest_alternatives
except Exception:
    # Minimal defensive fallbacks
    def validate_custom_slug(slug):
        return isinstance(slug, str) and 0 < len(slug) <= 255 and all(c.isalnum() or c in "-_" for c in slug)

    def suggest_alternatives(base_slug, count=5, session=None):
        return [f"{base_slug}-{i}" for i in range(1, count + 1)]

# Try to import URL validator; provide conservative fallback if absent.
try:
    from utils.validation import validate_and_normalize_url
except Exception:
    from urllib.parse import urlparse, urlunparse, quote, unquote

    def validate_and_normalize_url(raw_url: str) -> str:
        if not isinstance(raw_url, str):
            raise ValueError("URL must be a string.")
        url = raw_url.strip()
        if not url:
            raise ValueError("Empty URL provided.")
        if "\n" in url or "\r" in url:
            raise ValueError("Invalid characters in URL.")
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError("URL must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("URL must include a network location.")
        netloc = parsed.netloc
        userinfo = ""
        hostport = netloc
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            try:
                int(port)
                port_part = f":{port}"
            except Exception:
                host = hostport
                port_part = ""
        else:
            host = hostport
            port_part = ""
        try:
            host_idna = host.encode("idna").decode("ascii")
        except Exception:
            raise ValueError("Invalid hostname in URL.")
        normalized_netloc = host_idna + port_part
        if userinfo:
            normalized_netloc = f"{userinfo}@{normalized_netloc}"
        path = quote(unquote(parsed.path), safe="/%:@[]!$&'()*+,;=")
        query = quote(unquote(parsed.query), safe="=&?/")
        fragment = quote(unquote(parsed.fragment), safe="")
        return urlunparse((scheme, normalized_netloc, path, parsed.params, query, fragment))


dashboard_bp = Blueprint("dashboard", __name__)

def _write_trace_117(entry: str):
    """Best-effort trace writer for KAN-117."""
    try:
        with open("trace_KAN-117.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass

def _require_auth():
    """
    Ensure an authenticated user is present on g.current_user.
    Returns (user_id, error_response) tuple where error_response is None on success.
    """
    current_user = getattr(g, "current_user", None)
    if not current_user or not getattr(current_user, "id", None):
        return None, (render_layout("<h1>Unauthorized</h1><p>You must be authenticated to access the dashboard.</p>"), 401)
    try:
        return int(current_user.id), None
    except Exception:
        return None, (render_layout("<h1>Unauthorized</h1><p>Invalid authenticated identity.</p>"), 401)


@dashboard_bp.route("/dashboard", methods=["GET"])
def dashboard_index():
    """
    List the authenticated user's short URLs.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        # ID Filter applied here: only show current user's links
        items = session.query(models.ShortURL).filter_by(user_id=user_id).order_by(models.ShortURL.created_at.desc()).all()

        # CSRF token for delete forms
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        rows_html = "<table style='width:100%; border-collapse: collapse;'>"
        rows_html += "<tr><th>Slug</th><th>Target</th><th>Hits</th><th>Created</th><th>Actions</th></tr>"
        for s in items:
            short_url = url_for("shortener.redirect_slug", slug=s.slug, _external=True)
            edit_url = url_for("dashboard.dashboard_edit", link_id=s.id)
            # Delete is a POST form to avoid CSRF-less delete
            delete_form = f"""
              <form method="post" action="/dashboard/{s.id}/delete" style="display:inline;">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit" onclick="return confirm('Delete this short link?');">Delete</button>
              </form>
            """
            rows_html += f"<tr style='border-top:1px solid #ddd;'><td><a href='{short_url}'>{s.slug}</a></td><td><a href='{s.target_url}'>{s.target_url}</a></td><td style='text-align:center'>{s.hit_count or 0}</td><td>{s.created_at}</td><td><a href='{edit_url}'>Edit</a> {delete_form}</td></tr>"
        rows_html += "</table>"

        try:
            _write_trace_117(f"DASHBOARD_VIEW user_id={user_id} count={len(items)}")
        except Exception:
            pass

        return render_layout(f"<h1>Your Short URLs</h1>{rows_html}")
    finally:
        try:
            session.close()
        except Exception:
            pass


@dashboard_bp.route("/dashboard/<int:link_id>/edit", methods=["GET", "POST"])
def dashboard_edit(link_id):
    """
    Edit a short URL owned by the authenticated user.

    GET: render a form pre-populated with the current values (CSRF token included).
    POST: apply changes (target_url, optional slug, expire_at). Ownership enforced.

    Ownership & error handling:
      - If the row exists but is not owned by the current user -> return 403.
      - If the row does not exist -> return 404.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        # Preferred, owner-filtered fetch
        short = session.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            # To return accurate 403 vs 404, check for existence without user filter
            exists = session.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                try:
                    _write_trace_117(f"DASHBOARD_EDIT_FORBIDDEN user_id={user_id} link_id={link_id}")
                except Exception:
                    pass
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to modify this link.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # GET: render form
        if request.method == "GET":
            try:
                from flask_wtf.csrf import generate_csrf
                csrf_token = generate_csrf()
            except Exception:
                csrf_token = ""

            expire_at_val = short.expire_at.strftime("%Y-%m-%d %H:%M") if short.expire_at else ""
            # Unique ids per-short (helps when multiple forms or client reuse)
            slug_id = f"slug-{short.id}"
            target_id = f"target-{short.id}"
            expire_id = f"expire-{short.id}"
            html = f"""
              <h1>Edit Short URL</h1>
              <form method="post" action="/dashboard/{short.id}/edit" novalidate>
                <label for="{slug_id}">Slug</label>
                <input id="{slug_id}" type="text" name="slug" maxlength="255" value="{short.slug}">
                <label for="{target_id}">Target URL</label>
                <input id="{target_id}" type="url" name="target_url" required style="width:100%" value="{short.target_url}">
                <label for="{expire_id}">Expire At (YYYY-MM-DD HH:MM UTC)</label>
                <input id="{expire_id}" type="text" name="expire_at" value="{expire_at_val}">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit">Save</button>
              </form>
              <p><a href="{url_for('dashboard.dashboard_index')}">Back to dashboard</a></p>
            """
            return render_layout(html)

        # POST: apply update
        # Collect form fields
        new_target_raw = request.form.get("target_url", "").strip()
        new_slug = request.form.get("slug", "").strip()
        new_expire_raw = request.form.get("expire_at", "").strip()
        new_expire = None
        if new_expire_raw:
            try:
                new_expire = datetime.strptime(new_expire_raw, "%Y-%m-%d %H:%M")
            except Exception:
                return render_layout("<p>expire_at must be in 'YYYY-MM-DD HH:MM' UTC format.</p>"), 400

        if not new_target_raw:
            return render_layout("<p>Missing target URL.</p>"), 400

        # Normalize new target
        try:
            new_target = validate_and_normalize_url(new_target_raw)
        except Exception as e:
            return render_layout(f"<h1>Invalid URL</h1><p>{str(e)}</p>"), 400

        # If slug changed, validate and ensure uniqueness
        if new_slug and new_slug != short.slug:
            if not validate_custom_slug(new_slug):
                return render_layout("<h1>Invalid Slug</h1><p>The provided slug is invalid. Allowed characters: A-Z a-z 0-9 _ -</p>"), 400
            # Check for collisions
            collision = session.query(models.ShortURL).filter_by(slug=new_slug).first()
            if collision and collision.id != short.id:
                # Conflict: slug already in use
                try:
                    suggestions = suggest_alternatives(new_slug, count=5, session=session)
                except Exception:
                    suggestions = []
                sug_html = "<p>Slug already taken.</p>"
                if suggestions:
                    sug_html += "<ul>"
                    for s in suggestions:
                        short_path = url_for("shortener.redirect_slug", slug=s, _external=True)
                        sug_html += f"<li>{s} -> <a href='{short_path}'>{short_path}</a></li>"
                    sug_html += "</ul>"
                return render_layout(f"<h1>Slug Conflict</h1>{sug_html}"), 400
            # All good: assign new slug
            short.slug = new_slug

        # Apply updates
        short.target_url = new_target
        short.expire_at = new_expire

        try:
            session.add(short)
            session.commit()
            session.refresh(short)
            try:
                _write_trace_117(f"DASHBOARD_EDIT_APPLIED user_id={user_id} link_id={short.id} slug={short.slug}")
            except Exception:
                pass
            return render_layout(f"<h1>Saved</h1><p>Your changes have been saved.</p><p><a href='{url_for('dashboard.dashboard_index')}'>Back to dashboard</a></p>")
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            return render_layout(f"<h1>Database Error</h1><p>{str(e)}</p>"), 500
    finally:
        try:
            session.close()
        except Exception:
            pass


@dashboard_bp.route("/dashboard/<int:link_id>/delete", methods=["POST"])
def dashboard_delete(link_id):
    """
    Delete (or soft-delete) a short URL owned by the authenticated user.

    Behavior:
      - Ownership enforced (403 if exists but not owned; 404 if not found).
      - If current_app.config["SHORTURL_SOFT_DELETE"] is truthy: apply soft-delete by setting expire_at to now.
      - Otherwise: delete the row.
      - Commit with rollback on exception.
      - CSRF-protected form expected in caller.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        # Owner-filtered fetch
        short = session.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            exists = session.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                try:
                    _write_trace_117(f"DASHBOARD_DELETE_FORBIDDEN user_id={user_id} link_id={link_id}")
                except Exception:
                    pass
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to delete this link.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # Decide soft vs hard delete based on config
        try:
            soft_delete = bool(current_app.config.get("SHORTURL_SOFT_DELETE", False))
        except Exception:
            soft_delete = False

        try:
            if soft_delete:
                short.expire_at = datetime.utcnow()
                session.add(short)
            else:
                session.delete(short)
            session.commit()
            try:
                _write_trace_117(f"DASHBOARD_DELETE_APPLIED user_id={user_id} link_id={link_id} soft_delete={soft_delete}")
            except Exception:
                pass
            return redirect(url_for("dashboard.dashboard_index"))
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            return render_layout(f"<h1>Database Error</h1><p>{str(e)}</p>"), 500
    finally:
        try:
            session.close()
        except Exception:
            pass
# --- END FILE: routes/dashboard.py ---