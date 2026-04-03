#!/usr/bin/env python3
"""
High-Performance URL Shortening Web Service - Surgical Flask Blueprint

Single-file Flask application using SQLAlchemy + SQLite. Provides:
- Secure user login & registration (password hashing)
- Dashboard UI (rendered via render_template_string) to create/manage short links
- API (token-based via X-API-Key) for programmatic shortening and stats
- Blazing-fast redirect engine with in-memory caching + lightweight rate limiting
- Basic analytics tracking (clicks with timestamp, IP, UA, referrer)
- Basic CSRF protection for forms
- Security-conscious defaults (input validation, prepared updates, session protections)

Usage:
    python app.py

Notes:
- This file intentionally uses only Python, Flask, and SQLAlchemy (SQLite) per requirements.
- No JavaScript or Node is used.
"""

import os
import time
import secrets
import string
from collections import deque, defaultdict
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, request, redirect, url_for, session, abort, jsonify,
    make_response, flash, get_flashed_messages, render_template_string
)
from werkzeug.security import generate_password_hash, check_password_hash

# SQLAlchemy imports
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker

# -----------------------
# Configuration
# -----------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "shortener.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

APP_SECRET_KEY = os.environ.get("SHORTENER_SECRET") or secrets.token_urlsafe(32)
DEFAULT_DOMAIN = os.environ.get("SHORTENER_DOMAIN") or "http://localhost:5000"
SHORT_CODE_LENGTH = 6  # default length for generated short codes
CACHE_TTL_SECONDS = 60  # TTL for in-memory redirect cache
REDIRECT_RATE_LIMIT_PER_MIN = int(os.environ.get("REDIRECT_RPM", "1000"))  # per IP
API_RATE_LIMIT_PER_MIN = int(os.environ.get("API_RPM", "60"))

# -----------------------
# App & DB setup
# -----------------------
app = Flask(__name__)
app.secret_key = APP_SECRET_KEY
# Security-relevant session settings
app.config.update({
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
})

# SQLAlchemy engine & session
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


