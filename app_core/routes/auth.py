"""routes/auth.py - Authentication blueprint with registration, email verification and brute-force protected login.

Modifications for KAN-130 (US-032):
 - Server-side password strength enforcement via utils.passwords.password_policy_check.
 - Client-side lightweight strength hint/meter injected into registration and password-reset templates (non-blocking).
 - New password-reset request & token-based reset endpoints (minimal dev-friendly flow).
 - All forms include CSRF hidden input as required.
 - Defensive imports and fallbacks retained.
"""

from flask import Blueprint, request, current_app, redirect, url_for, jsonify, make_response
from utils.templates import render_layout
from utils.crypto import hash_password, verify_password, create_verification_token, decode_verification_token
from utils.email_dev_stub import send_verification_email
# Brute-force protection helpers (KAN-127)
try:
    from utils.bruteforce import check_lockout, register_failed_attempt, reset_failed_login_state
except Exception:
    # Defensive fallbacks if the module is absent (shouldn't happen once deployed with KAN-127)
    def check_lockout(user_id=None, ip=None):
        return False, 0, {}

    def register_failed_attempt(user_id=None, ip=None):
        return {}

    def reset_failed_login_state(user_id=None, ip=None):
        return

# Password policy helpers (KAN-130)
try:
    from utils.passwords import password_policy_check, policy_hints
except Exception:
    # Defensive fallback: enforce a minimal policy if utils.passwords missing
    def password_policy_check(password, email=None):
        violations = []
        try:
            if not password or len(password) < 8:
                violations.append("Password must be at least 8 characters long.")
            if password and not any(c.isdigit() for c in password):
                violations.append("Password must include at least one digit (0-9).")
        except Exception:
            violations.append("Password validation unavailable.")
        return violations

    def policy_hints():
        return ["At least 8 characters", "Include a digit"]

from sqlalchemy.exc import IntegrityError
import models
import time
from datetime import datetime

auth_bp = Blueprint("auth", __name__)

# Helper: render JS + hint block used by registration and reset forms
def _password_strength_widget_html(hints: list):
    """
    Return inline HTML (as string) that:
      - Renders a small strength meter and list of hints
      - Contains accessible aria-live region so screen readers receive updates
      - Is non-blocking: client-side only; server-side never trusts it
    """
    # Build hints list HTML
    hints_html = "<ul id='pw-hints' aria-hidden='false'>"
    for h in hints:
        hints_html += f"<li class='pw-hint'>{h}</li>"
    hints_html += "</ul>"

    # Lightweight JS: compute a simple score and update UI. Non-blocking for submission.
    # Uses progressive enhancement: if JS disabled, static hints remain visible.
    js = r"""
    <style>
      .pw-meter { height: 8px; background: #eee; border-radius: 4px; margin-top: 4px; }
      .pw-meter > .pw-meter-bar { height: 100%; width: 0%; background: #e74c3c; border-radius: 4px; transition: width 150ms ease; }
      .pw-hint { font-size: 0.9em; color: #333; margin: 0.1em 0; }
      .pw-hint.ok { color: green; }
      .pw-score { font-size: 0.9em; margin-left: 0.5em; font-weight: bold; }
    </style>
    <div id="pw-strength-widget" aria-live="polite">
      <div class="pw-meter" aria-hidden="true"><div class="pw-meter-bar" id="pw-meter-bar"></div></div>
      <div><span id="pw-score" class="pw-score"> </span></div>
      <div id="pw-hints-wrapper">
        %HINTS%
      </div>
    </div>
    <script>
    (function(){
      try {
        var input = document.querySelector('input[type="password"][name="password"]');
        if (!input) return;
        var meterBar = document.getElementById('pw-meter-bar');
        var scoreText = document.getElementById('pw-score');
        var hints = Array.prototype.slice.call(document.querySelectorAll('#pw-hints .pw-hint'));
        // simple evaluator
        function scorePassword(pw){
          if (!pw) return 0;
          var score = 0;
          if (pw.length >= 8) score += 1;
          if (pw.length >= 12) score += 1;
          if (/[A-Z]/.test(pw)) score += 1;
          if (/[a-z]/.test(pw)) score += 1;
          if (/[0-9]/.test(pw)) score += 1;
          if (/[!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]/.test(pw)) score += 1;
          // penalize long runs
          if (/(.)\1\1\1/.test(pw)) score = Math.max(0, score - 2);
          return Math.min(6, score);
        }
        function updateUI(){
          var pw = input.value || '';
          var s = scorePassword(pw);
          var percent = Math.round((s / 6) * 100);
          if (meterBar) {
            meterBar.style.width = percent + '%';
            // color step
            if (s <= 2) meterBar.style.background = '#e74c3c';
            else if (s <= 4) meterBar.style.background = '#f1c40f';
            else meterBar.style.background = '#2ecc71';
          }
          if (scoreText) {
            var txt = ['Very weak','Weak','Okay','Good','Strong','Very Strong','Excellent'][s] || '';
            scoreText.textContent = txt;
          }
          // toggle hints "ok" class when hint satisfied (best-effort mapping)
          hints.forEach(function(h){
            var text = h.textContent || '';
            var ok = false;
            if (text.match(/\\b\\d+ characters\\b/) && pw.length >= parseInt((text.match(/\\d+/) || ['0'])[0])) ok = true;
            if (text.match(/uppercase/i) && /[A-Z]/.test(pw)) ok = true;
            if (text.match(/lowercase/i) && /[a-z]/.test(pw)) ok = true;
            if (text.match(/digit/i) && /[0-9]/.test(pw)) ok = true;
            if (text.match(/symbol/i) && /[!\"#$%&'()*+,\\-./:;<=>?@[\\\]\\\\\\^_`{|}~]/.test(pw)) ok = true;
            if (text.match(/common password/i) && pw.length > 0 && pw.length > 8) {
              // can't check server-side common-list here; simply mark as ok if longer than threshold
              ok = true;
            }
            if (ok) h.classList.add('ok'); else h.classList.remove('ok');
          });
        }
        input.addEventListener('input', updateUI, {passive:true});
        // initial run in case browser autofill
        window.setTimeout(updateUI, 200);
      } catch (e) {
        // Do not let widget errors break page
        console && console.debug && console.debug('pw widget error', e);
      }
    })();
    </script>
    """
    try:
        widget = js.replace("%HINTS%", hints_html)
    except Exception:
        widget = js.replace("%HINTS%", "<ul><li>Use a long, unique password</li></ul>")
    return widget


