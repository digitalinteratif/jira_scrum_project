try:
    from flask import (
        Flask,
        Blueprint,
        render_template_string,
        request,
        redirect,
        url_for,
        jsonify,
        current_app,
        g,
        make_response,
    )
except Exception as e:
    raise ImportError("Flask is required. Install via `pip install Flask`") from e

# Optional CSRF protection wiring
try:
    from flask_wtf import CSRFProtect
except Exception:
    CSRFProtect = None  # handle gracefully; tests/dev may not have it

try:
    # generate_csrf is used to populate csrf_token() in templates
    from flask_wtf.csrf import generate_csrf
except Exception:
    generate_csrf = None

# SQLAlchemy imports (guarded)
try:
    from sqlalchemy import (
        create_engine,
        Column,
        Integer,
        String,
        DateTime,
        Boolean,
        Text,
        ForeignKey,
        MetaData,
    )
    from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker, relationship
    from sqlalchemy.exc import IntegrityError
except Exception:
    # Surface a clear error later when DB is required; for now keep symbols as None/placeholders.
    create_engine = None
    Column = None
    Integer = None
    String = None
    DateTime = None
    Boolean = None
    Text = None
    ForeignKey = None
    MetaData = None
    declarative_base = None
    scoped_session = None
    sessionmaker = None
    relationship = None
    IntegrityError = Exception  # fallback

# PyJWT import (guarded)
try:
    import jwt  # PyJWT
    from jwt import ExpiredSignatureError, InvalidTokenError
except Exception:
    jwt = None
    ExpiredSignatureError = Exception
    InvalidTokenError = Exception

# argon2 (guarded) for password hashing
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError as Argon2VerifyMismatchError
except Exception:
    PasswordHasher = None
    Argon2VerifyMismatchError = Exception

import os
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Dict, Any
import secrets

logger = logging.getLogger(__name__)