# -----------------------
# Models
# -----------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    short_urls = relationship("ShortURL", back_populates="owner")

    def verify_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class ShortURL(Base):
    __tablename__ = "shorturls"
    id = Column(Integer, primary_key=True)
    code = Column(String(128), unique=True, nullable=False, index=True)
    target_url = Column(Text, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    visits = Column(Integer, default=0, nullable=False)

    owner = relationship("User", back_populates="short_urls")
    clicks = relationship("Click", back_populates="shorturl", cascade="all, delete-orphan")


class Click(Base):
    __tablename__ = "clicks"
    id = Column(Integer, primary_key=True)
    short_id = Column(Integer, ForeignKey("shorturls.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    ip = Column(String(45))
    user_agent = Column(Text)
    referrer = Column(Text)

    shorturl = relationship("ShortURL", back_populates="clicks")


# Create DB tables
Base.metadata.create_all(bind=engine)


# -----------------------
# Utility helpers
# -----------------------
def db_session():
    """Provides a SQLAlchemy session (scoped). Remember to close after usage if you take it manually."""
    return SessionLocal()


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        return True
    except Exception:
        return False


def generate_code(length=SHORT_CODE_LENGTH) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_current_user():
    if "user_id" not in session:
        return None
    db = db_session()
    try:
        return db.query(User).filter_by(id=session["user_id"]).first()
    finally:
        db.close()


def require_api_key(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if not key:
            return jsonify({"error": "API key required"}), 401
        db = db_session()
        try:
            user = db.query(User).filter_by(api_key=key).first()
            if not user:
                return jsonify({"error": "Invalid API key"}), 403
            request.api_user = user  # attach to request context for endpoint use
            return view(*args, **kwargs)
        finally:
            db.close()
    return wrapped


# Simple in-memory rate limiter per key/ip
class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.access = defaultdict(lambda: deque())

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - 60
        dq = self.access[key]
        # pop outdated timestamps
        while dq and dq[0] < window_start:
            dq.popleft()
        if len(dq) >= self.per_minute:
            return False
        dq.append(now)
        return True


redirect_rate_limiter = RateLimiter(REDIRECT_RATE_LIMIT_PER_MIN)
api_rate_limiter = RateLimiter(API_RATE_LIMIT_PER_MIN)


def rate_limit_for_ip(limit_obj):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            if not limit_obj.is_allowed(ip):
                return make_response("Too Many Requests", 429)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Fast in-memory cache for redirects: code -> (target_url, short_id, expiry_timestamp)
redirect_cache = {}
cache_lock = None  # For single-process demo not strictly necessary


def cache_get(code):
    ent = redirect_cache.get(code)
    if not ent:
        return None
    target, sid, expiry = ent
    if expiry < time.time():
        del redirect_cache[code]
        return None
    return (target, sid)


def cache_set(code, target, sid):
    redirect_cache[code] = (target, sid, time.time() + CACHE_TTL_SECONDS)


# CSRF token helpers
def generate_csrf_token():
    token = secrets.token_urlsafe(16)
    session['_csrf_token'] = token
    return token


def validate_csrf(token):
    saved = session.pop('_csrf_token', None)
    return bool(saved and token and secrets.compare_digest(saved, token))


# -----------------------
# HTML Templates (render_template_string)
# -----------------------
BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{ title or "URL Shortener" }}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; max-width:900px; margin:20px auto; color:#121212; }
    header { margin-bottom: 1rem; }
    .card { border: 1px solid #e3e3e3; padding: 1rem; border-radius:6px; background:#fff; }
    input[type=text], input[type=password], textarea { width:100%; padding:8px; margin:6px 0 12px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box; }
    button { padding:8px 14px; border-radius:4px; border:0; background:#007bff; color:#fff; cursor:pointer; }
    table { width:100%; border-collapse:collapse; margin-top:12px; }
    th, td { text-align:left; padding:8px; border-bottom:1px solid #f1f1f1; }
    .muted { color:#666; font-size:.9rem; }
    nav a { margin-right:12px; }
    .flash { padding:8px; background:#fff8d5; border:1px solid #f0e6a8; border-radius:4px; margin-bottom:12px; }
  </style>
</head>
<body>
  <header>
    <h1>{{ title or "URL Shortener" }}</h1>
    <nav>
      {% if current_user %}
        <span class="muted">Logged in as {{ current_user.username }}</span>
        <a href="{{ url_for('dashboard') }}">Dashboard</a>
        <a href="{{ url_for('logout') }}">Logout</a>
      {% else %}
        <a href="{{ url_for('login') }}">Login</a>
        <a href="{{ url_for('register') }}">Register</a>
      {% endif %}
      <a href="{{ url_for('index') }}">Home</a>
    </nav>
  </header>

  {% for msg in get_flashed_messages() %}
    <div class="flash">{{ msg }}</div>
  {% endfor %}

  <main>
    {{ content }}
  </main>

  <footer style="margin-top:2rem; color:#666; font-size:.9rem;">
    <div>Powered by a surgical Flask + SQLAlchemy blueprint. No JavaScript backend.</div>
  </footer>
</body>
</html>
"""

INDEX_CONTENT = """
<div class="card">
  <h2>Shorten a URL</h2>
  <form method="post" action="{{ url_for('create_public') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Target URL</label>
    <input type="text" name="target_url" placeholder="https://example.com/path" required>
    <label>Custom code (optional)</label>
    <input type="text" name="custom_code" placeholder="customCode">
    <button type="submit">Shorten</button>
  </form>
  <p class="muted">You may register to manage links and access API keys for programmatic creation & stats.</p>
</div>

{% if created_short %}
<div class="card" style="margin-top:12px;">
  <h3>Short link created</h3>
  <p><a href="{{ short_url }}" target="_blank">{{ short_url }}</a></p>
  <p class="muted">Click to test redirect. Analytics will be recorded.</p>
</div>
{% endif %}
"""

LOGIN_CONTENT = """
<div class="card">
  <h2>Login</h2>
  <form method="post" action="{{ url_for('login') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Username</label>
    <input type="text" name="username" required>
    <label>Password</label>
    <input type="password" name="password" required>
    <button type="submit">Login</button>
  </form>
</div>
"""

REGISTER_CONTENT = """
<div class="card">
  <h2>Register</h2>
  <form method="post" action="{{ url_for('register') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Username</label>
    <input type="text" name="username" required>
    <label>Password</label>
    <input type="password" name="password" required>
    <button type="submit">Register</button>
  </form>
</div>
"""

DASHBOARD_CONTENT = """
<div class="card">
  <h2>Create Short Link</h2>
  <form method="post" action="{{ url_for('create') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label>Target URL</label>
    <input type="text" name="target_url" placeholder="https://example.com/path" required>
    <label>Custom code (optional)</label>
    <input type="text" name="custom_code" placeholder="customCode">
    <button type="submit">Create</button>
  </form>
  <p class="muted">Your API key: <strong>{{ current_user.api_key }}</strong></p>
</div>

<div class="card" style="margin-top:12px;">
  <h2>Your Links</h2>
  {% if urls %}
    <table>
      <thead><tr><th>Short</th><th>Target</th><th>Visits</th><th>Created</th><th>Actions</th></tr></thead>
      <tbody>
      {% for u in urls %}
        <tr>
          <td><a href="{{ domain }}/r/{{ u.code }}" target="_blank">{{ domain }}/r/{{ u.code }}</a></td>
          <td style="max-width:420px; overflow-wrap:anywhere;">{{ u.target_url }}</td>
          <td>{{ u.visits }}</td>
          <td>{{ u.created_at.strftime('%Y-%m-%d') }}</td>
          <td><a href="{{ url_for('stats_ui', code=u.code) }}">Stats</a></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">You haven't created any links yet.</p>
  {% endif %}
</div>
"""

STATS_CONTENT = """
<div class="card">
  <h2>Stats for {{ domain }}/r/{{ code }}</h2>
  <p>Target: <a href="{{ target }}" target="_blank">{{ target }}</a></p>
  <p>Visits: {{ visits }}</p>
  <h3>Recent Clicks</h3>
  {% if clicks %}
    <table>
      <thead><tr><th>Time</th><th>IP</th><th>User Agent</th><th>Referrer</th></tr></thead>
      <tbody>
      {% for c in clicks %}
        <tr>
          <td>{{ c.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
          <td>{{ c.ip }}</td>
          <td style="max-width:420px; overflow-wrap:anywhere;">{{ c.user_agent }}</td>
          <td style="max-width:240px; overflow-wrap:anywhere;">{{ c.referrer }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="muted">No clicks yet.</p>
  {% endif %}
</div>
"""


# -----------------------
# Routes - UI
# -----------------------
@app.context_processor
def inject_globals():
    return {
        "current_user": get_current_user(),
        "domain": DEFAULT_DOMAIN,
        "get_flashed_messages": get_flashed_messages,
    }


@app.route("/", methods=["GET"])
def index():
    created_short = None
    short_url = None
    # If there was a previous creation result stored in session, display it once
    if session.pop("_created_short", None):
        created_short = True
        short_url = session.pop("_created_short_url", None)
    content = render_template_string(INDEX_CONTENT, created_short=created_short, short_url=short_url, csrf_token=generate_csrf_token())
    return render_template_string(BASE_HTML, title="Home", content=content)


@app.route("/create_public", methods=["POST"])
def create_public():
    # Allow public shortening (rate-limited)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    if not api_rate_limiter.is_allowed(ip):  # reuse API limiter to avoid abuse
        flash("Too many requests - try again later.")
        return redirect(url_for("index"))

    csrf_token = request.form.get("csrf_token")
    if not validate_csrf(csrf_token):
        flash("Invalid CSRF token.")
        return redirect(url_for("index"))

    target_url = (request.form.get("target_url") or "").strip()
    custom_code = (request.form.get("custom_code") or "").strip()

    if not is_valid_url(target_url):
        flash("Invalid URL. Use http:// or https://")
        return redirect(url_for("index"))

    db = db_session()
    try:
        if custom_code:
            code = custom_code
            # enforce alphanumeric and dash/underscore only for safety
            if not all(c.isalnum() or c in "-_" for c in code):
                flash("Custom code may only contain letters, numbers, - and _")
                return redirect(url_for("index"))
            # check uniqueness
            exists = db.query(ShortURL).filter_by(code=code).first()
            if exists:
                flash("Custom code is already in use. Choose another.")
                return redirect(url_for("index"))
        else:
            # generate unique code (with retries)
            for _ in range(6):
                code = generate_code()
                if not db.query(ShortURL).filter_by(code=code).first():
                    break
            else:
                flash("Could not generate a unique short code. Try again.")
                return redirect(url_for("index"))

        su = ShortURL(code=code, target_url=target_url, owner_id=None)
        db.add(su)
        db.commit()
        session["_created_short"] = True
        session["_created_short_url"] = f"{DEFAULT_DOMAIN}/r/{code}"
        flash("Short URL created.")
        return redirect(url_for("index"))
    except Exception as e:
        db.rollback()
        app.logger.exception("Error creating public short URL")
        flash("Error creating short URL.")
        return redirect(url_for("index"))
    finally:
        db.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        content = render_template_string(LOGIN_CONTENT, csrf_token=generate_csrf_token())
        return render_template_string(BASE_HTML, title="Login", content=content)

    csrf_token = request.form.get("csrf_token")
    if not validate_csrf(csrf_token):
        flash("Invalid CSRF token.")
        return redirect(url_for("login"))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    db = db_session()
    try:
        user = db.query(User).filter_by(username=username).first()
        if user and user.verify_password(password):
            session["user_id"] = user.id
            flash("Logged in.")
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        else:
            flash("Invalid credentials.")
            return redirect(url_for("login"))
    finally:
        db.close()


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        content = render_template_string(REGISTER_CONTENT, csrf_token=generate_csrf_token())
        return render_template_string(BASE_HTML, title="Register", content=content)

    csrf_token = request.form.get("csrf_token")
    if not validate_csrf(csrf_token):
        flash("Invalid CSRF token.")
        return redirect(url_for("register"))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("register"))

    db = db_session()
    try:
        existing = db.query(User).filter_by(username=username).first()
        if existing:
            flash("Username already taken.")
            return redirect(url_for("register"))
        api_key = secrets.token_hex(32)
        user = User(username=username, password_hash=generate_password_hash(password), api_key=api_key)
        db.add(user)
        db.commit()
        flash("Registered. Please log in.")
        return redirect(url_for("login"))
    except Exception:
        db.rollback()
        app.logger.exception("Registration error")
        flash("Error during registration.")
        return redirect(url_for("register"))
    finally:
        db.close()


@app.route("/dashboard", methods=["GET"])
@require_login
def dashboard():
    user = get_current_user()
    db = db_session()
    try:
        urls = db.query(ShortURL).filter_by(owner_id=user.id).order_by(ShortURL.created_at.desc()).all()
        content = render_template_string(DASHBOARD_CONTENT, urls=urls, csrf_token=generate_csrf_token())
        return render_template_string(BASE_HTML, title="Dashboard", content=content)
    finally:
        db.close()


@app.route("/create", methods=["POST"])
@require_login
def create():
    csrf_token = request.form.get("csrf_token")
    if not validate_csrf(csrf_token):
        flash("Invalid CSRF token.")
        return redirect(url_for("dashboard"))

    target_url = (request.form.get("target_url") or "").strip()
    custom_code = (request.form.get("custom_code") or "").strip()

    if not is_valid_url(target_url):
        flash("Invalid URL.")
        return redirect(url_for("dashboard"))

    user = get_current_user()
    db = db_session()
    try:
        if custom_code:
            code = custom_code
            if not all(c.isalnum() or c in "-_" for c in code):
                flash("Custom code may only contain letters, numbers, - and _")
                return redirect(url_for("dashboard"))
            if db.query(ShortURL).filter_by(code=code).first():
                flash("Custom code is already in use.")
                return redirect(url_for("dashboard"))
        else:
            for _ in range(6):
                code = generate_code()
                if not db.query(ShortURL).filter_by(code=code).first():
                    break
            else:
                flash("Failed to generate code.")
                return redirect(url_for("dashboard"))

        su = ShortURL(code=code, target_url=target_url, owner_id=user.id)
        db.add(su)
        db.commit()
        flash("Short URL created.")
        return redirect(url_for("dashboard"))
    except Exception:
        db.rollback()
        app.logger.exception("Error creating short URL")
        flash("Error creating short URL.")
        return redirect(url_for("dashboard"))
    finally:
        db.close()


@app.route("/stats/<code>")
@require_login
def stats_ui(code):
    user = get_current_user()
    db = db_session()
    try:
        su = db.query(ShortURL).filter_by(code=code).first()
        if not su:
            flash("Short URL not found.")
            return redirect(url_for("dashboard"))
        if su.owner_id != user.id and not user.is_admin:
            flash("Access denied to stats.")
            return redirect(url_for("dashboard"))
        clicks = db.query(Click).filter_by(short_id=su.id).order_by(Click.timestamp.desc()).limit(200).all()
        content = render_template_string(STATS_CONTENT, code=code, target=su.target_url, visits=su.visits, clicks=clicks, csrf_token=generate_csrf_token())
        return render_template_string(BASE_HTML, title=f"Stats - {code}", content=content)
    finally:
        db.close()


# -----------------------
# API Endpoints
# -----------------------
@app.route("/api/v1/shorten", methods=["POST"])
@require_api_key
def api_shorten():
    # API rate limit per API key
    key = request.headers.get("X-API-Key") or request.args.get("api_key") or "unknown"
    if not api_rate_limiter.is_allowed(key):
        return jsonify({"error": "rate_limited"}), 429

    data = request.get_json() or {}
    target = (data.get("url") or data.get("target") or "").strip()
    custom = (data.get("custom_code") or "").strip()

    if not is_valid_url(target):
        return jsonify({"error": "invalid_url"}), 400

    db = db_session()
    try:
        if custom:
            code = custom
            if not all(c.isalnum() or c in "-_" for c in code):
                return jsonify({"error": "invalid_custom_code"}), 400
            if db.query(ShortURL).filter_by(code=code).first():
                return jsonify({"error": "custom_code_taken"}), 409
        else:
            for _ in range(8):
                code = generate_code()
                if not db.query(ShortURL).filter_by(code=code).first():
                    break
            else:
                return jsonify({"error": "could_not_generate_code"}), 500

        su = ShortURL(code=code, target_url=target, owner_id=request.api_user.id)
        db.add(su)
        db.commit()
        return jsonify({"short_url": f"{DEFAULT_DOMAIN}/r/{code}", "code": code, "target": target}), 201
    except IntegrityError:
        db.rollback()
        return jsonify({"error": "duplicate"}), 409
    except Exception:
        db.rollback()
        app.logger.exception("API create error")
        return jsonify({"error": "server_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/stats/<code>", methods=["GET"])
@require_api_key
def api_stats(code):
    # API rate limit per API key
    key = request.headers.get("X-API-Key") or request.args.get("api_key") or "unknown"
    if not api_rate_limiter.is_allowed(key):
        return jsonify({"error": "rate_limited"}), 429

    db = db_session()
    try:
        su = db.query(ShortURL).filter_by(code=code).first()
        if not su:
            return jsonify({"error": "not_found"}), 404
        # Allow only owner or admin
        if su.owner_id != request.api_user.id and not request.api_user.is_admin:
            return jsonify({"error": "forbidden"}), 403

        total = su.visits
        last_clicks = db.query(Click).filter_by(short_id=su.id).order_by(Click.timestamp.desc()).limit(100).all()
        clicks_data = [{
            "timestamp": c.timestamp.isoformat(),
            "ip": c.ip,
            "ua": c.user_agent,
            "referrer": c.referrer
        } for c in last_clicks]
        return jsonify({"code": code, "target": su.target_url, "visits": total, "recent_clicks": clicks_data})
    finally:
        db.close()


# -----------------------
# Redirect Engine (blazing-fast)
# -----------------------
@app.route("/r/<code>", methods=["GET"])
@rate_limit_for_ip(redirect_rate_limiter)
def redirect_code(code):
    """
    High-performance redirect handler:
    - Check in-memory cache first to avoid DB roundtrip if possible.
    - On cache miss, fetch minimal fields and populate cache.
    - Record click (lightweight) and increment visit count. Use direct SQL to increment to minimize race overhead.
    """
    # Try cache
    cached = cache_get(code)
    db = db_session()
    try:
        if cached:
            target, sid = cached
        else:
            su = db.query(ShortURL.id, ShortURL.target_url).filter_by(code=code).first()
            if not su:
                # 404 fallback
                return make_response("Not Found", 404)
            sid = su.id
            target = su.target_url
            cache_set(code, target, sid)

        # Record click - do not block redirect rendering; but since we're in a single-process app, we will attempt a quick insert
        try:
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
            ua = (request.headers.get("User-Agent") or "")[:1000]
            ref = (request.headers.get("Referer") or "")[:1000]
            click = Click(short_id=sid, ip=ip, user_agent=ua, referrer=ref)
            db.add(click)
            # Use an efficient atomic update for visits
            # Minimal ORM approach: fetch object and increment then commit (acceptable for SQLite + low concurrency)
            db.query(ShortURL).filter_by(id=sid).update({"visits": ShortURL.visits + 1})
            db.commit()
        except Exception:
            db.rollback()
            # non-fatal: log and continue with redirect
            app.logger.exception("Error recording click")

        # Build redirect response
        resp = redirect(target, code=302)
        # Security headers for redirect responses
        resp.headers['Referrer-Policy'] = 'no-referrer-when-downgrade'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        return resp
    finally:
        db.close()


# Optionally expose raw link info (public) - but avoid leaking owner info
@app.route("/info/<code>", methods=["GET"])
def info(code):
    db = db_session()
    try:
        su = db.query(ShortURL).filter_by(code=code).first()
        if not su:
            return jsonify({"error": "not_found"}), 404
        return jsonify({
            "code": code,
            "target": su.target_url,
            "visits": su.visits,
            "created_at": su.created_at.isoformat()
        })
    finally:
        db.close()


# -----------------------
# Security headers
# -----------------------
@app.after_request
def set_security_headers(resp):
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer-when-downgrade')
    resp.headers.setdefault('Content-Security-Policy', "default-src 'self'")
    return resp


# -----------------------
# CLI helper to create admin
# -----------------------
def create_admin_if_none():
    db = db_session()
    try:
        admin = db.query(User).filter_by(is_admin=True).first()
        if not admin:
            username = "admin"
            password = secrets.token_urlsafe(12)
            api_key = secrets.token_hex(32)
            admin = User(username=username, password_hash=generate_password_hash(password), api_key=api_key, is_admin=True)
            db.add(admin)
            db.commit()
            print("Created admin user:")
            print(f"  username: {username}")
            print(f"  password: {password}")
            print(f"  api_key: {api_key}")
        else:
            print("Admin already exists:", admin.username)
    finally:
        db.close()


# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Flask URL Shortener service")
    parser.add_argument("--init-admin", action="store_true", help="Create an admin user if none exist (prints credentials)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=5000, type=int)
    args = parser.parse_args()

    if args.init_admin:
        create_admin_if_none()

    # Ensure DB file exists and is writable
    if not os.path.exists(DB_PATH):
        # touch it by creating tables (already done above) but ensure directory
        dirname = os.path.dirname(DB_PATH)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname, exist_ok=True)

    # Start Flask app
    app.run(host=args.host, port=args.port, debug=False)