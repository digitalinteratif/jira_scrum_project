URL Shortener — Startup & Run Guide (KAN-158)
----------------------------------------------

Purpose
-------
This repository contains the URL shortener application and developer tooling. This README provides a quick-start to create a local development environment, run the app, verify database initialization, and run tests.

Quickstart (Unix / macOS)
-------------------------
1. Clone the repository
   - git clone <repo-url>
   - cd <repo-root>

2. Create and activate a virtual environment
   - python -m venv .venv
   - source .venv/bin/activate

3. Install dependencies
   - pip install -r requirements.txt

4. Create configuration (see .env example below) or export environment variables
   - cp .env.example .env
   - edit .env to your needs (see `.env.example` in this README)

   Example one-liners (bash/zsh):
   - export FLASK_APP=app.py
   - export FLASK_ENV=development
   - export SECRET_KEY=devsecret
   - export DATABASE_URL="sqlite:///./data/app.db"
   - export BASE_URL="http://localhost:5000"

5. Start the application
   - flask run

6. Verify database initialization (quick check)
   - In a second terminal (or from the same terminal if you redirected logs), look for the logger entry:
     - grep "Database initialized" -n app.log || journalctl -u <service> | grep "Database initialized"
   - Or run the app and capture stdout:
     - flask run > app.log 2>&1 &
     - tail -n +1 -f app.log | sed -n '1,200p'
     - Look for an INFO entry containing: Database initialized

Quickstart (Windows PowerShell)
------------------------------
1. python -m venv .venv
2. .\.venv\Scripts\Activate.ps1
3. pip install -r requirements.txt
4. $env:FLASK_APP="app.py"; $env:FLASK_ENV="development"; $env:SECRET_KEY="devsecret"; $env:DATABASE_URL="sqlite:///.\data\app.db"
5. flask run

Files created / locations
-------------------------
- Local SQLite DB (dev): default when DATABASE_URL omitted -> ./shortener.db, or set DATABASE_URL to sqlite:///./data/app.db
- App logs: printed to stdout by default (when running flask run or gunicorn). The utils/logging module writes traces to trace_KAN-*.txt files in the repository root for Architectural Memory (examples: trace_KAN-110.txt, trace_KAN-116.txt, trace_KAN-141.txt). Use tail/less/grep to inspect them.

Environment variable examples (.env)
-----------------------------------
Create a file named .env in the repo root (do NOT check secrets into VCS). Example:

# .env.example
SECRET_KEY=devsecret
DATABASE_URL=sqlite:///./data/app.db       # Local sqlite path as a URL (sqlite:///./data/app.db)
BASE_URL=http://localhost:5000
SHORT_CODE_LENGTH=8
SHORT_CODE_ATTEMPTS=8
SESSION_TIMEOUT_SECONDS=3600
LOG_LEVEL=INFO
LOG_FILE=
ALLOW_DEMO_USER_ID=True
FLASK_ENV=development

Key explanations
---------------
- SECRET_KEY: Flask secret for sessions. (Required)
- DATABASE_URL: SQLAlchemy URL. For a local SQLite file use sqlite:///./data/app.db. The app will create tables if they do not exist.
- BASE_URL: app's canonical base URL used when generating absolute short links.
- SHORT_CODE_LENGTH: default generated slug length for the shortener.
- SHORT_CODE_ATTEMPTS: number of attempts when generating a unique slug before failing.
- SESSION_TIMEOUT_SECONDS: default session cookie lifetime (seconds).
- LOG_LEVEL / LOG_FILE: configure logging behavior via utils/logging. Default logs go to stdout.

Database path / initialization
------------------------------
- The app uses SQLAlchemy and accepts DATABASE_URL (recommended) or the fallback sqlite:///shortener.db.
- On create_app() the app binds SQLAlchemy and runs db.create_all() within app.app_context() to create missing tables (idempotent).
- Acceptance verification: when the app starts you should see an INFO log entry "Database initialized" (see verification below). If you don't, verify app logs and that the path in DATABASE_URL is writable.

