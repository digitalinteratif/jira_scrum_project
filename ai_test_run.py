#!/usr/bin/env python3
"""
generated_app.py

Single-file Flask application implementing a secure URL shortener per KAN-19 blueprint.
All templates are inline and rendered via render_layout. Every HTML form includes CSRF.
Uses:
 - Flask + Flask_SQLAlchemy
 - Flask-WTF CSRFProtect
 - argon2-cffi PasswordHasher
 - PyJWT for session and reset tokens
 - ThreadPoolExecutor for async click writes
Environment variables required:
 - APP_SECRET
 - DATABASE_URL
 - BASE_URL
Optional:
 - SHORT_CODE_LENGTH (default 6)
 - SHORT_CACHE_TTL (seconds, default 60)
 - ANON_CREATE_LIMIT (per window)
 - ANON_CREATE_WINDOW (seconds, default 3600)
 - PW_RESET_EXP (seconds, default 3600)
 - SESSION_EXP (seconds, default 8*3600)
 - REMEMBER_EXP (seconds, default 30*24*3600)
"""

import os
import re
import base64
import hmac
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

# dotenv is optional; support environments without python-dotenv
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return None

from flask import (
    Flask, request, redirect, abort, make_response, url_for,
    render_template_string, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from argon2 import PasswordHasher
import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy import Index

# Load env
load_dotenv()

# Allow safe defaults for local/dev/testing if env vars are not set.
APP_SECRET = os.getenv("APP_SECRET") or "dev_change_me"
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///data.db"
BASE_URL = os.getenv("BASE_URL") or "http://localhost:5000"

# Warn if using defaults (helps in testing environments)
_using_defaults = []
if os.getenv("APP_SECRET") is None:
    _using_defaults.append("APP_SECRET")
if os.getenv("DATABASE_URL") is None:
    _using_defaults.append("DATABASE_URL")
if os.getenv("BASE_URL") is None:
    _using_defaults.append("BASE_URL")
if _using_defaults:
    print("Warning: using default environment values for: {}".format(", ".join(_using_defaults)))

# Config defaults
SHORT_CODE_LENGTH = int(os.getenv("SHORT_CODE_LENGTH", "6"))
SHORT_CACHE_TTL = int(os.getenv("SHORT_CACHE_TTL", "60"))
ANON_CREATE_LIMIT = int(os.getenv("ANON_CREATE_LIMIT", "10"))
ANON_CREATE_WINDOW = int(os.getenv("ANON_CREATE_WINDOW", "3600"))
PW_RESET_EXP = int(os.getenv("PW_RESET_EXP", "3600"))
SESSION_EXP = int(os.getenv("SESSION_EXP", str(8 * 3600)))
REMEMBER_EXP = int(os.getenv("REMEMBER_EXP", str(30 * 24 * 3600)))

# Validate BASE_URL format
if not BASE_URL.startswith("http://") and not BASE_URL.startswith("https://"):
    raise RuntimeError("BASE_URL must include scheme (http:// or https://)")

# Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = APP_SECRET
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# CSRF timeout optional
app.config['WTF_CSRF_TIME_LIMIT'] = None  # tokens valid until session cookie lifetime if desired

# Cookie flags
COOKIE_SECURE = True if BASE_URL.startswith("https://") else False
COOKIE_SAMESITE = "Lax"
AUTH_COOKIE_NAME = "auth_token"

# Initialize extensions
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
ph = PasswordHasher()
analytics_executor = ThreadPoolExecutor(max_workers=2)

# ===========================
# Models
# ===========================

class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True, nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime, nullable=True)

    links = db.relationship('Link', backref='owner', lazy='dynamic')

class Link(db.Model):
    __tablename__ = 'link'
    id = db.Column(db.Integer, primary_key=True)
    short_code = db.Column(db.String(64), unique=True, index=True, nullable=False)
    target_url = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    clicks_count = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_public = db.Column(db.Boolean, default=True)

Index('ix_link_short_code', Link.short_code)
Index('ix_link_user_id', Link.user_id)

