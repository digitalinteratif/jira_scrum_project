#!/usr/bin/env python3
"""
Surgical Blueprint: Custom Domain Integration for URL Redirection (Flask + SQLAlchemy)

Purpose:
- Implements a small, self-contained Flask Blueprint (shortly_bp) that provides:
  - UI using render_template_string (index, register, login, dashboard, password reset, shortener)
  - Short URL generation that uses a configurable BASE_URL (from environment/.env)
  - Root-domain slug listener: requests to /<7-8 char slug> redirect to destination
  - HTTPS upgrade when BASE_URL is configured with https:// (checks X-Forwarded-Proto)
  - Server-side sessions, secure password hashing, redirect cache and analytics enqueue

Notes:
- Uses only Python + Flask + SQLAlchemy + SQLite
- No Node.js or backend JavaScript is used
- Blueprint UI is server-rendered via render_template_string
- To configure production domain, set environment variable BASE_URL (e.g., https://digitalinteractif.com)
- Optional: install python-dotenv to load .env files (load_dotenv is used if available)

Usage:
    from flask import Flask
    from shortly_custom_domain import shortly_bp, init_shortly_app

    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'replace-with-secure-random'
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///shortly_demo_custom_domain.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    init_shortly_app(app)      # initializes DB, config (BASE_URL from env), email templates etc.
    app.register_blueprint(shortly_bp)

    app.run(debug=True)
"""

import os
import re
import uuid
import time
import secrets
import hashlib
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin

