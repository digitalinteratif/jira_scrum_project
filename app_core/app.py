"""app.py - Application factory and bootstrap for modular Flask app."""

import os
import datetime
from flask import Flask, jsonify
from flask_wtf import CSRFProtect

# Try/except for Flask-Talisman per dependency tolerance
try:
    from flask_talisman import Talisman
except Exception:
    Talisman = None

# SQLAlchemy core imports
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session

import models

# Structured logging & request instrumentation imports (KAN-141)
import logging
import sys
import uuid
import time
import json
from flask import g, request as _request

def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    # Basic config
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key"),
        DATABASE_URL=os.environ.get("DATABASE_URL", "sqlite:///local_dev.db"),
        JWT_SECRET=os.environ.get("JWT_SECRET", os.environ.get("SECRET_KEY", "dev-secret-key")),
        # token expiry in seconds (24h)
        EMAIL_VERIFY_EXPIRY_SECONDS=int(os.environ.get("EMAIL_VERIFY_EXPIRY_SECONDS", 24 * 3600)),
        # Add this new default into the existing mapping
        "DATA_RETENTION_DAYS": int(os.environ.get("DATA_RETENTION_DAYS", 90)),

        # --- Security / session cookie defaults (KAN-128) ---
        # Name of the cookie used to transport session/JWT tokens (can be overridden per deploy)
        JWT_COOKIE_NAME=os.environ.get("JWT_COOKIE_NAME", "smartlink_jwt"),
        # Whether cookies should be marked 'Secure'. Default False for local/dev, should be True in prod.
        JWT_COOKIE_SECURE=(os.environ.get("JWT_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")),
        # SameSite attribute for the JWT cookie. Accepts 'Lax', 'Strict', or 'None'.
        JWT_SAMESITE=os.environ.get("JWT_SAMESITE", "Lax"),

        # Security header defaults; these can be overridden in prod config
        SECURE_X_FRAME_OPTIONS=os.environ.get("SECURE_X_FRAME_OPTIONS", "DENY"),
        SECURE_X_CONTENT_TYPE_OPTIONS=os.environ.get("SECURE_X_CONTENT_TYPE_OPTIONS", "nosniff"),
        SECURE_REFERRER_POLICY=os.environ.get("SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin"),
        # A conservative default CSP that allows only same-origin resources and denies frames/objects.
        # Operators should review CSP for their UI needs; this default is intentionally restrictive.
        SECURE_CONTENT_SECURITY_POLICY=os.environ.get("SECURE_CONTENT_SECURITY_POLICY",
            "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; script-src 'self'"),
    )

    if test_config:
        app.config.update(test_config)

    # Initialize DB engine & session factory and expose on models module
    engine = create_engine(app.config["DATABASE_URL"], connect_args={"check_same_thread": False} if "sqlite" in app.config["DATABASE_URL"] else {})
    SessionLocal = scoped_session(sessionmaker(bind=engine))
    models.init_db(engine, SessionLocal)

    # Create tables in dev/local (in production migrations would be used)
    models.Base.metadata.create_all(engine)

    # -----------------------------
    # Structured logging initialization (KAN-141)
    # -----------------------------
    try:
        # Defensive import of our logging initializer. If the module isn't present or fails, fall back to basic logging.
        try:
            from utils.logging import init_logging, get_logger  # new surgical module
            init_logging(app)
            _logger = get_logger("smartlink")
        except Exception:
            # fallback to basic logger config
            logging.basicConfig(level=logging.INFO)
            _logger = logging.getLogger("smartlink_fallback")
            _logger.info("utils.logging unavailable; using fallback logging")
        # Attach logger to app for use elsewhere
        app.logger = _logger
    except Exception:
        # Do not allow logging setup to break app creation
        try:
            logging.basicConfig(level=logging.INFO)
        except Exception:
            pass
        _logger = logging.getLogger("smartlink_fallback")

    # -----------------------------
    # Optional Sentry initialization (KAN-141)
    # -----------------------------
    try:
        sentry_dsn = app.config.get("SENTRY_DSN") or os.environ.get("SENTRY_DSN")
        if sentry_dsn:
            try:
                import sentry_sdk
                from sentry_sdk.integrations.flask import FlaskIntegration
                traces_sample_rate = float(app.config.get("SENTRY_TRACES_SAMPLE_RATE", 0.0))
                sentry_sdk.init(dsn=sentry_dsn, integrations=[FlaskIntegration()], traces_sample_rate=traces_sample_rate)
                try:
                    with open("trace_KAN-141.txt", "a") as f:
                        f.write(f"{datetime.datetime.utcnow().isoformat()} SENTRY_INITIALIZED dsn_set=1\n")
                except Exception:
                    pass
                _logger.info("Sentry initialized (DSN provided)")
            except Exception as e:
                _logger.warning("sentry_sdk not available or init failed; continuing without Sentry: %s", str(e))
    except Exception:
        # Do not allow Sentry initialization to break app creation
        try:
            _logger.exception("Error during Sentry init")
        except Exception:
            pass

    # CSRF
    try:
        csrf = CSRFProtect()
        csrf.init_app(app)
        app.csrf_protect = csrf
    except Exception:
        # Fallback: No runtime CSRF integration, but forms still render a token
        app.csrf_protect = None

    # Talisman (security headers)
    # If flask_talisman is available, prefer to configure it using our config so it enforces HTTPS & headers.
    if Talisman is not None:
        try:
            # Map config-driven policies into Talisman options where applicable. Keep defensive try/except.
            talisman_kwargs = {}
            # Content Security Policy may be a string; Talisman accepts dict or string in newer versions.
            csp = app.config.get("SECURE_CONTENT_SECURITY_POLICY")
            if csp:
                talisman_kwargs["content_security_policy"] = csp
            # Referrer policy
            rp = app.config.get("SECURE_REFERRER_POLICY")
            if rp:
                talisman_kwargs["referrer_policy"] = rp
            # Frame options (X-Frame-Options)
            # Some Talisman versions accept 'frame_options' param; use defensively.
            try:
                talisman_kwargs["frame_options"] = app.config.get("SECURE_X_FRAME_OPTIONS")
            except Exception:
                pass

            # Enforce HTTPS only when JWT_COOKIE_SECURE is True or when explicitly configured
            force_https = bool(app.config.get("JWT_COOKIE_SECURE", False))
            if force_https:
                talisman_kwargs["force_https"] = True

            # Apply Talisman with our conservative kwargs. If any kwarg isn't supported by installed Talisman, fallback to simple init.
            try:
                Talisman(app, **talisman_kwargs)
            except Exception:
                # Best-effort: attach without kwargs to avoid breaking app creation.
                Talisman(app)
        except Exception:
            # If any issue initializing Talisman, continue without raising (dependency tolerance).
            try:
                Talisman(app)
            except Exception:
                pass

    # Register blueprints (auth)
    from routes.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")

    # Register sessions management blueprint (KAN-129)
    try:
        from routes.sessions import sessions_bp
        app.register_blueprint(sessions_bp)
    except Exception:
        # Do not break app creation if sessions module not present in constrained test runs
        pass

    # Register shortener blueprint (creates & redirects short URLs)
    try:
        from routes.shortener import shortener_bp
        app.register_blueprint(shortener_bp)
    except Exception:
        # If import fails (e.g., tests that don't include the file), continue without breaking app creation
        pass

    # Register API blueprint (KAN-145) - programmatic shorten endpoint
    try:
        from routes.api import api_bp
        app.register_blueprint(api_bp, url_prefix="/api")
    except Exception:
        # Do not break app creation if API module not present in constrained runs
        pass

    # Register custom domains blueprint (KAN-144)
    try:
        from routes.domains import domains_bp
        app.register_blueprint(domains_bp)
    except Exception:
        # Do not break app creation if domains module not present in constrained test runs
        pass

    # Register dashboard blueprint (KAN-117) - surgical addition using Blueprints
    try:
        from routes.dashboard import dashboard_bp
        app.register_blueprint(dashboard_bp)
    except Exception:
        # If import fails, do not break app creation
        pass

    # Register analytics blueprint (KAN-120) - surgical addition for per-link analytics
    # This registration is intentionally best-effort to avoid breaking environments that may not include the file.
    try:
        from routes.analytics import analytics_bp
        app.register_blueprint(analytics_bp)
    except Exception:
        # Do not break app creation if analytics module can't be imported in constrained test runs.
        pass

    # Register tools/linting blueprint (KAN-125) - surgical addition to expose linting utilities when enabled
    try:
        from routes.tools_lint import tools_bp
        # Expose under /tools prefix
        app.register_blueprint(tools_bp, url_prefix="/tools")
    except Exception:
        # Non-critical; if routes/tools_lint.py not present in some test runs, continue
        pass

    # Register PR guidelines blueprint (KAN-139) - surgical addition to expose PR checklist & guidelines (read-only)
    try:
        from routes.pr_guidelines import pr_guidelines_bp
        # Expose under /tools prefix alongside other dev tools
        app.register_blueprint(pr_guidelines_bp, url_prefix="/tools")
    except Exception:
        # Do not break app creation if pr_guidelines module not present in constrained test runs
        pass

    # Register contributing / onboarding blueprint (KAN-140) - surgical addition to expose CONTRIBUTING.md
    try:
        from routes.contributing import contributing_bp
        # Expose under /tools prefix alongside other dev tools
        app.register_blueprint(contributing_bp, url_prefix="/tools")
    except Exception:
        # Do not break app creation if contributing module not present in constrained test runs
        pass

    # -----------------------------
    # Request instrumentation hooks (KAN-141)
    # -----------------------------
    @app.before_request
    def _record_start_time_and_request_id():
        """
        Attach request_id and start_time to flask.g for per-request logging and tracing (KAN-141).
        If client provides X-Request-ID, respect it; otherwise generate a new UUID4 hex.
        """
        try:
            rid = _request.headers.get("X-Request-ID")
            if not rid:
                rid = uuid.uuid4().hex
            g.request_id = rid
            # epoch seconds float
            g.start_time = time.time()
        except Exception:
            try:
                # best-effort defaults
                g.request_id = uuid.uuid4().hex
                g.start_time = time.time()
            except Exception:
                pass

    # health endpoint
    @app.route("/health")
    def health():
        return "ok", 200

    @app.route("/_stability_check")
    def stability_check():
        """
        Lightweight DB check to help external stability tests exercise the app entry point.
        Returns 200 if a trivial DB statement runs successfully; otherwise returns 500.
        """
        session = None
        try:
            session = models.Session()
            # Use SQLAlchemy text to remain compatible across DB backends
            session.execute(text("SELECT 1"))
            return jsonify({"status": "ok"}), 200
        except Exception:
            # Do not leak internals; return generic failure
            return jsonify({"status": "error"}), 500
        finally:
            try:
                if session is not None:
                    # session may be a Session instance from scoped_session()
                    session.close()
            except Exception:
                pass

    # Application-level helper to set JWT cookie consistently (KAN-128)
    # Other modules can call app.set_jwt_cookie(response, token, max_age=...)
    def _set_jwt_cookie(response, token, max_age=None, path="/"):
        """
        Attach the JWT token into the configured cookie name with secure defaults:
          - httponly=True
          - secure=app.config['JWT_COOKIE_SECURE']
          - samesite=app.config['JWT_SAMESITE']
        This helper is defensive and will not raise if response.set_cookie fails.
        """
        try:
            cname = app.config.get("JWT_COOKIE_NAME", "smartlink_jwt")
            secure_flag = bool(app.config.get("JWT_COOKIE_SECURE", False))
            samesite = app.config.get("JWT_SAMESITE", "Lax")
            # Flask's set_cookie may accept samesite values; ensure proper call
            response.set_cookie(
                cname,
                token,
                httponly=True,
                secure=secure_flag,
                samesite=samesite,
                max_age=max_age,
                path=path,
            )
        except Exception:
            # Do not raise; best-effort cookie setting only
            pass
        return response

    # Attach helper to app instance for use by blueprints / utilities
    app.set_jwt_cookie = _set_jwt_cookie

    # After-request handler to enforce security headers from configuration (KAN-128)
    @app.after_request
    def _apply_security_headers(response):
        """
        Ensure responses include key security headers. Values come from app.config with
        conservative defaults established above. This runs for every response and is
        intentionally idempotent (overwrites existing values to ensure policy).
        """
        try:
            # Prepare header values from config with safe defaults
            x_frame = app.config.get("SECURE_X_FRAME_OPTIONS", "DENY")
            x_cto = app.config.get("SECURE_X_CONTENT_TYPE_OPTIONS", "nosniff")
            referrer = app.config.get("SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")
            csp = app.config.get("SECURE_CONTENT_SECURITY_POLICY", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; script-src 'self'")

            # Apply headers (unconditionally set to ensure enforcement)
            try:
                response.headers["X-Frame-Options"] = x_frame
            except Exception:
                pass
            try:
                response.headers["X-Content-Type-Options"] = x_cto
            except Exception:
                pass
            try:
                response.headers["Referrer-Policy"] = referrer
            except Exception:
                pass
            try:
                # CSP can be long; set as provided
                response.headers["Content-Security-Policy"] = csp
            except Exception:
                pass

            # Optionally add other security headers if present in config (extensible)
            # For backward compatibility, honor any dict stored in SECURE_ADDITIONAL_HEADERS
            try:
                additional = app.config.get("SECURE_ADDITIONAL_HEADERS", {})
                if isinstance(additional, dict):
                    for hk, hv in additional.items():
                        try:
                            response.headers[str(hk)] = str(hv)
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            # Do not allow header-application failures to break responses
            pass

        return response

    # -----------------------------
    # Structured access logging after_request (KAN-141)
    # -----------------------------
    @app.after_request
    def _structured_access_log(response):
        """
        Emit a compact structured JSON access log per request (KAN-141).
        Fields: timestamp, request_id, method, path, status, duration_ms, remote_addr, user_id (if available)
        Adds X-Request-ID header to response.
        """
        try:
            start = getattr(g, "start_time", None)
            duration_ms = None
            if start:
                try:
                    duration_ms = int((time.time() - float(start)) * 1000)
                except Exception:
                    duration_ms = None
            record = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "request_id": getattr(g, "request_id", "") or "",
                "method": _request.method,
                "path": _request.path,
                "status": getattr(response, "status_code", None),
                "duration_ms": duration_ms,
                "remote_addr": _request.remote_addr or "",
            }
            # include user_id if the request has a recognized g.current_user
            try:
                cu = getattr(g, "current_user", None)
                if cu and getattr(cu, "id", None):
                    record["user_id"] = int(cu.id)
            except Exception:
                pass

            # Log as INFO with structured JSON payload
            try:
                # Prefer app.logger (initialized above) else Python logging
                if hasattr(app, "logger") and app.logger:
                    # include request_id on the LogRecord via extra so JSONFormatter may surface it
                    app.logger.info(json.dumps(record, separators=(",", ":"), ensure_ascii=False), extra={"request_id": record.get("request_id")})
                else:
                    logging.getLogger("smartlink").info(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            except Exception:
                try:
                    # As last-resort, write to stdout
                    sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
                except Exception:
                    pass

            # Attach X-Request-ID header for downstream visibility
            try:
                response.headers["X-Request-ID"] = getattr(g, "request_id", "") or ""
            except Exception:
                pass
        except Exception:
            try:
                # ensure logging of any unexpected error in logging pipeline doesn't break response
                if hasattr(app, "logger") and app.logger:
                    app.logger.exception("Error while emitting access log")
            except Exception:
                pass
        return response

    # Global exception/error handler (catch-all). It logs exception details and returns a safe HTML page.
    try:
        from utils.templates import render_layout as _render_layout  # local alias for safety
    except Exception:
        # As a fallback, define a minimal render wrapper
        def _render_layout(inner_html: str):
            return f"<html><body>{inner_html}</body></html>"

    @app.errorhandler(Exception)
    def _handle_unexpected_exception(err):
        """
        Global error handler registered for Exception. Logs the exception with stacktrace,
        emits to Sentry if configured, and returns a safe HTML response wrapped via render_layout.
        """
        # Acquire a best-effort request_id
        try:
            rid = getattr(g, "request_id", None) or _request.headers.get("X-Request-ID") or uuid.uuid4().hex
        except Exception:
            rid = uuid.uuid4().hex

        # Log exception with stacktrace
        try:
            # app.logger.exception will include stacktrace
            if hasattr(app, "logger") and app.logger:
                app.logger.exception("Unhandled exception occurred (request_id=%s path=%s): %s", rid, getattr(_request, "path", ""), str(err), extra={"request_id": rid})
            else:
                logging.exception("Unhandled exception occurred (request_id=%s): %s", rid, str(err))
        except Exception:
            pass

        # Capture in Sentry if available (best-effort)
        try:
            # use sentry_sdk if it was successfully imported during init
            if "sentry_sdk" in globals():
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception(err)
                except Exception:
                    pass
        except Exception:
            pass

        # Write a trace to trace_KAN-141.txt for Architectural Memory
        try:
            with open("trace_KAN-141.txt", "a") as f:
                f.write(f"{datetime.datetime.utcnow().isoformat()} EXCEPTION request_id={rid} path={getattr(_request, 'path', '')} exc={str(err)}\n")
        except Exception:
            pass

        # Return a safe HTML page (do not leak stacktrace in body)
        try:
            body = _render_layout(f"<h1>Server Error</h1><p>An internal error occurred. Reference ID: {rid}</p>")
            return body, 500
        except Exception:
            # Ultimate fallback
            return ("Internal Server Error", 500)

    # Application-level helper to set JWT cookie consistently (KAN-128)
    # Other modules can call app.set_jwt_cookie(response, token, max_age=...)
    def _set_jwt_cookie(response, token, max_age=None, path="/"):
        """
        Attach the JWT token into the configured cookie name with secure defaults:
          - httponly=True
          - secure=app.config['JWT_COOKIE_SECURE']
          - samesite=app.config['JWT_SAMESITE']
        This helper is defensive and will not raise if response.set_cookie fails.
        """
        try:
            cname = app.config.get("JWT_COOKIE_NAME", "smartlink_jwt")
            secure_flag = bool(app.config.get("JWT_COOKIE_SECURE", False))
            samesite = app.config.get("JWT_SAMESITE", "Lax")
            # Flask's set_cookie may accept samesite values; ensure proper call
            response.set_cookie(
                cname,
                token,
                httponly=True,
                secure=secure_flag,
                samesite=samesite,
                max_age=max_age,
                path=path,
            )
        except Exception:
            # Do not raise; best-effort cookie setting only
            pass
        return response

    # Attach helper to app instance for use by blueprints / utilities
    app.set_jwt_cookie = _set_jwt_cookie

    return app
--- END FILE ---