# Registration form (GET) & process (POST)
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        # CSRF token generation (rendered into form)
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""
        # Build password widget based on server-side hints
        try:
            hints = policy_hints()
        except Exception:
            hints = ["Use a long, unique password"]
        pw_widget = _password_strength_widget_html(hints)

        html = f"""
        <h1>Register</h1>
        <form method="post" action="/auth/register" novalidate>
          <label for="register-email">Email</label>
          <input id="register-email" type="email" name="email" required aria-required="true">
          <label for="register-password">Password</label>
          <input id="register-password" type="password" name="password" required aria-describedby="pw-strength-widget" aria-required="true">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button type="submit">Register</button>
        </form>
        <div>{pw_widget}</div>
        """
        return render_layout(html)
    # POST - process
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        return render_layout("<p>Missing email or password.</p>"), 400

    # Server-side password policy enforcement
    try:
        violations = password_policy_check(password, email=email)
    except Exception as e:
        # Defensive: on unexpected policy check failure, treat as violation to be conservative
        try:
            with open("trace_KAN-130.txt", "a") as f:
                f.write(f"{time.time():.6f} POLICY_CHECK_ERROR err={str(e)}\n")
        except Exception:
            pass
        violations = ["Password validation could not be completed. Choose a stronger password."]

    if violations:
        # Re-render form with violations (friendly)
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""
        # Build a simple messages list
        msgs = "<ul>"
        for v in violations:
            msgs += f"<li>{v}</li>"
        msgs += "</ul>"
        try:
            hints = policy_hints()
        except Exception:
            hints = ["Use a long, unique password"]
        pw_widget = _password_strength_widget_html(hints)
        html = f"""
        <h1>Register</h1>
        <p><strong>We could not accept that password for the following reasons:</strong></p>
        {msgs}
        <form method="post" action="/auth/register" novalidate>
          <label for="register-email-err">Email</label>
          <input id="register-email-err" type="email" name="email" required value="{email}">
          <label for="register-password-err">Password</label>
          <input id="register-password-err" type="password" name="password" required aria-describedby="pw-strength-widget" aria-required="true">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button type="submit">Register</button>
        </form>
        <div>{pw_widget}</div>
        """
        return render_layout(html), 400

    session = models.Session()
    try:
        pwd_hash = hash_password(password, current_app.config.get("SECRET_KEY"))
        user = models.User(email=email, password_hash=pwd_hash, is_active=False)
        session.add(user)
        session.commit()
    except IntegrityError:
        session.rollback()
        return render_layout("<p>Email already registered.</p>"), 400

    # Generate verification token and "send" via dev stub
    token = create_verification_token({"user_id": user.id}, purpose="email_verify", secret=current_app.config["JWT_SECRET"], expires_seconds=current_app.config.get("EMAIL_VERIFY_EXPIRY_SECONDS", 24 * 3600))
    # Dev email stub records token. The actual application would send a real email.
    verification_url = request.url_root.rstrip("/") + url_for("auth.verify_email", token=token)
    send_verification_email(to_email=user.email, verification_url=verification_url, token=token)

    return render_layout("<p>Registered. A verification email has been sent to {email}. Please verify to activate your account.</p>".format(email=email))