from flask import (
    Blueprint,
    Flask,
    current_app,
    request,
    redirect,
    render_template_string,
    make_response,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# Optional dotenv loading (graceful if not installed)
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    # ignore if python-dotenv not present; environment variables can still be used
    pass

# SQLAlchemy instance (initialized in init_shortly_app)
db = SQLAlchemy()

# Blueprint
shortly_bp = Blueprint("shortly", __name__)

# Internal pointer to the app (set in init_shortly_app)
_APP = None

# --------------------
# Templates (strings) - UI served via render_template_string
# --------------------
LAYOUT = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ title or "Shortly" }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin:0; padding:0; background:#f7f7f9; color:#222; }
    header { background:#004a99; color:white; padding:12px 24px; }
    header .brand { font-weight:700; font-size:20px; }
    nav a { color: #bfe0ff; margin-right:10px; text-decoration:none; }
    main { max-width:900px; margin:28px auto; background:white; padding:24px; border-radius:6px; box-shadow:0 2px 8px rgba(0,0,0,0.06); }
    .form-row { margin-bottom:12px; }
    label { display:block; font-size:14px; margin-bottom:6px; }
    input[type=text], input[type=password], input[type=email] { width:100%; padding:8px; border:1px solid #d0d7de; border-radius:4px; }
    button { background:#0066cc; color:white; border:none; padding:10px 14px; border-radius:4px; cursor:pointer; }
    .muted { color:#666; font-size:13px; }
    .danger { color:#b00020; }
    .success { color: #016D11; }
    table { width:100%; border-collapse:collapse; margin-top:12px; }
    th, td { padding:8px; border-bottom:1px solid #eee; text-align:left; font-size:14px; }
    .short-url { font-weight:600; font-family:monospace; }
    footer { padding:12px 24px; text-align:center; color:#777; font-size:13px; margin-top:12px; }
    .flash { padding:10px 12px; border-radius:6px; margin-bottom:12px; }
  </style>
</head>
<body>
<header>
  <div style="display:flex;align-items:center;justify-content:space-between;max-width:1000px;margin:0 auto;">
    <div class="brand"><a href="/" style="color:inherit;text-decoration:none;">Shortly</a></div>
    <nav>
      {% if current_user %}
        <span class="muted">Signed in as {{ current_user.username }}</span>
        <a href="/dashboard">Dashboard</a>
        <a href="/logout">Logout</a>
      {% else %}
        <a href="/register">Register</a>
        <a href="/login">Login</a>
      {% endif %}
    </nav>
  </div>
</header>
<main>
  {{ content|safe }}
</main>
<footer>
  Built with security & speed in mind — UI-first. © {{ year }}
</footer>
</body>
</html>
"""

INDEX_HTML = """
<h1>Shortly — Fast, secure URL shortener</h1>
<p class="muted">Shorten links, track clicks, and manage them from your dashboard.</p>

<section>
  <h3>Get started</h3>
  {% if not current_user %}
    <p><a href="/register"><button>Register</button></a> or <a href="/login"><button>Login</button></a></p>
  {% else %}
    <p><a href="/dashboard"><button>Go to Dashboard</button></a></p>
  {% endif %}
</section>

<section style="margin-top:18px;">
  <h3>Shorten a public link (no account required)</h3>
  <form method="post" action="/shorten-public" id="shorten-public">
    <div class="form-row">
      <label for="url">URL</label>
      <input type="text" id="url" name="url" placeholder="https://example.com/very/long" required>
    </div>
    <div class="form-row">
      <label for="custom">Custom alias (optional)</label>
      <input type="text" id="custom" name="custom" placeholder="my-alias (alphanumeric, - , _ )">
    </div>
    <button type="submit">Shorten</button>
  </form>
  <p class="muted">Shortened links created when not signed-in are anonymous and not manageable from a dashboard.</p>
</section>
"""

LOGIN_HTML = """
<h1>Login</h1>
<form method="post" action="/login">
  <div class="form-row">
    <label for="username">Username or Email</label>
    <input type="text" id="username" name="username" placeholder="username or email" required>
  </div>
  <div class="form-row">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" placeholder="password" required>
  </div>
  <div class="form-row">
    <label><input type="checkbox" name="remember"> Remember this device</label>
  </div>
  <button type="submit">Login</button>
</form>
<p class="muted">Forgot your password? <a href="/password-reset-request">Reset it</a></p>
<p class="muted">Don't have an account? <a href="/register">Register</a></p>
"""

REGISTER_HTML = """
<h1>Register</h1>
<form method="post" action="/register">
  <div class="form-row">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" placeholder="username" pattern="[A-Za-z0-9_\\-]{3,30}" required>
  </div>
  <div class="form-row">
    <label for="email">Email</label>
    <input type="email" id="email" name="email" placeholder="you@example.com" required>
  </div>
  <div class="form-row">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" placeholder="Choose a strong password" required>
  </div>
  <div class="form-row">
    <label for="password2">Confirm Password</label>
    <input type="password" id="password2" name="password2" placeholder="Confirm password" required>
  </div>
  <button type="submit">Create account</button>
</form>
<p class="muted">We will send a verification email to activate your account.</p>
"""

DASHBOARD_HTML = """
<h1>Your Dashboard</h1>

<section>
  <h3>Create a short link</h3>
  <form method="post" action="/dashboard/shorten">
    <div class="form-row">
      <label for="url">Destination URL</label>
      <input type="text" id="url" name="url" placeholder="https://example.com/..." required>
    </div>
    <div class="form-row">
      <label for="custom">Custom alias (optional)</label>
      <input type="text" id="custom" name="custom" placeholder="alias">
    </div>
    <div class="form-row">
      <label for="private">Private (only you can view analytics)</label>
      <input type="checkbox" id="private" name="private">
    </div>
    <button type="submit">Shorten</button>
  </form>
</section>

<section style="margin-top:16px;">
  <h3>Your links</h3>
  {% if urls %}
    <table>
      <thead><tr><th>Short</th><th>Destination</th><th>Clicks</th><th>Created</th><th>Actions</th></tr></thead>
      <tbody>
      {% for u in urls %}
        <tr>
          <td class="short-url"><a href="{{ base_url }}/{{ u.code }}">{{ base_url }}/{{ u.code }}</a></td>
          <td><a href="{{ u.destination }}" target="_blank" rel="noopener">{{ u.destination }}</a></td>
          <td>{{ u.clicks_count }}</td>
          <td>{{ u.created_at }}</td>
          <td><a href="/dashboard/url/{{ u.id }}">Manage</a></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">You haven't created any short links yet.</p>
  {% endif %}
</section>
"""

PASSWORD_RESET_REQUEST_HTML = """
<h1>Password Reset</h1>
<p class="muted">Enter the email address associated with your account; we'll send a reset link.</p>
<form method="post" action="/password-reset-request">
  <div class="form-row">
    <label for="email">Email</label>
    <input type="email" id="email" name="email" placeholder="you@example.com" required>
  </div>
  <button type="submit">Send reset link</button>
</form>
"""

PASSWORD_RESET_HTML = """
<h1>Choose a new password</h1>
<form method="post" action="/password-reset/{{ token }}">
  <div class="form-row">
    <label for="password">New password</label>
    <input type="password" id="password" name="password" required>
  </div>
  <div class="form-row">
    <label for="password2">Confirm new password</label>
    <input type="password" id="password2" name="password2" required>
  </div>
  <div class="form-row">
    <label for="code">Reset code</label>
    <input type="text" id="code" name="code" placeholder="Enter the numeric/alphanumeric code from your email" required>
  </div>
  <button type="submit">Reset password</button>
</form>
"""

# --------------------
# Models
# --------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class URL(db.Model):
    __tablename__ = "urls"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    code = db.Column(db.String(128), unique=True, nullable=False, index=True)
    destination = db.Column(db.Text, nullable=False)
    is_private = db.Column(db.Boolean, default=False, nullable=False)
    clicks_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Click(db.Model):
    __tablename__ = "clicks"
    id = db.Column(db.Integer, primary_key=True)
    url_id = db.Column(db.Integer, db.ForeignKey("urls.id"), nullable=False, index=True)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(1024))
    referer = db.Column(db.String(1024))
    country = db.Column(db.String(100))
    region = db.Column(db.String(100))


class EmailTemplate(db.Model):
    __tablename__ = "email_templates"
    name = db.Column(db.String(80), primary_key=True)
    subject_template = db.Column(db.Text, nullable=False)
    body_template = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PasswordReset(db.Model):
    __tablename__ = "password_resets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ip_request = db.Column(db.String(45))


class EmailVerification(db.Model):
    __tablename__ = "email_verifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ip_request = db.Column(db.String(45))


class SessionModel(db.Model):
    __tablename__ = "sessions"
    session_id = db.Column(db.String(128), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    data = db.Column(db.Text)


# --------------------
# Helpers: rendering
# --------------------
def render_with_layout(content_template, **context):
    try:
        content_html = render_template_string(content_template, **context)
    except Exception as e:
        content_html = f"<h1>Rendering error</h1><p class='muted'>Template rendering failed: {e}</p>"
    base_context = dict(year=datetime.utcnow().year)
    base_context.update(context)
    base_context.update({"content": content_html})
    try:
        return render_template_string(LAYOUT, **base_context)
    except Exception:
        return f"<html><body>{content_html}</body></html>"


def render_with_message_and_template(template_str, message=None, kind="danger", **context):
    try:
        content_html = render_template_string(template_str, **context)
    except Exception as e:
        content_html = f"<h1>Rendering error</h1><p class='muted'>Template rendering failed: {e}</p>"
    if message:
        msg_html = f"<div class='flash {kind}'><strong>{message}</strong></div>"
        content_html = msg_html + content_html
    base_context = dict(year=datetime.utcnow().year)
    base_context.update(context)
    base_context.update({"content": content_html})
    try:
        return render_template_string(LAYOUT, **base_context)
    except Exception:
        return f"<html><body>{content_html}</body></html>"


# --------------------
# Utilities & constants
# --------------------
SESSION_COOKIE_NAME = "session_id"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_\-]{2,64}$")
_CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ROOT_SLUG_RE = re.compile(r"^[A-Za-z0-9]{7,8}$")  # AC2: 7-8 character slugs (letters/digits)

def now_utc():
    return datetime.utcnow()


# --------------------
# Redirect cache (tiny in-memory TTL cache)
# --------------------
_redirect_cache = {}
_cache_lock = threading.Lock()

def cache_set_code(code: str, obj: dict, ttl: int = 60):
    with _cache_lock:
        _redirect_cache[code] = {"obj": obj, "expires": time.time() + ttl}

def cache_get_code(code: str):
    with _cache_lock:
        entry = _redirect_cache.get(code)
        if not entry:
            return None
        if entry["expires"] < time.time():
            del _redirect_cache[code]
            return None
        return entry["obj"]


# --------------------
# DB-centric helpers (SQLAlchemy)
# --------------------
def find_user_by_username_or_email(identifier: str):
    if not identifier:
        return None
    return User.query.filter(
        (User.username.ilike(identifier)) | (User.email.ilike(identifier))
    ).first()

def find_user_by_email(email: str):
    if not email:
        return None
    return User.query.filter(User.email.ilike(email)).first()

def load_user_by_id(user_id: int):
    if not user_id:
        return None
    return User.query.get(user_id)

def create_user_record(username: str, email: str, password_hash: str):
    u = User(username=username, email=email, password_hash=password_hash)
    db.session.add(u)
    db.session.commit()
    return u

def create_email_verification(user_id: int, code_hash: str, token: str, expires_at: datetime, ip_request: str = None):
    ev = EmailVerification(user_id=user_id, code_hash=code_hash, token=token, expires_at=expires_at, ip_request=ip_request)
    db.session.add(ev)
    db.session.commit()

def create_password_reset_entry(user_id: int, token: str, code_hash: str, expires_at: datetime, ip_request: str = None):
    pr = PasswordReset(user_id=user_id, token=token, code_hash=code_hash, expires_at=expires_at, ip_request=ip_request)
    db.session.add(pr)
    db.session.commit()

def find_password_reset_by_token(token: str):
    if not token:
        return None
    return PasswordReset.query.filter_by(token=token).first()

def mark_password_reset_used(reset_id: int):
    pr = PasswordReset.query.get(reset_id)
    if pr:
        pr.used = True
        db.session.commit()

def update_user_password(user_id: int, new_hash: str):
    u = User.query.get(user_id)
    if u:
        u.password_hash = new_hash
        db.session.commit()

def db_fetch_url_by_code(code: str):
    if not code:
        return None
    return URL.query.filter_by(code=code).first()

def get_urls_for_user(user_id: int):
    if not user_id:
        return []
    rows = URL.query.filter_by(owner_id=user_id).order_by(URL.created_at.desc()).limit(100).all()
    return rows

def create_url(owner_id, code, destination, is_private):
    url = URL(owner_id=owner_id, code=code, destination=destination, is_private=bool(is_private), created_at=now_utc())
    db.session.add(url)
    db.session.commit()
    # prime cache
    cache_set_code(code, {
        "id": url.id,
        "owner_id": url.owner_id,
        "code": url.code,
        "destination": url.destination,
        "is_private": url.is_private,
    }, ttl=60)
    return url

def generate_random_code(length=6):
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))

def create_unique_code(custom: str = None):
    if custom:
        if not ALIAS_RE.match(custom):
            raise ValueError("Invalid custom alias")
        if URL.query.filter_by(code=custom).first():
            raise ValueError("Alias already in use")
        return custom
    attempt = 0
    while True:
        code = generate_random_code(6)
        if not URL.query.filter_by(code=code).first():
            return code
        attempt += 1
        if attempt > 20:
            return generate_random_code(8)

def validate_url(dest: str):
    try:
        parsed = urlparse(dest)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        return True
    except Exception:
        return False


# --------------------
# Email templates & simulated send
# --------------------
def load_email_template(name: str):
    return EmailTemplate.query.get(name)

def send_email(to: str, subject: str, html: str):
    # Simulated send (print). Replace with SMTP/SES/SendGrid in production.
    print("----- Simulated email -----")
    print(f"To: {to}")
    print(f"Subject: {subject}")
    print("Body (HTML):")
    print(html)
    print("----- End email -----")


# --------------------
# Async analytics: click recording
# --------------------
def enqueue_click_event(url_id: int, ip: str, ua: str, referer: str):
    """
    Fire-and-forget worker thread that records Click and increments URL.clicks_count.
    We push an app_context to ensure SQLAlchemy session works inside the thread.
    """
    global _APP

    def worker(app_ref, url_id_, ip_, ua_, referer_):
        with app_ref.app_context():
            try:
                click = Click(url_id=url_id_, occurred_at=now_utc(), ip=ip_, user_agent=ua_, referer=referer_)
                db.session.add(click)
                # update count atomically within this DB session
                url_obj = URL.query.get(url_id_)
                if url_obj:
                    url_obj.clicks_count = (url_obj.clicks_count or 0) + 1
                db.session.commit()
            except Exception as e:
                # Print for demo; replace with proper logging
                print("Error recording click:", e)

    if _APP is None:
        # cannot enqueue reliably without app context; skip recording (rare in dev)
        return
    t = threading.Thread(target=worker, args=(_APP, url_id, ip, ua, referer), daemon=True)
    t.start()


# --------------------
# Session management (server-side)
# --------------------
def create_session(user_id: int, remember: bool = False):
    sid = secrets.token_urlsafe(32)
    created_at = now_utc()
    expires = created_at + (timedelta(days=7) if remember else timedelta(days=1))
    s = SessionModel(session_id=sid, user_id=user_id, created_at=created_at, expires_at=expires, data=None)
    db.session.add(s)
    db.session.commit()
    return sid, expires

def get_session(session_id: str):
    if not session_id:
        return None
    s = SessionModel.query.get(session_id)
    if not s:
        return None
    if s.expires_at < now_utc():
        db.session.delete(s)
        db.session.commit()
        return None
    return s

def delete_session(session_id: str):
    s = SessionModel.query.get(session_id)
    if s:
        db.session.delete(s)
        db.session.commit()

def invalidate_all_sessions_for_user(user_id: int):
    SessionModel.query.filter_by(user_id=user_id).delete()
    db.session.commit()


# --------------------
# Rate limiting (basic in-memory)
# --------------------
_RATE_LIMITS = {}  # ip -> [timestamps]
_RATE_LOCK = threading.Lock()

def check_rate_limit(ip: str, window_seconds=60, max_requests=20):
    now = time.time()
    with _RATE_LOCK:
        lst = _RATE_LIMITS.setdefault(ip, [])
        cutoff = now - window_seconds
        # remove old
        while lst and lst[0] < cutoff:
            lst.pop(0)
        if len(lst) >= max_requests:
            return False
        lst.append(now)
    return True


# --------------------
# Current user helper
# --------------------
def get_current_user():
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None
    s = get_session(sid)
    if not s:
        return None
    return load_user_by_id(s.user_id)


# --------------------
# Helpers for BASE_URL (AC1)
# --------------------
def app_base_url():
    """
    Return configured BASE_URL (from app config) if present and valid, otherwise fallback to request.url_root.
    Ensures no trailing slash.
    """
    base = current_app.config.get("BASE_URL")
    if base:
        # basic validation
        try:
            parsed = urlparse(base)
            if parsed.scheme and parsed.netloc:
                return base.rstrip("/")
        except Exception:
            pass
    # fallback to runtime request root
    return request.url_root.rstrip("/")


def is_production_domain_active():
    """
    Determine whether the configured BASE_URL indicates a production domain (non-localhost/127.*).
    """
    base = current_app.config.get("BASE_URL")
    if not base:
        return False
    try:
        parsed = urlparse(base)
        host = parsed.hostname or ""
        if host.startswith("127.") or host == "localhost":
            return False
        return True
    except Exception:
        return False


def base_url_uses_https():
    base = current_app.config.get("BASE_URL") or ""
    return base.lower().startswith("https://")


# --------------------
# Routes
# --------------------
@shortly_bp.before_app_request
def enforce_https_if_configured():
    """
    AC3: If BASE_URL is configured with https and points to a non-localhost domain,
    upgrade incoming HTTP requests to HTTPS (respecting X-Forwarded-Proto for reverse proxies).
    """
    # Only enforce for non-GET assets and for real requests (skip CLI, static etc.)
    if not request.host:
        return
    # Get configured base
    if not is_production_domain_active() or not base_url_uses_https():
        return
    # Determine whether the incoming request is already secure.
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    if (request.is_secure or forwarded_proto.lower() == "https"):
        return
    # If the incoming host does not match configured domain, we don't force.
    try:
        expected_host = urlparse(current_app.config.get("BASE_URL")).netloc
    except Exception:
        expected_host = ""
    if expected_host and request.host != expected_host:
        # If host differs, do not force upgrade here; leave routing to proxy or other rules.
        return
    # Upgrade to https preserving path and querystring
    url = request.url
    secure_url = url.replace("http://", "https://", 1)
    return redirect(secure_url, code=301)


@shortly_bp.route("/", methods=["GET"])
def index():
    user = get_current_user()
    return render_with_layout(INDEX_HTML, current_user=user)


@shortly_bp.route("/shorten-public", methods=["POST"])
def shorten_public():
    url = (request.form.get("url") or "").strip()
    custom = (request.form.get("custom") or "").strip()
    if not validate_url(url):
        return render_with_message_and_template(INDEX_HTML, "Invalid URL (must be http/https)", kind="danger", current_user=get_current_user())
    try:
        code = create_unique_code(custom if custom else None)
    except ValueError as e:
        return render_with_message_and_template(INDEX_HTML, str(e), kind="danger", current_user=get_current_user())
    create_url(None, code, url, is_private=False)
    base = app_base_url()
    message_html = f"<p class='success'>Short URL created: <a class='short-url' href='{base}/{code}'>{base}/{code}</a></p>"
    return render_with_layout(message_html + INDEX_HTML, current_user=get_current_user())


@shortly_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_with_layout(REGISTER_HTML, current_user=get_current_user())
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if not username or not email or not password:
        return render_with_message_and_template(REGISTER_HTML, "Missing fields", kind="danger", current_user=get_current_user())
    if password != password2:
        return render_with_message_and_template(REGISTER_HTML, "Passwords do not match", kind="danger", current_user=get_current_user())
    # uniqueness checks
    if User.query.filter(User.username.ilike(username)).first():
        return render_with_message_and_template(REGISTER_HTML, "Username already exists", kind="danger", current_user=get_current_user())
    if find_user_by_email(email):
        return render_with_message_and_template(REGISTER_HTML, "Email already registered", kind="danger", current_user=get_current_user())
    pw_hash = generate_password_hash(password)
    user = create_user_record(username=username, email=email, password_hash=pw_hash)
    # create email verification
    token = secrets.token_urlsafe(32)
    code = secrets.token_hex(3)  # short code
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    expires_at = now_utc() + timedelta(hours=24)
    create_email_verification(user.id, code_hash, token, expires_at, ip_request=request.remote_addr)
    tpl = load_email_template("email_verification")
    if tpl:
        verify_url = app_base_url() + "/email-verify/" + token
        body = render_template_string(tpl.body_template, user=user, code=code, verify_url=verify_url, minutes=24 * 60)
        send_email(user.email, tpl.subject_template, body)
    return render_with_layout("<p class='success'>Account created. Check your email for verification.</p>", current_user=None)


@shortly_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_with_layout(LOGIN_HTML, current_user=get_current_user())
    identifier = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    remember = True if request.form.get("remember") else False
    if not identifier or not password:
        return render_with_message_and_template(LOGIN_HTML, "Missing credentials", kind="danger", current_user=get_current_user())
    user = find_user_by_username_or_email(identifier)
    if not user or not check_password_hash(user.password_hash, password):
        return render_with_message_and_template(LOGIN_HTML, "Invalid credentials", kind="danger", current_user=None)
    sid, expires = create_session(user.id, remember=remember)
    resp = make_response(redirect("/dashboard"))
    # Set secure flag for session cookie based on configured BASE_URL (AC1 & AC3)
    secure_flag = base_url_uses_https()
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        sid,
        httponly=True,
        secure=secure_flag,
        samesite="Strict",
        expires=expires,
        path="/",
    )
    return resp


@shortly_bp.route("/logout", methods=["GET"])
def logout():
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        delete_session(sid)
    resp = make_response(redirect("/"))
    resp.set_cookie(SESSION_COOKIE_NAME, "", expires=0, httponly=True, samesite="Strict", path="/")
    return resp


def require_login_redirect():
    return redirect("/login")


@shortly_bp.route("/dashboard", methods=["GET"])
def dashboard():
    user = get_current_user()
    if not user:
        return require_login_redirect()
    urls = get_urls_for_user(user.id)
    base_url = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
    # format created_at as string for display
    urls_view = []
    for u in urls:
        urls_view.append({
            "id": u.id,
            "code": u.code,
            "destination": u.destination,
            "clicks_count": u.clicks_count,
            "created_at": u.created_at.isoformat(sep=" ", timespec="seconds")
        })
    return render_with_layout(DASHBOARD_HTML, current_user=user, urls=urls_view, base_url=base_url)


@shortly_bp.route("/dashboard/shorten", methods=["POST"])
def dashboard_shorten():
    user = get_current_user()
    if not user:
        return require_login_redirect()
    destination = (request.form.get("url") or "").strip()
    custom = (request.form.get("custom") or "").strip()
    is_private = True if request.form.get("private") else False
    if not validate_url(destination):
        return render_with_message_and_template(DASHBOARD_HTML, "Invalid destination URL", kind="danger", current_user=user, urls=get_urls_for_user(user.id), base_url=current_app.config.get("BASE_URL") or request.url_root.rstrip("/"))
    try:
        code = create_unique_code(custom if custom else None)
    except ValueError as e:
        return render_with_message_and_template(DASHBOARD_HTML, str(e), kind="danger", current_user=user, urls=get_urls_for_user(user.id), base_url=current_app.config.get("BASE_URL") or request.url_root.rstrip("/"))
    create_url(owner_id=user.id, code=code, destination=destination, is_private=is_private)
    return redirect("/dashboard")


@shortly_bp.route("/dashboard/url/<int:url_id>", methods=["GET"])
def dashboard_manage_url(url_id):
    user = get_current_user()
    if not user:
        return require_login_redirect()
    u = URL.query.filter_by(id=url_id, owner_id=user.id).first()
    if not u:
        return render_with_layout("<h1>Not found</h1><p class='muted'>You don't have access to that resource.</p>", current_user=user), 404
    base_url = current_app.config.get("BASE_URL") or request.url_root.rstrip("/")
    content = f"""
    <h1>Manage URL</h1>
    <p>Short: <span class='short-url'>{base_url}/{u.code}</span></p>
    <p>Destination: <a href="{u.destination}" target="_blank">{u.destination}</a></p>
    <p>Clicks: {u.clicks_count}</p>
    <p>Created: {u.created_at}</p>
    <p class='muted'>Analytics and edit features would appear here (demo).</p>
    """
    return render_with_layout(content, current_user=user)


@shortly_bp.route("/r/<code>", methods=["GET"])
def redirect_code(code):
    """
    Existing redirect route under /r/<code>. Keeps existing behavior (302).
    """
    # Check cache
    obj = cache_get_code(code)
    if not obj:
        db_obj = db_fetch_url_by_code(code)
        if not db_obj:
            return render_with_layout("<h1>Not found</h1><p>The short link does not exist.</p>", current_user=get_current_user()), 404
        obj = {
            "id": db_obj.id,
            "owner_id": db_obj.owner_id,
            "code": db_obj.code,
            "destination": db_obj.destination,
            "is_private": db_obj.is_private,
        }
        cache_set_code(code, obj, ttl=60)
    # privacy check
    if obj.get("is_private"):
        user = get_current_user()
        if not user or user.id != obj.get("owner_id"):
            return render_with_layout("<h1>Private link</h1><p>This link is private.</p>", current_user=get_current_user()), 403
    # enqueue analytics
    enqueue_click_event(obj["id"], ip=request.remote_addr or "", ua=request.headers.get("User-Agent", ""), referer=request.headers.get("Referer", ""))
    # Use 302 for /r/<code> (existing behavior)
    return redirect(obj["destination"], code=302)


@shortly_bp.route("/password-reset-request", methods=["GET", "POST"])
def password_reset_request():
    if request.method == "GET":
        return render_with_layout(PASSWORD_RESET_REQUEST_HTML, current_user=get_current_user())
    email = (request.form.get("email") or "").strip().lower()
    user = find_user_by_email(email)
    ip = request.remote_addr
    if user:
        if not check_rate_limit(ip, window_seconds=3600, max_requests=5):
            return render_with_layout("<p>If an account with that email exists, a reset link has been sent.</p>", current_user=get_current_user())
        token = secrets.token_urlsafe(32)
        code = "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = now_utc() + timedelta(hours=1)
        create_password_reset_entry(user.id, token, code_hash, expires_at, ip_request=ip)
        tpl = load_email_template("password_reset")
        if tpl:
            reset_url = app_base_url() + "/password-reset/" + token
            html_body = render_template_string(tpl.body_template, user=user, code=code, reset_url=reset_url, minutes=60)
            send_email(user.email, tpl.subject_template, html_body)
    # neutral response
    return render_with_layout("<p>If an account with that email exists, a reset link has been sent.</p>", current_user=get_current_user())


@shortly_bp.route("/password-reset/<token>", methods=["GET", "POST"])
def password_reset(token):
    if request.method == "GET":
        return render_with_layout(PASSWORD_RESET_HTML, current_user=get_current_user(), token=token)
    code_submitted = (request.form.get("code") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if password != password2:
        return render_with_message_and_template(PASSWORD_RESET_HTML, "Passwords do not match", kind="danger", current_user=get_current_user(), token=token)
    row = find_password_reset_by_token(token)
    if not row:
        return render_with_layout("<p>Reset token invalid or expired.</p>", current_user=get_current_user()), 400
    if row.used:
        return render_with_layout("<p>Reset token has already been used.</p>", current_user=get_current_user()), 400
    if row.expires_at < now_utc():
        return render_with_layout("<p>Reset token invalid or expired.</p>", current_user=get_current_user()), 400
    if hashlib.sha256(code_submitted.encode()).hexdigest() != row.code_hash:
        return render_with_message_and_template(PASSWORD_RESET_HTML, "Invalid code", kind="danger", current_user=get_current_user(), token=token)
    new_hash = generate_password_hash(password)
    update_user_password(row.user_id, new_hash)
    mark_password_reset_used(row.id)
    invalidate_all_sessions_for_user(row.user_id)
    user = load_user_by_id(row.user_id)
    if user:
        send_email(user.email, "Your password has been changed", f"<p>Hello {user.username},</p><p>Your password was successfully changed. If this wasn't you, contact support.</p>")
    return render_with_layout("<p class='success'>Password changed. You may now <a href='/login'>login</a>.</p>", current_user=None)


# --------------------
# AC2: Global redirection listener at root-level for 7-8 char slugs
# This route intentionally sits near the end so explicit routes are matched first.
# --------------------
@shortly_bp.route("/<path:maybe_slug>", methods=["GET"])
def root_slug_listener(maybe_slug):
    """
    AC2: If a request hits the root domain with a 7-8 character slug (e.g., /XyZ1234),
    and that slug exists in the database, perform a permanent redirect to the destination.
    This allows short URLs to be served directly from the apex domain:
      https://digitalinteractif.com/AbC1234 -> original destination
    This route will not interfere with other known endpoints, because explicit routes (e.g., /login, /r/<code>) are registered earlier.
    """
    # Ignore well-known endpoints (simple guard)
    reserved = {
        "login", "register", "dashboard", "logout", "r", "password-reset-request", "password-reset", "static", ""
    }
    # If path contains slashes, only consider single-segment slugs
    if "/" in maybe_slug:
        return render_with_layout("<h1>Not found</h1><p>The requested resource does not exist.</p>", current_user=get_current_user()), 404
    slug = maybe_slug.strip()
    if not slug or slug in reserved:
        # Not a slug we should attempt to resolve here
        return render_with_layout("<h1>Not found</h1><p>The requested resource does not exist.</p>", current_user=get_current_user()), 404
    # Only handle 7-8 character alphanumeric slugs per AC2
    if not ROOT_SLUG_RE.match(slug):
        return render_with_layout("<h1>Not found</h1><p>The short link does not exist.</p>", current_user=get_current_user()), 404
    # Lookup in cache/db
    obj = cache_get_code(slug)
    if not obj:
        db_obj = db_fetch_url_by_code(slug)
        if not db_obj:
            return render_with_layout("<h1>Not found</h1><p>The short link does not exist.</p>", current_user=get_current_user()), 404
        obj = {
            "id": db_obj.id,
            "owner_id": db_obj.owner_id,
            "code": db_obj.code,
            "destination": db_obj.destination,
            "is_private": db_obj.is_private,
        }
        cache_set_code(slug, obj, ttl=60)
    # privacy check
    if obj.get("is_private"):
        user = get_current_user()
        if not user or user.id != obj.get("owner_id"):
            return render_with_layout("<h1>Private link</h1><p>This link is private.</p>", current_user=get_current_user()), 403
    # enqueue analytics
    enqueue_click_event(obj["id"], ip=request.remote_addr or "", ua=request.headers.get("User-Agent", ""), referer=request.headers.get("Referer", ""))
    # AC2: Global root redirect returns 301 (permanent). This can be changed to 302 if desired.
    return redirect(obj["destination"], code=301)


# --------------------
# Initialization helpers
# --------------------
def _seed_email_templates():
    if not EmailTemplate.query.get("password_reset"):
        tpl = EmailTemplate(
            name="password_reset",
            subject_template="Reset your Shortly password",
            body_template="<p>Hello {{ user.username }},</p><p>We received a password reset request. Use the code <strong>{{ code }}</strong> or click <a href='{{ reset_url }}'>this link</a> to reset your password. This link expires in {{ minutes }} minutes.</p><p>If you did not request this, ignore this email.</p>",
        )
        db.session.add(tpl)
    if not EmailTemplate.query.get("email_verification"):
        tpl = EmailTemplate(
            name="email_verification",
            subject_template="Verify your Shortly account",
            body_template="<p>Hello {{ user.username }},</p><p>Welcome! Please verify your email by using this code <strong>{{ code }}</strong> or clicking <a href='{{ verify_url }}'>this link</a>. This expires in {{ minutes }} minutes.</p>",
        )
        db.session.add(tpl)
    db.session.commit()


def init_shortly_app(app: Flask):
    """
    Initialize the SQLAlchemy extension, create tables and seed templates.
    Also configure BASE_URL from environment variable 'BASE_URL' (AC1).
    Must be called before registering the blueprint or before first request.
    """
    global _APP
    _APP = app
    # Load BASE_URL from environment into app.config for use throughout the app
    # The environment variable should be set in production (e.g., BASE_URL=https://digitalinteractif.com)
    base_from_env = os.environ.get("BASE_URL")
    if base_from_env:
        # Basic normalization: remove trailing slash
        app.config["BASE_URL"] = base_from_env.rstrip("/")
    else:
        # Not configured: leave unset; functions will fallback to request.url_root
        app.config.pop("BASE_URL", None)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _seed_email_templates()


# --------------------
# Standalone run support for demo (uses environment BASE_URL if set)
# --------------------
def create_app_for_demo(db_path: str = "sqlite:///shortly_demo_custom_domain.db"):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_urlsafe(32)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_path
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # allow override BASE_URL via environment before init_shortly_app (init reads env)
    init_shortly_app(app)
    app.register_blueprint(shortly_bp)
    return app


if __name__ == "__main__":
    demo_app = create_app_for_demo()
    # Inform about configured BASE_URL for clarity (AC1)
    configured = demo_app.config.get("BASE_URL") or "not configured (falling back to localhost during requests)"
    print(f"Starting Shortly demo (custom-domain-aware) with BASE_URL={configured}")
    # In production, run behind Gunicorn/Nginx on ports 80/443; demo uses Flask dev server only.
    demo_app.run(debug=True)