# CONTRIBUTING.md

This document explains how to onboard as a contributor to the Smart Link (digitalinteractif.com / smart-link) codebase, get a local development environment running, run tests and migrations, and follow the project's mandatory guardrails (CSRF, ID Filter rule, render_layout, dependency tolerance, and surgical module updates). Follow these steps to be productive and produce reviewable, safe changes.

If you are working on a specific Jira ticket (e.g., KAN-140), write a short trace entry to trace_KAN-140.txt describing your intent before you start and record key interactions as you progress. See "Trace Logging" below.

---

Table of contents
- Quickstart (5-minute path)
- System requirements
- Create a virtualenv & install dependencies
- Environment variables (common list)
- Running the app locally (create_app usage + quick manual smoke)
- Running tests
- Running migrations (alembic and fallback)
- Creating a migration (autogenerate)
- Adding a Blueprint (surgical example)
- Building HTML forms (CSRF requirement)
- ID Filter rule — canonical patterns & forbidden patterns
- render_layout — how templates must be returned
- Dependency tolerance — import and fallback patterns
- Pre-commit & local CI smoke runs
- Trace logging & scanning the codebase (Architectural Memory)
- Troubleshooting (common issues: SQLite vs Postgres, migrations, dependencies)
- PR checklist & surgical update policy
- Helpful snippets & references

---

Quickstart (5-minute path)
1. Clone the repo.
2. Create and activate a Python 3.12.9 virtual environment.
   - python3.12 -m venv .venv
   - source .venv/bin/activate
3. Install dependencies:
   - pip install -r requirements.txt
4. Start a local dev DB (SQLite fallback is OK):
   - export DATABASE_URL="sqlite:///local_dev.db"
5. Start the app:
   - python -m wsgi.prod_wsgi
6. Visit http://127.0.0.1:8000/health -> should return "ok".

If you need a production-like smoke test that runs migrations and gunicorn in CI, see bin/smoke_ci.py (requires Docker).

---

System requirements
- Python 3.12.9 (the project is strict about Python version).
- PostgreSQL (for a production-like environment) or SQLite for local development/testing.
- Docker (only required to run CI smoke runner).
- Optional (dev tooling): alembic, pre-commit, pytest, gunicorn, requests.

Note: The codebase is defensive about missing external libraries (PyJWT, argon2, flask_talisman, etc.) — the app will fall back to stdlib implementations where available. However, install the optional dependencies if you want full runtime parity.

---

Create a virtualenv & install dependencies

Unix/macOS:
```
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# Optional dev tools
pip install -r requirements-dev.txt
```

Windows (PowerShell):
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

If repository does not have requirements files in your checkout, ask the repository owner — standard development packages include pytest, alembic, pre-commit, requests, gunicorn.

---

Important environment variables (non-exhaustive)
Below are environment variables read by app.create_app() and other modules that you will commonly set for local development. Use test_config when calling create_app to override these in tests.