# New: Password reset request (send reset token via dev stub)
@auth_bp.route("/reset-request", methods=["GET", "POST"])
def reset_request():
    """
    GET: render a small form to request a password reset (enter your email).
    POST: if email exists, create a short-lived reset token and send via dev stub.
    For privacy, responses are identical whether or not the email exists.
    """
    if request.method == "GET":
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""
        html = f"""
          <h1>Password Reset</h1>
          <p>Enter your account email to receive a password reset link.</p>
          <form method="post" action="/auth/reset-request" novalidate>
            <label for="reset-email">Email</label>
            <input id="reset-email" type="email" name="email" required aria-required="true">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <button type="submit">Send Reset Link</button>
          </form>
        """
        return render_layout(html)

    # POST: send token if user exists (best-effort, privacy-preserving)
    email = request.form.get("email", "").strip().lower()
    try:
        session = models.Session()
        user = session.query(models.User).filter_by(email=email).first()
    except Exception:
        user = None
    finally:
        try:
            session.close()
        except Exception:
            pass

    if user:
        try:
            token = create_verification_token({"user_id": user.id}, purpose="password_reset", secret=current_app.config.get("JWT_SECRET", current_app.config.get("SECRET_KEY", "")), expires_seconds=int(current_app.config.get("PASSWORD_RESET_EXPIRY_SECONDS", 3600)))
            reset_url = request.url_root.rstrip("/") + url_for("auth.reset_password", token=token)
            # Reuse dev email stub for simplicity (development only)
            send_verification_email(to_email=user.email, verification_url=reset_url, token=token)
            try:
                with open("trace_KAN-130.txt", "a") as f:
                    f.write(f"{time.time():.6f} RESET_REQUEST_SENT email={email}\n")
            except Exception:
                pass
        except Exception:
            # best-effort: swallow internal errors to preserve privacy of whether email exists
            try:
                with open("trace_KAN-130.txt", "a") as f:
                    f.write(f"{time.time():.6f} RESET_REQUEST_ERROR email={email}\n")
            except Exception:
                pass

    # Always show same message
    return render_layout("<p>If an account exists for that email, a password reset link has been sent.</p>")


