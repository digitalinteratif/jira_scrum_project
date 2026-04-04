"""
Playwright-based UI navigation smoke test (KAN-151 / AC2).

Behavior:
 - Uses Playwright sync API (no pytest-playwright fixture required).
 - Navigates to BASE_URL (env; default http://localhost:5000).
 - Verifies the landing page responds (no 500/404).
 - Clicks the Login link and Register/Get Started link and verifies the resulting pages.
 - Produces a structured JSON report at REPORT_PATH (env; default reports/ui_navigation_report.json).

Notes:
 - Test is intended to be executed via `pytest tests/test_ui_navigation.py` (satisfies AC2)
 - The test writes a JSON report with: overall pass bool, per-step entries, and trace details.
"""

import os
import json
import time
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
REPORT_PATH = os.environ.get("REPORT_PATH", "reports/ui_navigation_report.json")
# Ensure reports directory present
os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)

def _write_report(report):
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    except Exception:
        pass

def run_checks():
    report = {
        "base_url": BASE_URL,
        "started_at": time.time(),
        "steps": [],
        "passed": True,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            # 1) GET base URL
            step = {"name": "navigate_home", "ok": False, "detail": None}
            try:
                resp = page.goto(BASE_URL, timeout=8000)
                status = resp.status if resp else None
                title = page.title() if page else ""
                step["detail"] = {"status": status, "title": title}
                # Assert not a server error / not 404
                if status and int(status) < 500 and int(status) != 404:
                    step["ok"] = True
                else:
                    step["ok"] = False
                report["steps"].append(step)
            except Exception as e:
                step["detail"] = {"error": str(e)}
                report["steps"].append(step)
                report["passed"] = False
                # abort further steps
                _write_report(report)
                return report

            # 2) Click Login (accept several possible link selectors)
            step = {"name": "click_login", "ok": False, "detail": None}
            try:
                # Try several selectors defensively
                clicked = False
                for sel in ["a[href*='/login']", "text=Log In", "text=Sign In", "a:has-text('Log In')", "a:has-text('Sign In')"]:
                    try:
                        el = page.locator(sel)
                        if el.count() and el.first().is_visible():
                            # capture navigation response
                            with page.expect_navigation(timeout=6000):
                                el.first().click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError("Login link not found")
                # verify current URL path ends with /login or contains /auth/login
                cur = page.url
                step["detail"] = {"url": cur, "status": page.evaluate("document.readyState")}
                if "/login" in cur:
                    step["ok"] = True
                else:
                    step["ok"] = False
                report["steps"].append(step)
            except Exception as e:
                step["detail"] = {"error": str(e)}
                report["steps"].append(step)
                report["passed"] = False
                _write_report(report)
                return report

            # 3) Go back to home if necessary
            try:
                page.goto(BASE_URL, timeout=6000)
            except Exception:
                pass

            # 4) Click Get Started / Register
            step = {"name": "click_register", "ok": False, "detail": None}
            try:
                clicked = False
                for sel in ["a[href*='/register']", "text=Get Started", "text=Sign Up", "a:has-text('Get Started')", "a:has-text('Sign Up')"]:
                    try:
                        el = page.locator(sel)
                        if el.count() and el.first().is_visible():
                            with page.expect_navigation(timeout=6000):
                                el.first().click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    raise RuntimeError("Register/Get Started link not found")
                cur = page.url
                step["detail"] = {"url": cur}
                if "/register" in cur:
                    step["ok"] = True
                else:
                    step["ok"] = False
                report["steps"].append(step)
            except Exception as e:
                step["detail"] = {"error": str(e)}
                report["steps"].append(step)
                report["passed"] = False
                _write_report(report)
                return report

            # 5) Final check: ensure no 500 on /health (server-side health check)
            step = {"name": "health_endpoint", "ok": False, "detail": None}
            try:
                health_url = BASE_URL.rstrip("/") + "/health"
                resp = page.goto(health_url, timeout=4000)
                status = resp.status if resp else None
                step["detail"] = {"health_status": status}
                if status and int(status) == 200:
                    step["ok"] = True
                else:
                    step["ok"] = False
                report["steps"].append(step)
            except Exception as e:
                step["detail"] = {"error": str(e)}
                report["steps"].append(step)
                report["passed"] = False

            # Close browser
            try:
                context.close()
                browser.close()
            except Exception:
                pass

    except Exception as e:
        report["passed"] = False
        report["error"] = str(e)

    report["finished_at"] = time.time()
    # Overall pass is True only if every step.ok is True
    report["passed"] = all(s.get("ok") for s in report.get("steps", []))
    _write_report(report)
    return report


def test_ui_navigation():
    # Pytest-friendly wrapper that will assert overall pass and also write the JSON report
    r = run_checks()
    assert r.get("passed", False), f"UI navigation failed; see report at {REPORT_PATH}"


if __name__ == "__main__":
    # Allow running directly as a script for debugging
    out = run_checks()
    print("UI navigation completed. Report written to", REPORT_PATH)
    if not out.get("passed", False):
        raise SystemExit(2)