Core:
- SECRET_KEY — Flask secret (default: "dev-secret-key" when not set)
- DATABASE_URL — SQLAlchemy URL (default local sqlite: sqlite:///local_dev.db)
- JWT_SECRET — secret used for token signing (defaults to SECRET_KEY)
- EMAIL_VERIFY_EXPIRY_SECONDS — seconds until email verification token expires (default 24h)

Cookie/security:
- JWT_COOKIE_NAME — cookie name for JWT tokens (default smartlink_jwt)
- JWT_COOKIE_SECURE — "true"/"false" (cookie secure flag; default false in dev)
- JWT_SAMESITE — cookie SameSite ("Lax" default)
- SECURE_X_FRAME_OPTIONS — X-Frame-Options header (DENY default)
- SECURE_X_CONTENT_TYPE_OPTIONS — X-Content-Type-Options header (nosniff default)
- SECURE_REFERRER_POLICY — Referrer-Policy
- SECURE_CONTENT_SECURITY_POLICY — CSP string (default conservative policy)

Feature toggles & dev-tools:
- ALLOW_DEMO_USER_ID — true/false to allow using user_id via form (dev only)
- ENABLE_DEV_TOOLS — true/false to enable /tools endpoints (do NOT enable in prod)
- ANALYTICS_MAX_RANGE_DAYS — limit on analytics date ranges (default 365)

Networking & infra:
- DATABASE_URL as above
- GUNICORN_BIND, WORKER_COUNT, GUNICORN_THREADS, GUNICORN_PRELOAD — used by gunicorn.conf.py
- IPV6_ANON_MASK_LOWER_BITS — used by utils.ip anonymizer (int 0..128)

Misc:
- RESERVED_SLUGS, BLACKLISTED_SLUGS — comma-separated lists for slug validation
- DISALLOW_NUMERIC_SLUGS — whether to disallow numeric-only slugs (default true)

When in doubt, consult create_app() in app.py for additional config keys and defaults.

---

Running the app locally (create_app usage)
You can use the factory directly (useful for debugging and tests):

Example Python snippet:
```python
from app import create_app
app = create_app(test_config={
    "DATABASE_URL": "sqlite:///local_dev.db",
    "SECRET_KEY": "dev-secret",
    "JWT_SECRET": "dev-jwt",
    "JWT_COOKIE_SECURE": False,
    "ALLOW_DEMO_USER_ID": True,  # dev helper
})
# For quick manual run (flask builtin)
if __name__ == "__main__":
    app.run(debug=True)
```

Stability test (10-second guarantee):
- After changing code, run app._stability_check: start the Flask app and query GET /_stability_check which executes a trivial DB query and returns 200 on success. This helps ensure app bootstrap and DB wiring are healthy.

---

Running tests
- Run unit & integration tests with pytest:
  ```
  pytest -q
  ```

- For a single test:
  ```
  pytest tests/test_click_event.py::test_redirect_flow_persists_clickevent -q
  ```

- Tests use in-memory SQLite by default via tests/conftest.py. If you want to run tests against Postgres, export DATABASE_URL before running tests.

Common test patterns:
- Use create_app(test_config=...) when instantiating the app in test fixtures to override DATABASE_URL and secrets.
- Use models.Session() to get a DB session for direct DB assertions.

---

Running migrations (alembic preferred, fallback to create_all)
Preferred: alembic (if the project has alembic configured)

- To apply migrations:
  ```
  export DATABASE_URL=postgresql://user:pass@localhost:5432/dbname
  alembic upgrade head
  ```

- To create a new migration (autogenerate):
  ```
  alembic revision --autogenerate -m "kan_xxx: short summary"
  # then inspect migration file, then:
  alembic upgrade head
  ```
  Note: The repo's models.py includes a naming convention for constraints (ix/uq/ck/fk/pk). Keep migrations consistent with these conventions.

Fallback (no alembic available):
- The app supports a safe fallback to SQLAlchemy create_all():
  ```
  python - <<PY
  from app import create_app
  import models
  app = create_app(test_config={"DATABASE_URL": "sqlite:///local_dev.db"})
  # app.create_app already calls Base.metadata.create_all in dev mode if tables missing
  print("DB initialized")
  PY
  ```

CI & smoke-runner:
- The CI smoke script bin/smoke_ci.py tries alembic first; if missing it falls back to models.Base.metadata.create_all(). You can use that script to exercise a production-like flow (it requires Docker).

---

Creating a migration (autogenerate example)
1. Make model changes in models.py only when necessary; prefer small surgical updates. Add new models or ALTER tables via a migration rather than modifying generated schema in production.
2. Run:
   ```
   alembic revision --autogenerate -m "KAN-XXX: brief description"
   # Inspect the generated file under migrations/versions; edit if manual adjustments are required.
   alembic upgrade head
   ```
3. If you cannot run alembic locally, add a migration in migrations/versions and explain the reason in the PR for CI maintainers to review.

---

Adding a new Blueprint (surgical example)

Policy: Changes must be surgical. If adding a new feature:
- Create only the new file(s) required (e.g., routes/my_feature.py).
- Register the blueprint in app.py using a minimal, defensive import (surrounded by try/except if optional).

Example: routes/my_feature.py
```python
from flask import Blueprint, request
from utils.templates import render_layout

my_feature_bp = Blueprint("my_feature", __name__)

@my_feature_bp.route("/my-feature", methods=["GET"])
def index():
    return render_layout("<h1>My Feature</h1><p>Replace with your UI.</p>")
```

Register blueprint in app.py (surgical, minimal change):
```python
# near other blueprint registrations
try:
    from routes.my_feature import my_feature_bp
    app.register_blueprint(my_feature_bp, url_prefix="/my")
except Exception:
    # non-fatal if module missing in some constrained environments
    pass
```

Notes:
- Keep registration minimal and defensive to avoid breaking tests/environments where modules might be intentionally omitted.
- If your change requires touching multiple modules (models + routes), include a SURGICAL RATIONALE in your PR explaining why cross-boundary changes are necessary. Large refactors across many modules are not allowed in a single PR.

---

Code patterns: forms & CSRF (MUST)
All server-rendered HTML forms must include an explicit hidden CSRF input. This is non-negotiable.

Server-side: generate CSRF token in the view using flask_wtf:
```python
from flask_wtf.csrf import generate_csrf

csrf_token = ""
try:
    csrf_token = generate_csrf()
except Exception:
    csrf_token = ""
```

When building the HTML string, include:
```html
<input type="hidden" name="csrf_token" value="{csrf_token}">
```
Example (string-based rendering pattern used throughout repo):
```python
html = f"""
<form method="post" action="/auth/register">
  <label>Email: <input type="email" name="email" required></label>
  <label>Password: <input type="password" name="password" required></label>
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  <button type="submit">Register</button>
</form>
"""
return render_layout(html)
```

Tests: tests/test_csrf_templates.py scans inline template strings for <form> blocks and asserts presence of name="csrf_token" or hidden_tag variants. Make sure all forms pass this static audit.

Forbidden:
- Do NOT omit the explicit input. Do NOT rely on Jinja extends/blocks to inject CSRF tokens — this project uses string-based render_layout.

---

ID Filter Rule — ownership scoping (MUST)
All queries that return or modify user-owned data must filter by the owner’s user_id.

Correct/Canonical patterns (use models.Session()):
```python
session = models.Session()
link = session.query(models.ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
```

Forbidden patterns (do not use):
```python
# Forbidden because it bypasses owner scoping
link = session.query(models.ShortURL).get(link_id)
```

Examples:
- Listing:
  ```python
  q = session.query(models.ShortURL).filter_by(user_id=current_user_id).order_by(models.ShortURL.created_at.desc())
  ```
- Deleting:
  ```python
  s = session.query(models.ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
  if s:
      session.delete(s)
      session.commit()
  ```

When returning 403 vs 404:
- If the row exists but not owned by the current user -> return 403 (forbidden).
- If the row does not exist at all -> return 404 (not found).
Pattern:
```python
short = session.query(models.ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
if not short:
    exists = session.query(models.ShortURL).filter_by(id=link_id).first()
    if exists:
        return render_layout("<h1>Forbidden</h1>"), 403
    else:
        return render_layout("<h1>Not Found</h1>"), 404
```

---

render_layout — UI wrapper & template rules (MUST)
- Every route that returns HTML MUST return content wrapped in utils.templates.render_layout(inner_html).
- Do NOT use Jinja2 {% extends %} or {% block %} tags. The app uses string-based rendering for consistent modularity.

Example:
```python
from utils.templates import render_layout
return render_layout("<h1>Title</h1><p>body</p>")
```

---

Dependency tolerance & safe imports (MUST)
- Use try/except on imports from optional dependencies and provide a safe fallback.
- Do not allow missing optional libs to crash create_app() or blueprint registration.

Example pattern:
```python
try:
    from flask_talisman import Talisman
except Exception:
    Talisman = None
```

If code relies on an external module for cryptography or JWTs, prefer trying the specialized lib first (argon2, PyJWT) and fall back to a standard-library approach (hashlib/hmac) when not installed. See utils/crypto.py for canonical examples.

---

Pre-commit & local CI

Install and run pre-commit hooks (if .pre-commit-config.yaml exists):
```
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

Local CI smoke-run (production-like; requires Docker & gunicorn):
```
# runs ephemeral Postgres, migrations and a smoke scenario; writes trace_KAN-133.txt
python bin/smoke_ci.py
```
Note: bin/smoke_ci.py expects docker CLI and gunicorn; read the script for environment overrides.

If you cannot run smoke_ci locally, run the minimal app._stability_check endpoint:
1. Start the app (python wsgi/prod_wsgi.py or via create_app).
2. curl http://127.0.0.1:8000/_stability_check

---

Trace Logging Mandate (Architectural Memory, MUST)
- Any interaction between agents (or major script runs for a ticket) must append a line to trace_[ticket_id].txt. For example, for this ticket (KAN-140) create or append trace_KAN-140.txt lines like:
  ```
  2026-03-31T12:34:56Z PREPARE_WORK start user=you@org commit=abc123 intent="write CONTRIBUTING.md"
  2026-03-31T12:35:10Z UPDATE_PROGRESS created docs/CONTRIBUTING.md
  ```
- Use existing code patterns from bin/* and other route modules (non-blocking, best-effort file append). The repository contains many such trace files; follow their format.

---

Scanning the codebase & "app_core" awareness (per Architectural Rule)
Before making updates, build a quick map of the codebase modules so reviewers can validate surgical changes.

Quick mapping script (example you can run locally):
```python
import os, json
root = os.path.abspath(".")
map = {}
for dirpath, dirs, files in os.walk(os.path.join(root, "")):
    if "venv" in dirpath or ".git" in dirpath:
        continue
    py = [f for f in files if f.endswith(".py")]
    if py:
        rel = os.path.relpath(dirpath, root)
        map[rel] = py
print(json.dumps(map, indent=2))
```
Save the output and attach to your PR if your change touches multiple modules.

---

Troubleshooting (common issues)

1. Tests failing due to DB or alembic:
   - Ensure DATABASE_URL is set. For local quick runs use:
     ```
     export DATABASE_URL="sqlite:///:memory:"
     ```
     or file-based:
     ```
     export DATABASE_URL="sqlite:///local_dev.db"
     ```
   - If alembic not installed, tests will often fall back to create_all; try `pip install alembic`.

2. Missing optional libraries (PyJWT, argon2, flask_talisman):
   - The app includes fallbacks; install the optional deps for parity:
     ```
     pip install pyjwt[argon2] argon2-cffi flask-talisman
     ```
     or the project's dev requirements.

3. CSRF token missing on rendered pages in tests:
   - Ensure flask-wtf is installed and app.config has a SECRET_KEY. Tests use create_app(test_config=...) to set SECRET_KEY; when running manually, export SECRET_KEY.

4. SQLite concurrency issues:
   - In-memory SQLite (sqlite:///:memory:) is process-local. If you run the app in multiple processes (gunicorn preload_app=True), you may see DB access errors. For local multi-process testing use a file-based sqlite (sqlite:///./dev.db) or a local Postgres instance.

5. Gunicorn & preload_app:
   - gunicorn.conf.py has notes: preload_app=True will call create_app() in master process. Ensure create_app() is safe to run in master (do not spawn background threads at import time).

6. Local redirect/URL normalization causing 404:
   - validate_and_normalize_url rejects private IPs by default. If testing against 127.0.0.1 or private IP targets, configure ALLOW_PRIVATE_TARGETS=True in app config only for local testing.

---

PR Checklist & Surgical Update Policy (MUST before merging)
- Is this PR surgical? Change only the minimal set of files the ticket requires. If not, add a SURGICAL RATIONALE section to the PR explaining why multiple modules must be touched.
- All new HTML forms include explicit CSRF hidden input: <input type="hidden" name="csrf_token" value="{csrf_token}">
- All user-owned DB queries apply the ID Filter rule (filter_by(..., user_id=...)).
- All new or modified routes return HTML via render_layout(...) (no Jinja extends/blocks).
- All optional external imports are wrapped in try/except with acceptable fallbacks (see utils/* modules for patterns).
- Did you run pre-commit and unit tests locally?
  - pre-commit run --all-files
  - pytest -q
- Did you run the app entry-point stability check for 10 seconds (/_stability_check) or run bin/smoke_ci.py if applicable?
- Add a trace line to trace_[ticket].txt recording your work and key decisions (pragmatic step-by-step updates).
- Include a concise PR description with the list of file changes and justification for each.

Suggested PR description summary:
- One-line summary.
- Files changed (surgical list).
- Why surgical: explain minimality and follow-up story if additional work is needed.
- Command output from pre-commit and tests.

---

Example snippets (copy/paste)

A. Correct owner-scoped query
```python
session = models.Session()
link = session.query(models.ShortURL).filter_by(id=link_id, user_id=current_user_id).first()
```

B. Forbidden owner-scoped bypass
```python
# forbidden
link = session.query(models.ShortURL).get(link_id)
```

C. Form with CSRF token (server builds token using generate_csrf())
```python
from flask_wtf.csrf import generate_csrf
csrf_token = generate_csrf()
html = f"""
<form method="post" action="/shorten">
  <input type="hidden" name="csrf_token" value="{csrf_token}">
  ...
</form>
"""
return render_layout(html)
```

D. Minimal blueprint file (routes/example.py)
```python
from flask import Blueprint
from utils.templates import render_layout

example_bp = Blueprint("example", __name__)

@example_bp.route("/example", methods=["GET"])
def example_index():
    return render_layout("<h1>Example</h1><p>Example page</p>")
```

E. Register blueprint in app.py (defensive)
```python
try:
    from routes.example import example_bp
    app.register_blueprint(example_bp, url_prefix="/example")
except Exception:
    # fallback - do not let missing dev-only module break app
    pass
```

F. create_app stability check (callable)
```python
from app import create_app
app = create_app(test_config={"DATABASE_URL":"sqlite:///local_dev.db"})
with app.test_client() as c:
    resp = c.get("/_stability_check")
    assert resp.status_code == 200
```

---

Local CI smoke & debugging tips
- If you can't run bin/smoke_ci.py because Docker or gunicorn is missing, run the minimal E2E flow manually:
  1. Start a DB (Postgres or file SQLite).
  2. Run alembic upgrade head (or models.Base.metadata.create_all).
  3. Start app (python wsgi/prod_wsgi.py).
  4. Use a browser or curl to exercise the main flows (register, verify via trace_KAN-110.txt, login, create short URL, redirect).
- Examine trace files (trace_KAN-110.txt, trace_KAN-113.txt, etc.) in the repo root for diagnostic messages the app writes during dev flows.

---

Contacts & next steps
- If something in this document is unclear or missing, leave a brief message in the ticket (KAN-140) indicating which step you attempted and what failed, attaching relevant trace file snippets.
- Remember: surgical changes + trace logging = faster reviews and safer mainline.

---

Appendix: Example trace entry template (KAN-140)
Add lines to trace_KAN-140.txt like:
```
2026-04-03T14:20:00Z KAN-140 START user=you@company intent="Add CONTRIBUTING.md" notes="creating initial developer onboarding doc"
2026-04-03T14:25:00Z KAN-140 TESTS_RUN pre-commit=ok pytest=ok
2026-04-03T14:27:00Z KAN-140 COMPLETE files=docs/CONTRIBUTING.md
```

---

Thank you for contributing. Follow the above guardrails carefully — they exist to protect user data and keep the codebase modular and maintainable.