#!/usr/bin/env python3
"""
validate_and_ticket.py - Staging orchestrator for local Docker + Playwright validation (KAN-151)

Usage:
  python validate_and_ticket.py            # uses .env in repo root
  python validate_and_ticket.py --no-tickets  # run validations but do not create Jira issues (dry-run)

Behavior:
  - Builds and starts docker-compose stack (app service).
  - Waits for app health endpoint to respond.
  - Runs the 10-second stability test inside the running container (app_core/tools/stability_test.py).
  - Runs pytest on tests/test_ui_navigation.py (Playwright test). The test writes a JSON report.
  - On failures, create Jira child story tickets under TARGET_JIRA_TICKET via jira.create_issue (if jira available).
  - Writes trace entries to trace_KAN-151.txt for Architectural Memory.
"""

from __future__ import annotations
import os
import sys
import time
import json
import subprocess
import argparse
import hashlib
from datetime import datetime
from pathlib import Path

# Dependency tolerant imports
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# Trace file
TRACE_FILE = "trace_KAN-151.txt"


def _trace(msg: str):
    ts = datetime.utcnow().isoformat()
    safe_msg = f"{ts} {msg}\n"
    try:
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(safe_msg)
    except Exception:
        # Best-effort only
        pass


def _mask_secret(s: str) -> str:
    if not s:
        return ""
    try:
        # mask middle of secret
        if len(s) <= 8:
            return s[:2] + "***"
        return s[:4] + ("*" * max(3, len(s) - 8)) + s[-4:]
    except Exception:
        return "****"

def _run(cmd, cwd=None, capture_output=False, env=None, check=False):
    _trace(f"CMD: {' '.join(cmd)} cwd={cwd or os.getcwd()}")
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=capture_output, text=True, env=env, check=check)
        _trace(f"CMD_DONE rc={res.returncode}")
        if capture_output:
            return res.returncode, res.stdout, res.stderr
        return res.returncode, None, None
    except Exception as e:
        _trace(f"CMD_ERROR {str(e)}")
        return 1, None, str(e)


def load_env(dotenv_path: str = ".env"):
    if load_dotenv is not None:
        try:
            load_dotenv(dotenv_path)
            _trace(f".env loaded from {dotenv_path}")
        except Exception as e:
            _trace(f".env load failed: {str(e)}")
    else:
        _trace("python-dotenv not available; relying on environment variables already set")


def wait_for_http(url: str, timeout: int = 30, interval: float = 0.5) -> bool:
    end = time.time() + timeout
    # Try requests if available, else fallback to urllib
    try:
        import requests
    except Exception:
        requests = None

    while time.time() < end:
        try:
            if requests:
                r = requests.get(url, timeout=3)
                if r.status_code < 500:
                    return True
            else:
                from urllib.request import Request, urlopen
                req = Request(url, headers={"User-Agent": "validate_and_ticket/1.0"})
                with urlopen(req, timeout=3) as resp:
                    if resp.status < 500:
                        return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def docker_compose_up():
    # Use docker-compose; call via subprocess. Respect DOCKER_COMPOSE environment override if present.
    cmd = ["docker-compose", "up", "--build", "-d"]
    rc, out, err = _run(cmd, capture_output=True)
    if rc != 0:
        _trace("docker-compose up failed")
        return False
    _trace("docker-compose up success")
    return True


def docker_compose_down():
    try:
        _run(["docker-compose", "down"], capture_output=True)
        _trace("docker-compose down executed")
    except Exception:
        pass


def run_stability_test_in_container():
    """
    Try to execute the project's stability test inside the running container via docker-compose exec.
    Fallback to running the stability script locally if exec fails.
    """
    # prefer docker-compose exec (non-interactive)
    cmd = ["docker-compose", "exec", "-T", "app", "python", "app_core/tools/stability_test.py"]
    rc, out, err = _run(cmd, capture_output=True)
    if rc == 0:
        _trace("stability_test passed inside container")
        return True, out or ""
    _trace(f"stability_test inside container returned rc={rc}; falling back to local invocation")
    # fallback: run local stability_test.py if present
    local_script = Path("app_core/tools/stability_test.py")
    if local_script.exists():
        rc2, out2, err2 = _run(["python", str(local_script)], capture_output=True)
        if rc2 == 0:
            _trace("local stability_test passed")
            return True, out2 or ""
        else:
            _trace(f"local stability_test failed rc={rc2}")
            return False, (out2 or "") + (err2 or "")
    else:
        _trace("stability_test not found locally; cannot perform 10s stability check")
        return False, ""


