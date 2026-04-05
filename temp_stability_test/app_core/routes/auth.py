"""
routes/auth.py - Authentication routes (KAN-169, updated for KAN-171)

Provides:
 - GET/POST /auth/login  -> login form and login handler implementing server-side session creation
 - GET/POST /auth/register -> registration form and handler that persists a user and emits verification token via dev-stub
 - POST /auth/logout -> logout
 - Standardized ValidationError usage for auth/form error handling
"""

from flask import Blueprint, request, current_app, url_for, redirect, session
from utils.templates import render_layout
from flask_wtf.csrf import generate_csrf
from html import escape
import traceback
from time import time

# Defensive logger import
try:
    from app_core.app_logging import get_logger
    logger = get_logger(__name__)
except Exception:
    import logging as _logging
    logger = _logging.getLogger(__name__)

# Defensive import of auth service
try:
    from services import auth_service  # type: ignore
except Exception:
    auth_service = None

# DB helpers
try:
    from app_core.db import get_db_connection, get_user_by_email, create_user
except Exception:
    get_db_connection = None
    get_user_by_email = None
    create_user = None

# SQLAlchemy models fallback
try:
    import models
except Exception:
    models = None

# Bruteforce hook (best-effort)
try:
    from utils import bruteforce as bruteforce_mod  # type: ignore
except Exception:
    bruteforce_mod = None

# Password policy helpers and crypto
try:
    from utils import passwords as passwords_mod
except Exception:
    passwords_mod = None

try:
    from utils.crypto import hash_password, create_verification_token
except Exception:
    hash_password = None
    create_verification_token = None

# Email dev stub
try:
    from utils.email_dev_stub import send_verification_email
except Exception:
    def send_verification_email(to_email, verification_url, token=None):
        # No-op fallback
        pass

# ValidationError
try:
    from app_core.utils.errors import ValidationError
except Exception:
    ValidationError = Exception  # fallback; handler may not be registered

# sqlite3 for IntegrityError mapping
try:
    import sqlite3
except Exception:
    sqlite3 = None

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _render_login_page(prefill: dict = None, error: str = ""):
    """Render a simple login form wrapped in render_layout. Preserve email safely via prefill dict."""
    prefill = prefill or {}
    safe_email = escape(prefill.get("email", "") or "")
    safe_error = escape(error or "")
    csrf_token = ""
    try:
        csrf_token = generate_csrf()
    except Exception:
        csrf_token = ""
    inner = f"""
      <div class="max-w-md mx-auto bg-white p-8 border border-slate-200 rounded-xl shadow-sm">
        <h2 class="text-2xl font-bold mb-6">Sign in</h2>
        {'<p role="alert" aria-live="assertive" style="color:#b00020;">' + safe_error + '</p>' if safe_error else ''}
        <form method="post" action="{url_for('auth.login')}" novalidate>
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <div class="mb-4">
                <label for="login-email" class="block text-sm font-medium mb-2">Email</label>
                <input id="login-email" name="email" type="email" class="w-full p-3 border rounded-lg" required value="{safe_email}">
            </div>
            <div class="mb-6">
                <label for="login-password" class="block text-sm font-medium mb-2">Password</label>
                <input id="login-password" name="password" type="password" class="w-full p-3 border rounded-lg" required>
            </div>
            <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold shadow-md hover:bg-blue-700">Sign In</button>
        </form>
      </div>
    """
    return render_layout(inner)