# Password reset endpoint (token-based)
@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    """
    GET: render password entry form for provided reset token.
    POST: validate token and apply new password (enforcing server-side password policy).
    """
    # Validate token on GET just to show a friendly error if invalid/expired
    if request.method == "GET":
        try:
            payload = decode_verification_token(token, secret=current_app.config.get("JWT_SECRET", current_app.config.get("SECRET_KEY", "")), expected_purpose="password_reset")
            user_id = payload.get("user_id")
            if not user_id:
                raise Exception("Invalid token payload.")
        except Exception as e:
            html = "<h1>Reset Link Invalid</h1><p>{msg}</p>".format(msg=str(e))
            return render_layout(html), 400

        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        # Show password form with strength widget
        try:
            hints = policy_hints()
        except Exception:
            hints = ["Use a long, unique password"]
        pw_widget = _password_strength_widget_html(hints)

        html = f"""
          <h1>Set New Password</h1>
          <form method="post" action="/auth/reset/{token}" novalidate>
            <label for="reset-password">New Password</label>
            <input id="reset-password" type="password" name="password" required aria-describedby="pw-strength-widget" aria-required="true">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <button type="submit">Set Password</button>
          </form>
          <div>{pw_widget}</div>
        """
        return render_layout(html)

    # POST: accept new password
    password = request.form.get("password", "")
    if not password:
        return render_layout("<p>Missing password.</p>"), 400

    # Decode token and find user
    try:
        payload = decode_verification_token(token, secret=current_app.config.get("JWT_SECRET", current_app.config.get("SECRET_KEY", "")), expected_purpose="password_reset")
        user_id = payload.get("user_id")
        if not user_id:
            raise Exception("Invalid token payload.")
    except Exception as e:
        html = "<h1>Reset Failed</h1><p>{msg}</p>".format(msg=str(e))
        return render_layout(html), 400

    # Server-side password policy enforcement
    try:
        violations = password_policy_check(password, email=None)
    except Exception as e:
        try:
            with open("trace_KAN-130.txt", "a") as f:
                f.write(f"{time.time():.6f} POLICY_CHECK_ERROR_RESET err={str(e)}\n")
        except Exception:
            pass
        violations = ["Password validation could not be completed. Choose a stronger password."]

    if violations:
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""
        msgs = "<ul>"
        for v in violations:
            msgs += f"<li>{v}</li>"
        msgs += "</ul>"
        try:
            hints = policy_hints()
        except Exception:
            hints = ["Use a long, unique password"]
        pw_widget = _password_strength_widget_html(hints)
        html = f"""
          <h1>Set New Password</h1>
          <p><strong>We could not accept that password for the following reasons:</strong></p>
          {msgs}
          <form method="post" action="/auth/reset/{token}" novalidate>
            <label for="reset-password-err">New Password</label>
            <input id="reset-password-err" type="password" name="password" required aria-describedby="pw-strength-widget" aria-required="true">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <button type="submit">Set Password</button>
          </form>
          <div>{pw_widget}</div>
        """
        return render_layout(html), 400

    # Apply new password
    session = models.Session()
    try:
        user = session.query(models.User).filter_by(id=user_id).first()
        if not user:
            return render_layout("<p>User not found.</p>"), 404
        user.password_hash = hash_password(password, current_app.config.get("SECRET_KEY"))
        session.add(user)
        session.commit()
        try:
            with open("trace_KAN-130.txt", "a") as f:
                f.write(f"{time.time():.6f} PASSWORD_RESET_APPLIED user_id={user_id}\n")
        except Exception:
            pass
        return render_layout("<p>Your password has been updated. You may now log in.</p>")
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        return render_layout(f"<h1>Database Error</h1><p>{str(e)}</p>"), 500
    finally:
        try:
            session.close()
        except Exception:
            pass


