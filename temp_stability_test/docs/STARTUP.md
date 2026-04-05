Startup & Run Guide — Detailed (KAN-158)
----------------------------------------

Purpose
-------
This guide provides step-by-step instructions for devs and new team members to set up the local environment, initialize the database, run the application, verify database initialization logging, and run tests. It includes exact commands for Unix/macOS and Windows PowerShell, example .env keys, troubleshooting steps, and CI notes.

Table of contents
-----------------
1. Prerequisites
2. Exact commands (Unix / macOS)
3. Exact commands (Windows PowerShell)
4. Example .env (file contents)
5. Key environment variables (explanations)
6. How the DB gets initialized (technical detail)
7. Verifying "Database initialized" log entry
8. Running tests (unit & integration)
9. Resetting the local DB (dev)
10. Troubleshooting (file permissions, missing env keys)
11. CI integration and test automation
12. Common gotchas and tips

1) Prerequisites
----------------
- Python 3.10+ (match project's target Python - check pyproject/requirements).
- pip
- (Optional) Docker CLI for smoke CI tests (bin/smoke_ci.py).
- Git and network access to clone repository.

2) Exact commands — Unix / macOS (copy/paste)
---------------------------------------------
# Clone repository
git clone <repo-url>
cd <repo-root>

# Create virtual environment and activate
python -m venv .venv
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Create data directory for SQLite DB (if using file path)
mkdir -p ./data

# Example env exports (bash/zsh)
export FLASK_APP=app.py
export FLASK_ENV=development
export SECRET_KEY=devsecret
export DATABASE_URL="sqlite:///./data/app.db"
export BASE_URL="http://localhost:5000"
export LOG_LEVEL=INFO

# Start dev server
flask run

# (Optional) Start server and capture logs to file
flask run > app.log 2>&1 &

3) Exact commands — Windows PowerShell
-------------------------------------
# Clone
git clone <repo-url>
cd <repo-root>

# Create venv & activate
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install deps
pip install -r requirements.txt

# Ensure data directory exists
New-Item -ItemType Directory -Force -Path .\data

# Set env vars (PowerShell)
$env:FLASK_APP="app.py"
$env:FLASK_ENV="development"
$env:SECRET_KEY="devsecret"
$env:DATABASE_URL="sqlite:///.\data\app.db"
$env:BASE_URL="http://localhost:5000"

# Start
flask run

4) Example .env file
--------------------
Place a file named .env at the repo root (this project uses python-dotenv load behavior).
Do NOT commit secrets.

# .env
SECRET_KEY=devsecret
DATABASE_URL=sqlite:///./data/app.db
BASE_URL=http://localhost:5000
SHORT_CODE_LENGTH=8
SHORT_CODE_ATTEMPTS=8
SESSION_TIMEOUT_SECONDS=3600
LOG_LEVEL=INFO
ENABLE_DEV_TOOLS=True
ALLOW_DEMO_USER_ID=True

5) Environment variables — definitions & meanings
------------------------------------------------
- SECRET_KEY: Flask's secret key for sessions. Use a secure random value for production.
- DATABASE_URL: SQLAlchemy connection string. Examples:
  - SQLite file: sqlite:///./data/app.db
  - In-memory (for tests): sqlite:///:memory:
  - Postgres: postgresql://user:pass@host:5432/dbname
- BASE_URL: canonical base URL for generating absolute short links.
- SHORT_CODE_LENGTH: default slug length used by the shortener.
- SHORT_CODE_ATTEMPTS: number of attempts to generate a unique slug in the generation loop.
- SESSION_TIMEOUT_SECONDS: mobile/session lifetime.
- LOG_LEVEL: standard logging level (DEBUG, INFO, WARN, ERROR).
- LOG_FILE: optional file path to persist rotating logs (utils/logging).
- ENABLE_DEV_TOOLS: when True exposes dev-only endpoints like /tools/lint_scoping.
- ALLOW_DEMO_USER_ID: allow passing user_id in forms for tests.

6) How DB initialization works (technical)
-----------------------------------------
- The app uses Flask + SQLAlchemy.
- create_app() (app.py) reads DATABASE_URL, sets app.config['SQLALCHEMY_DATABASE_URI'], and calls db.init_app(app).
- After blueprints are registered, the factory executes:
  with app.app_context():
      db.create_all()
  This is idempotent and creates missing tables (Users, ShortURL, ClickEvent, SessionToken, etc.) without dropping existing data.
- models.init_db(engine, SessionLocal) is used by scripts/tools to initialize module-level references (models.Engine / models.Session).
- The codebase also writes trace entries to trace_KAN-*.txt files across modules for Architectural Memory.

7) Verifying the "Database initialized" log entry
-------------------------------------------------
Acceptance requires you to "observe the 'INFO: Database initialized' log entry and run tests as documented."

To verify:
- Start the app (see commands above) and ensure logs are captured.
- The repository's logging subsystem uses utils/logging to emit structured logs to stdout. By default, db.init/create_all logs an initialization message. Look for:
  - INFO ... Database initialized
  or, if your environment has LOG_LEVEL=DEBUG:
  - DEBUG / INFO messages around the DB init step.