def _render_register_page(prefill: dict = None, error: str = ""):
    """Render a simple registration form. Preserve safe fields via prefill dict."""
    prefill = prefill or {}
    safe_name = escape(prefill.get("name", "") or "")
    safe_email = escape(prefill.get("email", "") or "")
    safe_error = escape(error or "")
    csrf_token = ""
    try:
        csrf_token = generate_csrf()
    except Exception:
        csrf_token = ""
    inner = f"""
      <div class="max-w-md mx-auto bg-white p-8 border border-slate-200 rounded-xl shadow-sm">
        <h2 class="text-2xl font-bold mb-6">Create your account</h2>
        {'<p role="alert" aria-live="assertive" style="color:#b00020;">' + safe_error + '</p>' if safe_error else ''}
        <form method="post" action="{url_for('auth.register')}" novalidate>
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <div class="mb-4">
                <label for="register-name" class="block text-sm font-medium mb-2">Full Name</label>
                <input id="register-name" name="name" type="text" class="w-full p-3 border rounded-lg" required value="{safe_name}">
            </div>
            <div class="mb-4">
                <label for="register-email" class="block text-sm font-medium mb-2">Email Address</label>
                <input id="register-email" name="email" type="email" class="w-full p-3 border rounded-lg" required value="{safe_email}">
            </div>
            <div class="mb-6">
                <label for="register-password" class="block text-sm font-medium mb-2">Password</label>
                <input id="register-password" name="password" type="password" class="w-full p-3 border rounded-lg" required aria-describedby="pw-strength-widget">
                <div id="pw-strength-widget" style="font-size:0.85rem; color:#666;">Password must meet policy requirements.</div>
            </div>
            <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold shadow-md hover:bg-blue-700">Sign Up</button>
        </form>
      </div>
    """
    return render_layout(inner)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """
    GET: render registration form
    POST: validate input, persist user (password hashed), emit verification token via dev stub, and render confirmation.
    On validation failure raise ValidationError so centralized handler can render with prefill.
    """
    if request.method == "GET":
        try:
            return _render_register_page()
        except Exception:
            try:
                logger.exception("Failed to render register page")
            except Exception:
                pass
            return "<h1>Register</h1>", 200

    # POST handling
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    # Basic validation
    if not email or not password:
        try:
            logger.debug("Register attempt with missing fields", extra={"message_key": "auth.register_missing_fields", "email": email})
        except Exception:
            pass
        # Use ValidationError so central handler renders form with prefill
        raise ValidationError("Email and password are required.", status_code=400, extra={"template_renderer": _render_register_page, "prefill": {"email": email, "name": name}})

    # Password policy check (best-effort)
    try:
        if passwords_mod is not None:
            violations = passwords_mod.password_policy_check(password, email=email)
            if violations:
                # Choose first violation as user-facing message
                msg = violations[0] if isinstance(violations, list) and violations else "Password does not meet policy requirements."
                raise ValidationError(msg, status_code=400, extra={"template_renderer": _render_register_page, "prefill": {"email": email, "name": name}})
    except ValidationError:
        raise
    except Exception:
        # If policy check fails unexpectedly, log and continue with basic validation
        try:
            logger.exception("Password policy check error", extra={"message_key": "auth.register_password_policy_error", "email": email})
        except Exception:
            pass

    # Hash password
    try:
        if hash_password is None:
            raise RuntimeError("Password hashing unavailable")
        pw_hash = hash_password(password)
    except Exception as e:
        try:
            logger.exception("Failed to hash password during registration", extra={"message_key": "auth.register_hash_error", "email": email})
        except Exception:
            pass
        # Operational error -> return server error (not ValidationError)
        return _render_register_page(prefill={"email": email, "name": name}, error="Server error while creating account. Please try again later."), 500

    # Persist user (prefer sqlite helper, fallback to SQLAlchemy models)
    try:
        if get_db_connection is not None and create_user is not None:
            try:
                with get_db_connection() as conn:
                    try:
                        new_id = create_user(conn, email, pw_hash)
                    except sqlite3.IntegrityError:
                        # Duplicate email
                        try:
                            logger.warning("Failed to create user due to IntegrityError (possible duplicate email).", extra={"message_key": "auth.register_duplicate_email", "email": email})
                        except Exception:
                            pass
                        raise ValidationError("An account with this email already exists.", status_code=409, extra={"template_renderer": _render_register_page, "prefill": {"email": email, "name": name}})
            except ValueError:
                # Non-sqlite DATABASE_URL -> fall through to SQLAlchemy path
                new_id = None
        else:
            new_id = None
    except ValidationError:
        raise
    except Exception:
        try:
            logger.exception("DB error during user creation", extra={"message_key": "auth.register_db_error", "email": email})
        except Exception:
            pass
        return _render_register_page(prefill={"email": email, "name": name}, error="Server error while creating account. Please try again later."), 500

    # SQLAlchemy fallback if necessary
    if (new_id is None or new_id <= 0) and models is not None:
        try:
            Session = getattr(models, "Session", None)
            if Session is not None:
                s = Session()
                try:
                    u = models.User(email=email, password_hash=pw_hash, is_active=False)
                    s.add(u)
                    s.commit()
                    s.refresh(u)
                    new_id = getattr(u, "id", None)
                except sqlite3.IntegrityError:
                    try:
                        logger.warning("Failed to create user via SQLAlchemy due to IntegrityError (duplicate email).", extra={"message_key": "auth.register_duplicate_email", "email": email})
                    except Exception:
                        pass
                    try:
                        s.rollback()
                    except Exception:
                        pass
                    raise ValidationError("An account with this email already exists.", status_code=409, extra={"template_renderer": _render_register_page, "prefill": {"email": email, "name": name}})
                except Exception:
                    try:
                        s.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
        except ValidationError:
            raise
        except Exception as e:
            try:
                logger.exception("Unexpected error while creating user", extra={"message_key": "auth.register_db_error", "email": email})
            except Exception:
                pass
            return _render_register_page(prefill={"email": email, "name": name}, error="Server error while creating account. Please try again later."), 500

    # At this point user created; generate verification token and send dev stub email
    try:
        secret = current_app.config.get("JWT_SECRET", current_app.config.get("SECRET_KEY", "")) if current_app else ""
        token = None
        if create_verification_token is not None:
            try:
                token = create_verification_token({"user_id": int(new_id)}, purpose="email_verify", secret=secret, expires_seconds=int(current_app.config.get("EMAIL_VERIFY_EXPIRY_SECONDS", 24 * 3600)))
            except Exception:
                token = None
        # Compose verification URL (relative acceptable for dev stub)
        verification_url = f"/auth/verify-email/{token}" if token else "/auth/verify-email/"

        try:
            send_verification_email(email, verification_url, token=token)
        except Exception:
            # Best-effort: log failure but do not treat as fatal
            try:
                logger.exception("Failed to send verification email via dev-stub", extra={"message_key": "auth.register_email_send_failed", "email": email})
            except Exception:
                pass
    except Exception:
        # Continue: user created, but email emission failed
        try:
            logger.exception("Verification token generation or email send error", extra={"message_key": "auth.register_verification_error", "email": email})
        except Exception:
            pass

    # Successful registration: render login page with a friendly message and prefilled email (password cleared)
    return _render_login_page(prefill={"email": email}, error="Registration successful. Please check your email to verify your account."), 200


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """
    GET: render login form
    POST: validate credentials, create server-side session on success, redirect to /dashboard.
    On invalid credentials: raise ValidationError (401) so centralized handler re-renders form with prefill.
    On operational errors: 500 + re-render login with generic error.
    """
    if request.method == "GET":
        try:
            return _render_login_page()
        except Exception:
            # Defensive: if render fails, return minimal fallback
            try:
                logger.exception("Failed to render login page")
            except Exception:
                pass
            return "<h1>Login</h1>", 200

    # POST handling
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    # Basic validation
    if not email or not password:
        try:
            logger.debug("Login attempt with missing credentials", extra={"message_key": "auth.login_missing_fields", "email": email})
        except Exception:
            pass
        raise ValidationError("Email and password are required.", status_code=400, extra={"template_renderer": _render_login_page, "prefill": {"email": email}})

    user_row = None
    user_id = None
    password_hash = None

    # Primary path: sqlite helper get_db_connection + get_user_by_email
    try:
        if get_db_connection is not None and get_user_by_email is not None:
            try:
                with get_db_connection() as conn:
                    user_row = get_user_by_email(conn, email)
            except ValueError:
                # Non-sqlite DATABASE_URL; signal to fallback to SQLAlchemy below
                user_row = None
            except Exception:
                # DB error: log and respond with server error
                try:
                    logger.exception("DB error during login get_user_by_email", extra={"message_key": "auth.login_db_error", "email": email})
                except Exception:
                    pass
                return _render_login_page(prefill={"email": email}, error="Server error while authenticating. Please try again later."), 500
    except Exception:
        # Defensive: proceed to fallback
        user_row = None

    # Fallback to SQLAlchemy models if no sqlite row found and models available
    if user_row is None and models is not None:
        try:
            Session = getattr(models, "Session", None)
            if Session is not None:
                s = Session()
                try:
                    u = s.query(models.User).filter_by(email=email).first()
                    if u:
                        # Build a compatible mapping for our usage
                        user_id = getattr(u, "id", None)
                        password_hash = getattr(u, "password_hash", None)
                    else:
                        user_id = None
                        password_hash = None
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
        except Exception:
            try:
                logger.exception("SQLAlchemy fallback DB error during login", extra={"message_key": "auth.login_db_error_fallback", "email": email})
            except Exception:
                pass
            return _render_login_page(prefill={"email": email}, error="Server error while authenticating. Please try again later."), 500
    elif user_row is not None:
        try:
            # sqlite3.Row like
            user_id = user_row.get("id") if isinstance(user_row, dict) else user_row["id"]
            password_hash = user_row.get("password_hash") if isinstance(user_row, dict) else user_row["password_hash"]
        except Exception:
            # Defensive
            try:
                user_id = user_row["id"]
                password_hash = user_row["password_hash"]
            except Exception:
                user_id = None
                password_hash = None

    # If no user found
    if not user_id or not password_hash:
        try:
            logger.warning("Login failed: user not found or missing password_hash", extra={"message_key": "auth.login_user_not_found", "email": email, "remote_addr": request.remote_addr})
        except Exception:
            pass
        # Optionally register failed attempt by IP
        try:
            if bruteforce_mod is not None:
                try:
                    bruteforce_mod.register_failed_attempt(user_id=None, ip=request.remote_addr)
                except Exception:
                    pass
        except Exception:
            pass
        # Raise ValidationError so centralized handler re-renders with prefill
        raise ValidationError("Invalid email or password.", status_code=401, extra={"template_renderer": _render_login_page, "prefill": {"email": email}})

    # Verify password using auth_service preferred, fallback to utils.crypto
    try:
        verified = False
        if auth_service is not None and hasattr(auth_service, "verify_password"):
            try:
                verified = auth_service.verify_password(password, password_hash)
            except Exception as e:
                # Treat operational errors separately
                try:
                    logger.exception("Password verification service error during login", extra={"message_key": "auth.login_verify_service_error", "email": email})
                except Exception:
                    pass
                return _render_login_page(prefill={"email": email}, error="Server error while authenticating. Please try again later."), 500
        else:
            # fallback
            try:
                from utils.crypto import verify_password
                verified = verify_password(password, password_hash)
            except Exception:
                try:
                    logger.exception("Password verification error during login (fallback)", extra={"message_key": "auth.login_verify_error", "email": email})
                except Exception:
                    pass
                return _render_login_page(prefill={"email": email}, error="Server error while authenticating. Please try again later."), 500
    except Exception:
        try:
            logger.exception("Unexpected error during password verification", extra={"message_key": "auth.login_verify_unexpected", "email": email})
        except Exception:
            pass
        return _render_login_page(prefill={"email": email}, error="Server error while authenticating. Please try again later."), 500

    if not verified:
        try:
            logger.warning("Invalid credentials", extra={"message_key": "auth.login_invalid_credentials", "email": email, "remote_addr": request.remote_addr})
        except Exception:
            pass
        # Bruteforce logging
        try:
            if bruteforce_mod is not None:
                try:
                    bruteforce_mod.register_failed_attempt(user_id=user_id, ip=request.remote_addr)
                except Exception:
                    pass
        except Exception:
            pass
        raise ValidationError("Invalid email or password.", status_code=401, extra={"template_renderer": _render_login_page, "prefill": {"email": email}})

    # Success: create server-side session
    try:
        session.clear()
        session['user_id'] = int(user_id)
        session.permanent = True
        # Record last activity for server-side session inactivity enforcement
        try:
            session['last_activity'] = int(time())
            session.modified = True
        except Exception:
            # best-effort only; do not fail login if session write fails
            try:
                logger.warning("Failed to set last_activity in session after login", extra={"message_key": "auth.login_last_activity_failed", "user_id": user_id})
            except Exception:
                pass
    except Exception:
        try:
            logger.exception("Failed to set session during login", extra={"message_key": "auth.login_session_error", "email": email, "user_id": user_id})
        except Exception:
            pass
        return _render_login_page(prefill={}, error="Server error while creating session. Please try again later."), 500

    try:
        logger.info("User logged in", extra={"message_key": "auth.login_success", "email": email, "user_id": user_id})
    except Exception:
        pass

    # Redirect to dashboard
    try:
        return redirect(url_for("dashboard.dashboard_index"))
    except Exception:
        # In case dashboard route missing, fall back to root
        try:
            return redirect(url_for("home.index"))
        except Exception:
            return "Logged in", 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """
    POST /auth/logout

    Invalidate the server-side session and redirect the user to /login.

    Notes:
      - CSRF-protected (CSRFProtect is initialized globally). Prefer POST.
      - Idempotent: safe to call when no session exists.
      - Logs an INFO event 'User logged out' with optional user_id in structured extra.

    Behavior:
      - Attempts to clear session via session.clear().
      - Logs success or any errors (no sensitive data).
      - Redirects to the canonical login page (/login) per acceptance criteria.
    """
    try:
        # Extract user id for structured logging if available
        user_id = None
        try:
            user_id = int(session.get("user_id")) if session.get("user_id") else None
        except Exception:
            user_id = None

        # Clear session server-side (best-effort)
        try:
            session.clear()
        except Exception:
            # Log but continue to redirect
            try:
                logger.exception("Failed to clear session during logout", extra={"message_key": "auth.logout_session_clear_error", "user_id": user_id})
            except Exception:
                pass

        # Log the logout event
        try:
            logger.info("User logged out", extra={"message_key": "auth.logout", "user_id": user_id})
        except Exception:
            # Fallback to plain logging if structured extra fails
            try:
                logger.info("User logged out user_id=%s", user_id)
            except Exception:
                pass

        # Redirect to canonical login path as per acceptance criteria
        try:
            return redirect("/login")
        except Exception:
            # Fallback redirect if url_for unavailable
            return redirect(url_for("auth.login")) if url_for else redirect("/login")
    except Exception:
        # On unexpected top-level error, try to log and still redirect to login
        try:
            logger.exception("Unexpected error during logout", extra={"message_key": "auth.logout_error", "user_id": session.get("user_id")})
        except Exception:
            pass
        try:
            session.clear()
        except Exception:
            pass
        try:
            return redirect("/login")
        except Exception:
            return "Logged out", 200
--- END FILE: app_core/routes/auth.py ---