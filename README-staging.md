# Local Staging & UI Automated Validation (KAN-151)

This document explains how to run the local Docker staging environment and the automated UI validation that will produce a JSON report and optionally create Jira tickets for failures.

Prerequisites
- Docker & docker-compose installed and usable by your user.
- Python 3.12.9 installed (for running the orchestrator script locally).
- Optional local dev tools:
  - pip install python-dotenv pytest playwright jira requests
  - python -m playwright install

Environment
Create a `.env` file in the repository root with the following (example):

```
BASE_URL=http://localhost:5000
TARGET_JIRA_TICKET=KAN-151
JIRA_SERVER=https://jira.example.com
JIRA_USERNAME=automation@company.com
JIRA_API_TOKEN=your_api_token_here
JIRA_PROJECT_KEY=KAN
```

Running validation (local)
1. Start validation and allow ticket creation:
   ```
   python validate_and_ticket.py
   ```

2. Run validation without creating Jira tickets (dry-run):
   ```
   python validate_and_ticket.py --no-tickets
   ```

What the orchestrator does
- Builds and starts the `app` service via `docker-compose` (binds to port 5000).
- Waits for `http://localhost:5000/health` to return `200`.
- Runs the 10-second stability test to ensure the app is stable.
- Executes the Playwright-based pytest test `tests/test_ui_navigation.py`. The test writes `reports/ui_navigation_report.json`.
- If failures are present and Jira credentials provided, creates child issues under `TARGET_JIRA_TICKET`.
- Writes `trace_KAN-151.txt` with a detailed trace of actions.

Report location
- `reports/ui_navigation_report.json` contains the structured test results.

Notes
- Do not modify files under `app_core/` as part of this ticket — keep updates surgical and limited to infra/test artifacts.