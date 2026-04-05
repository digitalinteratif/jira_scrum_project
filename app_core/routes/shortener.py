from flask import Blueprint, render_template_string, request, redirect, url_for, current_app, session, flash
from html import escape
import sqlite3
import logging
import secrets

# Defensive imports for helpers
try:
    from app_core.db import get_db_connection, create_url_mapping
except Exception:
    get_db_connection = None
    create_url_mapping = None

try:
    from utils.validation import validate_and_normalize_url
except Exception:
    validate_and_normalize_url = None

try:
    from utils import shortener as shortener_utils
except Exception:
    shortener_utils = None

# short code service
try:
    from app_core import short_code_service
    ShortCodeGenerationError = short_code_service.ShortCodeGenerationError
except Exception:
    short_code_service = None
    ShortCodeGenerationError = Exception

# Defensive logger import
try:
    from app_core.app_logging import get_logger
    _logger = get_logger(__name__)
except Exception:
    _logger = logging.getLogger(__name__)

shortener_bp = Blueprint('shortener', __name__)

# GET handler: render the shorten form
@shortener_bp.route('/shorten', methods=['GET'])
def shorten():
    """
    Render the shorten form. The form posts to the POST handler named 'shortener.create'.
    """
    # Provide CSRF token via template rendering caller (csrf extension)
    return render_template_string("""
        <main class="max-w-3xl mx-auto p-6">
          <h1 class="text-2xl font-semibold text-slate-800 mb-4">Shorten a URL</h1>
          <form method="POST" action="{{ url_for('shortener.create') }}" id="dashboard-shorten-form" novalidate>
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div>
              <label for="shorten-target_url" class="block text-sm font-medium mb-2">Long URL</label>
              <input id="shorten-target_url" name="target_url" type="url" required placeholder="https://example.com/..." class="w-full p-3 border rounded-lg" value="">
            </div>
            <div class="mt-3">
              <label for="shorten-slug" class="block text-sm font-medium mb-2">Custom slug (optional)</label>
              <input id="shorten-slug" name="slug" type="text" maxlength="255" class="w-full p-3 border rounded-lg" value="">
            </div>
            {% if config_allow_demo %}
            <input type="hidden" name="user_id" value="{{ demo_user_id }}">
            {% endif %}
            <div class="mt-4">
              <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold">Shorten</button>
            </div>
          </form>
        </main>
    """, config_allow_demo=bool(current_app.config.get("ALLOW_DEMO_USER_ID", False)), demo_user_id=request.values.get("user_id", ""))


