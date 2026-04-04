#!/usr/bin/env python3
"""
validate_and_ticket.py - Orchestrator for Docker-based local validation and automated Jira ticketing (KAN-152)

Behavior:
 - Builds and brings up the Docker environment via `docker-compose up -d --build`.
 - Waits for http://localhost:5000/health to respond "ok" (polling health endpoint).
 - Runs the pytest Playwright suite (tests/test_ui_navigation.py).
 - Tears down the environment with `docker-compose down`.
 - If tests fail, creates a child Story under the Epic/Parent ticket specified by the TARGET_JIRA_TICKET env var
   using the python `jira` package. The new ticket includes the pytest output (stdout/stderr) in its description
   and attempts to set the workflow/status into a backlog-like state ("To Do" or "Idea") when possible.

Environment variables used:
 - TARGET_JIRA_TICKET   : e.g. "PROJ-123" (Epic or parent story in Jira)
 - JIRA_SERVER          : Jira server URL (e.g., https://yourcompany.atlassian.net)
 - JIRA_USER            : username / service account email for Jira API
 - JIRA_API_TOKEN       : API token/password for Jira basic auth
 - BASE_URL             : base URL to wait for (default http://localhost:5000)
"""

from __future__ import annotations
import os
import sys
import time
import subprocess
import requests
import tempfile
import traceback
from datetime import datetime

TRACE_FILE = "trace_KAN-152.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass

def run_cmd(cmd, cwd=None, timeout=None):
    _trace(f"RUN_CMD start: {' '.join(cmd)} cwd={cwd}")
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    _trace(f"RUN_CMD exit: rc={proc.returncode} out_len={len(proc.stdout)} err_len={len(proc.stderr)}")
    return proc

def wait_for_health(url="http://localhost:5000/health", timeout=60):
    _trace(f"WAIT_FOR_HEALTH start url={url} timeout={timeout}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                # prefer textual "ok" but accept 200 body
                body = (r.text or "").strip().lower()
                if body == "" or body == "ok" or r.status_code == 200:
                    _trace(f"WAIT_FOR_HEALTH ok status={r.status_code} body_snippet={(body[:80])}")
                    return True
        except Exception as e:
            _trace(f"WAIT_FOR_HEALTH attempt err={str(e)[:200]}")
        time.sleep(1)
    _trace("WAIT_FOR_HEALTH timeout")
    return False

def run_pytest_and_capture(test_path="tests/test_ui_navigation.py"):
    """
    Run pytest for the single Playwright test file and capture output.
    Returns (rc, stdout + stderr)
    """
    # Run in verbose mode to keep readable output
    cmd = ["pytest", "-q", test_path]
    proc = run_cmd(cmd)
    combined = f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}\n"
    return proc.returncode, combined

def create_jira_ticket(parent_key: str, title: str, description: str):
    """
    Best-effort creation of a Jira Story child under parent_key (Epic).
    This function is defensive: if Jira configuration is missing or API operations fail,
    it writes traces and returns False.
    """
    try:
        from jira import JIRA
    except Exception as e:
        _trace(f"JIRA_IMPORT_FAILED err={str(e)}")
        return False, f"jira lib import failed: {e}"

    JIRA_SERVER = os.environ.get("JIRA_SERVER")
    JIRA_USER = os.environ.get("JIRA_USER")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
    if not (JIRA_SERVER and JIRA_USER and JIRA_API_TOKEN and parent_key):
        _trace("JIRA_CREDENTIALS_MISSING cannot create ticket")
        return False, "JIRA credentials or TARGET_JIRA_TICKET missing in environment."

    try:
        jira = JIRA(server=JIRA_SERVER, basic_auth=(JIRA_USER, JIRA_API_TOKEN), timeout=30)
    except Exception as e:
        _trace(f"JIRA_CONNECT_FAILED err={str(e)}")
        return False, f"Failed to connect to JIRA: {e}"

    # Derive project key from parent ticket (assumes format PROJ-123)
    try:
        project_key = parent_key.split("-", 1)[0]
    except Exception:
        project_key = None

    # Build issue payload
    issue_fields = {
        "project": {"key": project_key} if project_key else {},
        "summary": title,
        "description": description,
        "issuetype": {"name": "Story"},
        "labels": ["automated-ui-validation", "kan-152"],
    }

    # Try to create the issue
    try:
        new_issue = jira.create_issue(fields=issue_fields)
        new_key = getattr(new_issue, "key", None)
        _trace(f"JIRA_CREATED issue={new_key}")
    except Exception as e:
        _trace(f"JIRA_CREATE_FAILED err={str(e)}")
        return False, f"Failed to create Jira issue: {e}"

    # Attempt to attach the created issue to the parent epic.
    # Prefer add_issues_to_epic when available (Atlassian Cloud), else attempt issue-link fallback.
    attached = False
    try:
        if hasattr(jira, "add_issues_to_epic"):
            try:
                jira.add_issues_to_epic(parent_key, [new_key])
                attached = True
                _trace(f"JIRA_ADDED_TO_EPIC via add_issues_to_epic parent={parent_key} child={new_key}")
            except Exception as e:
                _trace(f"JIRA_ADD_ISSUES_TO_EPIC_FAILED err={str(e)}")
        if not attached:
            # Fallback: create issue link (relate them). This does not set the Epic Link field but still records relation.
            try:
                jira.create_issue_link(type="Relates", inwardIssue=new_key, outwardIssue=parent_key)
                attached = True
                _trace(f"JIRA_ISSUE_LINKED parent={parent_key} child={new_key}")
            except Exception as e:
                _trace(f"JIRA_CREATE_ISSUE_LINK_FAILED err={str(e)}")
    except Exception as e:
        _trace(f"JIRA_ATTACH_EXCEPTION err={str(e)}")

    # Attempt to transition the new issue to a backlog-like status if transitions are available.
    try:
        # query transitions
        trans = jira.transitions(new_issue)
        # search for common backlog status names
        target_names = {"To Do", "ToDo", "Idea", "Backlog", "Open"}
        chosen = None
        for t in trans:
            name = (t.get("name") or "").strip()
            if name in target_names:
                chosen = t
                break
        if chosen:
            jira.transition_issue(new_issue, chosen.get("id"))
            _trace(f"JIRA_TRANSITIONED issue={new_key} to={chosen.get('name')}")
        else:
            _trace(f"JIRA_NO_BACKLOG_TRANSITION_AVAILABLE transitions={trans}")
    except Exception as e:
        _trace(f"JIRA_TRANSITION_FAILED err={str(e)}")

    return True, new_key

