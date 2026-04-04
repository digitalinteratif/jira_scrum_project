"""routes/tools_lint.py - Blueprint exposing the scoping linter as an administrative/dev tool (KAN-125)

This module provides an optional HTTP interface to run the AST-based scoping linter (tools/lint_scoping.py)
against the repository or specific paths. It is intentionally guarded by a configuration flag
(app.config["ENABLE_DEV_TOOLS"] must be truthy) to avoid exposing developer tooling in production.

Routes:
 - GET  /tools/lint_scoping         -> run linter over repository root (or configured paths) and render HTML report (render_layout)
 - POST /tools/lint_scoping         -> accept JSON payload {"paths": ["path1","path2"], "format": "json"|"html"} to run linter
 - GET  /tools/lint_scoping.json    -> convenience JSON endpoint

Notes:
 - The linter detection honors inline "# noqa:scoping" comments for per-line whitelisting.
 - Writes a best-effort trace to trace_KAN-125.txt for Architectural Memory.
 - Defensive imports & fallbacks per project guardrails.
"""

from flask import Blueprint, request, current_app, jsonify
from utils.templates import render_layout
import os
import time
import json

tools_bp = Blueprint("tools", __name__)

TRACE_FILE = "trace_KAN-125.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

# Defensive import of linter implementation
try:
    from tools import lint_scoping
except Exception:
    lint_scoping = None  # signaling that the linter is not available


def _default_scan_paths():
    """
    Default paths to scan when no explicit paths provided.
    Use repository-root-like defaults to avoid scanning large / system directories.
    """
    cwd = os.getcwd()
    # Prefer scanning app's top-level python package directories
    candidates = ["routes", "utils", "models.py", "app.py", "tools", "tests"]
    existing = [os.path.join(cwd, p) for p in candidates if os.path.exists(os.path.join(cwd, p))]
    # Always fall back to cwd if nothing else found
    if not existing:
        existing = [cwd]
    return existing


@tools_bp.route("/lint_scoping", methods=["GET", "POST"])
def lint_scoping_view():
    """
    Run the scoping linter.

    Behavior:
      - If current_app.config['ENABLE_DEV_TOOLS'] is falsy -> return 404 to avoid exposing tooling.
      - GET: runs linter on default paths and returns HTML report (wrapped via render_layout).
      - POST: accepts JSON payload:
            {
              "paths": ["routes", "utils/validation.py"],
              "format": "json"   # optional; "html" or "json"
            }
          Returns either HTML or JSON based on 'format' or Accept header.
    """
    # Guard: allow only when dev tools explicitly enabled
    try:
        enabled = bool(current_app.config.get("ENABLE_DEV_TOOLS", False))
    except Exception:
        enabled = False

    if not enabled:
        # Do not expose this endpoint in production by default
        return ("Not Found", 404)

    _trace(f"LINT_INVOCATION method={request.method} remote={request.remote_addr}")

    if lint_scoping is None:
        _trace("LINT_ERROR linter_module_missing")
        return render_layout("<h1>Linter Not Available</h1><p>The scoping linter module is not installed in this environment.</p>"), 500

    # Determine requested paths
    paths = None
    requested_format = None

    if request.method == "POST":
        try:
            payload = request.get_json(silent=True) or {}
            paths = payload.get("paths", None)
            requested_format = payload.get("format", None)
        except Exception:
            paths = None
    else:
        # GET: check query args
        qs_paths = request.args.getlist("paths")
        if qs_paths:
            paths = qs_paths
        if "format" in request.args:
            requested_format = request.args.get("format")

    if not paths:
        paths = _default_scan_paths()

    # Normalize paths to absolute paths where possible (but keep relative for readability)
    norm_paths = []
    for p in paths:
        if isinstance(p, str) and p:
            # allow both repo-relative and absolute
            if os.path.isabs(p):
                norm_paths.append(p)
            else:
                norm_paths.append(os.path.join(os.getcwd(), p))
    if not norm_paths:
        norm_paths = _default_scan_paths()

    # Run linter
    try:
        issues = lint_scoping.run_on_paths(norm_paths)
    except Exception as e:
        _trace(f"LINT_ERROR run_failed err={str(e)}")
        return render_layout("<h1>Linter Error</h1><p>An error occurred while running the linter.</p>"), 500

    _trace(f"LINT_COMPLETED issues_count={len(issues)} scanned_paths={len(norm_paths)}")

    # Prepare JSON payload
    out = {
        "scanned_paths": norm_paths,
        "issues_count": len(issues),
        "issues": issues,
    }

    # Decide response format
    accept = request.headers.get("Accept", "")
    if requested_format and requested_format.lower() == "json":
        return jsonify(out)
    if request.path.endswith(".json") or "application/json" in accept.lower():
        return jsonify(out)

    # Render HTML report wrapped by render_layout
    html_parts = []
    html_parts.append(f"<h1>Scoping Linter Report</h1>")
    html_parts.append(f"<p>Scanned {len(norm_paths)} path(s). Issues found: <strong>{len(issues)}</strong></p>")
    if not issues:
        html_parts.append("<p>No issues detected. Good job!</p>")
    else:
        html_parts.append("<ol>")
        for i in issues:
            # format: path:lineno:col: message
            relpath = i.get("path", "")
            lineno = i.get("lineno", "")
            col = i.get("col_offset", "")
            msg = i.get("message", "")
            html_parts.append(f"<li><pre style='white-space:pre-wrap'>[{relpath}:{lineno}:{col}] {msg}</pre></li>")
        html_parts.append("</ol>")

    # Provide small form to rescan with JSON via POST (CSRF token is not strictly required here but include placeholder)
    try:
        from flask_wtf.csrf import generate_csrf
        csrf_token = generate_csrf()
    except Exception:
        csrf_token = ""

    form_html = f"""
      <h2>Run Linter</h2>
      <form method="post" action="/tools/lint_scoping" id="lint-form">
        <label>Paths (comma-separated, optional): <input type="text" name="paths" style="width:60%"></label>
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <button type="submit">Run</button>
      </form>
      <p>Tip: To get a machine-readable JSON report, POST JSON {{"paths":["routes"], "format":"json"}} to this endpoint.</p>
    """

    html = "\n".join(html_parts) + form_html
    return render_layout(html)
# --- END FILE: routes/tools_lint.py ---