def run_pytest_ui(base_url: str, report_path: str):
    """
    Run pytest for the Playwright script. It is expected that the script writes its JSON report to 'report_path'.
    We call pytest so AC2 is satisfied.
    """
    env = os.environ.copy()
    env["BASE_URL"] = base_url
    env["REPORT_PATH"] = report_path
    # ensure pytest exists and run it
    rc, out, err = _run(["pytest", "-q", "tests/test_ui_navigation.py"], capture_output=True, env=env)
    _trace(f"pytest rc={rc}")
    # Regardless of rc, attempt to read report file
    return rc, out, err


def read_report(report_path: str):
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _trace(f"read_report error: {str(e)}")
        return None


def create_jira_tickets_from_report(report: dict, parent_key: str, jira_cfg: dict, dry_run=False):
    """
    For each failing test step, create a child Jira Story under parent_key.
    Use jira package (python-jira) if installed. Use create_issue(fields=...) as requested.
    Idempotency: attempt to search for existing issue with same signature summary; if found skip creation.
    """
    # dependency-tolerant import
    try:
        from jira import JIRA
    except Exception:
        _trace("jira package not available; skipping ticket creation (intended tickets recorded in trace)")
        # Record intended tickets
        if report:
            for step in report.get("steps", []):
                if not step.get("ok"):
                    summary = f"UI: {step.get('name')} failed in staging validation"
                    desc = json.dumps(step, indent=2)
                    _trace(f"INTENDED_TICKET summary={summary} description={desc}")
        return []

    jira_server = jira_cfg.get("server")
    jira_user = jira_cfg.get("username")
    jira_token = jira_cfg.get("api_token")
    project_key = jira_cfg.get("project_key")
    if not (jira_server and jira_user and jira_token and project_key and parent_key):
        _trace("jira configuration incomplete; skipping ticket creation")
        return []

    # connect
    try:
        jira_opts = {"server": jira_server}
        jira = JIRA(options=jira_opts, basic_auth=(jira_user, jira_token))
    except Exception as e:
        _trace(f"JIRA_CONNECT_FAILED {str(e)}")
        return []

    created = []
    # For each failing step, create one issue
    for step in report.get("steps", []):
        if step.get("ok"):
            continue
        summary = f"[AUTOVALIDATION] UI - {step.get('name')} failed on {parent_key}"
        # Create deterministic fingerprint to avoid duplicates
        fingerprint_input = summary + (json.dumps(step.get("detail", {}), sort_keys=True) or "")
        fid = hashlib.sha1(fingerprint_input.encode("utf-8")).hexdigest()[:8]
        summary_fpid = f"{summary} [{fid}]"

        description = (
            f"Automated UI validation failure detected by validate_and_ticket.py\n\n"
            f"Parent validation ticket: {parent_key}\n"
            f"Validation run time: {datetime.utcnow().isoformat()} UTC\n\n"
            f"Step: {step.get('name')}\n"
            f"Details:\n{json.dumps(step, indent=2)}\n\n"
            f"Playwright report: see attached or repo path: reports/ui_navigation_report.json\n"
        )

        # Idempotent check: search for existing issues with key fingerprint in summary
        jql = f'project = "{project_key}" AND summary ~ "{fid}"'
        try:
            existing = jira.search_issues(jql, maxResults=5)
            if existing:
                _trace(f"JIRA_TICKET_EXISTS fid={fid} issue={existing[0].key}")
                created.append({"step": step.get("name"), "skipped_existing": existing[0].key})
                continue
        except Exception:
            # search may fail due to permission; continue with best-effort creation
            pass

        # Build fields for issue creation. Parent linking behavior differs per Jira configuration.
        # Try to set parent field (works for Sub-tasks & some project configs); otherwise fall back to creating Story and linking.
        fields = {
            "project": {"key": project_key},
            "summary": summary_fpid,
            "description": description,
            "issuetype": {"name": "Story"},
        }
        # Try to attach parent (Epic) using 'parent' field where supported:
        try:
            fields["parent"] = {"key": parent_key}
        except Exception:
            pass

        if dry_run:
            _trace(f"JIRA_DRYRUN would create issue with fields: {json.dumps(fields, default=str)[:800]}")
            created.append({"step": step.get("name"), "dryrun": True, "summary": summary_fpid})
            continue

        try:
            new_issue = jira.create_issue(fields=fields)
            _trace(f"JIRA_CREATED issue={new_issue.key} step={step.get('name')}")
            # Attempt to set status to "Idea" if provided via transitions (best-effort)
            try:
                transitions = jira.transitions(new_issue)
                # find "Idea" transition id if present
                tid = None
                for t in transitions:
                    name = t.get("name", "").lower()
                    if "idea" in name or "backlog" in name:
                        tid = t.get("id")
                        break
                if tid:
                    jira.transition_issue(new_issue, tid)
                    _trace(f"JIRA_TRANSITIONED issue={new_issue.key} to Idea via transition id={tid}")
            except Exception as e:
                _trace(f"JIRA_TRANSITION_FAILED issue={getattr(new_issue, 'key', '<unknown>')} err={str(e)}")
            created.append({"step": step.get("name"), "issue": new_issue.key})
        except Exception as e:
            _trace(f"JIRA_CREATE_FAILED summary={summary_fpid} err={str(e)}")
            created.append({"step": step.get("name"), "error": str(e)})

    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate local staging (Docker + Playwright) and auto-ticket failures to Jira.")
    parser.add_argument("--no-tickets", action="store_true", help="Run validations but do not create Jira tickets (dry-run).")
    parser.add_argument("--report-path", type=str, default="reports/ui_navigation_report.json", help="Path to write/read the Playwright JSON report.")
    parser.add_argument("--base-url", type=str, default=None, help="Override base URL for validation (defaults to BASE_URL env or http://localhost:5000).")
    args = parser.parse_args(argv)

    # load env
    load_env()

    BASE_URL = args.base_url or os.environ.get("BASE_URL", "http://localhost:5000")
    REPORT_PATH = args.report_path

    TARGET_JIRA_TICKET = os.environ.get("TARGET_JIRA_TICKET", "")
    JIRA_SERVER = os.environ.get("JIRA_SERVER", "")
    JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")

    # Masked logging for trace
    _trace(f"VALIDATION_START base_url={BASE_URL} TARGET_JIRA_TICKET={TARGET_JIRA_TICKET} jira_server={JIRA_SERVER} jira_user={JIRA_USERNAME}")

    # 1) Start docker-compose
    ok = docker_compose_up()
    if not ok:
        _trace("COMPOSE_UP_FAILED aborting")
        docker_compose_down()
        return 2

    try:
        # 2) Wait for health endpoint
        health_url = BASE_URL.rstrip("/") + "/health"
        _trace(f"WAITING_FOR_HEALTH {health_url}")
        ready = wait_for_http(health_url, timeout=40)
        if not ready:
            _trace("HEALTHCHECK_FAILED aborting")
            # capture container logs for debugging
            _run(["docker", "logs", "kan151_staging_app"], capture_output=True)
            docker_compose_down()
            return 3
        _trace("HEALTHCHECK_OK")

        # 3) Run 10-second stability test inside container
        stable, stab_out = run_stability_test_in_container()
        _trace(f"STABILITY_TEST result={stable} output_snip={(stab_out or '')[:1000]}")
        if not stable:
            _trace("STABILITY_TEST_FAILED aborting further UI tests")
            # continue to run UI tests optionally depending on policy; here we abort and return failure
            docker_compose_down()
            return 4

        # 4) Run pytest Playwright UI test (AC2)
        rc, out, err = run_pytest_ui(BASE_URL, REPORT_PATH)
        _trace(f"PYTEST_DONE rc={rc}")
        # Read the JSON report
        report = read_report(REPORT_PATH)
        if report is None:
            _trace("REPORT_READ_FAILED; treat as test failure")
            # fallback: parse pytest output to detect failures
            docker_compose_down()
            return 5

        # 5) If failures, create Jira tickets (AC3) unless --no-tickets
        if not report.get("passed", False):
            _trace(f"UI_TESTS_FAILED; preparing tickets -- no_tickets={args.no_tickets}")
            jira_cfg = {"server": JIRA_SERVER, "username": JIRA_USERNAME, "api_token": JIRA_API_TOKEN, "project_key": JIRA_PROJECT_KEY}
            created = create_jira_tickets_from_report(report, TARGET_JIRA_TICKET, jira_cfg, dry_run=args.no_tickets)
            _trace(f"TICKETS_CREATED {created}")
            docker_compose_down()
            # return non-zero to indicate validation failure
            return 6
        else:
            _trace("UI_TESTS_PASSED")
    finally:
        # Always attempt to tear down compose stack
        docker_compose_down()

    _trace("VALIDATION_SUCCEEDED")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)