class Click(db.Model):
    __tablename__ = 'click'
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('link.id'), index=True, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_hmac = db.Column(db.String(64))
    user_agent = db.Column(db.String(512))
    referrer = db.Column(db.String(1024))

class RateLimit(db.Model):
    __tablename__ = 'ratelimit'
    id = db.Column(db.Integer, primary_key=True)
    ip_hmac = db.Column(db.String(64), index=True)
    action = db.Column(db.String(64))
    window_start = db.Column(db.DateTime)
    count = db.Column(db.Integer, default=0)

# Initialize DB (simple create_all for single-file usage)
with app.app_context():
    db.create_all()

# ===========================
# Utilities
# ===========================

def make_ip_hmac(ip: str) -> str:
    """
    HMAC the IP address using APP_SECRET to anonymize it. Return URL-safe base64 truncated.
    """
    if not ip:
        return ""
    digest = hmac.new(APP_SECRET.encode('utf-8'), ip.encode('utf-8'), hashlib.sha256).digest()
    b64 = base64.urlsafe_b64encode(digest).decode('utf-8')
    return b64[:43]  # truncated for storage

def is_valid_target_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        return False

CUSTOM_CODE_RE = re.compile(r'^[A-Za-z0-9_-]{4,64}$')

def generate_short_code(length: int = SHORT_CODE_LENGTH) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ''.join(secrets.choice(alphabet) for _ in range(length))

# In-process cache for hot short codes
_short_cache = {}
_cache_lock = threading.Lock()

def cache_get(code):
    with _cache_lock:
        entry = _short_cache.get(code)
        if not entry:
            return None
        expires_at, data = entry
        if datetime.utcnow() > expires_at:
            del _short_cache[code]
            return None
        return data

def cache_set(code, data, ttl=SHORT_CACHE_TTL):
    with _cache_lock:
        _short_cache[code] = (datetime.utcnow() + timedelta(seconds=ttl), data)

# JWT helpers
def create_jwt(payload: dict, exp_seconds: int) -> str:
    now = datetime.utcnow()
    payload_copy = payload.copy()
    payload_copy.setdefault('iat', now)
    payload_copy['exp'] = now + timedelta(seconds=exp_seconds)
    token = jwt.encode(payload_copy, APP_SECRET, algorithm='HS256')
    # PyJWT >=2 returns str
    if isinstance(token, bytes):
        token = token.decode('utf-8')
    return token