Reset local DB (development)
----------------------------
To remove and reset local SQLite DB:
- UNIX / macOS:
  - rm -f ./data/app.db
  - mkdir -p ./data
- Windows PowerShell:
  - Remove-Item .\data\app.db -Force
- After deletion, re-run the app; the database file and tables will be created automatically.

Running tests
-------------
There are unit and integration tests under app_core/tests. Tests are written with pytest.

Common test commands (from repository root)
- Install test dependencies: pip install -r requirements.txt
- Run unit tests:
  - pytest app_core/tests -q
  - or to run only unit bucketed tests (if you organize them separately): pytest app_core/tests/unit -q
- Run integration tests:
  - pytest app_core/tests -q -k "integration"   # or run the whole integration folder if present
  - Example: pytest app_core/tests/test_integration_auth_shortener.py -q
- Alternative explicit example (ticket-provided example):
  - pytest tests/unit
  - pytest tests/integration --db-path=./temp_test.db

Notes:
- Many tests set up an in-memory SQLite DB via create_app(test_config={...}) so they run fast and isolated.
- If a test relies on Docker (smoke CI), ensure docker CLI is available and that you have enough privileges.

How to verify "Database initialized" (Acceptance Criteria)
---------------------------------------------------------
Acceptance criteria for KAN-158: when following the README quickstart you can start the app and observe the log entry "INFO: Database initialized".

Steps:
1. Ensure your environment variables are set (see .env.example). At minimum set DATABASE_URL to a writable sqlite file.
2. Start the app and capture logs:
   - flask run > app.log 2>&1 &
   - tail -n +1 -f app.log
3. Look for:
   - An INFO-level message that includes the text "Database initialized"
   - Example grep:
     - grep -n "Database initialized" app.log
4. If you do not see it:
   - Confirm FLASK_ENV=development (Talisman force_https can be disabled for local)
   - Confirm the process started successfully (no import errors in app.log)
   - Confirm the directory for the database file exists and is writable
   - If not found, inspect the create_app() path in app.py (search for db.create_all() and info logs). In the codebase the DB create operation is performed in create_app(); add LOG_LEVEL=DEBUG for more context.

Where logs are written / inspected
---------------------------------
- Default: logs are emitted to stdout (visible in terminal). utils/logging writes JSON-formatted logs to stdout by default.
- Traces: non-critical trace files trace_KAN-*.txt are written into the repository root (e.g., trace_KAN-110.txt used by the email dev stub).
- To persist logs to a file in development, set LOG_FILE=/path/to/app.log or redirect stdout:
  - flask run > app.log 2>&1

Troubleshooting
---------------
- Missing dependencies: pip install -r requirements.txt
- DB not writable:
  - Ensure the parent directory exists: mkdir -p ./data
  - Check file permissions: ls -la ./data or Get-ACL on Windows
- "Port already in use": change FLASK_RUN_PORT or use flask run --port=XXXX
- Blueprint import errors show up during create_app(); app will log full traceback and exit (fail-fast).
- Tests failing:
  - Confirm pytest version and test dependencies installed
  - If tests require Docker (smoke_ci), ensure docker daemon running and docker CLI present.

CI considerations
----------------
- CI should run:
  - python -m venv .venv
  - . .venv/bin/activate
  - pip install -r requirements.txt
  - export DATABASE_URL=sqlite:///./temp_ci.db
  - pytest -q app_core/tests
- Integration smoke that starts ephemeral Postgres depends on docker (bin/smoke_ci.py); add appropriate permissions and available ports.

Contributing & Git Workflow
---------------------------
- Branching: create feature branches per user story (e.g., feature/db-init)
- PRs: include tests and update docs; ensure "Database initialized" verification step passes locally
- Peer review required before merge to main