from flask import Blueprint, request, redirect, url_for, abort
from typing import Any

# Ensure the blueprint is defined at module import time BEFORE any decorator usage.
shortener_bp = Blueprint("shortener", __name__, url_prefix="/shorten")

# Note: to avoid circular imports with render_layout in home.py, import locally inside functions.

@shortener_bp.route("/", methods=["GET", "POST"])
def shorten_index():
    """
    Shorten URL page.
    - GET: render shorten form
    - POST: accept 'url' form field and redirect to created short url (mock behavior)
    Security notes:
    - Includes CSRF hidden input literal for string-based rendering.
    - Important: when making DB queries, always filter by owner_id/current_user_id per guardrails.
    """
    # Local import to avoid potential circular import with app_core.routes.home
    try:
        from app_core.routes.home import render_layout
    except Exception:
        # Minimal safe fallback layout if home.render_layout is temporarily unavailable.
        def render_layout(content_html: str, **ctx: Any) -> str:
            return (
                "<!doctype html><html><head>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Shorten URL</title>"
                "<link rel='stylesheet' href='/static/styles.css'>"
                "</head><body>"
                f"{content_html}"
                "</body></html>"
            )

    if request.method == "POST":
        # Basic POST handling (surgical minimal logic).
        # NOTE: Replace with proper DB logic ensuring logical scoping:
        # Link.query.filter_by(id=..., owner_id=current_user.id)
        url_to_shorten = request.form.get("url", "").strip()
        if not url_to_shorten:
            # Bad request: show error on same page
            content = """
            <main class="max-w-3xl mx-auto p-6">
              <h1 class="text-2xl font-semibold text-slate-800 mb-4">Shorten a URL</h1>
              <p class="text-sm text-red-600">Please provide a valid URL.</p>
              <form method="POST" action="/shorten" class="mt-4 space-y-4">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input aria-label="URL to shorten" name="url" type="text" placeholder="https://example.com"
                  class="w-full px-3 py-2 border rounded-md bg-slate-50 text-slate-900" />
                <button type="submit" class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700">Shorten</button>
              </form>
            </main>
            """
            return render_layout(content)
        # Mock shorten operation: in real code create Link row for current_user and return success url.
        short_code = "abcd12"  # placeholder; real logic should generate collision-resistant code
        # Redirect to a page showing created link (could be /shorten/<id> or similar)
        return redirect(url_for("shortener.shorten_result", code=short_code))

    # GET: render form
    content = """
    <main class="max-w-3xl mx-auto p-6">
      <h1 class="text-2xl font-semibold text-slate-800 mb-4">Shorten a URL</h1>
      <form method="POST" action="/shorten" class="mt-4 space-y-4">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <label class="block">
          <span class="text-slate-700">URL</span>
          <input aria-label="URL to shorten" name="url" type="text" placeholder="https://example.com"
            class="mt-1 block w-full px-3 py-2 border rounded-md bg-slate-50 text-slate-900" />
        </label>
        <button type="submit" class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700">Shorten</button>
      </form>
    </main>
    """
    return render_layout(content)


@shortener_bp.route("/result/<code>", methods=["GET"])
def shorten_result(code):
    """
    Simple result page for a shortened code.
    """
    try:
        from app_core.routes.home import render_layout
    except Exception:
        def render_layout(content_html: str, **ctx: Any) -> str:
            return (
                "<!doctype html><html><head>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Shorten Result</title>"
                "</head><body>"
                f"{content_html}"
                "</body></html>"
            )

    # Note: in real app, query DB with ownership filter when necessary.
    content = f"""
    <main class="max-w-3xl mx-auto p-6">
      <h1 class="text-2xl font-semibold text-slate-800 mb-4">Shortened URL</h1>
      <p class="text-slate-700">Your short code: <strong class="text-blue-600">{code}</strong></p>
      <p class="mt-4"><a class="text-blue-600 hover:underline" href="/">Back to Home</a></p>
    </main>
    """
    return render_layout(content)