# -------------------------
# Template strings
# -------------------------
_layout_template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Smart Link - Dev</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; }
    nav { margin-bottom: 1rem; }
    .container { max-width: 800px; margin: auto; }
    .error { color: #a00; }
    .success { color: #060; }
  </style>
</head>
<body>
  <nav>
    <a href="/">Home</a> |
    <a href="/auth/">Auth</a> |
    <a href="/s/">Shortener</a> |
    <a href="/analytics/">Analytics</a>
  </nav>
  <div class="container">
    {{ body | safe }}
  </div>
  <footer style="margin-top: 2rem; font-size: .9rem; color: #666;">
    Running in DEV mode. Do not use this secret in production.
  </footer>
</body>
</html>
"""

_auth_index_template = """<h1>Auth</h1>
<p>
  Use <a href="/auth/register">Register</a> to see a form sample with CSRF.
</p>

<!-- Logout form: included here to surface a safe, CSRF-protected POST to /auth/logout.
     Forms must include the mandatory CSRF hidden input per the Epic. -->
<form method="POST" action="/auth/logout" style="margin-top:1rem;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <button type="submit">Logout (invalidate token cookie)</button>
</form>
"""

_auth_register_template = """<h2>Register (dev scaffold)</h2>
<form method="POST" action="/auth/register">
  <!-- MANDATORY CSRF token field as required by the Epic -->
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label>Email: <input name="email" type="email" required /></label>
  </div>
  <div>
    <label>Password: <input name="password" type="password" required /></label>
  </div>
  <div>
    <button type="submit">Register</button>
  </div>
</form>
<p>Note: In this dev scaffold passwords may be hashed with argon2 if available.</p>
"""

_auth_register_success_template = """<h2 class="success">Registration Successful</h2>
<p>User created with email: {{ email }}</p>
<p><a href="/auth/">Back to Auth Index</a></p>
"""

_auth_register_error_template = """<h2 class="error">Registration Error</h2>
<p class="error">{{ message }}</p>
<form method="GET" action="/auth/register">
  <button type="submit">Try Again</button>
</form>
"""

_shorten_index_template = """<h2>Shorten a URL</h2>
<form method="POST" action="/s/create">
  <!-- MANDATORY CSRF token field as required by the Epic -->
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label>Target URL: <input name="target_url" type="url" placeholder="https://example.com" required /></label>
  </div>
  <div>
    <button type="submit">Create Short Link</button>
  </div>
</form>
"""

_shorten_result_template = """<h2>Short Link Created (dev scaffold)</h2>
<p>Target: {{ target }}</p>
<p>(Creation omitted in scaffold)</p>
"""

_analytics_index_template = """<h2>Analytics</h2>
<p>Minimal analytics scaffold.</p>
"""

# -------------------------
# render_layout: UI Wrapper helper (no {% extends %} or {% block %})
# -------------------------
def render_layout(content_or_template_string, **context):
    """
    If content_or_template_string looks like a template (contains templating markers or '<'),
    we render it with the provided context and then inject the result into the global layout.
    This avoids using Jinja2 {% extends %} or {% block %} constructs.
    """
    # Heuristic: if the string contains '<' or '{{' or '{%', treat it as a template to render
    if isinstance(content_or_template_string, str) and (
        "<" in content_or_template_string or "{{" in content_or_template_string or "{%" in content_or_template_string
    ):
        body_html = render_template_string(content_or_template_string, **context)
    else:
        # Already rendered content passed in
        body_html = content_or_template_string
    return render_template_string(_layout_template, body=body_html)


# -------------------------
# models (surgical)
# -------------------------
# Ensure SQLAlchemy is available
if declarative_base is None:
    # Provide a clear ImportError to help developer
    raise ImportError("SQLAlchemy is required. Install via `pip install SQLAlchemy`")

# Naming convention to ensure predictable constraint/index names across DBs.
naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=naming_convention)
Base = declarative_base(metadata=metadata)

# Module-level session factory (initialized by init_db)
SessionLocal = None
_engine = None


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    failed_login_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<User id={self.id} email={self.email}>"


class SessionToken(Base):
    __tablename__ = "session_tokens"
    id = Column(Integer, primary_key=True)
    jti = Column(String(64), nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("users.id", name="fk_session_tokens_user_id_users"), nullable=False)
    issued_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)

    user = relationship("User", backref="session_tokens")

    def __repr__(self):
        return f"<SessionToken jti={self.jti} user_id={self.user_id} revoked={self.revoked}>"


# New model: RevokedToken
class RevokedToken(Base):
    """
    Tracks revoked JWT identifiers (jti). This table is intended to be queried by
    jwt_utils.is_revoked to determine whether a presented token must be rejected.
    Fields:
      - jti (PK): token identifier
      - revoked_at: when we recorded the revocation
      - expires_at: original token expiry (optional, used for cleanup or short-circuit)
    """
    __tablename__ = "revoked_tokens"
    jti = Column(String(64), primary_key=True)
    revoked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<RevokedToken jti={self.jti} revoked_at={self.revoked_at} expires_at={self.expires_at}>"


def init_db(app):
    """
    Initialize DB engine and scoped session.

    - app.config['DATABASE_URL'] is required.
    - Creates tables if they do not exist (lenient; dev convenience).
    """
    global SessionLocal, _engine
    database_url = app.config.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set in app.config")

    _engine = create_engine(database_url, future=True, echo=app.config.get("SQL_ECHO", False))
    SessionLocal = scoped_session(sessionmaker(bind=_engine, autoflush=False, autocommit=False))

    try:
        Base.metadata.create_all(_engine)
    except Exception:
        # Do not crash startup; tests and health endpoint can surface issues.
        pass


def get_session():
    global SessionLocal
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db(app) first.")
    return SessionLocal()


def create_user(email: str, password: str, session=None) -> User:
    # Lazy import of password utils
    close_session = False
    if session is None:
        session = get_session()
        close_session = True

    try:
        pwd_hash = password
        if PasswordHasher is not None:
            try:
                ph = PasswordHasher()
                pwd_hash = ph.hash(password)
            except Exception:
                # fallback to raw (dev only)
                pwd_hash = password

        user = User(email=email, password_hash=pwd_hash, created_at=datetime.utcnow())
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    except Exception:
        session.rollback()
        raise
    finally:
        if close_session:
            try:
                session.close()
            except Exception:
                pass


# -------------------------
# utils.passwords (small wrapper)
# -------------------------
class passwords:
    """
    Lightweight password utilities. Uses argon2 if available, otherwise falls back
    to a dev-only compare_digest approach (not secure).
    """

    @staticmethod
    def hash_password(plain: str) -> str:
        if PasswordHasher is not None:
            ph = PasswordHasher()
            return ph.hash(plain)
        # dev fallback (insecure): return plain
        return plain

    @staticmethod
    def verify_password(stored_hash: str, plain: str) -> bool:
        if PasswordHasher is not None:
            ph = PasswordHasher()
            try:
                return ph.verify(stored_hash, plain)
            except Argon2VerifyMismatchError:
                return False
            except Exception:
                # On unexpected errors, be conservative and return False
                return False
        # dev fallback: constant-time compare
        try:
            return secrets.compare_digest(stored_hash, plain)
        except Exception:
            return False


# -------------------------
# utils.jwt (surgical)
# -------------------------
class jwt_utils:
    """
    JWT helper utilities.
    """

    @staticmethod
    def _get_secret_key():
        return current_app.config.get("SECRET_KEY", "dev-secret-key")

    @staticmethod
    def _get_jwt_settings():
        return {
            "exp_seconds": int(current_app.config.get("JWT_EXP_SECONDS", 3600)),
            "cookie_name": current_app.config.get("JWT_COOKIE_NAME", "access_token"),
            "algorithm": current_app.config.get("JWT_ALGORITHM", "HS256"),
        }

    @staticmethod
    def create_access_token(user_id: int, jti: str, purpose: str = None, exp_seconds: int = None) -> str:
        """
        Create a JWT access token for a given user_id and jti.

        Optional:
         - purpose: a short string indicating intended use (e.g., 'password_reset')
         - exp_seconds: override default expiry seconds from config

        The token payload will include:
          - user_id (int)
          - jti (str)
          - iat (int timestamp)
          - exp (int timestamp)
          - purpose (if provided)

        Uses current_app config via jwt_utils._get_jwt_settings and _get_secret_key.
        """
        if jwt is None:
            raise RuntimeError("PyJWT is required for JWT operations. Install via `pip install PyJWT`")
        settings = jwt_utils._get_jwt_settings()
        now = datetime.now(tz=timezone.utc)
        # Choose expiry: explicit override beats configured value
        if exp_seconds is None:
            exp_seconds = int(current_app.config.get("JWT_EXP_SECONDS", settings.get("exp_seconds", 3600)))
        exp = now + timedelta(seconds=int(exp_seconds))
        payload = {
            "user_id": int(user_id),
            "jti": str(jti),
            "exp": int(exp.timestamp()),
            "iat": int(now.timestamp()),
        }
        if purpose:
            payload["purpose"] = str(purpose)
        token = jwt.encode(payload, jwt_utils._get_secret_key(), algorithm=settings["algorithm"])
        # PyJWT v2 returns str
        return token

    @staticmethod
    def decode_token(token: str) -> Dict[str, Any]:
        """
        Decode a token and return its payload as a dict. No additional validation beyond
        PyJWT verification (signature and expiry) is performed here; callers should
        check 'purpose' and other app-level semantics as needed.
        """
        if jwt is None:
            raise RuntimeError("PyJWT is required for JWT operations. Install via `pip install PyJWT`")
        settings = jwt_utils._get_jwt_settings()
        payload = jwt.decode(token, jwt_utils._get_secret_key(), algorithms=[settings["algorithm"]])
        # Normalize types where convenient
        if "user_id" in payload:
            try:
                payload["user_id"] = int(payload.get("user_id"))
            except Exception:
                # Keep original if cast fails; callers must validate
                pass
        return payload

    @staticmethod
    def is_revoked(jti: str) -> bool:
        """
        Determine whether a token (by jti) must be rejected.

        Order:
         - If jti exists in RevokedToken and not yet expired (or expired), consider revoked.
         - Else fall back to SessionToken table (existing behavior) to consider revoked/expired.
         - Any unexpected error returns True (fail-closed).
        """
        sess = None
        try:
            sess = get_session()
            # First consult revoked_tokens table
            rt = sess.query(RevokedToken).filter(RevokedToken.jti == jti).first()
            now = datetime.now(tz=timezone.utc)
            if rt:
                # If we have a RevokedToken record, treat it as revoked regardless of expires_at.
                # But for completeness, if the revoked record has an expires_at and it's in the past,
                # we still consider the token revoked (revocation is authoritative).
                return True

            # Fall back to older SessionToken semantics if no explicit RevokedToken exists
            st = sess.query(SessionToken).filter(SessionToken.jti == jti).first()
            if not st:
                # If we don't know about this session token, treat it as revoked (unknown/jti not issued)
                return True
            if st.revoked:
                return True
            exp = st.expires_at
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now:
                    return True
            return False
        except Exception as e:
            logger.exception("Error checking is_revoked for jti=%s: %s", jti, e)
            # Fail-closed: on DB errors or other exceptions, treat as revoked
            return True
        finally:
            if sess:
                try:
                    sess.close()
                except Exception:
                    pass

    @staticmethod
    def jwt_required(fn):
        def wrapper(*args, **kwargs):
            settings = jwt_utils._get_jwt_settings()
            cookie_name = settings["cookie_name"]
            token = None

            if request.cookies:
                token = request.cookies.get(cookie_name)

            if not token:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header.split(" ", 1)[1].strip()

            if not token:
                return jsonify({"error": "missing_token"}), 401

            try:
                payload = jwt_utils.decode_token(token)
            except ExpiredSignatureError:
                return jsonify({"error": "token_expired"}), 401
            except InvalidTokenError:
                return jsonify({"error": "invalid_token"}), 401
            except Exception:
                return jsonify({"error": "token_decode_error"}), 401

            jti = payload.get("jti")
            user_id = payload.get("user_id")
            if not jti or not user_id:
                return jsonify({"error": "invalid_token_claims"}), 401

            try:
                if jwt_utils.is_revoked(jti):
                    return jsonify({"error": "token_revoked"}), 401
            except Exception:
                return jsonify({"error": "token_revoked_check_failed"}), 401

            sess = None
            try:
                sess = get_session()
                user = sess.query(User).filter(User.id == int(user_id)).first()
                if not user:
                    return jsonify({"error": "user_not_found"}), 401
                g.current_user = user
                return fn(*args, **kwargs)
            finally:
                if sess:
                    try:
                        sess.close()
                    except Exception:
                        pass

        # Preserve wrapper attributes
        import functools
        return functools.wraps(fn)(wrapper)


# -------------------------
# Blueprints: auth, shortener, analytics
# -------------------------
auth_bp = Blueprint("auth", __name__)
shortener_bp = Blueprint("shortener", __name__)
analytics_bp = Blueprint("analytics", __name__)

# Auth templates (all include CSRF hidden input)
_login_template = """<h2>Login</h2>
<form method="POST" action="/auth/login">
  <!-- MANDATORY CSRF token field as required by the Epic -->
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label>Email: <input name="email" type="email" required /></label>
  </div>
  <div>
    <label>Password: <input name="password" type="password" required /></label>
  </div>
  <div>
    <button type="submit">Sign In</button>
  </div>
</form>
<p><a href="/auth/register">Register</a></p>
"""

_login_error_template = """<h2 class="error">Login Failed</h2>
<p class="error">{{ message }}</p>
<form method="GET" action="/auth/login">
  <button type="submit">Try Again</button>
</form>
"""

_login_success_template = """<h2 class="success">Login Successful</h2>
<p>Welcome back, {{ email }}. You should be redirected.</p>
<p><a href="/">Go Home</a></p>
"""

_register_get_template = _auth_register_template

_register_success_template = _auth_register_success_template
_register_error_template = _auth_register_error_template


@auth_bp.route("/", methods=["GET"])
def auth_index():
    return render_layout(_auth_index_template)


@auth_bp.route("/register", methods=["GET"])
def register_get():
    return render_layout(_register_get_template)


@auth_bp.route("/register", methods=["POST"])
def register_post():
    form = request.form or {}
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""

    if not email or not password:
        return render_layout(_register_error_template, message="Email and password required"), 400

    sess = None
    try:
        sess = get_session()
        try:
            # Use create_user helper (which uses hashing if available)
            user = create_user(email=email, password=password, session=sess)
            return render_layout(_register_success_template, email=user.email)
        except IntegrityError:
            sess.rollback()
            return render_layout(_register_error_template, message="Email already registered"), 400
        except Exception as e:
            sess.rollback()
            current_app.logger.exception("Registration error: %s", e)
            return render_layout(_register_error_template, message="Internal error"), 500
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass


@auth_bp.route("/login", methods=["GET"])
def login_get():
    return render_layout(_login_template)


@auth_bp.route("/login", methods=["POST"])
def login_post():
    form = request.form or {}
    email = (form.get("email") or "").strip()
    password = form.get("password") or ""

    if not email or not password:
        return render_layout(_login_error_template, message="Email and password are required"), 400

    sess = None
    try:
        sess = get_session()
        user = sess.query(User).filter(User.email == email).first()

        password_ok = False
        if user:
            try:
                password_ok = passwords.verify_password(user.password_hash, password)
            except Exception:
                password_ok = False
        else:
            # Timing equalizer to avoid user enumeration
            try:
                if PasswordHasher is not None:
                    ph = PasswordHasher()
                    dummy_hash = ph.hash("dummy-password")
                    try:
                        ph.verify(dummy_hash, password)
                    except Exception:
                        pass
                else:
                    secrets.compare_digest("dummy", password[:5])
            except Exception:
                pass

        if not password_ok:
            if user:
                try:
                    user.failed_login_count = (user.failed_login_count or 0) + 1
                    sess.add(user)
                    sess.commit()
                except Exception:
                    sess.rollback()
            return render_layout(_login_error_template, message="Invalid credentials"), 401

        # success path
        try:
            user.failed_login_count = 0
            sess.add(user)

            jti = str(uuid4())
            token = jwt_utils.create_access_token(user_id=user.id, jti=jti)

            payload = jwt_utils.decode_token(token)
            exp_ts = int(payload.get("exp"))
            expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)

            st = SessionToken(
                jti=jti,
                user_id=user.id,
                issued_at=datetime.now(tz=timezone.utc),
                expires_at=expires_at,
                revoked=False,
            )
            sess.add(st)
            sess.commit()
        except Exception as e:
            sess.rollback()
            current_app.logger.exception("Error creating session token: %s", e)
            return render_layout(_login_error_template, message="Internal error during login"), 500

        cookie_name = current_app.config.get("JWT_COOKIE_NAME", "access_token")
        cookie_secure = bool(current_app.config.get("SESSION_COOKIE_SECURE", current_app.config.get("JWT_COOKIE_SECURE", False)))
        cookie_httponly = bool(current_app.config.get("SESSION_COOKIE_HTTPONLY", True))
        cookie_samesite = current_app.config.get("SESSION_COOKIE_SAMESITE", current_app.config.get("JWT_COOKIE_SAMESITE", "Lax"))

        resp = make_response(redirect(url_for("index")))
        resp.set_cookie(
            cookie_name,
            token,
            httponly=cookie_httponly,
            secure=cookie_secure,
            samesite=cookie_samesite,
            path="/",
            expires=expires_at,
        )
        return resp
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass


# New endpoint: POST /auth/logout
@auth_bp.route("/logout", methods=["POST"])
def logout_post():
    """
    POST /auth/logout
    - Reads token from cookie (or Authorization header fallback)
    - Attempts to decode (if expired, decodes without verifying exp to extract jti and exp)
    - Inserts RevokedToken record with jti and original expires_at (idempotent)
    - Clears the cookie (set empty value and immediate expiry)
    - Returns a redirect to auth index (or JSON if prefered)
    """
    settings = jwt_utils._get_jwt_settings()
    cookie_name = settings["cookie_name"]

    token = None
    if request.cookies:
        token = request.cookies.get(cookie_name)

    if not token:
        # Try Authorization header fallback
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    # Prepare cookie clearing response regardless of token validity
    cookie_secure = bool(current_app.config.get("SESSION_COOKIE_SECURE", current_app.config.get("JWT_COOKIE_SECURE", False)))
    cookie_httponly = bool(current_app.config.get("SESSION_COOKIE_HTTPONLY", True))
    cookie_samesite = current_app.config.get("SESSION_COOKIE_SAMESITE", current_app.config.get("JWT_COOKIE_SAMESITE", "Lax"))

    # Default response: redirect back to auth index
    resp = make_response(redirect(url_for("auth.auth_index")))

    # Clear cookie in any case (idempotent)
    try:
        resp.set_cookie(cookie_name, "", httponly=cookie_httponly, secure=cookie_secure, samesite=cookie_samesite, path="/", expires=0)
    except Exception:
        # set_cookie may raise in odd environments; ignore to ensure logout flow continues
        pass

    if not token:
        # No token presented; logout is effectively a cookie clear -> return success
        return resp

    # Try to extract jti and exp. If token is expired, decode without expiry verification to obtain jti and exp for revocation record
    if jwt is None:
        # PyJWT not available; we cannot decode token to record jti. Still clear cookie and return success.
        return resp

    jti = None
    exp_ts = None
    try:
        # First, try the normal strict decode (which will raise on expired)
        payload = jwt_utils.decode_token(token)
        jti = payload.get("jti")
        exp_value = payload.get("exp")
        if exp_value:
            try:
                exp_ts = int(exp_value)
            except Exception:
                exp_ts = None
    except ExpiredSignatureError:
        # Token expired: decode without verifying expiration to obtain jti/exp for revocation record
        try:
            settings = jwt_utils._get_jwt_settings()
            payload = jwt.decode(token, jwt_utils._get_secret_key(), algorithms=[settings["algorithm"]], options={"verify_exp": False})
            jti = payload.get("jti")
            exp_value = payload.get("exp")
            if exp_value:
                try:
                    exp_ts = int(exp_value)
                except Exception:
                    exp_ts = None
        except Exception:
            # Malformed token: nothing to revoke; treat logout as successful cookie clear
            return resp
    except InvalidTokenError:
        # Invalid token: nothing to revoke
        return resp
    except Exception:
        # Other decode errors: log for debugging and treat as success (cookie cleared)
        current_app.logger.exception("Unexpected error decoding token during logout")
        return resp

    if not jti:
        # Nothing to revoke; cookie is cleared already
        return resp

    # Convert exp_ts to timezone-aware datetime if present
    expires_at = None
    try:
        if exp_ts is not None:
            expires_at = datetime.fromtimestamp(int(exp_ts), tz=timezone.utc)
    except Exception:
        expires_at = None

    # Insert RevokedToken record (idempotent)
    sess = None
    try:
        sess = get_session()
        rt = RevokedToken(jti=jti, revoked_at=datetime.now(tz=timezone.utc), expires_at=expires_at)
        sess.add(rt)
        try:
            sess.commit()
        except IntegrityError:
            # Duplicate insertion (already revoked) -> idempotent success
            sess.rollback()
        except Exception:
            sess.rollback()
            current_app.logger.exception("Error committing RevokedToken during logout")
    except Exception:
        current_app.logger.exception("Error creating RevokedToken during logout")
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass

    # Return cleared cookie response
    return resp


# Optional protected endpoint for integration tests to assert jwt_required behavior
@auth_bp.route("/me", methods=["GET"])
@jwt_utils.jwt_required
def me():
    # g.current_user is set by jwt_required decorator
    user = getattr(g, "current_user", None)
    if not user:
        return jsonify({"error": "no_current_user"}), 401
    return jsonify({"id": user.id, "email": user.email}), 200


# -------------------------
# Password Reset: Templates + Endpoints (KAN-109)
# -------------------------

# Templates: must include the mandatory CSRF hidden input per Epic.
_password_reset_request_template = """<h2>Password Reset</h2>
<p>Enter the email address associated with your account. If an account exists, you'll receive a reset link (dev: logged).</p>
<form method="POST" action="/auth/password-reset-request">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label>Email: <input name="email" type="email" required /></label>
  </div>
  <div>
    <button type="submit">Request Password Reset</button>
  </div>
</form>
<p><a href="/auth/">Back to Auth</a></p>
"""

_password_reset_requested_template = """<h2>Password Reset Requested</h2>
<p class="success">If that email exists, a password reset link has been sent (dev: logged). Please check your email.</p>
<p><a href="/auth/">Back to Auth</a></p>
"""

_password_reset_form_template = """<h2>Reset Password</h2>
<form method="POST" action="/auth/password-reset/{{ token | e }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label>New password: <input name="password" type="password" required /></label>
  </div>
  <div>
    <label>Confirm password: <input name="password_confirm" type="password" required /></label>
  </div>
  <div>
    <button type="submit">Set New Password</button>
  </div>
</form>
<p><a href="/auth/">Back to Auth</a></p>
"""

_password_reset_success_template = """<h2 class="success">Password Reset Successful</h2>
<p>Your password has been updated. You can now sign in with your new password.</p>
<p><a href="/auth/login">Login</a></p>
"""

_password_reset_error_template = """<h2 class="error">Password Reset Error</h2>
<p class="error">{{ message }}</p>
<form method="GET" action="/auth/password-reset-request">
  <button type="submit">Request a new reset link</button>
</form>
"""

# Lightweight in-memory rate limiter for reset requests (per-app; restart clears)
# Structure: { key: [timestamp1, timestamp2, ...] }
_password_reset_rl = {}
from time import time as _now_ts

def _rate_limit_allow(key: str, window_seconds: int, limit: int) -> bool:
    """
    Returns True if request should be allowed. Mutates the in-memory store.
    key: a string identifying the principal (email or IP)
    window_seconds: time window to consider
    limit: max requests allowed in window
    """
    try:
        ts = int(_now_ts())
        bucket = _password_reset_rl.get(key) or []
        # prune
        cutoff = ts - int(window_seconds)
        bucket = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            _password_reset_rl[key] = bucket
            return False
        bucket.append(ts)
        _password_reset_rl[key] = bucket
        return True
    except Exception:
        # On any unexpected error, be conservative and allow (do not block legitimate usage)
        return True

def _append_trace(entry: str):
    """
    Append a trace line to trace_KAN-109.txt for architectural memory.
    This is intentionally lightweight: append-only, newline-delimited.
    """
    try:
        trace_path = os.path.join(os.getcwd(), "trace_KAN-109.txt")
        with open(trace_path, "a", encoding="utf-8") as fh:
            fh.write(entry.rstrip() + "\n")
    except Exception:
        # Never fail the request because tracing failed; but log
        current_app.logger.exception("Failed to write KAN-109 trace file")

@auth_bp.route("/password-reset-request", methods=["GET"])
def password_reset_request_get():
    """Render the password reset request form."""
    return render_layout(_password_reset_request_template)

@auth_bp.route("/password-reset-request", methods=["POST"])
def password_reset_request_post():
    """
    POST /auth/password-reset-request
    - Accepts form with 'email' and CSRF (CSRF enforced by template inclusion/wiring)
    - Rate-limits per IP and per email
    - Does NOT reveal whether the email exists -> always returns a generic confirmation page.
    - If user exists: creates a purpose-scoped token using jwt_utils.create_access_token with purpose='password_reset'
      and logs a dev reset link (current_app.logger.info). Also writes trace_KAN-109.txt with event.
    """
    form = request.form or {}
    email = (form.get("email") or "").strip().lower()
    # Simple rate-limiting: per-email 5/hour, per-IP 10/hour
    rl_email_key = f"pwreset:email:{email}"
    rl_ip_key = f"pwreset:ip:{request.remote_addr or 'unknown'}"
    email_allowed = _rate_limit_allow(rl_email_key, window_seconds=60 * 60, limit=5)
    ip_allowed = _rate_limit_allow(rl_ip_key, window_seconds=60 * 60, limit=10)
    if not (email_allowed and ip_allowed):
        # Return generic response (do not reveal throttling details to attacker)
        current_app.logger.warning("Password reset rate-limited for email=%s ip=%s", email, request.remote_addr)
        _append_trace(f"{datetime.utcnow().isoformat()}Z RATE_LIMIT email={email} ip={request.remote_addr}")
        return render_layout(_password_reset_requested_template)

    # Attempt to find user and issue token if found (do not reveal existence)
    sess = None
    try:
        sess = get_session()
        user = None
        if email:
            user = sess.query(User).filter(User.email == email).first()
        if user:
            try:
                jti = str(uuid4())
                # Use a purpose-scoped token with a configured expiry for resets
                reset_exp = int(current_app.config.get("PASSWORD_RESET_EXP_SECONDS", 60 * 60))  # default 1 hour
                token = jwt_utils.create_access_token(user_id=user.id, jti=jti, purpose="password_reset", exp_seconds=reset_exp)
                reset_url = url_for("auth.password_reset_form_get", token=token, _external=True)
                # Dev: log the link. Tests/integration extract token from logs.
                current_app.logger.info("Password reset link (dev): %s", reset_url)
                _append_trace(f"{datetime.utcnow().isoformat()}Z ISSUED_RESET email={email} user_id={user.id} jti={jti} reset_url={reset_url}")
                # Do not persist SessionToken here; rely on RevokedToken table on revocation.
            except Exception:
                current_app.logger.exception("Error issuing password reset token for user_id=%s", getattr(user, "id", None))
                # Continue to generic response
        else:
            # No user found: do nothing sensitive, but record trace of attempt to operational logs (no details)
            _append_trace(f"{datetime.utcnow().isoformat()}Z REQUEST_NO_ACCOUNT email={email} ip={request.remote_addr}")
    except Exception:
        current_app.logger.exception("Error handling password reset request for email=%s", email)
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass

    # Always return the same confirmation page to avoid user enumeration
    return render_layout(_password_reset_requested_template)

# Named GET endpoint to render the form (so user can open link)
@auth_bp.route("/password-reset/<token>", methods=["GET"])
def password_reset_form_get(token):
    """
    Render the password reset form for a token. The form posts to the same URL with the token.
    We render the page regardless of token validity to avoid leaking.
    """
    return render_layout(_password_reset_form_template, token=token)

@auth_bp.route("/password-reset/<token>", methods=["POST"])
def password_reset_form_post(token):
    """
    POST /auth/password-reset/<token>
    - Validates token by decoding with jwt_utils.decode_token (which enforces signature and exp).
    - Verifies payload.purpose == 'password_reset'.
    - Verifies token jti is not already revoked (jwt_utils.is_revoked).
    - Accepts 'password' and 'password_confirm' fields and CSRF token in the form.
    - On success: updates user's password_hash (using passwords.hash_password), commits,
      inserts RevokedToken record for the token's jti (with expires_at from token) to prevent reuse,
      writes trace entry, and renders success template.
    - On failure: renders error template with a generic message (no sensitive details).
    """
    form = request.form or {}
    new_pwd = form.get("password") or ""
    confirm = form.get("password_confirm") or ""
    if not new_pwd or not confirm:
        return render_layout(_password_reset_error_template, message="Password and confirmation are required"), 400
    if new_pwd != confirm:
        return render_layout(_password_reset_error_template, message="Passwords do not match"), 400
    # Enforce minimal password policy in dev scaffold (example): length >= 8
    if len(new_pwd) < int(current_app.config.get("MIN_PASSWORD_LENGTH", 8)):
        return render_layout(_password_reset_error_template, message=f"Password must be at least {int(current_app.config.get('MIN_PASSWORD_LENGTH', 8))} characters"), 400

    # Decode and verify token
    try:
        payload = jwt_utils.decode_token(token)
    except ExpiredSignatureError:
        _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_EXPIRED token={token[:32]}...")
        return render_layout(_password_reset_error_template, message="Reset link has expired"), 400
    except InvalidTokenError:
        _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_INVALID token={token[:32]}...")
        return render_layout(_password_reset_error_template, message="Invalid reset link"), 400
    except Exception:
        current_app.logger.exception("Unexpected error decoding reset token")
        return render_layout(_password_reset_error_template, message="Invalid reset link"), 400

    # Validate purpose
    purpose = payload.get("purpose")
    if purpose != "password_reset":
        _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_WRONG_PURPOSE payload_purpose={purpose}")
        return render_layout(_password_reset_error_template, message="Invalid reset link"), 400

    jti = payload.get("jti")
    user_id = payload.get("user_id")
    exp_ts = payload.get("exp")
    if not jti or not user_id:
        _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_MISSING_CLAIMS payload={payload}")
        return render_layout(_password_reset_error_template, message="Invalid reset link"), 400

    # Check revocation
    try:
        if jwt_utils.is_revoked(jti):
            _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_REVOKED jti={jti} user_id={user_id}")
            return render_layout(_password_reset_error_template, message="This reset link has already been used or invalidated"), 400
    except Exception:
        # On DB/check errors, be conservative and reject
        current_app.logger.exception("Error checking revocation for jti=%s", jti)
        return render_layout(_password_reset_error_template, message="Unable to validate reset link"), 500

    # Update password and revoke token
    sess = None
    try:
        sess = get_session()
        user = sess.query(User).filter(User.id == int(user_id)).first()
        if not user:
            _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_FAILED_NO_USER user_id={user_id}")
            return render_layout(_password_reset_error_template, message="Invalid reset link"), 400

        # Hash and set
        try:
            hashed = passwords.hash_password(new_pwd)
            user.password_hash = hashed
            sess.add(user)
            # Insert RevokedToken record to block reuse
            expires_at = None
            try:
                if exp_ts is not None:
                    expires_at = datetime.fromtimestamp(int(exp_ts), tz=timezone.utc)
            except Exception:
                expires_at = None
            rt = RevokedToken(jti=jti, revoked_at=datetime.now(tz=timezone.utc), expires_at=expires_at)
            sess.add(rt)
            try:
                sess.commit()
            except IntegrityError:
                # Already revoked record: rollback but consider operation completed
                sess.rollback()
                # Ensure password was persisted; set again in a separate transaction
                try:
                    sess.add(user)
                    sess.commit()
                except Exception:
                    sess.rollback()
                    current_app.logger.exception("Failed to commit password update after revocation conflict")
                    return render_layout(_password_reset_error_template, message="Internal error"), 500
        except Exception:
            sess.rollback()
            current_app.logger.exception("Error updating password for user_id=%s", user_id)
            return render_layout(_password_reset_error_template, message="Internal error"), 500

        # Successful reset
        _append_trace(f"{datetime.utcnow().isoformat()}Z RESET_SUCCESS user_id={user.id} jti={jti}")
        return render_layout(_password_reset_success_template)
    finally:
        if sess:
            try:
                sess.close()
            except Exception:
                pass


# Shortener blueprint endpoints (minimal scaffolding)
@shortener_bp.route("/", methods=["GET"])
def shorten_index():
    return render_layout(_shorten_index_template)


@shortener_bp.route("/create", methods=["POST"])
def shorten_create():
    target = request.form.get("target_url")
    return render_layout(_shorten_result_template, target=target)


# Analytics scaffold
@analytics_bp.route("/", methods=["GET"])
def analytics_index():
    return render_layout(_analytics_index_template)


# -------------------------
# App factory
# -------------------------
def create_app(config_name=None):
    """
    create_app factory pattern.

    - Loads configuration from config.py (DevConfig, ProdConfig, TestingConfig) selected by argument
      or FLASK_ENV environment variable.
    - Minimal dev-safe defaults preserved for local imports.
    - Wires CSRFProtect if available.
    - Attaches SQLAlchemy engine and scoped_session to app via models.init_db for downstream use.
    - Registers blueprints.
    - Provides /health/db for integration tests.
    """
    app = Flask(__name__)
    app.template_folder = None  # we use inline templates via render_template_string
    app.static_folder = None

    # Attempt to load centralized configuration helpers (surgical).
    try:
        from config import get_config_class, apply_config_to_app, validate_production_config, ProdConfig  # type: ignore
    except Exception:
        app.config.setdefault("SECRET_KEY", "dev-secret-key")
        default_sqlite_path = os.path.join(os.getcwd(), "dev.db")
        app.config.setdefault("DATABASE_URL", f"sqlite:///{default_sqlite_path}")
    else:
        cfg_cls = get_config_class(config_name)
        apply_config_to_app(app, cfg_cls)
        if cfg_cls is ProdConfig:
            validate_production_config(app.config)
        app.config.setdefault("SECRET_KEY", "dev-secret-key")
        default_sqlite_path = os.path.join(os.getcwd(), "dev.db")
        app.config.setdefault("DATABASE_URL", f"sqlite:///{default_sqlite_path}")

    # CSRF protection wiring (if Flask-WTF is installed)
    if CSRFProtect is not None:
        try:
            csrf = CSRFProtect()
            csrf.init_app(app)
        except Exception:
            csrf = None
    else:
        csrf = None

    # Expose csrf_token() in templates; if flask-wtf not available, provide a safe lambda
    if generate_csrf is not None:
        app.jinja_env.globals["csrf_token"] = generate_csrf
    else:
        app.jinja_env.globals["csrf_token"] = lambda: ""

    # Attach render_layout to Jinja globals for templates that might expect it
    app.jinja_env.globals["render_layout"] = render_layout

    # Initialize DB via models.init_db(app)
    init_db(app)

    # Register blueprints
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(shortener_bp, url_prefix="/s")
    app.register_blueprint(analytics_bp, url_prefix="/analytics")

    @app.route("/", methods=["GET"])
    def index():
        body = "<h1>Welcome to Smart Link (dev scaffold)</h1><p>Use the nav to browse.</p>"
        return render_layout(body)

    # Health endpoint for DB connectivity (uses Session to run a simple SELECT 1)
    @app.route("/health/db")
    def health_db():
        try:
            sess = get_session()
            try:
                # Try a lightweight query to ensure DB connectivity
                sess.execute("SELECT 1")
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
            return jsonify({"status": "ok", "db": True}), 200
        except Exception as e:
            return jsonify({"status": "error", "db": False, "detail": str(e)}), 500

    return app


# If executed directly, run a development server (useful for manual testing)
if __name__ == "__main__":
    application = create_app()
    print("Starting dev server on http://127.0.0.1:5000")
    application.run(debug=True)