def main():
    base = os.environ.get("BASE_URL", "http://localhost:5000")
    parent = os.environ.get("TARGET_JIRA_TICKET", "")
    _trace("VALIDATE_ORCH_STARTED")
    # 1) Build & start containers
    up_proc = run_cmd(["docker-compose", "up", "-d", "--build"])
    if up_proc.returncode != 0:
        _trace(f"DOCKER_COMPOSE_UP_FAILED rc={up_proc.returncode} out={up_proc.stdout[:1000]} err={up_proc.stderr[:1000]}")
        print("docker-compose up failed. See trace for details.")
        sys.exit(2)

    try:
        # 2) Wait for health endpoint
        healthy = wait_for_health(url=f"{base}/health", timeout=60)
        if not healthy:
            _trace("ENV_NOT_HEALTHY after docker-compose up; proceeding to capture logs and abort tests")
            print("Container did not become healthy within timeout. See trace for details.")
            # attempt gather docker-compose ps for debug
            run_cmd(["docker-compose", "ps"])
            # Do not forget to tear down in finally
            rc = 3
            # create ticket describing failure to start if desired
            description = f"Automated validation environment failed to reach healthy state at {base}/health.\n\nCaptured docker-compose up stdout/stderr:\n\nSTDOUT:\n{up_proc.stdout}\n\nSTDERR:\n{up_proc.stderr}\n"
            if parent:
                create_jira_ticket(parent, "Validation environment failed to start (KAN-152)", description)
            sys.exit(rc)

        # 3) Run pytest Playwright tests
        rc, out = run_pytest_and_capture("tests/test_ui_navigation.py")
        _trace(f"PYTEST_RC={rc} output_len={len(out)}")
        print(out)

        # 4) If tests failed -> create Jira ticket with failure trace
        if rc != 0:
            _trace("PYTEST_FAILED preparing to create jira ticket")
            title = f"Automated UI validation failure (KAN-152) - {datetime.utcnow().isoformat()}"
            description = (
                f"Automated Playwright test run failed against local staging at {base}.\n\n"
                f"Test run output:\n\n{out}\n\n"
                "This issue was created automatically by validate_and_ticket.py.\n"
                "Please triage and create follow-up child tickets as needed."
            )
            ok, info = create_jira_ticket(parent, title, description)
            if ok:
                _trace(f"JIRA_TICKET_CREATED {info}")
                print(f"Created Jira ticket: {info}")
            else:
                _trace(f"JIRA_TICKET_CREATE_FAILED reason={info}")
                print("Failed to create Jira ticket automatically:", info)
            return rc

        _trace("PYTEST_PASSED all tests OK")
        print("UI validation passed.")
        return 0
    finally:
        # Tear down containers regardless of success/failure
        _trace("TEARDOWN_START docker-compose down")
        down_proc = run_cmd(["docker-compose", "down", "-v", "--remove-orphans"])
        _trace(f"TEARDOWN_DONE rc={down_proc.returncode}")
        # Slight pause to ensure containers truly stop
        time.sleep(1)

if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        _trace(f"ORCHESTRATOR_ERROR {traceback.format_exc()}")
        print("Orchestrator encountered an exception:", e)
        rc = 10
    sys.exit(rc)
--- END FILE: validate_and_ticket.py ---