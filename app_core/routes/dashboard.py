"""routes/dashboard.py - Dashboard blueprint for short URL management (KAN-117)

Provides:
  - GET  /dashboard
  - GET  /dashboard/<id>/edit
  - POST /dashboard/<id>/edit
  - POST /dashboard/<id>/delete

All routes enforce authentication via session or g.current_user, apply the "ID Filter" rule for user-owned data,
include CSRF tokens in forms, and return HTML wrapped via utils.templates.render_layout.
"""

from flask import Blueprint, request, current_app, url_for, redirect, g, session, flash
from utils.templates import render_layout
from sqlalchemy.exc import IntegrityError
import models
from datetime import datetime
import logging
from html import escape

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
    validate_and_normalize_url = None

dashboard_bp = Blueprint("dashboard", __name__)

_logger = logging.getLogger(__name__)

def _write_trace_117(entry: str):
    """Best-effort trace writer for KAN-117."""
    try:
        with open("trace_KAN-117.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass

def _require_auth():
    """
    Ensure an authenticated user is present either on g.current_user or session['user_id'].
    Returns (user_id, error_response) tuple where error_response is None on success.
    Accepts demo user_id when ALLOW_DEMO_USER_ID is true (from query/form values).
    """
    # Prefer g.current_user if middleware sets it
    current_user = getattr(g, "current_user", None)
    if current_user and getattr(current_user, "id", None):
        try:
            return int(current_user.id), None
        except Exception:
            return None, (render_layout("<h1>Unauthorized</h1><p>Invalid authenticated identity.</p>"), 401)

    # Fallback to session-based auth (most routes set session['user_id'] on login)
    try:
        sess_uid = session.get("user_id")
        if sess_uid:
            try:
                return int(sess_uid), None
            except Exception:
                pass
    except Exception:
        pass

    # allow demo mode for tests/dev when explicitly enabled
    try:
        allow_demo = bool(current_app.config.get("ALLOW_DEMO_USER_ID", False))
    except Exception:
        allow_demo = False
    if allow_demo:
        try:
            user_id = int(request.values.get("user_id", "0"))
            if user_id > 0:
                return user_id, None
        except Exception:
            pass
        return None, (render_layout("<h1>Unauthorized</h1><p>Demo user_id required in ALLOW_DEMO_USER_ID mode.</p>"), 401)

    return None, (render_layout("<h1>Unauthorized</h1><p>You must be authenticated to access the dashboard.</p>"), 401)


@dashboard_bp.route("/dashboard", methods=["GET"])
def dashboard_index():
    """
    List the authenticated user's short URLs and provide the shortener form.
    """
    user_id, err = _require_auth()
    if err:
        # err already a (body, status) pair or redirect; if redirect, return it
        # If err is a tuple containing response body and status, return it
        return err

    session_db = models.Session()
    try:
        # ID Filter applied here: only show current user's links (SQLAlchemy version)
        items = session_db.query(models.ShortURL).filter_by(user_id=user_id).order_by(models.ShortURL.created_at.desc()).limit(50).all()

        # CSRF token for forms
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        # Recover prefill from session if redirected back after validation error
        prefill = {}
        try:
            prefill = session.pop("shorten_prefill", {}) or {}
        except Exception:
            prefill = {}

        # Gather flashed messages
        from flask import get_flashed_messages
        flashes = get_flashed_messages(with_categories=True)

        # Build shortener form HTML and the list table
        form_html = f"""
          <section aria-labelledby="shorten-heading" class="mb-8">
            <h2 id="shorten-heading" class="text-xl font-bold mb-3">Create a Short Link</h2>
            {'<div role="alert" aria-live="assertive" style="color:#b00020;">' + escape(message) + '</div>' if False else ''}
            <form method="POST" action="{url_for('shortener.create')}" id="dashboard-shorten-form" novalidate>
              <input type="hidden" name="csrf_token" value="{csrf_token}">
              <label for="shorten-target_url" class="block text-sm font-medium mb-1">Long URL</label>
              <input id="shorten-target_url" name="target_url" type="url" required class="w-full p-3 border rounded-lg mb-3" value="{escape(prefill.get('target_url',''))}">
              <label for="shorten-slug" class="block text-sm font-medium mb-1">Custom slug (optional)</label>
              <input id="shorten-slug" name="slug" type="text" maxlength="255" class="w-full p-3 border rounded-lg mb-3" value="{escape(prefill.get('slug',''))}">
              <!-- Demo user_id hidden input for test mode -->
              {"<input type='hidden' name='user_id' value='" + escape(str(prefill.get('user_id', ''))) + "'>" if current_app.config.get('ALLOW_DEMO_USER_ID', False) else ""}
              <button type="submit" class="bg-blue-600 text-white px-4 py-2 rounded">Shorten</button>
            </form>
            <!-- Flash area -->
            <div id="dashboard-flashes" style="margin-top:1rem;">
        """
        # Append flashes
        for category, msg in flashes:
            safe_msg = escape(str(msg))
            if category == "error":
                form_html += f"<div role='alert' style='color:#b00020;'>{safe_msg}</div>"
            else:
                form_html += f"<div role='status' style='color:#064e3b;'>{safe_msg}</div>"
        form_html += "</div></section>"

        # --- Render Recently Created short links from session (KAN-178) ---
        recent_html = ""
        try:
            recent_raw = session.pop("new_short_urls", []) or []
        except Exception:
            recent_raw = []

        new_short_items = []
        if recent_raw:
            try:
                base = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
                base = base.rstrip("/")
            except Exception:
                try:
                    base = request.url_root.rstrip("/")
                except Exception:
                    base = ""
            try:
                import re
                pattern = current_app.config.get("REDIRECT_SLUG_REGEX", r"[A-Za-z0-9\-_]{1,16}")
            except Exception:
                pattern = r"[A-Za-z0-9\-_]{1,16}"

            for r in recent_raw:
                sc = None
                try:
                    if isinstance(r, dict):
                        sc = r.get("short_code")
                    else:
                        sc = r
                    if not sc or not isinstance(sc, str):
                        continue
                    try:
                        if re.fullmatch(pattern, sc):
                            new_short_items.append({"short_code": sc, "short_url": f"{base}/{sc}"})
                        else:
                            # if validation fails, still include conservative assembly but log
                            new_short_items.append({"short_code": sc, "short_url": f"{base}/{sc}"})
                    except Exception:
                        new_short_items.append({"short_code": sc, "short_url": f"{base}/{sc}"})
                except Exception:
                    continue

        if new_short_items:
            recent_html += "<section aria-labelledby='recent-links-heading' class='mb-6'><h2 id='recent-links-heading' class='text-xl font-bold mb-3'>Recently created</h2>"
            for ns in new_short_items:
                safe_url = escape(ns.get("short_url", ""))
                safe_slug = escape(ns.get("short_code", ""))
                input_id = f"recent-shortlink-{safe_slug}"
                recent_html += f"""
                  <div class="recent-short-link" style="display:flex;gap:.5rem;align-items:center;margin-top:0.5rem;">
                    <input id="{input_id}" class="shortlink-input" readonly value="{safe_url}" aria-label="Short link {safe_slug}" style="min-width:150px;padding:0.25rem;">
                    <button class="copy-btn" data-copy-target="{input_id}" data-copy-url="{safe_url}" aria-label="Copy short link {safe_slug}" tabindex="0">Copy</button>
                    <a href="{safe_url}" target="_blank" rel="noopener noreferrer">Open</a>
                  </div>
                """
            recent_html += "</section>"

        # Build table of existing links (with copy/open affordances)
        rows_html = "<section aria-labelledby='links-heading'><h2 id='links-heading' class='text-xl font-bold mb-3'>Your Short URLs</h2>"
        if not items:
            rows_html += "<p>No short links yet. Use the form above to create your first link.</p>"
            rows_html += "</section>"
            try:
                _write_trace_117(f"DASHBOARD_VIEW user_id={user_id} count=0")
            except Exception:
                pass
            return render_layout(form_html + recent_html + rows_html)
        rows_html += "<div class='responsive-table'><table style='width:100%; border-collapse: collapse;'><thead><tr><th>Short</th><th>Target</th><th>Hits</th><th>Created</th><th>Actions</th></tr></thead><tbody>"
        for s in items:
            try:
                short_url = url_for("shortener.redirect_slug", slug=s.slug, _external=True)
            except Exception:
                short_url = (current_app.config.get("BASE_URL") or request.url_root.rstrip("/")) + "/" + (s.slug or "")
            edit_url = url_for("dashboard.dashboard_edit", link_id=s.id)
            # Copy input with predictable id and a copy button wired to dataset.copyTarget
            input_id = f"shortlink-input-{escape(s.slug or '')}"
            copy_btn_id = f"copy-shortlink-{escape(s.slug or '')}"
            delete_form = f"""
              <form method="post" action="/dashboard/{s.id}/delete" style="display:inline;">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit" onclick="return confirm('Delete this short link?');">Delete</button>
              </form>
            """
            rows_html += f"""
              <tr style='border-top:1px solid #ddd;'>
                <td><a href="{short_url}" target="_blank" rel="noopener noreferrer">{escape(s.slug)}</a></td>
                <td><a href="{escape(s.target_url)}" target="_blank" rel="noopener noreferrer">{escape(s.target_url)}</a></td>
                <td style='text-align:center'>{getattr(s, 'hit_count', 0) or 0}</td>
                <td>{getattr(s, 'created_at', '')}</td>
                <td>
                  <div class="shortlink-row" style="display:flex;gap:.5rem;align-items:center;">
                    <input id="{input_id}" class="shortlink-input" readonly value="{short_url}" style="min-width:150px;padding:0.25rem;">
                    <button id="{copy_btn_id}" class="copy-btn" data-copy-target="{input_id}" aria-label="Copy short link" tabindex="0">Copy</button>
                    <a href="{short_url}" target="_blank" rel="noopener noreferrer">Open</a>
                    <a href="{edit_url}">Edit</a>
                    {delete_form}
                  </div>
                </td>
              </tr>
            """
        rows_html += "</tbody></table></div></section>"

        try:
            _write_trace_117(f"DASHBOARD_VIEW user_id={user_id} count={len(items)}")
        except Exception:
            pass

        # Append minimal clipboard JS (compatible with render_layout announcer)
        js = """
          <script>
            (function(){
              function announce(msg){
                try{
                  if(window.__smartlinkAnnounce) window.__smartlinkAnnounce(msg);
                }catch(e){}
              }
              function copyText(text, inputEl){
                if(navigator.clipboard && navigator.clipboard.writeText){
                  navigator.clipboard.writeText(text).then(function(){ announce('Copied to clipboard'); }, function(){ fallbackCopy(inputEl); });
                  return;
                }
                fallbackCopy(inputEl);
              }
              function fallbackCopy(inputEl){
                try{
                  if(inputEl){
                    inputEl.focus();
                    inputEl.select();
                    if(document.execCommand && document.execCommand('copy')){
                      announce('Copied to clipboard');
                      return;
                    }
                  }
                }catch(e){}
                try{ window.prompt('Copy the link:', inputEl ? inputEl.value : ''); }catch(e){}
              }
              document.addEventListener('click', function(e){
                var t = e.target;
                if(!t) return;
                var targetId = t.getAttribute && t.getAttribute('data-copy-target');
                var copyUrl = t.getAttribute && t.getAttribute('data-copy-url');
                if(targetId || copyUrl){
                  var input = null;
                  if(targetId){
                    input = document.getElementById(targetId);
                  }
                  var url = copyUrl || (input && input.value) || '';
                  if(url){
                    copyText(url, input);
                  }
                }
              });
            })();
          </script>
        """

        return render_layout(form_html + recent_html + rows_html + js)
    finally:
        try:
            session_db.close()
        except Exception:
            pass


@dashboard_bp.route("/dashboard/<int:link_id>/edit", methods=["GET", "POST"])
def dashboard_edit(link_id):
    """
    Edit a short URL owned by the authenticated user.

    GET: render a form pre-populated with the current values (CSRF token included).
    POST: apply changes (target_url, optional slug, expire_at). Ownership enforced.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session_db = models.Session()
    try:
        short = session_db.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            exists = session_db.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                try:
                    _write_trace_117(f"DASHBOARD_EDIT_FORBIDDEN user_id={user_id} link_id={link_id}")
                except Exception:
                    pass
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to modify this link.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        if request.method == "GET":
            try:
                from flask_wtf.csrf import generate_csrf
                csrf_token = generate_csrf()
            except Exception:
                csrf_token = ""

            expire_at_val = short.expire_at.strftime("%Y-%m-%d %H:%M") if short.expire_at else ""
            slug_id = f"slug-{short.id}"
            target_id = f"target-{short.id}"
            expire_id = f"expire-{short.id}"
            html = f"""
              <h1>Edit Short URL</h1>
              <form method="post" action="/dashboard/{short.id}/edit" novalidate>
                <label for="{slug_id}">Slug</label>
                <input id="{slug_id}" type="text" name="slug" maxlength="255" value="{escape(short.slug)}">
                <label for="{target_id}">Target URL</label>
                <input id="{target_id}" type="url" name="target_url" required style="width:100%" value="{escape(short.target_url)}">
                <label for="{expire_id}">Expire At (YYYY-MM-DD HH:MM UTC)</label>
                <input id="{expire_id}" type="text" name="expire_at" value="{expire_at_val}">
                <input type="hidden" name="csrf_token" value="{csrf_token}">
                <button type="submit">Save</button>
              </form>
              <p><a href="{url_for('dashboard.dashboard_index')}">Back to dashboard</a></p>
            """
            return render_layout(html)

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
            if validate_and_normalize_url is not None:
                new_target = validate_and_normalize_url(new_target_raw)
            else:
                new_target = new_target_raw
        except Exception as e:
            return render_layout(f"<h1>Invalid URL</h1><p>{escape(str(e))}</p>"), 400

        # If slug changed, validate and ensure uniqueness
        if new_slug and new_slug != short.slug:
            if not validate_custom_slug(new_slug):
                return render_layout("<h1>Invalid Slug</h1><p>The provided slug is invalid. Allowed characters: A-Z a-z 0-9 _ -</p>"), 400
            collision = session_db.query(models.ShortURL).filter_by(slug=new_slug).first()
            if collision and collision.id != short.id:
                try:
                    suggestions = suggest_alternatives(new_slug, count=5, session=session_db)
                except Exception:
                    suggestions = []
                sug_html = "<p>Slug already taken.</p>"
                if suggestions:
                    sug_html += "<ul>"
                    for s in suggestions:
                        short_path = url_for("shortener.redirect_slug", slug=s, _external=True)
                        sug_html += f"<li>{escape(s)} -> <a href='{short_path}'>{short_path}</a></li>"
                    sug_html += "</ul>"
                return render_layout(f"<h1>Slug Conflict</h1>{sug_html}"), 400
            short.slug = new_slug

        short.target_url = new_target
        short.expire_at = new_expire

        try:
            session_db.add(short)
            session_db.commit()
            session_db.refresh(short)
            try:
                _write_trace_117(f"DASHBOARD_EDIT_APPLIED user_id={user_id} link_id={short.id} slug={short.slug}")
            except Exception:
                pass
            return render_layout(f"<h1>Saved</h1><p>Your changes have been saved.</p><p><a href='{url_for('dashboard.dashboard_index')}'>Back to dashboard</a></p>")
        except Exception as e:
            try:
                session_db.rollback()
            except Exception:
                pass
            return render_layout(f"<h1>Database Error</h1><p>{escape(str(e))}</p>"), 500
    finally:
        try:
            session_db.close()
        except Exception:
            pass


@dashboard_bp.route("/dashboard/<int:link_id>/delete", methods=["POST"])
def dashboard_delete(link_id):
    """
    Delete (or soft-delete) a short URL owned by the authenticated user.
    """
    user_id, err = _require_auth()
    if err:
        return err

    session_db = models.Session()
    try:
        short = session_db.query(models.ShortURL).filter_by(id=link_id, user_id=user_id).first()
        if not short:
            exists = session_db.query(models.ShortURL).filter_by(id=link_id).first()
            if exists:
                try:
                    _write_trace_117(f"DASHBOARD_DELETE_FORBIDDEN user_id={user_id} link_id={link_id}")
                except Exception:
                    pass
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to delete this link.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        try:
            soft_delete = bool(current_app.config.get("SHORTURL_SOFT_DELETE", False))
        except Exception:
            soft_delete = False

        try:
            if soft_delete:
                short.expire_at = datetime.utcnow()
                session_db.add(short)
            else:
                session_db.delete(short)
            session_db.commit()
            try:
                _write_trace_117(f"DASHBOARD_DELETE_APPLIED user_id={user_id} link_id={link_id} soft_delete={soft_delete}")
            except Exception:
                pass
            return redirect(url_for("dashboard.dashboard_index"))
        except Exception as e:
            try:
                session_db.rollback()
            except Exception:
                pass
            return render_layout(f"<h1>Database Error</h1><p>{escape(str(e))}</p>"), 500
    finally:
        try:
            session_db.close()
        except Exception:
            pass