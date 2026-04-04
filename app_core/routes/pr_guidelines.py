"""routes/pr_guidelines.py - PR checklist & surgical update guidelines (KAN-139)

This surgical blueprint exposes read-only guidance for contributors and reviewers:
 - GET /tools/pr-guidelines         -> HTML page rendering the PR checklist and guidance (wrapped via render_layout)
 - GET /tools/pr-guidelines/raw     -> Raw .github/PULL_REQUEST_TEMPLATE.md content (text/plain)

Design notes:
 - This file is intentionally self-contained and defensive.
 - It writes a best-effort trace to trace_KAN-139.txt on important interactions (Architectural Memory).
 - No forms are rendered here; when forms are present elsewhere they must include CSRF hidden input per guardrails.
"""

from flask import Blueprint, current_app, request, Response
from utils.templates import render_layout
import os
import time

pr_guidelines_bp = Blueprint("pr_guidelines", __name__)

TRACE_FILE = "trace_KAN-139.txt"
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".github", "PULL_REQUEST_TEMPLATE.md")


def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        # Trace writes are best-effort
        pass


# Module import-time trace to aid Architectural Memory
try:
    _trace("PR_GUIDELINES_MODULE_LOADED")
except Exception:
    pass


@pr_guidelines_bp.route("/pr-guidelines", methods=["GET"])
def pr_guidelines_page():
    """
    Render a human-friendly page showing the PR template/checklist and guidance.
    This is read-only and intended for developers/reviewers to consult the surgical update rules.
    """
    # Read the template content if available (best-effort)
    tmpl = ""
    try:
        if os.path.exists(TEMPLATE_PATH):
            try:
                tmpl = open(TEMPLATE_PATH, "r", encoding="utf-8").read()
            except Exception:
                tmpl = ""
    except Exception:
        tmpl = ""

    html_parts = []
    html_parts.append("<h1>PR Checklist & Surgical Update Guidelines (KAN-139)</h1>")
    html_parts.append("<p>This page surfaces the repository's Pull Request template and the mandatory checklist that reviewers must verify before merging surgical changes.</p>")
    html_parts.append("<p><strong>Key expectations:</strong></p>")
    html_parts.append("<ul>")
    html_parts.append("<li>PRs must be surgical: modify only the minimal set of files required for the ticket.</li>")
    html_parts.append("<li>All forms must include an explicit CSRF hidden input: <code>&lt;input type=\"hidden\" name=\"csrf_token\" value=\"...\"&gt;</code></li>")
    html_parts.append("<li>All user-owned DB queries must apply the ID Filter rule: <code>filter_by(..., user_id=current_user_id)</code></li>")
    html_parts.append("<li>All HTML responses must be wrapped with the <code>render_layout()</code> helper.</li>")
    html_parts.append("<li>CI enforces an app entry-point smoke test and a static scoping linter run before merge.</li>")
    html_parts.append("</ul>")

    # Accessibility checklist addition (KAN-143)
    html_parts.append("<h2>Accessibility Checklist (Reviewer)</h2>")
    html_parts.append("<ul>")
    html_parts.append("<li>All forms include explicit <code>&lt;label for=&gt;</code> with a matching <code>id</code> on inputs.</li>")
    html_parts.append("<li>Password inputs reference a descriptive hint via <code>aria-describedby</code> where present (e.g., strength meter).</li>")
    html_parts.append("<li>Pages include a visible or keyboard-accessible 'Skip to content' link (first focusable control).</li>")
    html_parts.append("<li>Interactive controls (copy buttons, toggles) are keyboard-focusable and have accessible names (<code>aria-label</code> or visible text).</li>")
    html_parts.append("<li>Responsive meta viewport present and basic layout adapts to mobile.</li>")
    html_parts.append("<li>Contrast and focus outlines are verified; critical images include <code>alt</code> attributes.</li>")
    html_parts.append("</ul>")

    html_parts.append("<h2>Pull Request Template (preview)</h2>")
    if tmpl:
        # Render the markdown inside a <pre> block for readable preview
        safe_tmpl = "<pre style='white-space:pre-wrap; background:#f8f8f8; padding:1rem; border:1px solid #eee;'>" + (tmpl.replace("<", "&lt;").replace(">", "&gt;")) + "</pre>"
        html_parts.append(safe_tmpl)
        html_parts.append("<p><a href='/tools/pr-guidelines/raw'>View raw template</a></p>")
    else:
        html_parts.append("<p><em>Pull request template file not found in repository (.github/PULL_REQUEST_TEMPLATE.md).</em></p>")

    # Guidance footer
    html_parts.append("<h2>When to expand surgical scope</h2>")
    html_parts.append("<p>If your work necessarily touches multiple modular boundaries (e.g., models.py and routes/), include a clear <strong>SURGICAL RATIONALE</strong> in the PR describing why cross-cutting changes are required and reference new Jira stories for follow-ups. Large refactors are forbidden in a single PR.</p>")

    # Trace the page view for Architectural Memory
    try:
        _trace(f"PR_GUIDELINES_VIEW remote={request.remote_addr} path={request.path}")
    except Exception:
        pass

    return render_layout("\n".join(html_parts))


@pr_guidelines_bp.route("/pr-guidelines/raw", methods=["GET"])
def pr_guidelines_raw():
    """
    Return the raw PR template as plain text for copy-paste into new PRs.
    This endpoint is read-only and best-effort: falls back to a small built-in template if the file is missing.
    """
    content = None
    try:
        if os.path.exists(TEMPLATE_PATH):
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception:
        content = None

    if not content:
        # Fallback minimal template (kept short)
        content = (
            "PULL REQUEST TEMPLATE (KAN-139)\n\n"
            "SUMMARY of CHANGE:\n"
            "- One-line summary:\n"
            "- Files changed:\n\n"
            "CHECKLIST:\n"
            "- [ ] I confirm this PR is surgical.\n"
            "- [ ] I ran the app.py smoke test and pasted the output.\n"
            "- [ ] All forms include CSRF token.\n"
            "- [ ] ID Filter check applied for user-owned queries.\n"
            "- [ ] All HTML uses render_layout().\n"
        )

    # Trace the raw template access
    try:
        _trace(f"PR_GUIDELINES_RAW_ACCESS remote={request.remote_addr} length={len(content)}")
    except Exception:
        pass

    return Response(content, content_type="text/plain; charset=utf-8")
--- END FILE: routes/pr_guidelines.py ---