#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import requests
import tempfile
import traceback
from datetime import datetime, UTC
from dotenv import load_dotenv

# --- FIX: Load the .env file so we can read Jira credentials ---
load_dotenv()

TRACE_FILE = "trace_KAN-152.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.now(UTC).isoformat()} {msg}\n")
    except Exception:
        pass

def run_cmd(cmd, cwd=None, timeout=None):
    _trace(f"RUN_CMD start: {' '.join(cmd)} cwd={cwd}")
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    stdout_len = len(proc.stdout) if proc.stdout else 0
    stderr_len = len(proc.stderr) if proc.stderr else 0
    _trace(f"RUN_CMD exit: rc={proc.returncode} out_len={stdout_len} err_len={stderr_len}")
    return proc

def wait_for_health(url="http://localhost:5000/", timeout=60):
    _trace(f"WAIT_FOR_HEALTH start url={url} timeout={timeout}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=3)
            # FIX: Check for standard 200 OK since we are hitting the homepage
            if r.status_code == 200:
                _trace(f"WAIT_FOR_HEALTH ok status={r.status_code}")
                return True
        except Exception as e:
            _trace(f"WAIT_FOR_HEALTH attempt err={str(e)[:200]}")
        time.sleep(1)
    _trace("WAIT_FOR_HEALTH timeout")
    return False

def run_pytest_and_capture(test_path="tests/test_ui_navigation.py"):
    cmd = ["pytest", "-q", test_path]
    proc = run_cmd(cmd)
    stdout_text = proc.stdout if proc.stdout else ""
    stderr_text = proc.stderr if proc.stderr else ""
    combined = f"STDOUT:\n{stdout_text}\n\nSTDERR:\n{stderr_text}\n"
    return proc.returncode, combined

def create_jira_ticket(parent_key: str, title: str, description: str):
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

    try:
        project_key = parent_key.split("-", 1)[0]
    except Exception:
        project_key = None

    issue_fields = {
        "project": {"key": project_key} if project_key else {},
        "summary": title,
        "description": description,
        "issuetype": {"name": "Story"},
        "labels": ["automated-ui-validation", "kan-152"],
    }

    try:
        new_issue = jira.create_issue(fields=issue_fields)
        new_key = getattr(new_issue, "key", None)
        _trace(f"JIRA_CREATED issue={new_key}")
    except Exception as e:
        _trace(f"JIRA_CREATE_FAILED err={str(e)}")
        return False, f"Failed to create Jira issue: {e}"

    attached = False
    try:
        if hasattr(jira, "add_issues_to_epic"):
            try:
                jira.add_issues_to_epic(parent_key, [new_key])
                attached = True
            except Exception as e:
                pass
        if not attached:
            try:
                jira.create_issue_link(type="Relates", inwardIssue=new_key, outwardIssue=parent_key)
                attached = True
            except Exception as e:
                pass
    except Exception as e:
        pass

    try:
        trans = jira.transitions(new_issue)
        target_names = {"To Do", "ToDo", "Idea", "Backlog", "Open"}
        chosen = None
        for t in trans:
            name = (t.get("name") or "").strip()
            if name in target_names:
                chosen = t
                break
        if chosen:
            jira.transition_issue(new_issue, chosen.get("id"))
    except Exception as e:
        pass

    return True, new_key

def main():
    # --- FIX: Force local testing regardless of what is in .env ---
    base = "http://localhost:5000"
    parent = os.environ.get("TARGET_JIRA_TICKET", "")
    _trace("VALIDATE_ORCH_STARTED")
    
    up_proc = run_cmd(["docker-compose", "up", "-d", "--build"])
    if up_proc.returncode != 0:
        up_stdout = (up_proc.stdout or "")[:1000]
        up_stderr = (up_proc.stderr or "")[:1000]
        _trace(f"DOCKER_COMPOSE_UP_FAILED rc={up_proc.returncode} out={up_stdout} err={up_stderr}")
        print("docker-compose up failed. See trace for details.")
        sys.exit(2)

    try:
        # --- FIX: Check the root homepage instead of a missing /health route ---
        healthy = wait_for_health(url=f"{base}/", timeout=60)
        if not healthy:
            _trace("ENV_NOT_HEALTHY after docker-compose up; proceeding to capture logs and abort tests")
            print("Container did not become healthy within timeout. See trace for details.")
            run_cmd(["docker-compose", "ps"])
            rc = 3
            up_stdout = up_proc.stdout if up_proc.stdout else ""
            up_stderr = up_proc.stderr if up_proc.stderr else ""
            description = f"Automated validation environment failed to reach healthy state at {base}/.\n\nCaptured docker-compose up stdout/stderr:\n\nSTDOUT:\n{up_stdout}\n\nSTDERR:\n{up_stderr}\n"
            if parent:
                create_jira_ticket(parent, "Validation environment failed to start (KAN-152)", description)
            sys.exit(rc)

        rc, out = run_pytest_and_capture("tests/test_ui_navigation.py")
        _trace(f"PYTEST_RC={rc} output_len={len(out)}")
        print(out)

        if rc != 0:
            _trace("PYTEST_FAILED preparing to create jira ticket")
            title = f"Automated UI validation failure (KAN-152) - {datetime.now(UTC).isoformat()}"
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
        _trace("TEARDOWN_START docker-compose down")
        down_proc = run_cmd(["docker-compose", "down", "-v", "--remove-orphans"])
        _trace(f"TEARDOWN_DONE rc={down_proc.returncode}")
        time.sleep(1)

if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        _trace(f"ORCHESTRATOR_ERROR {traceback.format_exc()}")
        print("Orchestrator encountered an exception:", e)
        rc = 10
    sys.exit(rc)