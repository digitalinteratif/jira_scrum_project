#!/usr/bin/env python3
"""
CI helper: validate PR branch name and presence of checklist/ticket references in PR body.

Exit codes:
  0 - OK
  1 - Validation failures (printed to stdout)
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

# Branch name regex aligned with docs/BRANCHING.md
BRANCH_REGEX = re.compile(r"^(feature|fix|chore|hotfix|release|docs|ci|test|perf|refactor)\/[A-Za-z0-9._-]+$")

# Minimum number of checklist items expected in PR body
MIN_CHECKLIST_BOXES = 3

# Keys to search for a ticket reference in PR body
TICKET_KEYS = ["related story", "related story / ticket", "primary ticket", "primary ticket:", "related story / Ticket", "primary ticket"]

def load_event() -> Dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        # When running locally for debug fallback to env variables if available
        print("ERROR: GITHUB_EVENT_PATH not set or file not found.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(event_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read GITHUB event JSON: {e}", file=sys.stderr)
        sys.exit(1)

def extract_pr_info(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    pr = event.get("pull_request", {}) or {}
    head = pr.get("head", {}) or {}
    head_ref = head.get("ref") or os.environ.get("GITHUB_HEAD_REF")
    title = pr.get("title", "") or ""
    body = pr.get("body", "") or ""
    user = pr.get("user", {}) or {}
    author = user.get("login", "")
    return {"head_ref": head_ref, "title": title, "body": body, "author": author}

def check_branch_name(branch: Optional[str]) -> Optional[str]:
    if not branch:
        return "Branch name could not be determined."
    if not BRANCH_REGEX.match(branch):
        return (
            f"Branch name '{branch}' does not follow the required pattern. "
            "Expected: <type>/<short-description> where type is one of "
            "(feature|fix|chore|hotfix|release|docs|ci|test|perf|refactor)."
        )
    return None

def check_pr_body_for_ticket(body: str) -> Optional[str]:
    lower = (body or "").lower()
    for key in TICKET_KEYS:
        if key in lower:
            return None
    # also accept presence of a ticket id like KAN-123
    if re.search(r"\bKAN-\d+\b", body or "", flags=re.IGNORECASE):
        return None
    return "PR body does not reference a ticket. Include a 'Related story / Ticket' section or a ticket id like 'KAN-123'."

def count_checklist_boxes(body: str) -> int:
    # Matches typical markdown checklist items: "- [ ]", "- [x]" with optional space after bracket
    matches = re.findall(r"-\s*\[[ xX]?\]", body or "")
    return len(matches)

def check_pr_body_checklist(body: str) -> Optional[str]:
    n = count_checklist_boxes(body or "")
    if n < MIN_CHECKLIST_BOXES:
        return (
            f"PR body appears to be missing the required checklist items. "
            f"Found {n} checklist box(es), expected at least {MIN_CHECKLIST_BOXES}. "
            "Please use the repository PR template and complete the checklist."
        )
    return None

def run_validations(pr_info: Dict[str, Optional[str]]) -> List[str]:
    errors: List[str] = []
    branch = pr_info.get("head_ref")
    title = pr_info.get("title") or ""
    body = pr_info.get("body") or ""

    br_err = check_branch_name(branch)
    if br_err:
        errors.append(br_err)

    ticket_err = check_pr_body_for_ticket(body)
    if ticket_err:
        errors.append(ticket_err)

    checklist_err = check_pr_body_checklist(body)
    if checklist_err:
        errors.append(checklist_err)

    # Encourage title to include ticket id (soft check): warn but not fail
    if not re.search(r"\bKAN-\d+\b", title or "", flags=re.IGNORECASE):
        print("NOTICE: PR title does not include a ticket id like 'KAN-123'. It's recommended to include the ticket id in the PR title.", file=sys.stderr)

    return errors

def main() -> int:
    event = load_event()
    pr_info = extract_pr_info(event)
    errors = run_validations(pr_info)

    if errors:
        print("=== PR Validation FAILED ===")
        for e in errors:
            print(f"- {e}")
        print("\nPlease update branch name or PR body to comply with the project's branching and PR guidelines.")
        return 1

    print("PR validation passed: branch name and PR body checklist/ticket references look good.")
    return 0

if __name__ == "__main__":
    rc = main()
    sys.exit(rc)