- Exact check (Unix):
  - flask run > app.log 2>&1 &
  - sleep 1
  - grep -n "Database initialized" app.log || (echo "No 'Database initialized' line; search for 'Database creation' or 'create_all' in app.log"; tail -n 200 app.log)
- If not present:
  - Inspect create_app() in app.py and search for db.create_all() and any app.logger.info("...") calls — adding or enabling LOG_LEVEL=DEBUG may expose necessary lines.
  - Confirm models.Base.metadata.create_all() ran (if running migrations with offline fallback, create_all() is the fallback when alembic not present).

Why this message matters
- Tests and integration smoke runners rely on DB initialization being completed during startup (so tests can create records). The smoke CI script (bin/smoke_ci.py) waits for DB readiness and for migrations/create_all to succeed.

8) Running tests (detailed)
---------------------------
Unit tests and integration tests are under app_core/tests. The test suite assumes a local Python environment and a writable filesystem.

Common commands:
- Run all tests:
  - pytest -q
- Run specific test file:
  - pytest app_core/tests/test_models.py -q
- Run integration tests only:
  - pytest -q app_core/tests/test_integration_auth_shortener.py
- Run tests with a temporary file DB (example):
  - pytest app_core/tests -q --db-path=./temp_test.db
  - Some tests call create_app(test_config=...) and use sqlite:///:memory: for isolation.

CI-friendly test command (example):
- python -m venv .venv
- source .venv/bin/activate
- pip install -r requirements.txt
- export DATABASE_URL="sqlite:///./temp_ci.db"
- pytest app_core/tests -q

Smoke tests:
- bin/smoke_ci.py is provided for CI (requires Docker). Run only in CI environments with Docker available.

9) Reset local DB (dev)
-----------------------
To wipe local SQLite DB and reinitialize:
- Stop any running server using the DB file.
- UNIX:
  - rm -f ./data/app.db
  - mkdir -p ./data
  - Start app (flask run) — DB and tables will be re-created.
- Windows PowerShell:
  - Remove-Item .\data\app.db -Force

If you need to re-run migrations (alembic):
- If alembic is configured, run:
  - alembic upgrade head
  - otherwise, create_all() is used as an idempotent fallback.

10) Troubleshooting (common failures)
-------------------------------------
DB file permission denied:
- Verify directory exists and current user has write permission.
- ls -la ./data && chmod u+w ./data or equivalent.

"Database initialized" not printed:
- Ensure your LOG_LEVEL allows INFO output (LOG_LEVEL=INFO)
- Confirm create_app() ran to completion; check logs for earlier exceptions (blueprint import failure)
- If using gunicorn with preload_app=True: create_app() runs in the master process; ensure create_app() safe to run there.

Missing env keys errors:
- If the app requires SECRET_KEY or other required keys, the app will fallback to dev-safe defaults in many places but some tests expect keys present. Populate missing keys in .env.

Windows path slashes for DATABASE_URL:
- For SQLite use: sqlite:///./data/app.db (forward slashes work on Windows in SQLAlchemy)

11) CI integration & acceptance tests
------------------------------------
- CI should:
  - Create a venv
  - Install dependencies
  - Run migrations or fallback create_all
  - Start the app or use the smoke CI helper for a production-like test (bin/smoke_ci.py)
  - Confirm presence of "Database initialized" in logs
  - Run pytest for unit and integration test folders
- Example CI steps (Linux runner):
  - python -m venv .venv
  - . .venv/bin/activate
  - pip install -r requirements.txt
  - export DATABASE_URL="sqlite:///./temp_ci.db"
  - pytest app_core/tests -q

Edge cases to assert in CI:
- Missing/malformed .env keys: CI should fail with a clear message (documented in this guide).
- DATABASE_URL points to unwritable directory: CI agent should detect and fail early.

12) Additional tips & notes
---------------------------
- Keep .env out of version control.
- When debugging startup failures, start the app interactively (python -m flask run) so Python tracebacks are visible.
- Use trace_KAN-*.txt files to see best-effort trace entries produced by dev-stub modules (email dev stub, geoip enrichment, etc.).
- Use LOG_LEVEL=DEBUG to increase verbosity when diagnosing DB or blueprint import issues.

Appendix: Common Commands Summary
--------------------------------
Create env, install, run:
- python -m venv .venv
- source .venv/bin/activate
- pip install -r requirements.txt
- export FLASK_APP=app.py
- export FLASK_ENV=development
- export DATABASE_URL="sqlite:///./data/app.db"
- mkdir -p ./data
- flask run

Run tests:
- pytest app_core/tests -q

Reset DB:
- rm -f ./data/app.db && mkdir -p ./data

Inspect logs:
- flask run > app.log 2>&1 &
- tail -f app.log | grep --line-buffered "Database initialized"

If you want a copy of this guide dropped into the repo:
- README.md (this file)
- docs/STARTUP.md (this file) — add to docs/STARTUP.md in repo root

If anything above does not work as expected in your environment, capture the output of `flask run` and open an issue (or attach app.log) and include:
- OS / shell details
- Python version
- Exact commands you ran
- Contents of .env (sanitized) or list of env keys you set

End of Technical Spec (KAN-158) README.md and docs/STARTUP.md content.