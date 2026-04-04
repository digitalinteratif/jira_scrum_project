"""routes/contributing.py - Contributor onboarding & developer docs (KAN-140)

Surgical blueprint to expose the repository's CONTRIBUTING.md for developer onboarding.

Routes:
 - GET  /tools/contributing      -> HTML page rendering CONTRIBUTING.md (wrapped via render_layout)
 - GET  /tools/contributing/raw  -> Raw CONTRIBUTING.md content (text/plain)

Design:
 - Best-effort trace to trace_KAN-140.txt for Architectural Memory.
 - Defensive: if docs/CONTRIBUTING.md is missing, fall back to an embedded copy of the essential guidance.
 - Read-only; no forms are rendered here (so CSRF rules are not applicable).
 - Uses render_layout for consistent UI wrapper.
"""

from flask import Blueprint, current_app, request, Response
from utils.templates import render_layout
import os
import time

contributing_bp = Blueprint("contributing", __name__)

TRACE_FILE = "trace_KAN-140.txt"
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "CONTRIBUTING.md")


def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # Best-effort only
        pass


# Module import-time trace to aid Architectural Memory
try:
    _trace("CONTRIBUTING_MODULE_LOADED")
except Exception:
    pass


@contributing_bp.route("/contributing", methods=["GET"])
def contributing_page():
    """
    Render a human-friendly page showing the developer onboarding CONTRIBUTING.md.
    This is read-only and intended for new contributors to consult the onboarding steps
    and project guardrails (CSRF, ID Filter rule, render_layout).
    """
    # Read the doc content if available (best-effort)
    content = ""
    try:
        if os.path.exists(TEMPLATE_PATH):
            try:
                content = open(TEMPLATE_PATH, "r", encoding="utf-8").read()
            except Exception:
                content = ""
    except Exception:
        content = ""

    html_parts = []
    html_parts.append("<h1>Contributor Onboarding & Developer Docs (CONTRIBUTING)</h1>")
    html_parts.append("<p>This page surfaces the repository's CONTRIBUTING.md to help new contributors get started.</p>")
    html_parts.append("<p><strong>Key topics covered:</strong></p>")
    html_parts.append("<ul>")
    html_parts.append("<li>Local setup and environment variables</li>")
    html_parts.append("<li>Running tests and migrations</li>")
    html_parts.append("<li>render_layout usage and UI wrapper</li>")
    html_parts.append("<li>CSRF token requirement for all forms</li>")
    html_parts.append("<li>ID Filter rule for user-scoped queries</li>")
    html_parts.append("<li>Surgical PR expectations and trace logging</li>")
    html_parts.append("</ul>")

    if content:
        # Render the markdown inside a <pre> block for readable preview (escaped)
        safe_content = "<pre style='white-space:pre-wrap; background:#f8f8f8; padding:1rem; border:1px solid #eee;'>" + (content.replace("<", "&lt;").replace(">", "&gt;")) + "</pre>"
        html_parts.append("<h2>CONTRIBUTING.md (preview)</h2>")
        html_parts.append(safe_content)
        html_parts.append("<p><a href='/tools/contributing/raw'>View raw CONTRIBUTING.md</a></p>")
    else:
        html_parts.append("<p><em>CONTRIBUTING.md file not found in repository (docs/CONTRIBUTING.md).</em></p>")
        html_parts.append("<p>Please consult the project's README or PR guidelines for onboarding steps.</p>")

    # Trace the page view for Architectural Memory
    try:
        _trace(f"CONTRIBUTING_VIEW remote={request.remote_addr} path={request.path} found={bool(content)}")
    except Exception:
        pass

    return render_layout("\n".join(html_parts))


@contributing_bp.route("/contributing/raw", methods=["GET"])
def contributing_raw():
    """
    Return the raw CONTRIBUTING.md as plain text for copy-paste by contributors.
    Falls back to a minimal inline template if the file is missing.
    """
    content = None
    try:
        if os.path.exists(TEMPLATE_PATH):
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception:
        content = None

    if not content:
        # Fallback minimal template
        content = (
            "CONTRIBUTING.md (KAN-140) - Developer Onboarding\n\n"
            "Quickstart:\n"
            " - Use Python 3.12.9\n"
            " - Set DATABASE_URL (sqlite or postgres)\n"
            " - Install dev deps and run pytest\n\n"
            "Key rules:\n"
            " - All HTML forms must include explicit CSRF hidden input named 'csrf_token'.\n"
            " - All user-owned queries must filter by user_id (ID Filter rule).\n"
            " - All HTML responses must be wrapped with render_layout().\n"
            " - PRs must be surgical: modify only the minimal set of files needed.\n"
            " - Record progress and agent interactions in trace_KAN-140.txt.\n"
        )

    # Trace the raw access
    try:
        _trace(f"CONTRIBUTING_RAW_ACCESS remote={request.remote_addr} length={len(content)}")
    except Exception:
        pass

    return Response(content, content_type="text/plain; charset=utf-8")
--- END FILE: routes/contributing.py ---