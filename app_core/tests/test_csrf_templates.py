"""tests/test_csrf_templates.py - CSRF enforcement audit for template strings and rendered pages (KAN-124)"""

import os
import re
import time
from app import create_app

TRACE_FILE = "trace_KAN-124.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

def _find_form_blocks(text: str):
    """
    Return list of (form_open_index, form_block_text) for each <form ...>...</form> occurrence.
    Conservative: uses regex to find <form ...> and the nearest following </form>.
    """
    pattern = re.compile(r"(<form\b[^>]*>)(.*?)</form>", re.IGNORECASE | re.DOTALL)
    return [m.group(0) for m in pattern.finditer(text)]

def _form_has_csrf_marker(form_block: str) -> bool:
    """
    Return True if the form block contains:
      - explicit hidden input named csrf_token (name="csrf_token")
      - OR hidden_tag() call (form.hidden_tag() or hidden_tag())
    """
    if re.search(r'name\s*=\s*["\']csrf_token["\']', form_block):
        return True
    if "hidden_tag()" in form_block or "form.hidden_tag" in form_block:
        return True
    # Some code may generate CSRF inputs via .format() or f-strings; detect the variable name insertion
    if "csrf_token" in form_block:
        # presence of the token placeholder string is acceptable (e.g., value=\"{csrf_token}\")
        return True
    return False

def test_template_string_forms_include_csrf():
    """
    Scan inline template strings in routes/ and utils/templates.py for <form> blocks and assert
    each form contains CSRF marker.
    """
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    routes_dir = os.path.join(base, "routes")
    files_to_scan = []

    # Add all route modules
    for fn in os.listdir(routes_dir):
        if fn.endswith(".py"):
            files_to_scan.append(os.path.join(routes_dir, fn))

    # Add utils/templates.py as well (global render wrapper)
    files_to_scan.append(os.path.join(base, "utils", "templates.py"))

    missing = []

    for fpath in files_to_scan:
        try:
            text = open(fpath, "r", encoding="utf-8").read()
        except Exception as e:
            _trace(f"CSRF_SCAN_SKIP unable_to_read {fpath} err={str(e)}")
            continue

        form_blocks = _find_form_blocks(text)
        if not form_blocks:
            _trace(f"CSRF_SCAN_NOFORMS file={fpath} forms=0")
            continue

        for idx, block in enumerate(form_blocks):
            ok = _form_has_csrf_marker(block)
            if not ok:
                missing.append({"file": fpath, "form_index": idx, "snippet": block[:400]})
                _trace(f"CSRF_SCAN_MISSING file={fpath} form_index={idx}")

    if missing:
        # Write a human-friendly summary to trace and fail test
        _trace(f"CSRF_AUDIT_FAILED missing_count={len(missing)} details={missing}")
        # Also include summary in assertion message
        msgs = [f"{os.path.relpath(m['file'], base)}:form[{m['form_index']}] snippet={m['snippet']!r}" for m in missing]
        assert False, "CSRF audit failed for inline template forms:\n" + "\n".join(msgs)

    _trace("CSRF_AUDIT_PASSED static_scan")


def test_rendered_public_pages_include_csrf():
    """
    Render known public pages that include forms and assert they include the explicit CSRF hidden input.
    This validates runtime rendering of the token placeholder.
    """
    # Create app with memory sqlite to match other tests; ensure token generation path is available
    app = create_app(test_config={
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "JWT_SECRET": "test-jwt",
    })
    client = app.test_client()

    endpoints = [
        "/auth/register",
        "/shorten",
    ]
    missing = []

    for ep in endpoints:
        try:
            resp = client.get(ep)
        except Exception as e:
            _trace(f"CSRF_RENDER_ERR endpoint={ep} err={str(e)}")
            missing.append({"endpoint": ep, "error": str(e)})
            continue
        data = resp.get_data(as_text=True) if resp is not None else ""
        # If page has a form, assert presence of explicit hidden csrf input or hidden_tag
        if "<form" in data.lower():
            if re.search(r'name\s*=\s*["\']csrf_token["\']', data, re.IGNORECASE):
                _trace(f"CSRF_RENDER_OK endpoint={ep}")
                continue
            if "hidden_tag()" in data or "form.hidden_tag" in data:
                _trace(f"CSRF_RENDER_OK_hidden_tag endpoint={ep}")
                continue
            # fail for this endpoint
            missing.append({"endpoint": ep, "status_code": getattr(resp, "status_code", None), "snippet": data[:400]})
            _trace(f"CSRF_RENDER_MISSING endpoint={ep} status={getattr(resp, 'status_code', None)}")

    if missing:
        _trace(f"CSRF_RENDER_FAIL missing_count={len(missing)} details={missing}")
        msgs = [f"{m['endpoint']} status={m.get('status_code')} snippet={m.get('snippet')!r}" for m in missing]
        assert False, "CSRF audit failed for rendered pages:\n" + "\n".join(msgs)

    _trace("CSRF_RENDER_PASSED runtime_check")