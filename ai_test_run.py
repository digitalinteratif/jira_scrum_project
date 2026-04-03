#!/usr/bin/env python3
"""
Single-file Flask app implementing "Technical Blueprint — High-Performance URL Shortening Web Service with Mandatory HTML5 UI"

- Uses a single LAYOUT string and render_with_layout helper (no Jinja extends).
- All UI routes return HTML pages (render_template_string).
- SQLite used for storage for demo purposes.
- Server-side sessions stored in the sessions table; cookie stores opaque session_id.
- Password hashing via werkzeug.security (pbkdf2:sha256). Replace with Argon2 in production.
- Email sending is simulated by printing to console; email templates are stored in DB.
- Redirect handler uses a minimal in-memory cache; click analytics recorded asynchronously.
- All resource access enforces owner_id filtering.
"""

from flask import (
    Flask,
    request,
    redirect,
    make_response,
    render_template_string,
    abort,
    url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import secrets
import hashlib
import uuid
import threading
import time
import re
from urllib.parse import urlparse
from jinja2.exceptions import TemplateNotFound, TemplateSyntaxError

app = Flask(__name__)
# Random secret for signing Flask session cookies (we use our server-side sessions)
app.secret_key = secrets.token_urlsafe(32)

DB_PATH = "shortly_demo.db"

# --------------------
# Templates (exact strings provided by spec)
# --------------------
LAYOUT = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{{ title or "Shortly" }}</title>
  <style>
    /* Minimal CSS for clarity; replace with your CSS system */
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
          <td class="short-url"><a href="{{ base_url }}/r/{{ u.code }}">{{ base_url }}/r/{{ u.code }}</a></td>
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
# Database helpers
# --------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    # users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uuid TEXT NOT NULL UNIQUE,
          username TEXT NOT NULL UNIQUE,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          email_verified_at TIMESTAMP NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    # urls
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS urls (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_id INTEGER NULL,
          code TEXT NOT NULL UNIQUE,
          destination TEXT NOT NULL,
          is_private INTEGER NOT NULL DEFAULT 0,
          clicks_count INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(owner_id) REFERENCES users(id)
        )
    """
    )
    # clicks
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clicks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url_id INTEGER NOT NULL,
          occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          ip TEXT,
          user_agent TEXT,
          referer TEXT,
          country TEXT,
          region TEXT,
          FOREIGN KEY(url_id) REFERENCES urls(id)
        )
    """
    )
    # email_templates
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS email_templates (
          name TEXT PRIMARY KEY,
          subject_template TEXT NOT NULL,
          body_template TEXT NOT NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    # password_resets
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS password_resets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          token TEXT NOT NULL UNIQUE,
          code_hash TEXT NOT NULL,
          expires_at TIMESTAMP NOT NULL,
          used INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          ip_request TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """
    )
    # email_verifications
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          token TEXT NOT NULL UNIQUE,
          code_hash TEXT NOT NULL,
          expires_at TIMESTAMP NOT NULL,
          used INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """
    )
    # sessions
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY,
          user_id INTEGER NOT NULL,
          created_at TIMESTAMP NOT NULL,
          expires_at TIMESTAMP NOT NULL,
          data BLOB,
          FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """
    )
    conn.commit()

    # Seed basic email_templates if absent
    cur.execute("SELECT 1 FROM email_templates WHERE name = ?", ("password_reset",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO email_templates (name, subject_template, body_template) VALUES (?, ?, ?)",
            (
                "password_reset",
                "Reset your Shortly password",
                "<p>Hello {{ user.username }},</p><p>We received a password reset request. Use the code <strong>{{ code }}</strong> or click <a href='{{ reset_url }}'>this link</a> to reset your password. This link expires in {{ minutes }} minutes.</p><p>If you did not request this, ignore this email.</p>",
            ),
        )
    cur.execute("SELECT 1 FROM email_templates WHERE name = ?", ("email_verification",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO email_templates (name, subject_template, body_template) VALUES (?, ?, ?)",
            (
                "email_verification",
                "Verify your Shortly account",
                "<p>Hello {{ user.username }},</p><p>Welcome! Please verify your email by using this code <strong>{{ code }}</strong> or clicking <a href='{{ verify_url }}'>this link</a>. This expires in {{ minutes }} minutes.</p>",
            ),
        )
    conn.commit()
    conn.close()


# --------------------
# Utility functions
# --------------------
def render_with_layout(content_template, **context):
    """
    Render an inner content template (string) then inject into LAYOUT.
    This follows the recommended pattern: first render inner content to HTML,
    then render LAYOUT with that content.
    """
    # Ensure we pass current_user for LAYOUT nav usage
    try:
        content_html = render_template_string(content_template, **context)
    except (TemplateNotFound, TemplateSyntaxError) as e:
        # Defensive: if a template tries to extend a missing file or has syntax errors,
        # present a minimal friendly error UI rather than throwing a TemplateNotFound
        content_html = f"<h1>Rendering error</h1><p class='muted'>Template rendering failed: {str(e)}</p>"
    base_context = dict(year=datetime.utcnow().year)
    base_context.update(context)
    base_context.update({"content": content_html})
    try:
        return render_template_string(LAYOUT, **base_context)
    except (TemplateNotFound, TemplateSyntaxError) as e:
        # If LAYOUT itself is broken or referencing external templates via extends,
        # fallback to a very simple wrapper
        simple = f"<html><head><title>{base_context.get('title','Shortly')}</title></head><body>{content_html}</body></html>"
        return simple


def render_with_message_and_template(template_str, message=None, kind="danger", **context):
    """
    Helper: render a message box above a content template (when template doesn't include error area).
    """
    try:
        content_html = render_template_string(template_str, **context)
    except (TemplateNotFound, TemplateSyntaxError) as e:
        content_html = f"<h1>Rendering error</h1><p class='muted'>Template rendering failed: {str(e)}</p>"
    if message:
        msg_html = f"<div class='flash {kind}'><strong>{message}</strong></div>"
        content_html = msg_html + content_html
    base_context = dict(year=datetime.utcnow().year)
    base_context.update(context)
    base_context.update({"content": content_html})
    try:
        return render_template_string(LAYOUT, **base_context)
    except (TemplateNotFound, TemplateSyntaxError):
        return f"<html><body>{content_html}</body></html>"


def now_utc():
    return datetime.utcnow()


# Session management (server-side)
SESSION_COOKIE_NAME = "session_id"


def create_session(user_id: int, remember: bool = False):
    """
    Create a server-side session row and return the session_id.
    Expiry: 1 day default, 7 days if remember=True
    """
    sid = secrets.token_urlsafe(32)
    created_at = now_utc()
    expires = created_at + (timedelta(days=7) if remember else timedelta(days=1))
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (session_id, user_id, created_at, expires_at, data) VALUES (?, ?, ?, ?, ?)",
        (sid, user_id, created_at, expires, None),
    )
    conn.commit()
    conn.close()
    return sid, expires


def get_session(session_id: str):
    if not session_id:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT session_id, user_id, created_at, expires_at FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    expires_at = datetime.fromisoformat(row["expires_at"]) if isinstance(row["expires_at"], str) else row["expires_at"]
    if expires_at < now_utc():
        # expired: delete
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
        return None
    return {"session_id": row["session_id"], "user_id": row["user_id"], "expires_at": expires_at}


def delete_session(session_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()


def invalidate_all_sessions_for_user(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# --------------------
# Simple in-memory cache for redirects (code -> dict)
# This is a tiny demo cache with TTL. In production use Redis/memcache.
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
# CRUD helpers
# --------------------
def find_user_by_username_or_email(identifier: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE LIMIT 1",
        (identifier, identifier),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def find_user_by_email(email: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def load_user_by_id(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_user_record(username: str, email: str, password_hash: str):
    conn = get_db()
    cur = conn.cursor()
    user_uuid = str(uuid.uuid4())
    created_at = now_utc()
    cur.execute(
        "INSERT INTO users (uuid, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_uuid, username, email, password_hash, created_at),
    )
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return load_user_by_id(uid)


def create_email_verification(user_id: int, code_hash: str, token: str, expires_at: datetime, ip_request: str = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO email_verifications (user_id, token, code_hash, expires_at, ip_request) VALUES (?, ?, ?, ?, ?)",
        (user_id, token, code_hash, expires_at, ip_request),
    )
    conn.commit()
    conn.close()


def create_password_reset_entry(user_id: int, token: str, code_hash: str, expires_at: datetime, ip_request: str = None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO password_resets (user_id, token, code_hash, expires_at, ip_request) VALUES (?, ?, ?, ?, ?)",
        (user_id, token, code_hash, expires_at, ip_request),
    )
    conn.commit()
    conn.close()


def find_password_reset_by_token(token: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM password_resets WHERE token = ? LIMIT 1", (token,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_password_reset_used(reset_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (reset_id,))
    conn.commit()
    conn.close()


def update_user_password(user_id: int, new_hash: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))
    conn.commit()
    conn.close()


def db_fetch_url_by_code(code: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM urls WHERE code = ? LIMIT 1", (code,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_urls_for_user(user_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, code, destination, clicks_count, created_at FROM urls WHERE owner_id = ? ORDER BY created_at DESC LIMIT 100",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_url(owner_id, code, destination, is_private):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO urls (owner_id, code, destination, is_private, created_at) VALUES (?, ?, ?, ?, ?)",
        (owner_id, code, destination, 1 if is_private else 0, now_utc()),
    )
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    # Invalidate cache for that code if any
    cache_set_code(code, {"id": uid, "owner_id": owner_id, "code": code, "destination": destination, "is_private": bool(is_private)}, ttl=60)
    return db_fetch_url_by_code(code)


# Short code generation
ALIAS_RE = re.compile(r"^[A-Za-z0-9_\-]{2,64}$")


def generate_random_code(length=6):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_unique_code(custom: str = None):
    conn = get_db()
    cur = conn.cursor()
    if custom:
        if not ALIAS_RE.match(custom):
            conn.close()
            raise ValueError("Invalid custom alias")
        # Ensure uniqueness
        cur.execute("SELECT 1 FROM urls WHERE code = ? LIMIT 1", (custom,))
        if cur.fetchone():
            conn.close()
            raise ValueError("Alias already in use")
        conn.close()
        return custom
    # auto-generate
    attempt = 0
    while True:
        code = generate_random_code(6)
        cur.execute("SELECT 1 FROM urls WHERE code = ? LIMIT 1", (code,))
        if not cur.fetchone():
            conn.close()
            return code
        attempt += 1
        if attempt > 20:
            # fallback to longer
            code = generate_random_code(8)
            conn.close()
            return code


def validate_url(dest: str):
    try:
        parsed = urlparse(dest)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        # Basic safety: reject javascript:, data:, file:, etc.
        return True
    except Exception:
        return False


# --------------------
# Email rendering & send (simulated)
# --------------------
def load_email_template(name: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT subject_template, body_template FROM email_templates WHERE name = ? LIMIT 1", (name,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"subject_template": row["subject_template"], "body_template": row["body_template"]}


def send_email(to: str, subject: str, html: str):
    # Simulated: in production, send via SMTP/SES/SendGrid.
    print("----- Simulated email -----")
    print(f"To: {to}")
    print(f"Subject: {subject}")
    print("Body (HTML):")
    print(html)
    print("----- End email -----")


# --------------------
# Analytics: record click asynchronously
# --------------------
def enqueue_click_event(url_id: int, ip: str, ua: str, referer: str):
    # Fire-and-forget thread to insert into DB
    def worker():
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO clicks (url_id, occurred_at, ip, user_agent, referer) VALUES (?, ?, ?, ?, ?)",
                (url_id, now_utc(), ip, ua, referer),
            )
            # Also increment clicks_count (simple approach)
            cur.execute("UPDATE urls SET clicks_count = clicks_count + 1 WHERE id = ?", (url_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            print("Error recording click:", e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()


# --------------------
# Current user helper
# --------------------
def get_current_user():
    # Read session_id cookie
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if not sid:
        return None
    sess = get_session(sid)
    if not sess:
        return None
    user = load_user_by_id(sess["user_id"])
    return user


# --------------------
# Rate limiting (very small demo)
# --------------------
_RATE_LIMITS = {}  # ip -> [timestamps]


def check_rate_limit(ip: str, window_seconds=60, max_requests=20):
    now = time.time()
    lst = _RATE_LIMITS.setdefault(ip, [])
    # drop old
    cutoff = now - window_seconds
    while lst and lst[0] < cutoff:
        lst.pop(0)
    if len(lst) >= max_requests:
        return False
    lst.append(now)
    return True


# --------------------
# Routes
# --------------------
@app.route("/", methods=["GET"])
def index():
    user = get_current_user()
    return render_with_layout(INDEX_HTML, current_user=user)


@app.route("/shorten-public", methods=["POST"])
def shorten_public():
    url = request.form.get("url", "").strip()
    custom = request.form.get("custom", "").strip()
    if not validate_url(url):
        return render_with_message_and_template(INDEX_HTML, "Invalid URL (must be http/https)", kind="danger", current_user=get_current_user())
    try:
        code = create_unique_code(custom if custom else None)
    except ValueError as e:
        return render_with_message_and_template(INDEX_HTML, str(e), kind="danger", current_user=get_current_user())
    # create anonymous url
    u = create_url(None, code, url, is_private=False)
    base = request.url_root.rstrip("/")
    message_html = f"<p class='success'>Short URL created: <a class='short-url' href='{base}/r/{code}'>{base}/r/{code}</a></p>"
    return render_with_layout(message_html + INDEX_HTML, current_user=get_current_user())


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_with_layout(REGISTER_HTML, current_user=get_current_user())
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if not username or not email or not password:
        return render_with_message_and_template(REGISTER_HTML, "Missing fields", kind="danger", current_user=get_current_user())
    if password != password2:
        return render_with_message_and_template(REGISTER_HTML, "Passwords do not match", kind="danger", current_user=get_current_user())
    # Basic uniqueness checks
    if find_user_by_username_or_email(username):
        return render_with_message_and_template(REGISTER_HTML, "Username or email already exists", kind="danger", current_user=get_current_user())
    if find_user_by_email(email):
        return render_with_message_and_template(REGISTER_HTML, "Email already registered", kind="danger", current_user=get_current_user())
    # Hash password
    pw_hash = generate_password_hash(password)
    user = create_user_record(username=username, email=email, password_hash=pw_hash)
    # Create email verification token & code and send email
    token = secrets.token_urlsafe(32)
    code = secrets.token_hex(3)  # short code (6 hex chars)
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    expires_at = now_utc() + timedelta(hours=24)
    create_email_verification(user["id"], code_hash, token, expires_at, ip_request=request.remote_addr)
    tpl = load_email_template("email_verification")
    if tpl:
        verify_url = request.url_root.rstrip("/") + "/email-verify/" + token
        body = render_template_string(tpl["body_template"], user=user, code=code, verify_url=verify_url, minutes=24 * 60)
        send_email(user["email"], tpl["subject_template"], body)
    return render_with_layout("<p class='success'>Account created. Check your email for verification.</p>", current_user=None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_with_layout(LOGIN_HTML, current_user=get_current_user())
    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remember = True if request.form.get("remember") else False
    if not identifier or not password:
        return render_with_message_and_template(LOGIN_HTML, "Missing credentials", kind="danger", current_user=get_current_user())
    user = find_user_by_username_or_email(identifier)
    if not user or not check_password_hash(user["password_hash"], password):
        # Don't reveal which part failed
        return render_with_message_and_template(LOGIN_HTML, "Invalid credentials", kind="danger", current_user=None)
    # Create server-side session and set cookie
    sid, expires = create_session(user["id"], remember=remember)
    resp = make_response(redirect("/dashboard"))
    # Set cookie flags; set Secure only if request.is_secure to not block in dev HTTP
    secure_flag = request.is_secure
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


@app.route("/logout", methods=["GET"])
def logout():
    sid = request.cookies.get(SESSION_COOKIE_NAME)
    if sid:
        delete_session(sid)
    resp = make_response(redirect("/"))
    resp.set_cookie(SESSION_COOKIE_NAME, "", expires=0, httponly=True, samesite="Strict", path="/")
    return resp


def require_login_redirect():
    return redirect("/login")


@app.route("/dashboard", methods=["GET"])
def dashboard():
    user = get_current_user()
    if not user:
        return require_login_redirect()
    urls = get_urls_for_user(user["id"])
    # format created_at nicely
    for u in urls:
        if isinstance(u["created_at"], str):
            u["created_at"] = u["created_at"]
    base_url = request.url_root.rstrip("/")
    return render_with_layout(DASHBOARD_HTML, current_user=user, urls=urls, base_url=base_url)


@app.route("/dashboard/shorten", methods=["POST"])
def dashboard_shorten():
    user = get_current_user()
    if not user:
        return require_login_redirect()
    destination = request.form.get("url", "").strip()
    custom = request.form.get("custom", "").strip()
    is_private = True if request.form.get("private") else False
    if not validate_url(destination):
        return render_with_message_and_template(DASHBOARD_HTML, "Invalid destination URL", kind="danger", current_user=user, urls=get_urls_for_user(user["id"]), base_url=request.url_root.rstrip("/"))
    try:
        code = create_unique_code(custom if custom else None)
    except ValueError as e:
        return render_with_message_and_template(DASHBOARD_HTML, str(e), kind="danger", current_user=user, urls=get_urls_for_user(user["id"]), base_url=request.url_root.rstrip("/"))
    create_url(owner_id=user["id"], code=code, destination=destination, is_private=is_private)
    return redirect("/dashboard")


@app.route("/dashboard/url/<int:url_id>", methods=["GET"])
def dashboard_manage_url(url_id):
    user = get_current_user()
    if not user:
        return require_login_redirect()
    conn = get_db()
    cur = conn.cursor()
    # Enforce owner filtering
    cur.execute("SELECT * FROM urls WHERE id = ? AND owner_id = ? LIMIT 1", (url_id, user["id"]))
    row = cur.fetchone()
    conn.close()
    if not row:
        return render_with_layout("<h1>Not found</h1><p class='muted'>You don't have access to that resource.</p>", current_user=user), 404
    u = dict(row)
    base_url = request.url_root.rstrip("/")
    content = f"""
    <h1>Manage URL</h1>
    <p>Short: <span class='short-url'>{base_url}/r/{u['code']}</span></p>
    <p>Destination: <a href="{u['destination']}" target="_blank">{u['destination']}</a></p>
    <p>Clicks: {u['clicks_count']}</p>
    <p>Created: {u['created_at']}</p>
    <p class='muted'>Analytics and edit features would appear here (demo).</p>
    """
    return render_with_layout(content, current_user=user)


@app.route("/r/<code>", methods=["GET"])
def redirect_code(code):
    # Resolve code from cache or DB
    obj = cache_get_code(code)
    if not obj:
        obj = db_fetch_url_by_code(code)
        if not obj:
            return render_with_layout("<h1>Not found</h1><p>The short link does not exist.</p>", current_user=get_current_user()), 404
        # cache for short time
        cache_set_code(code, obj, ttl=60)
    # privacy check
    if obj.get("is_private"):
        user = get_current_user()
        if not user or user["id"] != obj.get("owner_id"):
            return render_with_layout("<h1>Private link</h1><p>This link is private.</p>", current_user=get_current_user()), 403
    # enqueue analytics
    enqueue_click_event(obj["id"], ip=request.remote_addr, ua=request.headers.get("User-Agent", ""), referer=request.headers.get("Referer", ""))
    # perform redirect
    return redirect(obj["destination"], code=302)


@app.route("/password-reset-request", methods=["GET", "POST"])
def password_reset_request():
    if request.method == "GET":
        return render_with_layout(PASSWORD_RESET_REQUEST_HTML, current_user=get_current_user())
    email = request.form.get("email", "").strip().lower()
    # Always show neutral response to avoid enumeration
    user = find_user_by_email(email)
    ip = request.remote_addr
    if user:
        # Rate limit per IP / per account (very basic)
        if not check_rate_limit(ip, window_seconds=3600, max_requests=5):
            # don't reveal
            return render_with_layout("<p>If an account with that email exists, a reset link has been sent.</p>", current_user=get_current_user())
        token = secrets.token_urlsafe(32)
        code = "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        expires_at = now_utc() + timedelta(hours=1)
        create_password_reset_entry(user_id=user["id"], token=token, code_hash=code_hash, expires_at=expires_at, ip_request=ip)
        tpl = load_email_template("password_reset")
        if tpl:
            reset_url = request.url_root.rstrip("/") + "/password-reset/" + token
            html_body = render_template_string(tpl["body_template"], user=user, code=code, reset_url=reset_url, minutes=60)
            send_email(user["email"], tpl["subject_template"], html_body)
    return render_with_layout("<p>If an account with that email exists, a reset link has been sent.</p>", current_user=get_current_user())


@app.route("/password-reset/<token>", methods=["GET", "POST"])
def password_reset(token):
    if request.method == "GET":
        return render_with_layout(PASSWORD_RESET_HTML, current_user=get_current_user(), token=token)
    code_submitted = request.form.get("code", "").strip()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if password != password2:
        return render_with_message_and_template(PASSWORD_RESET_HTML, "Passwords do not match", kind="danger", current_user=get_current_user(), token=token)
    row = find_password_reset_by_token(token)
    if not row:
        return render_with_layout("<p>Reset token invalid or expired.</p>", current_user=get_current_user()), 400
    if row.get("used"):
        return render_with_layout("<p>Reset token has already been used.</p>", current_user=get_current_user()), 400
    expires_at = datetime.fromisoformat(row["expires_at"]) if isinstance(row["expires_at"], str) else row["expires_at"]
    if expires_at < now_utc():
        return render_with_layout("<p>Reset token invalid or expired.</p>", current_user=get_current_user()), 400
    # validate code
    if hashlib.sha256(code_submitted.encode()).hexdigest() != row["code_hash"]:
        return render_with_message_and_template(PASSWORD_RESET_HTML, "Invalid code", kind="danger", current_user=get_current_user(), token=token)
    # update password
    new_hash = generate_password_hash(password)
    update_user_password(row["user_id"], new_hash)
    mark_password_reset_used(row["id"])
    # invalidate all sessions for that user
    invalidate_all_sessions_for_user(row["user_id"])
    # send confirmation email (optional)
    user = load_user_by_id(row["user_id"])
    if user:
        send_email(user["email"], "Your password has been changed", f"<p>Hello {user['username']},</p><p>Your password was successfully changed. If this wasn't you, contact support.</p>")
    return render_with_layout("<p class='success'>Password changed. You may now <a href='/login'>login</a>.</p>", current_user=None)


# --------------------
# App start
# --------------------
if __name__ == "__main__":
    # Basic template sanity check: ensure no templates use external 'extends' which would require file templates.
    problematic = []
    for name in list(globals().keys()):
        if name.endswith("_HTML") or name == "LAYOUT":
            val = globals().get(name)
            if isinstance(val, str) and "{% extends" in val:
                problematic.append(name)
    if problematic:
        print("Detected templates using Jinja 'extends' which is unsupported in this single-file app. Rewriting to simple wrapper.")
        for n in problematic:
            globals()[n] = "<div><h1>Template removed</h1><p>Original used extends which is not allowed in single-file apps.</p></div>"
    init_db()
    print("Starting Shortly demo app on http://127.0.0.1:5000")
    app.run(debug=True)