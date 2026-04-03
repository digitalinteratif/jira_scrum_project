#!/usr/bin/env python3
"""
High-Performance URL Shortening Web Service — SURGICAL BLUEPRINT
Single-file Flask application that embeds templates and CSS and preserves the
original UI layout wrapper (render_layout). This implements the blueprint
structure (auth, shortener), the blazing-fast redirect path (Redis -> DB),
and an asynchronous analytics pipeline using a Redis list consumed by a worker.

Notes:
- Templates are embedded in TEMPLATES dict and loaded into Flask's Jinja loader.
- render_layout uses flask.render_template_string as required, while the loader
  ensures {% extends "layout.html" %} works as expected.
- Static CSS is served via a simple route for portability in single-file mode.
"""

import os
import re
import time
import json
import secrets
import string
from datetime import datetime

from flask import (
    Flask, request, redirect as flask_redirect, abort, flash, url_for,
    jsonify, Response, render_template_string, current_app
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import redis
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

# -----------------------
# Embedded templates & CSS (verbatim from blueprint)
# -----------------------
TEMPLATES = {
    "layout.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{ site_name }}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="stylesheet" href="{{ url_for('static_css') }}">
</head>
<body>
  <header class="site-header">
    <div class="container">
      <h1 class="logo"><a href="{{ url_for('home') }}">{{ site_name }}</a></h1>
      <nav class="site-nav">
        <a href="{{ url_for('home') }}">Home</a>
        {% if current_user.is_authenticated %}
          <a href="{{ url_for('shortener.create_short') }}">Create</a>
          <a href="{{ url_for('auth.dashboard') }}">Dashboard</a>
          <a href="{{ url_for('auth.logout') }}">Logout</a>
        {% else %}
          <a href="{{ url_for('auth.login') }}">Login</a>
          <a href="{{ url_for('auth.register') }}">Register</a>
        {% endif %}
      </nav>
    </div>
  </header>
  <main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <div class="messages">
            {% for category, msg in messages %}
              <div class="flash {{ category }}">{{ msg }}</div>
            {% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>
  <footer class="site-footer container">
    <small>&copy; {{ site_name }} — All links resolve on {{ default_domain }}</small>
  </footer>
</body>
</html>""",
    "home.html": """{% extends "layout.html" %}
{% block content %}
  <section>
    <h2>Welcome</h2>
    <p>Create short links that resolve on <strong>{{ default_domain }}</strong>.</p>
    <p><a class="btn" href="{{ url_for('shortener.create_short') }}">Create your first short link</a></p>
    <hr>
    <h3>How it works</h3>
    <ol>
      <li>Create a target URL (e.g. https://example.com/page)</li>
      <li>Receive a short link like https://{{ default_domain }}/abc123</li>
      <li>Clicks are redirected in microseconds and tracked asynchronously</li>
    </ol>
  </section>
{% endblock %}""",
    "login.html": """{% extends "layout.html" %}
{% block content %}
  <h2>Login</h2>
  <form method="post" action="{{ url_for('auth.login') }}">
    {{ csrf_token() }}
    <label>Email</label>
    <input type="email" name="email" required>
    <label>Password</label>
    <input type="password" name="password" required>
    <button type="submit">Login</button>
  </form>
  <p>Don't have an account? <a href="{{ url_for('auth.register') }}">Register</a>.</p>
{% endblock %}""",
    "register.html": """{% extends "layout.html" %}
{% block content %}
  <h2>Register</h2>
  <form method="post" action="{{ url_for('auth.register') }}">
    {{ csrf_token() }}
    <label>Email</label>
    <input type="email" name="email" required>
    <label>Password</label>
    <input type="password" name="password" required>
    <button type="submit">Register</button>
  </form>
  <p>Already have an account? <a href="{{ url_for('auth.login') }}">Login</a>.</p>
{% endblock %}""",
    "dashboard.html": """{% extends "layout.html" %}
{% block content %}
  <h2>Your Dashboard</h2>
  <p>Welcome, {{ current_user.email }} — create and manage links.</p>
  <p><a class="btn" href="{{ url_for('shortener.create_short') }}">Create new short link</a></p>
  <h3>Your Links</h3>
  {% if urls %}
    <table>
      <thead><tr><th>Short</th><th>Target</th><th>Clicks</th><th>Created</th></tr></thead>
      <tbody>
        {% for u in urls %}
          <tr>
            <td><a href="https://{{ default_domain }}/{{ u.code }}" target="_blank">https://{{ default_domain }}/{{ u.code }}</a></td>
            <td><a href="{{ u.target_url }}" target="_blank">{{ u.title or u.target_url }}</a></td>
            <td>{{ u.visits or 0 }}</td>
            <td>{{ u.created_at.strftime('%Y-%m-%d') }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p>No links yet — create your first one.</p>
  {% endif %}
{% endblock %}""",
    "create_short.html": """{% extends "layout.html" %}
{% block content %}
  <h2>Create Short Link</h2>
  {% if error %}
    <div class="error">{{ error }}</div>
  {% endif %}
  {% if success %}
    <div class="success">
      Short link created:
      <p><a href="{{ short_link }}" target="_blank">{{ short_link }}</a></p>
      <p>Target: {{ target }}</p>
    </div>
  {% endif %}
  <form method="post" action="{{ url_for('shortener.create_short') }}">
    {{ csrf_token() }}
    <label>Target URL</label>
    <input type="text" name="target" value="{{ target or '' }}" placeholder="https://example.com/page" required>
    <label>Custom code (optional, letters, numbers, - or _)</label>
    <input type="text" name="custom_code" placeholder="my-special-code">
    <label>Title (optional)</label>
    <input type="text" name="title" placeholder="Landing page title">
    <button type="submit">Create</button>
  </form>
{% endblock %}"""
}

STYLES_CSS = r"""/* Minimal styles for the UI */
body { font-family: Arial, sans-serif; margin:0; padding:0; color:#222; background:#f9f9f9; }
.container { max-width:900px; margin:0 auto; padding:20px; }
.site-header { background:#fff; box-shadow: 0 1px 3px rgba(0,0,0,0.06); padding:10px 0; margin-bottom:20px; }
.logo { display:inline-block; margin:0; }
.logo a { text-decoration:none; color:#333; font-weight:700; }
.site-nav { float:right; }
.site-nav a { margin-left:12px; text-decoration:none; color:#007bff; }
.site-footer { padding:20px 0; color:#666; font-size:14px; border-top:1px solid #eee; margin-top:40px; }
h2 { margin-top:0; }
form input[type="text"], form input[type="email"], form input[type="password"] { width:100%; padding:8px; margin-bottom:10px; box-sizing:border-box;}
form label { display:block; font-weight:600; margin-bottom:6px; }
button, .btn { background:#007bff; color:#fff; border:none; padding:10px 16px; cursor:pointer; text-decoration:none; display:inline-block; }
.success { background:#e6ffed; padding:10px; border:1px solid #c8f7d4; margin-bottom:10px; }
.error { background:#ffe6e6; padding:10px; border:1px solid #f5c2c2; margin-bottom:10px; }
table { width:100%; border-collapse:collapse; margin-top:10px; }
table th, table td { text-align:left; padding:8px; border-bottom:1px solid #efefef; }
.flash { padding:8px; margin-bottom:10px; }"""

# -----------------------
# Configuration
# -----------------------
class Config:
    basedir = os.path.abspath(os.path.dirname(__file__))
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-key"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(basedir, "app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    DEFAULT_DOMAIN = os.environ.get("DEFAULT_DOMAIN", "digitalinteractif.com")
    RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "100/hour")


# -----------------------
# Extensions (declared globally)
# -----------------------
db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = None  # will be initialized per-app
redis_client = None  # will be a redis.StrictRedis-like client


# -----------------------
# Utilities (render_layout + helpers)
# -----------------------
_ALPHABET = string.digits + string.ascii_letters

def generate_code(length=6):
    return ''.join(secrets.choice(_ALPHABET) for _ in range(length))


def build_short_link(code, app=None):
    # Use provided app or current environment if available
    if app:
        domain = app.config.get('DEFAULT_DOMAIN')
    else:
        try:
            domain = current_app.config.get('DEFAULT_DOMAIN')
        except Exception:
            domain = os.environ.get("DEFAULT_DOMAIN", "digitalinteractif.com")
    domain = domain or "digitalinteractif.com"
    return f"https://{domain}/{code}"


def render_layout(template_name, **context):
    """
    Wrap rendering through the shared site layout. Uses flask.render_template_string
    while relying on Flask app.jinja_loader to resolve {% extends %} for templates.
    """
    # ensure common context
    try:
        default_domain = current_app.config.get('DEFAULT_DOMAIN')
    except Exception:
        default_domain = os.environ.get("DEFAULT_DOMAIN", "digitalinteractif.com")
    context.setdefault('default_domain', default_domain)
    context.setdefault('site_name', 'Digital Interactif Shortener')
    # retrieve the template source from the embedded TEMPLATES and render via render_template_string
    tpl_src = TEMPLATES.get(template_name)
    if tpl_src is None:
        abort(500, description=f"Template not found: {template_name}")
    return render_template_string(tpl_src, **context)


# -----------------------
# Models (uses db)
# -----------------------
# We'll declare models after db is initialized in create_app, but class definitions can be here
# and will be bound to db when app context present. Flask-SQLAlchemy handles this.

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    urls = db.relationship('URL', backref='owner', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class URL(db.Model):
    __tablename__ = 'urls'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    target_url = db.Column(db.String(2048), nullable=False)
    title = db.Column(db.String(255), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    visits = db.Column(db.Integer, default=0)

    clicks = db.relationship('Click', backref='url', lazy='dynamic')


class Click(db.Model):
    __tablename__ = 'clicks'
    id = db.Column(db.BigInteger, primary_key=True)
    url_id = db.Column(db.Integer, db.ForeignKey('urls.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip = db.Column(db.String(100))
    user_agent = db.Column(db.String(512))
    referrer = db.Column(db.String(2048))


# -----------------------
# Blueprints: auth and shortener
# -----------------------
from flask import Blueprint

auth_bp = Blueprint('auth', __name__, template_folder='templates')
shortener_bp = Blueprint('shortener', __name__, template_folder='templates')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_layout('login.html')
    # POST
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    user = User.query.filter_by(email=email).first()
    if user and user.check_password(password):
        login_user(user)
        return flask_redirect(url_for('auth.dashboard'))
    flash('Invalid credentials', 'danger')
    return render_layout('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_layout('register.html')
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    if not email or not password:
        flash('Email and password required', 'warning')
        return render_layout('register.html')
    if User.query.filter_by(email=email).first():
        flash('Email already registered', 'warning')
        return render_layout('register.html')
    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return flask_redirect(url_for('auth.dashboard'))


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return flask_redirect(url_for('home'))


@auth_bp.route('/dashboard')
@login_required
def dashboard():
    urls = URL.query.filter_by(owner_id=current_user.id).order_by(URL.created_at.desc()).limit(50).all()
    return render_layout('dashboard.html', urls=urls)


@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None


# Attempt to import validators package if available (optional)
try:
    import validators as _validators  # type: ignore
except Exception:
    _validators = None  # not required for this surgical blueprint


@shortener_bp.route('/create', methods=['GET', 'POST'])
def create_short():
    # Note: limiter will be applied in create_app after limiter exists.
    if request.method == 'GET':
        return render_layout('create_short.html')
    # POST from UI form
    target = request.form.get('target', '').strip()
    custom = request.form.get('custom_code', '').strip()
    title = request.form.get('title', '').strip()
    owner_id = current_user.id if current_user.is_authenticated else None

    if not target:
        return render_layout('create_short.html', error='Target URL required.', target=target)
    # minimal URL normalization
    if not re.match(r'^https?://', target):
        target = 'https://' + target

    # validate
    if len(target) > 2048:
        return render_layout('create_short.html', error='URL too long.', target=target)

    # Decide code
    code = None
    if custom:
        if not re.match(r'^[A-Za-z0-9_-]{3,64}$', custom):
            return render_layout('create_short.html', error='Invalid custom code.', target=target)
        if URL.query.filter_by(code=custom).first():
            return render_layout('create_short.html', error='Custom code already in use.', target=target)
        code = custom
    else:
        # generate until unique (fast)
        for _ in range(5):
            attempt = generate_code(6)
            if not URL.query.filter_by(code=attempt).first():
                code = attempt
                break
        if not code:
            # Fallback to longer
            while True:
                attempt = generate_code(8)
                if not URL.query.filter_by(code=attempt).first():
                    code = attempt
                    break

    # Persist
    url = URL(code=code, target_url=target, title=title, owner_id=owner_id)
    db.session.add(url)
    db.session.commit()

    # Cache to Redis
    try:
        key = f"short:{code}"
        redis_client.hset(key, mapping={'target': target, 'url_id': str(url.id)})
        redis_client.expire(key, 60 * 60 * 24)
    except Exception:
        # Swallow caching errors
        pass

    short_link = build_short_link(code, app=current_app)
    return render_layout('create_short.html', success=True, short_link=short_link, target=target)


@shortener_bp.route('/api/create', methods=['POST'])
def api_create():
    data = request.get_json() or {}
    target = data.get('target', '').strip()
    custom = (data.get('custom_code') or '').strip()
    if not target:
        return jsonify({'error': 'target required'}), 400
    if not re.match(r'^https?://', target):
        target = 'https://' + target
    # generate same way as UI
    code = custom or generate_code(6)
    # ensure unique...
    if URL.query.filter_by(code=code).first():
        return jsonify({'error': 'code exists'}), 400
    url = URL(code=code, target_url=target)
    db.session.add(url)
    db.session.commit()
    try:
        key = f"short:{code}"
        redis_client.hset(key, mapping={'target': target, 'url_id': str(url.id)})
        redis_client.expire(key, 60 * 60 * 24)
    except Exception:
        pass
    return jsonify({'short_url': build_short_link(code, app=current_app)}), 201


# -----------------------
# Application factory & routes
# -----------------------
def create_app():
    global limiter, redis_client
    app = Flask(__name__)
    app.config.from_object(Config)

    # Load embedded templates into Jinja's DictLoader so {% extends "layout.html" %} works.
    app.jinja_loader = DictLoader(TEMPLATES)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    # Configure LoginManager
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'warning'

    # Redis client (for caching and queue)
    redis_client = redis.from_url(app.config['REDIS_URL'], decode_responses=True)

    # Limiter (default limits)
    limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[app.config['RATE_LIMIT_DEFAULT']])

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(shortener_bp)

    # Because limiter decorators were not available at definition time, bind limits to view functions now.
    try:
        app.view_functions['shortener.create_short'] = limiter.limit("10/minute")(app.view_functions['shortener.create_short'])
    except Exception:
        pass
    try:
        app.view_functions['shortener.api_create'] = limiter.limit("60/hour")(app.view_functions['shortener.api_create'])
    except Exception:
        pass

    # Expose static CSS route (single-file convenience)
    @app.route('/static/styles.css')
    def static_css():
        return Response(STYLES_CSS, mimetype='text/css')

    # Home route (UI)
    @app.route('/')
    def home():
        return render_layout('home.html')

    # Fast redirect endpoint
    @app.route('/<code>')
    def redirect_short(code):
        # Try Redis cache first
        key = f"short:{code}"
        target = None
        try:
            cached = redis_client.hgetall(key)
        except Exception:
            cached = {}
        if cached and 'target' in cached:
            target = cached['target']
        else:
            # Hit DB
            url = URL.query.filter_by(code=code).first()
            if not url:
                abort(404)
            target = url.target_url
            # Save to Redis for fast subsequent hits (TTL 24h)
            try:
                redis_client.hset(key, mapping={'target': target, 'url_id': str(url.id)})
                redis_client.expire(key, 60 * 60 * 24)
            except Exception:
                pass

        # Record analytics asynchronously: push to Redis list (fast)
        try:
            event = {
                'code': code,
                'time': str(int(time.time())),
                'ip': request.headers.get('X-Forwarded-For', request.remote_addr),
                'ua': (request.headers.get('User-Agent') or '')[:512],
                'referrer': request.referrer or '',
            }
            redis_client.lpush('events_queue', json.dumps(event))
        except Exception:
            # Swallow analytics errors to preserve redirect speed
            pass

        # 302 redirect
        return flask_redirect(target, code=302)

    # For Flask shell convenience and external access
    return app


# create app for WSGI
app = create_app()

# -----------------------
# Worker (analytics consumer)
# -----------------------
def run_worker():
    """
    Simple worker that BRPOP's the events_queue and writes Click rows + increments URL.visits.
    Intended to run as: python -m this_module (or import and call run_worker())
    """
    # Use same app context
    worker_app = create_app()
    with worker_app.app_context():
        r = redis.from_url(worker_app.config['REDIS_URL'], decode_responses=True)
        QUEUE = 'events_queue'
        print("Worker started, waiting for events...")
        while True:
            try:
                item = r.brpop(QUEUE, timeout=5)
                if item:
                    _, payload = item
                    try:
                        data = json.loads(payload)
                        code = data.get('code')
                        url_obj = URL.query.filter_by(code=code).first()
                        if not url_obj:
                            continue
                        click = Click(
                            url_id=url_obj.id,
                            ip=data.get('ip'),
                            user_agent=(data.get('ua') or '')[:512],
                            referrer=(data.get('referrer') or '')[:2048]
                        )
                        db.session.add(click)
                        # Increment visits counter
                        url_obj.visits = (url_obj.visits or 0) + 1
                        db.session.commit()
                    except Exception as e:
                        # In production, use structured logging
                        print("Worker processing error:", e)
                else:
                    # No item; loop again
                    continue
            except Exception as e:
                print("Worker exception:", e)
                time.sleep(1)


# -----------------------
# Run server or worker
# -----------------------
if __name__ == '__main__':
    # If invoked with "worker" argument, run the worker loop instead of the web server.
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        run_worker()
    else:
        # Start Flask dev server. For production use Gunicorn as recommended in blueprint.
        app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 8000)))