# Login form & process (GET /login, POST /login) with brute-force protections
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """
    GET: Render login form (with CSRF token).
    POST: Authenticate using email/password.
      - Before password verification: check for lockout by account (if user exists) and by client IP.
      - On failed authentication: register failed attempt (per-account and per-IP).
      - On successful authentication: reset failed login state (per-account and per-IP).
      - Apply small response backoff on failures to slow brute-force attempts (configurable).
      - Notify user when account or IP is locked (per configured duration).
    """
    # Helper to extract client IP honoring TRUST_X_FORWARDED_FOR
    def _client_ip():
        try:
            trust = bool(current_app.config.get("TRUST_X_FORWARDED_FOR", False))
        except Exception:
            trust = False
        try:
            if trust and request.headers.get("X-Forwarded-For"):
                return request.headers.get("X-Forwarded-For").split(",")[0].strip()
        except Exception:
            pass
        return request.remote_addr or ""

    if request.method == "GET":
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        html = f"""
        <h1>Login</h1>
        <form method="post" action="/auth/login" novalidate>
          <label for="login-email">Email</label>
          <input id="login-email" type="email" name="email" required aria-required="true">
          <label for="login-password">Password</label>
          <input id="login-password" type="password" name="password" required aria-required="true">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button type="submit">Login</button>
        </form>
        <p><a href="/auth/register">Register</a> | <a href="/auth/reset-request">Forgot password?</a></p>
        """
        return render_layout(html)

    # POST: authentication attempt
    email = (request.form.get("email", "") or "").strip().lower()
    password = request.form.get("password", "") or ""
    ip = _client_ip()

    if not email or not password:
        # Small constant-time sleep to reduce timing leakage
        time.sleep(0.02)
        return render_layout("<p>Missing email or password.</p>"), 400

    session = models.Session()
    try:
        # Attempt to find user by email (owner lookup is not sensitive — public)
        user = session.query(models.User).filter_by(email=email).first()
        user_id = user.id if user else None

        # 1) Check lockout status before attempting password verification
        blocked, retry_after, details = False, 0, {}
        try:
            blocked, retry_after, details = check_lockout(user_id=user_id, ip=ip)
        except Exception as e:
            # Defensive: if lockout subsystem errors, do not block auth (log trace)
            try:
                with open("trace_KAN-127.txt", "a") as f:
                    f.write(f"{datetime.utcnow().isoformat()} LOCKOUT_CHECK_ERROR email={email} ip={ip} err={str(e)}\n")
            except Exception:
                pass
            blocked, retry_after, details = False, 0, {}

        if blocked:
            # Inform caller (do not reveal which key triggered block). Use 429 Too Many Requests.
            msg = "<h1>Too Many Attempts</h1><p>Further login attempts have been temporarily blocked. Please try again later.</p>"
            # Optionally include a generic retry time if available
            try:
                if retry_after and int(retry_after) > 0:
                    msg += f"<p>Retry after: {int(retry_after)} seconds.</p>"
            except Exception:
                pass
            # Trace the blocked attempt
            try:
                with open("trace_KAN-127.txt", "a") as f:
                    f.write(f"{datetime.utcnow().isoformat()} LOGIN_BLOCKED email={email} ip={ip} details={details}\n")
            except Exception:
                pass
            resp = make_response(render_layout(msg), 429)
            return resp

        # 2) Verify credentials
        verified = False
        try:
            if user and verify_password(password, user.password_hash, current_app.config.get("SECRET_KEY")):
                verified = True
        except Exception:
            # Any verification issues should not leak details
            verified = False

        if not verified:
            # Record failed attempt for both user (if exists) and IP, best-effort
            try:
                res = register_failed_attempt(user_id=user_id, ip=ip)
                # Trace the registration result
                try:
                    with open("trace_KAN-127.txt", "a") as f:
                        f.write(f"{datetime.utcnow().isoformat()} LOGIN_FAILED_RECORD email={email} ip={ip} result={res}\n")
                except Exception:
                    pass
            except Exception:
                try:
                    with open("trace_KAN-127.txt", "a") as f:
                        f.write(f"{datetime.utcnow().isoformat()} LOGIN_FAILED_RECORD_ERROR email={email} ip={ip}\n")
                except Exception:
                    pass

            # Apply small response delay proportional to recent failures to slow brute force clients
            try:
                # derive an approximate "attempts" from details if record_failed_attempt returned counts; fallback tiny fixed delay
                delay = 0.1
                try:
                    # inspect results for ip or user keys to compute a modest delay
                    ip_key = f"ip:{ip}" if ip else None
                    user_key = f"user:{user_id}" if user_id is not None else None
                    attempts = 0
                    if ip_key and isinstance(res.get(ip_key), dict):
                        attempts = max(attempts, int(res[ip_key].get("count", 0)))
                    if user_key and isinstance(res.get(user_key), dict):
                        attempts = max(attempts, int(res[user_key].get("count", 0)))
                    base = float(current_app.config.get("BACKOFF_RESPONSE_BASE_SECONDS", 0.25))
                    max_delay = float(current_app.config.get("BACKOFF_RESPONSE_MAX_SECONDS", 5.0))
                    delay = min(max_delay, base * max(1, attempts))
                except Exception:
                    delay = min(5.0, float(current_app.config.get("BACKOFF_RESPONSE_MAX_SECONDS", 5.0)))
                time.sleep(delay)
            except Exception:
                # ignore sleep errors
                pass

            # Generic error message - do not disclose whether email exists
            return render_layout("<h1>Invalid credentials</h1><p>Email or password incorrect.</p>"), 401

        # Verified: ensure account is active
        if not getattr(user, "is_active", False):
            return render_layout("<h1>Inactive Account</h1><p>Please verify your email before logging in.</p>"), 403

        # Successful authentication: reset counters for user and IP
        try:
            reset_failed_login_state(user_id=user.id if user else None, ip=ip)
            try:
                with open("trace_KAN-127.txt", "a") as f:
                    f.write(f"{datetime.utcnow().isoformat()} LOGIN_SUCCESS_RESET email={email} user_id={user.id if user else None} ip={ip}\n")
            except Exception:
                pass
        except Exception:
            # Reset failure should not block login
            pass

        # At this time, create a session or token as needed. For now, issue a JWT-like token cookie for session.
        try:
            # Create a session token (expiry configurable). Use utils.jwt to create token + DB session row.
            expiry = int(current_app.config.get("JWT_SESSION_EXPIRY_SECONDS", 24 * 3600))
            token = None
            jti = None
            try:
                # Best-effort import of utils.jwt (may raise in constrained tests; fall back to existing create_verification_token)
                try:
                    from utils.jwt import create_access_token
                    # Provide session metadata for DB record creation
                    session_info = {
                        "user_id": user.id,
                        "ip": ip,
                        "user_agent": request.headers.get("User-Agent", "") or None,
                    }
                    token, jti = create_access_token({"user_id": user.id}, secret=current_app.config.get("JWT_SECRET", ""), expires_seconds=expiry, create_session=True, session_info=session_info)
                except Exception:
                    # Fallback to pre-existing verification-token creation (best-effort, no DB session row)
                    from utils.crypto import create_verification_token as _cv
                    token = _cv({"user_id": user.id}, purpose="session", secret=current_app.config.get("JWT_SECRET", ""), expires_seconds=expiry)
                    jti = None
            except Exception:
                token = None
                jti = None

            # Build response and attach cookie using app helper if available; fallback to utils.crypto.attach_jwt_cookie
            resp = make_response(render_layout("<h1>Logged In</h1><p>Authentication successful.</p>"))
            if token:
                try:
                    # Prefer app-level helper (attached in app.create_app)
                    if hasattr(current_app, "set_jwt_cookie") and callable(getattr(current_app, "set_jwt_cookie")):
                        try:
                            current_app.set_jwt_cookie(resp, token, max_age=expiry)
                        except Exception:
                            # Fallback to crypto helper
                            try:
                                from utils.crypto import attach_jwt_cookie
                                attach_jwt_cookie(resp, token, max_age=expiry)
                            except Exception:
                                pass
                    else:
                        # Fallback to crypto helper if app-level helper not present
                        try:
                            from utils.crypto import attach_jwt_cookie
                            attach_jwt_cookie(resp, token, max_age=expiry)
                        except Exception:
                            pass
                except Exception:
                    # Do not let cookie-setting break login response
                    pass

            return resp
        except Exception:
            # As a last resort, return a simple success page without cookie
            return render_layout("<h1>Logged In</h1><p>Authentication successful.</p>")

    finally:
        try:
            session.close()
        except Exception:
            pass


# Email verification endpoint
@auth_bp.route("/verify-email/<token>", methods=["GET"])
def verify_email(token):
    try:
        payload = decode_verification_token(token, secret=current_app.config["JWT_SECRET"], expected_purpose="email_verify")
    except Exception as e:
        # Friendly error with guidance
        html = "<h1>Verification Failed</h1><p>{msg}</p><p>If your token expired you can request a new verification email by logging in and clicking 'Resend verification' (not yet implemented).</p>".format(msg=str(e))
        return render_layout(html), 400

    user_id = payload.get("user_id")
    if not user_id:
        return render_layout("<p>Invalid token payload.</p>"), 400

    session = models.Session()
    # ID Filter rule: filter by id and ownership checks — here it's user-owned action so we filter by id field only
    user = session.query(models.User).filter_by(id=user_id).first()
    if not user:
        return render_layout("<p>User not found.</p>"), 404

    if user.is_active:
        return render_layout("<h1>Already Verified</h1><p>Your account is already active.</p>")

    user.is_active = True
    session.add(user)
    session.commit()

    return render_layout("<h1>Account Verified</h1><p>Thank you. Your account is now active.</p>")

# End of routes/auth.py