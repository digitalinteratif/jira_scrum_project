KAN-138 — Containerization & local dev recipe
=============================================

Summary
-------
This addition provides a multi-stage Dockerfile and a docker-compose development recipe so that developers can:

- Build a production image pinned to Python 3.12.9-slim.
- Start a local dev stack (app + Postgres) with a single docker-compose up command.
- Run the test suite inside the dev container to ensure environment parity.

Files added
- Dockerfile (multi-stage, runtime image runs Gunicorn + wsgi.prod_wsgi:app)
- docker-compose.yml (web + db)
- .dockerignore
- requirements.txt (base dependencies; operators can pin versions)
- README.md (this document)

Design highlights
- Multi-stage build: wheel caching in the builder stage reduces install time in subsequent builds.
- Production CMD: gunicorn --config gunicorn.conf.py wsgi.prod_wsgi:app (uses existing wsgi/prod_wsgi.py to create an app configured for production-like settings).
- Dev compose uses a bind-mount so code changes are visible without rebuilding; Postgres service available for integration testing.
- SQLite fallback: set DATABASE_URL to a sqlite path and start only the web service in compose to run in single-container dev mode.

Quickstart — Development (with Postgres)
----------------------------------------
1) Build and start dev stack:
   docker-compose up --build

2) Open http://localhost:8000 to interact with the app.

3) To run tests inside the container (uses the same environment used by the app):
   docker-compose run --rm web pytest -q

Quickstart — Development (SQLite fallback)
-----------------------------------------
If you prefer not to run Postgres locally, use a SQLite DB file:

1) Start web service only and point DATABASE_URL to a sqlite file:
   DATABASE_URL=sqlite:///data/local_dev.db docker-compose up --build web

2) Run tests:
   DATABASE_URL=sqlite:///data/local_dev.db docker-compose run --rm web pytest -q

Production image build & run (example)
-------------------------------------
1) Build production image:
   docker build -t smartlink:prod .

2) Run (provide production DATABASE_URL and secrets):
   docker run -e DATABASE_URL="postgresql://user:pass@db-host:5432/dbname" \
              -e SECRET_KEY="your-secret" \
              -e JWT_SECRET="your-jwt-secret" \
              -p 8000:8000 smartlink:prod

Notes:
- The image's default command runs Gunicorn using gunicorn.conf.py and wsgi.prod_wsgi:app (this module sets conservative prod-ish config).
- Ensure SECRET_KEY and JWT_SECRET are set in production. Do NOT bake secrets into image.

Running tests & CI integration
------------------------------
- Run tests locally inside the container using:
    docker-compose run --rm web pytest -q

- For CI:
  - Use the Dockerfile to build the test image (multi-stage helps build caches).
  - Ensure that CI sets required envs (DATABASE_URL) and runs migrations or uses create_all fallback to prepare DB (see scripts/bin/smoke_ci.py for example flows).
  - Example CI step:
      docker build -t smartlink-test:ci .
      docker run --rm -e DATABASE_URL="sqlite:///:memory:" smartlink-test:ci pytest -q

Implementation notes and operator guidance
------------------------------------------
- Keep requirements.txt under review and pin versions as needed for reproducible builds.
- For production deployments (Render, etc.), prefer building via the supplied Dockerfile and running with orchestration that sets secrets as environment variables or secret stores.
- When iterating locally, bind-mounting source via docker-compose avoids rebuilding; when upgrading deps, rebuild image to refresh wheels.
- Multi-stage caching: CI can cache the pip wheel layer (e.g., persist /wheels across CI runs) to speed builds.

Surgical change policy
----------------------
- This addition is intentionally surgical: it only adds the containerization/dev artifacts at repo root.
- Do NOT modify existing application modules (routes/, utils/, models.py, app.py, tests/) in this change set.
- Any future change that requires altering runtime behavior should be submitted as a targeted Jira story (e.g., update wsgi entrypoint, tweak Gunicorn config, etc.) and must follow the repository's surgical update rules.

Trace & verification
--------------------
- As required by project guardrails, please add a trace file detailing agent interactions for this ticket:
    trace_KAN-138.txt
  Include: who created these files, timestamps, and a pointer to this blueprint. (This blueprint itself serves as the "Architectural Memory" artifact for the containerization change.)

If you want, I can:
- Create these files in the repo (surgical file additions).
- Open a MR/PR with the files and a short runbook.
- Optionally, add a small helper Makefile or pre-built script (e.g., bin/docker_test.sh) to standardize test commands.

--- END OF BLUEPRINT ---
--- END FILE: README.md ---