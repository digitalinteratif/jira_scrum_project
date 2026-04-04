#!/usr/bin/env python3
"""
bin/smoke_ci.py - End-to-end CI smoke runner for KAN-133 (US-035)

Run this script in CI to perform a short production-like smoke test:
 - Start ephemeral Postgres in Docker
 - Set DATABASE_URL to the Postgres instance
 - Run DB migrations (alembic upgrade head if available, else fallback to models.Base.metadata.create_all)
 - Start the app under Gunicorn using wsgi.prod_wsgi:app (one worker)
 - Wait for health endpoints, then run scripted scenario:
     register -> extract token from trace_KAN-110.txt -> verify email -> login -> create short url -> redirect
 - Fail with non-zero exit on any error. Write trace_KAN-133.txt for Architectural Memory.

Usage (CI):
  python bin/smoke_ci.py

Notes:
 - Requires: Docker (docker CLI), gunicorn, Python deps (requests), and PostgreSQL image (fetched by docker).
 - The script times out aggressively to keep the CI job short (~2 minutes target). Adjust constants below as needed.
"""

from __future__ import annotations
import os
import sys
import time
import json
import shutil
import signal
import socket
import subprocess
import tempfile
import re
import random
from typing import Optional

TRACE_FILE = "trace_KAN-133.txt"
GUNICORN_LOG = "smoke_gunicorn.log"


def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


def _exit(msg: str, code: int = 1):
    _trace(f"SMOKE_ABORT {msg}")
    print("SMOKE_ABORT:", msg)
    sys.exit(code)


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    addr, port = s.getsockname()
    s.close()
    return port


def check_executable(name: str) -> bool:
    return shutil.which(name) is not None


def _mask_db_url(db_url: str) -> str:
    """
    Mask password in a DATABASE_URL for safe logging.
    e.g. postgresql://user:pass@host:port/db -> postgresql://user:***@host:port/db
    """
    try:
        # naive masking: replace :<password>@ with :***@
        return re.sub(r":([^:@/]+)@", r":***@", db_url, count=1)
    except Exception:
        return "[masked]"


def wait_for_http(url: str, timeout: int = 30, interval: float = 0.5, expected_text: Optional[str] = None) -> bool:
    """
    Poll HTTP URL until timeout. Use requests if available, else urllib.
    """
    end = time.time() + timeout
    try:
        import requests
        sess = requests.Session()
        while time.time() < end:
            try:
                r = sess.get(url, timeout=3)
                if r.status_code < 500:
                    if expected_text:
                        if expected_text in r.text:
                            return True
                    else:
                        return True
            except Exception:
                pass
            time.sleep(interval)
        return False
    except Exception:
        # fallback to urllib
        try:
            from urllib.request import urlopen, Request
            while time.time() < end:
                try:
                    req = Request(url, headers={"User-Agent": "smoke-ci/1.0"})
                    with urlopen(req, timeout=3) as resp:
                        body = resp.read().decode("utf-8", errors="ignore")
                        if expected_text:
                            if expected_text in body:
                                return True
                        else:
                            return True
                except Exception:
                    pass
                time.sleep(interval)
            return False
        except Exception:
            return False