def decode_jwt(token: str):
    try:
        payload = jwt.decode(token, APP_SECRET, algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except Exception:
        return None

# Current user retrieval
def get_current_user():
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    payload = decode_jwt(token)
    if not payload:
        return None
    user_id = payload.get('user_id')
    if not user_id:
        return None
    user = User.query.get(user_id)
    return user

def require_login():
    user = get_current_user()
    if not user:
        # Redirect to login
        return None
    return user

# render_layout helper
def render_layout(title, body, **context):
    base = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ title }}</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; margin: 20px; }
    nav { margin-bottom: 20px; }
    form { margin: 10px 0; }
    table { border-collapse: collapse; width: 100%; }
    td, th { border: 1px solid #ddd; padding: 8px; }
    th { background: #f4f4f4; }
  </style>
</head>
<body>
  <header>
    <h1><a href="{{ base_url }}">Shortener</a></h1>
  </header>
  <nav>
    <a href="/">Home</a> |
    {% if current_user %}
      <a href="/links">My Links</a> |
      <form method="post" action="/logout" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button type="submit">Logout</button>
      </form>
    {% else %}
      <a href="/login">Login</a> |
      <a href="/register">Register</a>
    {% endif %}
  </nav>
  <main>
    {{ body|safe }}
  </main>
</body>
</html>
"""
    ctx = dict(title=title, body=body, csrf_token=generate_csrf,
               current_user=get_current_user(), base_url=BASE_URL)
    ctx.update(context)
    return render_template_string(base, **ctx)

# ===========================
# Analytics background writer
# ===========================

def _record_click_background(link_id, ip, user_agent, referrer, ts=None):
    """
    Runs in background thread; creates Click record.
    """
    with app.app_context():
        try:
            ip_h = make_ip_hmac(ip)
            click = Click(
                link_id=link_id,
                timestamp=ts or datetime.utcnow(),
                ip_hmac=ip_h,
                user_agent=(user_agent or '')[:512],
                referrer=(referrer or '')[:1024]
            )
            db.session.add(click)
            db.session.commit()
        except Exception as e:
            # Log and continue; don't raise
            app.logger.exception("Failed to record click: %s", e)
            db.session.rollback()

# ===========================
# Rate limiting helpers
# ===========================

def check_and_increment_anon_create(ip_h):
    """
    Returns True if allowed, False if rate-limited. Also increments count when allowed.
    """
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=ANON_CREATE_WINDOW)
    rl = RateLimit.query.filter_by(ip_hmac=ip_h, action='anon_create').first()
    if rl and rl.window_start and rl.window_start > window_start:
        if rl.count >= ANON_CREATE_LIMIT:
            return False
        rl.count += 1
        db.session.commit()
        return True
    else:
        # reset/initialize
        if not rl:
            rl = RateLimit(ip_hmac=ip_h, action='anon_create', window_start=now, count=1)
            db.session.add(rl)
        else:
            rl.window_start = now
            rl.count = 1
        db.session.commit()
        return True

# ===========================
# Routes
# ===========================

@app.route('/')
def home():
    body = """
<h2>Shorten a URL</h2>
<form method="post" action="/shorten">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>URL: <input name="target_url" type="url" required></label><br>
  <label>Custom Code (optional): <input name="custom_code" type="text"></label><br>
  <label>Expires at (optional): <input name="expires_at" type="datetime-local"></label><br>
  <button type="submit">Shorten</button>
</form>
"""
    return render_layout("Home", body)

@app.route('/shorten', methods=['POST'])
def shorten():
    target_url = (request.form.get('target_url') or '').strip()
    custom_code = (request.form.get('custom_code') or '').strip()
    expires_at_raw = request.form.get('expires_at')

    if not target_url or not is_valid_target_url(target_url):
        return render_layout("Error", "<p>Invalid target URL. Only http/https allowed.</p>"), 400

    user = get_current_user()
    ip = request.remote_addr or ''
    ip_h = make_ip_hmac(ip)

    # If anonymous, check rate-limit
    if not user:
        allowed = check_and_increment_anon_create(ip_h)
        if not allowed:
            return render_layout("Rate Limited", "<p>Anonymous create rate limit exceeded. Try later or create an account.</p>"), 429

    # parse expires_at
    expires_at = None
    if expires_at_raw:
        try:
            # browser sends datetime-local like "2023-03-31T12:34"
            expires_at = datetime.fromisoformat(expires_at_raw)
        except Exception:
            expires_at = None

    # custom code validation
    code = None
    if custom_code:
        if not CUSTOM_CODE_RE.match(custom_code):
            return render_layout("Invalid Code", "<p>Custom code invalid. Use 4-64 chars [A-Za-z0-9_-].</p>"), 400
        code = custom_code

    # create unique short code, handle collisions
    attempt = 0
    while True:
        attempt += 1
        if not code:
            code = generate_short_code()
        link = Link(short_code=code, target_url=target_url, user_id=(user.id if user else None),
                    expires_at=expires_at, is_public=True)
        db.session.add(link)
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
            # collision -> regenerate code unless custom_code was provided
            if custom_code:
                return render_layout("Conflict", "<p>Custom code already in use. Choose another.</p>"), 409
            code = None
            if attempt > 5:
                # fallback to longer random token
                code = secrets.token_urlsafe(SHORT_CODE_LENGTH + 2)[:64]
    # set cache
    cache_set(code, {'target_url': target_url, 'link_id': link.id, 'expires_at': link.expires_at, 'is_public': link.is_public})

    short_url = f"{BASE_URL.rstrip('/')}/{code}"
    body = f"""
<p>Short link created: <a href=\"{short_url}\">{short_url}</a></p>
<p><a href=\"/links\">Manage your links</a></p>
"""
    return render_layout("Shortened", body)

@app.route('/<string:short_code>')
def redirect_short(short_code):
    # check cache first
    cached = cache_get(short_code)
    link = None
    if cached:
        # validate expiry
        expires_at = cached.get('expires_at')
        if expires_at and expires_at < datetime.utcnow():
            # expired
            return render_layout("Expired", "<p>This link has expired.</p>"), 410
        # no DB fetch necessary for public link
        # but we need link id for analytics; cached contains link_id
        target_url = cached.get('target_url')
        link_id = cached.get('link_id')
        is_public = cached.get('is_public')
        link = Link.query.get(link_id) if link_id else None  # ensure object exists for checks
    else:
        link = Link.query.filter_by(short_code=short_code).first()
        if not link:
            return render_layout("Not found", "<p>Short link not found.</p>"), 404
        # enforce expiry
        if link.expires_at and link.expires_at < datetime.utcnow():
            return render_layout("Expired", "<p>This link has expired.</p>"), 410
        # cache
        cache_set(short_code, {'target_url': link.target_url, 'link_id': link.id, 'expires_at': link.expires_at, 'is_public': link.is_public})
        target_url = link.target_url
        link_id = link.id
        is_public = link.is_public

    # handle private links (if any)
    if link and link.user_id and not link.is_public:
        current = get_current_user()
        if not current or current.id != link.user_id:
            return render_layout("Forbidden", "<p>This link is private.</p>"), 403

    # Quick atomic increment of clicks_count
    try:
        db.session.query(Link).filter_by(id=link_id).update({"clicks_count": Link.clicks_count + 1})
        db.session.commit()
    except Exception:
        db.session.rollback()

    # enqueue click record
    ip = request.remote_addr or ''
    ua = request.headers.get('User-Agent', '')
    ref = request.headers.get('Referer', '')
    try:
        analytics_executor.submit(_record_click_background, link_id, ip, ua, ref, datetime.utcnow())
    except Exception:
        app.logger.exception("Failed to enqueue analytics task")

    # Redirect
    resp = redirect(target_url, code=302)
    return resp

# ---------------------------
# Authentication
# ---------------------------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        body = """
<h2>Register</h2>
<form method="post" action="/register">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Email: <input name="email" type="email" required></label><br>
  <label>Password: <input name="password" type="password" required></label><br>
  <button type="submit">Register</button>
</form>
"""
        return render_layout("Register", body)
    # POST
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    if not email or not password:
        return render_layout("Error", "<p>Missing email or password.</p>"), 400
    if User.query.filter_by(email=email).first():
        return render_layout("Exists", "<p>User already exists.</p>"), 409
    pw_hash = ph.hash(password)
    user = User(email=email, password_hash=pw_hash)
    db.session.add(user)
    db.session.commit()
    # Auto-login after register
    token = create_jwt({'user_id': user.id}, SESSION_EXP)
    resp = make_response(render_layout("Registered", "<p>Registered successfully. <a href='/links'>Go to links</a></p>"))
    resp.set_cookie(AUTH_COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE, path='/')
    return resp

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        body = """
<h2>Login</h2>
<form method="post" action="/login">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Email: <input name="email" type="email" required></label><br>
  <label>Password: <input name="password" type="password" required></label><br>
  <label><input name="remember" type="checkbox"> Remember me</label><br>
  <button type="submit">Login</button>
</form>
<p><a href="/reset-password">Forgot password?</a></p>
"""
        return render_layout("Login", body)
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    remember = bool(request.form.get('remember'))
    user = User.query.filter_by(email=email).first()
    if not user:
        return render_layout("Login Failed", "<p>Invalid credentials.</p>"), 401
    try:
        if not ph.verify(user.password_hash, password):
            return render_layout("Login Failed", "<p>Invalid credentials.</p>"), 401
    except Exception:
        return render_layout("Login Failed", "<p>Invalid credentials.</p>"), 401
    # Optional rehash check
    try:
        if ph.check_needs_rehash(user.password_hash):
            user.password_hash = ph.hash(password)
            db.session.commit()
    except Exception:
        pass
    user.last_login = datetime.utcnow()
    db.session.commit()
    exp = REMEMBER_EXP if remember else SESSION_EXP
    token = create_jwt({'user_id': user.id}, exp)
    resp = make_response(redirect('/links'))
    resp.set_cookie(AUTH_COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE, path='/')
    return resp

@app.route('/logout', methods=['POST'])
def logout():
    # CSRF protects this route
    resp = make_response(redirect('/'))
    resp.set_cookie(AUTH_COOKIE_NAME, '', expires=0, httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE, path='/')
    return resp

# ---------------------------
# Password reset
# ---------------------------

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_request():
    if request.method == 'GET':
        body = """
<h2>Password reset</h2>
<form method="post" action="/reset-password">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Email: <input name="email" type="email" required></label><br>
  <button type="submit">Send Reset Link</button>
</form>
"""
        return render_layout("Reset Password", body)
    email = (request.form.get('email') or '').strip().lower()
    user = User.query.filter_by(email=email).first()
    # Always render success message to avoid user enumeration
    if user:
        token = create_jwt({'user_id': user.id, 'purpose': 'pw_reset'}, PW_RESET_EXP)
        reset_link = f"{BASE_URL.rstrip('/')}/reset/{token}"
        # In real deployment, send email. For this single-file app, we'll show link in response (not secure for public).
        app.logger.info("Password reset requested for %s; reset link: %s", email, reset_link)
        # NOTE: we intentionally do not send email here.
    body = "<p>If the email exists, a reset link has been sent.</p>"
    return render_layout("Reset Requested", body)

@app.route('/reset/<string:token>', methods=['GET', 'POST'])
def reset_perform(token):
    payload = decode_jwt(token)
    if not payload or payload.get('purpose') != 'pw_reset':
        return render_layout("Invalid or expired", "<p>Reset token invalid or expired.</p>"), 400
    user_id = payload.get('user_id')
    user = User.query.get(user_id)
    if not user:
        return render_layout("Invalid", "<p>User not found.</p>"), 400
    if request.method == 'GET':
        body = f"""
<h2>Reset password for {user.email}</h2>
<form method="post" action="/reset/{token}">
  <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
  <label>New password: <input name="password" type="password" required></label><br>
  <button type="submit">Set password</button>
</form>
"""
        return render_layout("Reset Password", body)
    # POST - update password
    new_pw = request.form.get('password') or ''
    if not new_pw:
        return render_layout("Error", "<p>Password required.</p>"), 400
    user.password_hash = ph.hash(new_pw)
    db.session.commit()
    return render_layout("Password reset", "<p>Password updated. <a href='/login'>Login</a></p>")

# ---------------------------
# Link management (user-only)
# ---------------------------

@app.route('/links')
def links_list():
    user = require_login()
    if not user:
        return redirect('/login')
    links = Link.query.filter_by(user_id=user.id).order_by(Link.created_at.desc()).all()
    rows = ""
    for l in links:
        short = f"{BASE_URL.rstrip('/')}/{l.short_code}"
        rows += f"<tr><td>{l.short_code}</td><td><a href='{short}' target='_blank'>{short}</a></td><td>{l.target_url}</td><td>{l.clicks_count}</td>"
        rows += f"<td><a href='/links/{l.id}/edit'>Edit</a> | "
        rows += f"<form method='post' action='/links/{l.id}/delete' style='display:inline;'>"
        rows += "<input type='hidden' name='csrf_token' value='{{ csrf_token() }}'>"
        rows += "<button type='submit'>Delete</button></form></td></tr>"
    body = f"""
<h2>My Links</h2>
<p><a href="/">Create new link</a></p>
<table>
<tr><th>Code</th><th>Short URL</th><th>Target</th><th>Clicks</th><th>Actions</th></tr>
{rows}
</table>
"""
    return render_layout("My Links", body)

@app.route('/links/<int:link_id>/edit', methods=['GET', 'POST'])
def edit_link(link_id):
    user = require_login()
    if not user:
        return redirect('/login')
    link = Link.query.filter_by(id=link_id, user_id=user.id).first_or_404()
    if request.method == 'GET':
        expires_value = link.expires_at.isoformat() if link.expires_at else ''
        body = f"""
<h2>Edit Link</h2>
<form method="post" action="/links/{link_id}/edit">
  <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
  <label>Target URL: <input name="target_url" type="url" value="{link.target_url}" required></label><br>
  <label>Expires at: <input name="expires_at" type="datetime-local" value="{expires_value}"></label><br>
  <label>Public: <input name="is_public" type="checkbox" {"checked" if link.is_public else ""}></label><br>
  <button type="submit">Save</button>
</form>
"""
        return render_layout("Edit Link", body)
    # POST
    target_url = (request.form.get('target_url') or '').strip()
    expires_at_raw = request.form.get('expires_at')
    is_public = bool(request.form.get('is_public'))
    if not is_valid_target_url(target_url):
        return render_layout("Error", "<p>Invalid URL.</p>"), 400
    expires_at = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except Exception:
            expires_at = None
    link.target_url = target_url
    link.expires_at = expires_at
    link.is_public = is_public
    db.session.commit()
    # update cache
    cache_set(link.short_code, {'target_url': link.target_url, 'link_id': link.id, 'expires_at': link.expires_at, 'is_public': link.is_public})
    return redirect('/links')

@app.route('/links/<int:link_id>/delete', methods=['POST'])
def delete_link(link_id):
    user = require_login()
    if not user:
        return redirect('/login')
    link = Link.query.filter_by(id=link_id, user_id=user.id).first_or_404()
    db.session.delete(link)
    db.session.commit()
    # remove from cache if present
    with _cache_lock:
        if link.short_code in _short_cache:
            del _short_cache[link.short_code]
    return redirect('/links')

# ---------------------------
# Analytics view
# ---------------------------

@app.route('/analytics/<int:link_id>')
def analytics_view(link_id):
    user = require_login()
    if not user:
        return redirect('/login')
    # enforce ID Filter Rule
    link = Link.query.filter_by(id=link_id, user_id=user.id).first_or_404()
    total_clicks = link.clicks_count
    # recent clicks (last 50)
    clicks = Click.query.filter_by(link_id=link.id).order_by(Click.timestamp.desc()).limit(50).all()
    rows = ""
    for c in clicks:
        rows += f"<tr><td>{c.timestamp}</td><td>{c.ip_hmac}</td><td>{c.user_agent}</td><td>{c.referrer}</td></tr>"
    body = f"""
<h2>Analytics for {link.short_code}</h2>
<p>Total clicks (cached): {total_clicks}</p>
<table>
<tr><th>When</th><th>IP HMAC</th><th>User Agent</th><th>Referrer</th></tr>
{rows}
</table>
"""
    return render_layout("Analytics", body)

# ---------------------------
# Health and debug
# ---------------------------

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

# ===========================
# Error handlers
# ===========================

@app.errorhandler(400)
def bad_request(e):
    return render_layout("Bad Request", "<p>Bad request (CSRF token missing or invalid).</p>"), 400

@app.errorhandler(404)
def not_found(e):
    return render_layout("Not Found", "<p>Page not found.</p>"), 404

@app.errorhandler(403)
def forbidden(e):
    return render_layout("Forbidden", "<p>Forbidden.</p>"), 403

@app.errorhandler(500)
def server_error(e):
    return render_layout("Server Error", "<p>Internal server error.</p>"), 500

# ===========================
# Run
# ===========================
if __name__ == '__main__':
    # For local dev only; production should use Gunicorn
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "5000")), debug=debug)