# POST handler: create short mapping
@shortener_bp.route('/shorten', methods=['POST'], endpoint='create')
def create():
    """
    POST handler that accepts form submissions from the dashboard shorten form.

    Flow:
      - Determine owner_user_id from session['user_id'] or demo user_id when allowed.
      - Validate target_url via utils.validation.validate_and_normalize_url.
      - If slug provided: attempt to insert; on duplicate return friendly message.
      - If slug not provided: try to generate a unique slug with retries.
      - On success: flash success and redirect to dashboard.
      - On validation error: preserve safe prefill in session, flash error, redirect to dashboard to re-render with prefill.
    """
    # Determine owner
    owner_user_id = None
    try:
        if session and session.get("user_id"):
            owner_user_id = int(session.get("user_id"))
        else:
            # allow demo user id for test harness when configured
            if current_app.config.get("ALLOW_DEMO_USER_ID", False):
                try:
                    owner_user_id = int(request.form.get("user_id", "0") or 0)
                    if owner_user_id <= 0:
                        owner_user_id = None
                except Exception:
                    owner_user_id = None
    except Exception:
        owner_user_id = None

    if owner_user_id is None:
        # Not authenticated and no demo user -> redirect to login
        _logger.debug("Shorten create attempted without authentication or demo user")
        return redirect(url_for("auth.login"))

    # Extract fields
    raw_target = (request.form.get("target_url") or "").strip()
    raw_slug = (request.form.get("slug") or "").strip()

    # Prefill preservation on errors via session (safe fields only)
    prefill = {"target_url": raw_target}
    if raw_slug:
        prefill["slug"] = raw_slug

    # Validate target URL
    if not raw_target:
        session['shorten_prefill'] = prefill
        flash("Please provide a target URL.", "error")
        return redirect(url_for("dashboard.dashboard_index"))

    # Validate & normalize
    if validate_and_normalize_url is None:
        # Defensive: validation helper missing
        session['shorten_prefill'] = prefill
        flash("Server validation unavailable. Please try again later.", "error")
        _logger.error("validate_and_normalize_url not available in environment")
        return redirect(url_for("dashboard.dashboard_index"))

    try:
        normalized = validate_and_normalize_url(raw_target)
    except Exception as e:
        # Validation error: preserve prefill and redirect to dashboard with error
        session['shorten_prefill'] = prefill
        msg = f"Invalid URL: {str(e)}"
        flash(msg, "error")
        _logger.info("Shorten validation failed for user_id=%s url=%s err=%s", owner_user_id, raw_target, str(e))
        return redirect(url_for("dashboard.dashboard_index"))

    # Prepare creation loop
    if get_db_connection is None or create_url_mapping is None or short_code_service is None:
        session['shorten_prefill'] = prefill
        flash("Server DB helpers unavailable. Please try again later.", "error")
        _logger.error("DB helpers or short_code_service not available for shorten.create")
        return redirect(url_for("dashboard.dashboard_index"))

    # Configurable generation parameters
    try:
        max_attempts = int(current_app.config.get("SHORTENER_MAX_GENERATION_ATTEMPTS", 8))
    except Exception:
        max_attempts = 8
    try:
        slug_length = int(current_app.config.get("SHORTENER_GENERATED_SLUG_LENGTH", 8))
    except Exception:
        slug_length = 8

    # Attempt to insert slug (if provided) or generate one
    try:
        if raw_slug:
            # Validate slug format defensively using utils.shortener.validate_custom_slug if available
            if shortener_utils and hasattr(shortener_utils, "validate_custom_slug"):
                try:
                    if not shortener_utils.validate_custom_slug(raw_slug):
                        session['shorten_prefill'] = prefill
                        flash("Provided slug is invalid. Use letters, numbers, '-' or '_'.", "error")
                        return redirect(url_for("dashboard.dashboard_index"))
                except Exception:
                    # Proceed conservatively
                    pass
            # Try to insert provided slug
            try:
                with get_db_connection() as conn:
                    create_url_mapping(conn, raw_slug, normalized, owner_user_id)
                # Success
                short_code = raw_slug
                _logger.info("Short link created", extra={"message_key": "shortener.created", "short_code": short_code, "owner_id": owner_user_id})
                base = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
                base = base.rstrip("/")
                short_url = f"{base}/{short_code}"
                # Queue for dashboard display (store only short_code in session; cap to last 10)
                try:
                    recent = session.get("new_short_urls", [])
                    if not isinstance(recent, list):
                        recent = []
                    recent.insert(0, {"short_code": short_code})
                    session["new_short_urls"] = recent[:10]
                    try:
                        _logger.info("Short link queued for dashboard display", extra={"message_key": "shortener.queue_display", "short_code": short_code})
                    except Exception:
                        pass
                except Exception:
                    # Do not break flow if session fails to write
                    try:
                        _logger.warning("Failed to write new_short_urls to session", extra={"message_key": "shortener.session_write_failed", "short_code": short_code})
                    except Exception:
                        pass

                flash(f"Short link created: {short_url}", "success")
                return redirect(url_for("dashboard.dashboard_index"))
            except sqlite3.IntegrityError:
                # Duplicate slug
                suggestions = []
                try:
                    if shortener_utils and hasattr(shortener_utils, "suggest_alternatives"):
                        # best-effort: pass None for session to avoid cross-dep; suggestions may be non-DB-checked
                        suggestions = shortener_utils.suggest_alternatives(raw_slug, count=5, session=None)
                except Exception:
                    suggestions = []
                session['shorten_prefill'] = prefill
                msg = "Slug already taken."
                if suggestions:
                    # include a few friendly suggestions
                    safe_sugs = [escape(s) for s in suggestions[:3]]
                    msg += " Suggestions: " + ", ".join(safe_sugs)
                flash(msg, "error")
                _logger.warning("Slug conflict when inserting provided slug", extra={"message_key": "shortener.slug_conflict", "slug": raw_slug, "user_id": owner_user_id})
                return redirect(url_for("dashboard.dashboard_index"))
        else:
            # Auto-generate slug with retries using centralized short_code_service
            try:
                with get_db_connection() as conn:
                    created_slug = short_code_service.allocate_short_code_and_insert(conn, normalized, owner_user_id)
            except ShortCodeGenerationError as e:
                session['shorten_prefill'] = prefill
                flash("Unable to generate unique slug; please try again.", "error")
                _logger.error("Failed to generate unique slug after %d attempts for user_id=%s", getattr(e, "attempts", 0), owner_user_id)
                return redirect(url_for("dashboard.dashboard_index"))
            except Exception as e:
                # Unexpected error
                session['shorten_prefill'] = prefill
                flash("Server error while creating short link. Please try again later.", "error")
                _logger.exception("Unexpected error during shorten.create for user_id=%s", owner_user_id)
                return redirect(url_for("dashboard.dashboard_index"))

            short_code = created_slug
            base = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
            base = base.rstrip("/")
            short_url = f"{base}/{short_code}"
            _logger.info("Short link created", extra={"message_key": "shortener.created", "short_code": short_code, "owner_id": owner_user_id})
            # Queue for dashboard display (store only short_code in session; cap to last 10)
            try:
                recent = session.get("new_short_urls", [])
                if not isinstance(recent, list):
                    recent = []
                recent.insert(0, {"short_code": short_code})
                session["new_short_urls"] = recent[:10]
                try:
                    _logger.info("Short link queued for dashboard display", extra={"message_key": "shortener.queue_display", "short_code": short_code})
                except Exception:
                    pass
            except Exception:
                try:
                    _logger.warning("Failed to write new_short_urls to session", extra={"message_key": "shortener.session_write_failed", "short_code": short_code})
                except Exception:
                    pass

            flash(f"Short link created: {short_url}", "success")
            return redirect(url_for("dashboard.dashboard_index"))
    except Exception as e:
        # Unexpected error
        session['shorten_prefill'] = prefill
        flash("Server error while creating short link. Please try again later.", "error")
        _logger.exception("Unexpected error during shorten.create for user_id=%s", owner_user_id)
        return redirect(url_for("dashboard.dashboard_index"))