def wait_for_postgres(database_url: str, timeout: int = 30, interval: float = 0.5) -> bool:
    """
    Attempt to open SQLAlchemy engine and run a trivial select to confirm availability.
    """
    try:
        from sqlalchemy import create_engine, text
    except Exception:
        _trace("SQLALCHEMY_MISSING cannot verify Postgres readiness via SQLAlchemy")
        return False

    end = time.time() + timeout
    while time.time() < end:
        try:
            engine = create_engine(database_url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            _trace(f"POSTGRES_WAIT try failed: {str(e)[:200]}")
        time.sleep(interval)
    return False


def extract_token_from_trace(email: str, purpose_key: Optional[str] = "token", timeout: int = 10) -> Optional[str]:
    """
    Read trace_KAN-110.txt (email dev stub trace) and extract the most recent token for 'email'.
    The dev stub writes entries like: EMAIL_SENT {'to': 'a@b', 'verification_url': '...', 'token': '...'}
    We'll perform a conservative regex parse.
    """
    trace = "trace_KAN-110.txt"
    end = time.time() + timeout
    token_re = re.compile(r"'token'\s*:\s*'([^']+)'")
    while time.time() < end:
        try:
            if not os.path.exists(trace):
                time.sleep(0.2)
                continue
            text_data = open(trace, "r", encoding="utf-8", errors="ignore").read()
            # Search for lines containing our email; parse from last such occurrence
            lines = [l for l in text_data.splitlines() if "EMAIL_SENT" in l and email in l]
            if not lines:
                time.sleep(0.2)
                continue
            last = lines[-1]
            m = token_re.search(last)
            if m:
                return m.group(1)
        except Exception:
            pass
        time.sleep(0.2)
    return None


def run_migrations_or_fallback(env: dict) -> bool:
    """
    Prefer alembic upgrade head if alembic CLI present; else fallback to SQLAlchemy create_all using models.
    """
    # Prefer alembic upgrade
    if check_executable("alembic"):
        _trace("MIGRATIONS: alembic detected, attempting 'alembic upgrade head'")
        try:
            res = subprocess.run(["alembic", "upgrade", "head"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
            _trace(f"ALEMBIC_OUTPUT {res.returncode} {res.stdout[:1000]}")
            if res.returncode != 0:
                _trace("ALEMBIC_FAILED falling back to metadata.create_all")
            else:
                return True
        except Exception as e:
            _trace(f"ALEMBIC_RUN_ERROR {str(e)}")
    # Fallback
    _trace("MIGRATIONS: falling back to SQLAlchemy create_all")
    try:
        # Use project's models to create tables
        # Import here to ensure DATABASE_URL env is set externally before calling
        import models
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker, scoped_session
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            _trace("MIGRATION_FALLBACK no DATABASE_URL env")
            return False
        connect_args = {"connect_args": {"check_same_thread": False}} if db_url.startswith("sqlite") else {}
        # create_engine signature used as in app.create_app
        engine = create_engine(db_url, **connect_args) if connect_args else create_engine(db_url)
        SessionLocal = scoped_session(sessionmaker(bind=engine))
        models.init_db(engine, SessionLocal)
        models.Base.metadata.create_all(engine)
        _trace("MIGRATION_FALLBACK create_all completed")
        return True
    except Exception as e:
        _trace(f"MIGRATION_FALLBACK_ERROR {str(e)}")
        return False


def start_gunicorn(env: dict, bind_addr: str = "127.0.0.1:8000"):
    """
    Start gunicorn pointing at wsgi.prod_wsgi:app with a single worker.
    Returns subprocess.Popen instance.
    """
    if not check_executable("gunicorn"):
        _exit("gunicorn executable not found in PATH; required for prod-like smoke test")

    cmd = [
        "gunicorn",
        "--capture-output",
        "--error-logfile", GUNICORN_LOG,
        "--access-logfile", GUNICORN_LOG,
        "--workers", "1",
        "--bind", bind_addr,
        "wsgi.prod_wsgi:app",
    ]
    _trace(f"GUNICORN_START cmd={' '.join(cmd)}")
    proc = subprocess.Popen(cmd, env=env)
    return proc


def stop_container(container_name: str):
    try:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except Exception:
        pass


def main():
    _trace("SMOKE_STARTED")
    # Quick environment safety checks
    if not check_executable("docker"):
        _exit("docker CLI not found; cannot launch ephemeral Postgres container")

    # Choose ephemeral host port for Postgres to avoid collisions
    host_port = find_free_port()
    container_name = f"smoke_pg_{int(time.time())}_{random.randint(1000,9999)}"
    pg_password = "postgres"
    pg_db = "smoke_db"
    image = os.environ.get("SMOKE_PG_IMAGE", "postgres:15-alpine")

    # Ensure we cleanup previous container if exists (best-effort)
    stop_container(container_name)

    # Launch Postgres container binding host_port -> container 5432
    docker_cmd = [
        "docker", "run", "--name", container_name,
        "-e", f"POSTGRES_PASSWORD={pg_password}",
        "-e", f"POSTGRES_DB={pg_db}",
        "-p", f"{host_port}:5432",
        "-d", image
    ]
    _trace(f"DOCKER_RUN {' '.join(docker_cmd)}")
    try:
        res = subprocess.run(docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode != 0:
            _trace(f"DOCKER_RUN_FAILED rc={res.returncode} out={res.stdout[:400]} err={res.stderr[:400]}")
            _exit(f"docker run failed: {res.stderr.strip()}")
        container_id = (res.stdout or "").strip()
        _trace(f"DOCKER_STARTED id={container_id} mapped_port={host_port}")
    except Exception as e:
        _exit(f"docker run raised exception: {str(e)}")

    # Ensure container removed on exit (best effort)
    def _cleanup(signum=None, frame=None):
        _trace("SMOKE_CLEANUP start")
        try:
            stop_container(container_name)
            _trace("SMOKE_CLEANUP container removed")
        except Exception:
            pass
        _trace("SMOKE_CLEANUP end")

    # Register basic signals
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _cleanup)

    # Compose DATABASE_URL
    database_url = f"postgresql://postgres:{pg_password}@127.0.0.1:{host_port}/{pg_db}"
    # Export into environment for downstream commands
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    # Minimal production-like overrides (keep conservative defaults)
    env.update({
        "SECRET_KEY": env.get("SECRET_KEY", "smoke-secret"),
        "JWT_SECRET": env.get("JWT_SECRET", env.get("SECRET_KEY", "smoke-secret")),
        "JWT_COOKIE_SECURE": "true",  # prod-like
        "JWT_SAMESITE": "Lax",
        "JWT_COOKIE_NAME": "smartlink_jwt",
    })

    masked_db = _mask_db_url(database_url)
    _trace(f"WAIT_POSTGRES starting wait for DB at {masked_db}")
    if not wait_for_postgres(database_url, timeout=40, interval=1.0):
        _cleanup()
        _exit("Postgres didn't become available in time")

    _trace("POSTGRES_READY")

    # Run migrations (alembic preferred, fallback to create_all)
    if not run_migrations_or_fallback(env):
        _cleanup()
        _exit("Migrations failed (alembic or create_all fallback)")

    _trace("MIGRATIONS_APPLIED")

    # Start gunicorn
    bind = "127.0.0.1:8000"
    proc = None
    try:
        proc = start_gunicorn(env=env, bind_addr=bind)
        _trace(f"GUNICORN_PID {getattr(proc, 'pid', None)}")
    except Exception as e:
        _trace(f"GUNICORN_START_ERROR {str(e)}")
        _cleanup()
        _exit("Failed to start Gunicorn")

    try:
        # Wait for health endpoint
        health_url = f"http://127.0.0.1:8000/health"
        if not wait_for_http(health_url, timeout=30, interval=0.5, expected_text="ok"):
            # Dump gunicorn logs for debugging
            _trace("GUNICORN_HEALTHCHECK_FAILED")
            try:
                if os.path.exists(GUNICORN_LOG):
                    with open(GUNICORN_LOG, "r", encoding="utf-8", errors="ignore") as f:
                        _trace("GUNICORN_LOG_START")
                        _trace(f.read()[:4000])
                        _trace("GUNICORN_LOG_END")
            except Exception:
                pass
            raise RuntimeError("Gunicorn did not respond on /health in time")

        _trace("GUNICORN_HEALTHCHECK_OK")

        # Run smoke scenario using requests.Session to preserve cookies
        try:
            import requests
            sess = requests.Session()
        except Exception:
            sess = None

        base = f"http://127.0.0.1:8000"

        # 1) Register
        smoke_email = f"smoke_{int(time.time())}@example.com"
        smoke_password = "Sm0kePass!23"

        _trace(f"SCENARIO_REGISTER email={smoke_email}")
        # GET register to obtain CSRF and cookie
        try:
            if sess:
                r = sess.get(f"{base}/auth/register", timeout=5)
                r.raise_for_status()
                html = r.text
            else:
                # fallback: urllib
                from urllib.request import urlopen, Request
                req = Request(f"{base}/auth/register", headers={"User-Agent": "smoke-ci/1.0"})
                with urlopen(req, timeout=5) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
            # Extract csrf token
            m = re.search(r'name\s*=\s*["\']csrf_token["\']\s+value\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
            csrf_val = m.group(1) if m else ""
        except Exception as e:
            raise RuntimeError(f"Failed getting registration form: {str(e)}")

        # POST register
        post_data = {"email": smoke_email, "password": smoke_password}
        if csrf_val:
            post_data["csrf_token"] = csrf_val
        try:
            if sess:
                r2 = sess.post(f"{base}/auth/register", data=post_data, timeout=8)
                # Accept 200 OK
                if r2.status_code not in (200, 201):
                    raise RuntimeError(f"Register returned status {r2.status_code}")
            else:
                # urllib fallback - send form-encoded
                from urllib.parse import urlencode
                from urllib.request import urlopen, Request
                body = urlencode(post_data).encode("utf-8")
                req = Request(f"{base}/auth/register", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
                with urlopen(req, timeout=8) as resp:
                    if resp.getcode() not in (200, 201):
                        raise RuntimeError(f"Register returned code {resp.getcode()}")
        except Exception as e:
            raise RuntimeError(f"Registration POST failed: {str(e)}")

        _trace("SCENARIO_REGISTER_SUBMITTED")

        # 2) Extract token from trace_KAN-110.txt that the dev email stub wrote
        _trace("SCENARIO_WAIT_FOR_EMAIL_TOKEN")
        token = extract_token_from_trace(smoke_email, timeout=10)
        if not token:
            raise RuntimeError("Could not extract verification token from trace_KAN-110.txt (dev email stub)")

        _trace(f"SCENARIO_VERIFY_TOKEN token_present length={len(token)}")
        # 3) Verify email
        try:
            vresp = sess.get(f"{base}/auth/verify-email/{token}", timeout=8) if sess else None
            if vresp is not None and vresp.status_code not in (200, 302):
                raise RuntimeError(f"Verify returned status {vresp.status_code}")
        except Exception as e:
            raise RuntimeError(f"Email verification GET failed: {str(e)}")

        _trace("SCENARIO_VERIFIED")

        # 4) Login (get login form for fresh CSRF)
        try:
            login_g = sess.get(f"{base}/auth/login", timeout=5)
            login_g.raise_for_status()
            login_html = login_g.text
            m2 = re.search(r'name\s*=\s*["\']csrf_token["\']\s+value\s*=\s*["\']([^"\']+)["\']', login_html, re.IGNORECASE)
            login_csrf = m2.group(1) if m2 else ""
        except Exception as e:
            raise RuntimeError(f"Failed to GET login form: {str(e)}")

        # POST login
        login_payload = {"email": smoke_email, "password": smoke_password}
        if login_csrf:
            login_payload["csrf_token"] = login_csrf
        try:
            login_post = sess.post(f"{base}/auth/login", data=login_payload, timeout=8)
            # Accept 200 or 302
            if login_post.status_code not in (200, 302):
                raise RuntimeError(f"Login failed status {login_post.status_code}")
        except Exception as e:
            raise RuntimeError(f"Login POST failed: {str(e)}")

        # After login, ensure cookie exists
        cookie_name = sess.cookies.keys() if sess else []
        _trace(f"SCENARIO_LOGIN_COOKIE cookie_names={','.join(list(cookie_name))}")

        # 5) Create Short URL via /shorten flow: GET form to extract CSRF, then POST; use demo user_id if app requires
        _trace("SCENARIO_SHORTEN_FLOW")
        try:
            g = sess.get(f"{base}/shorten", timeout=5)
            g.raise_for_status()
            shorten_html = g.text
            m3 = re.search(r'name\s*=\s*["\']csrf_token["\']\s+value\s*=\s*["\']([^"\']+)["\']', shorten_html, re.IGNORECASE)
            shorten_csrf = m3.group(1) if m3 else ""
        except Exception as e:
            raise RuntimeError(f"Failed to GET shorten form: {str(e)}")

        # To avoid relying on server-side g.current_user middleware, the shorten form supports demo user_id when ALLOW_DEMO_USER_ID is True.
        # We'll attempt to find a user id in DB via an API /shorten/list?user_id= that the app exposes? Simpler: create short using logged-in session.
        # Post payload: target_url
        short_payload = {"target_url": "http://example.com/smoke-target"}
        if shorten_csrf:
            short_payload["csrf_token"] = shorten_csrf
        # If the server requires ALLOW_DEMO_USER_ID, attempt to use that path by reading the shorten page for input name 'user_id'
        if 'name="user_id"' in shorten_html:
            # Prefer using the confirmed user id by asking the server to expose a list or by trying a simple approach: request dashboard listing not present; we will pass user_id=1 as a fallback (many CI DBs start empty)
            # Safer: fetch sessions? Instead, pass no user_id and let authenticated session be used.
            pass

        try:
            create_resp = sess.post(f"{base}/shorten", data=short_payload, timeout=8)
            if create_resp.status_code not in (200, 201):
                raise RuntimeError(f"Shorten POST returned status {create_resp.status_code} body={create_resp.text[:200]!r}")
            create_text = create_resp.text
        except Exception as e:
            raise RuntimeError(f"Shorten POST failed: {str(e)}")

        # Try to extract slug from returned page (pattern used by routes/shortener)
        mslug = re.search(r"Slug:\s*<strong>([^<]+)</strong>", create_text)
        if not mslug:
            # Fallback: search for 'Short Link:' href
            mlink = re.search(r"Short Link:\s*<a href=[\"']([^\"']+)[\"']", create_text)
            if mlink:
                short_url = mlink.group(1)
                slug = short_url.rstrip("/").split("/")[-1]
            else:
                raise RuntimeError("Could not parse created slug from shorten response")
        else:
            slug = mslug.group(1).strip()

        _trace(f"SCENARIO_SHORT_CREATED slug={slug}")

        # 6) Hit public redirect
        redirect_url = f"{base}/{slug}"
        _trace(f"SCENARIO_REDIRECT_HIT url={redirect_url}")
        try:
            r = sess.get(redirect_url, allow_redirects=False, timeout=8)
            if r.status_code not in (301, 302, 303, 307, 308):
                raise RuntimeError(f"Redirect endpoint returned unexpected status {r.status_code}")
            _trace(f"SCENARIO_REDIRECT_OK status={r.status_code} location={r.headers.get('Location')}")
        except Exception as e:
            raise RuntimeError(f"Redirect request failed: {str(e)}")

        # All done; success
        _trace("SMOKE_SCENARIO_COMPLETE SUCCESS")
        print("SMOKE RUN: SUCCESS")
        return 0

    except Exception as e:
        _trace(f"SMOKE_ERROR {str(e)}")
        # Dump gunicorn log for debugging
        try:
            if os.path.exists(GUNICORN_LOG):
                with open(GUNICORN_LOG, "r", encoding="utf-8", errors="ignore") as f:
                    _trace("GUNICORN_LOG_ON_ERROR_START")
                    _trace(f.read()[:4000])
                    _trace("GUNICORN_LOG_ON_ERROR_END")
        except Exception:
            pass
        print("SMOKE RUN FAILED:", str(e))
        return 2
    finally:
        _trace("SMOKE_TEARDOWN begin")
        try:
            if proc and getattr(proc, "poll", lambda: None)() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass
        # Remove Postgres container
        try:
            stop_container(container_name)
        except Exception:
            pass
        _trace("SMOKE_TEARDOWN end")


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
--- END FILE: bin/smoke_ci.py ---