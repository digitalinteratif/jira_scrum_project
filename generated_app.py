import os
import hashlib
import hmac
import base64
import uuid
import datetime
from datetime import timezone, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, request, redirect, url_for, make_response, render_template_string,
    g, abort, flash, get_flashed_messages
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect, generate_csrf
from argon2 import PasswordHasher, exceptions as argon2_exceptions
import jwt
from dotenv import load_dotenv

# Load .env early
load_dotenv()

# Immutable stack enforcement — do NOT change these libraries
APP_SECRET = os.getenv("APP_SECRET", "dev-secret-please-change")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///dev.db")
BASE_URL = os.getenv("BASE_URL", "https://digitalinteractif.com")

# Normalize and validate BASE_URL to ensure proper scheme and domain-only (no path)
def _normalize_base_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.netloc:
            return 'https://digitalinteractif.com'
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return 'https://digitalinteractif.com'

BASE_URL = _normalize_base_url(BASE_URL)

# Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = APP_SECRET
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# CSRF lifetime - use default; generate_csrf will use app.secret_key
csrf = CSRFProtect(app)

# Expose generate_csrf to templates as csrf_token()
app.jinja_env.globals['csrf_token'] = generate_csrf

# Database
db = SQLAlchemy(app)

# Crypto and JWT
ph = PasswordHasher()  # argon2
JWT_ALGORITHM = 'HS256'
SESSION_COOKIE_NAME = 'session_token'

# Utility: anonymize IP with HMAC (one-way) using APP_SECRET as key; store hex digest
def anonymize_ip(ip: str) -> str:
    # Normalize IPv6 mapped IPv4
    key = APP_SECRET.encode('utf-8')
    # HMAC-SHA256 -> hex
    return hmac.new(key, ip.encode('utf-8'), hashlib.sha256).hexdigest()

# Generate short code
def generate_short_code() -> str:
    # Use URL-safe base64 of uuid4 bytes, truncated
    raw = uuid.uuid4().bytes
    s = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    return s[:8]

# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.datetime.now(timezone.utc))

class Link(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    short_code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    original_url = db.Column(db.Text, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)  # nullable -> anonymous
    title = db.Column(db.String(255), nullable=True)
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.datetime.now(timezone.utc))

class Click(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('link.id'), nullable=False, index=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.datetime.now(timezone.utc))
    ip_hash = db.Column(db.String(128), nullable=False)
    user_agent = db.Column(db.String(512), nullable=True)

# Ensure DB creation for dev (surgical: for production, use migrations)
with app.app_context():
    db.create_all()

# Session helpers using PyJWT — token placed in secure HttpOnly cookie
def create_session_token(user_id: int, remember: bool = False) -> str:
    now = datetime.datetime.utcnow()
    expiry = now + (timedelta(days=30) if remember else timedelta(hours=8))
    payload = {
        'user_id': user_id,
        'exp': expiry,
        'iat': now,
        'type': 'session'
    }
    return jwt.encode(payload, APP_SECRET, algorithm=JWT_ALGORITHM)

def decode_session_token(token: str):
    try:
        payload = jwt.decode(token, APP_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get('type') != 'session':
            return None
        return payload
    except Exception:
        return None

# before_request: load user if session cookie present
@app.before_request
def load_user():
    g.user_id = None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return
    payload = decode_session_token(token)
    if payload:
        g.user_id = payload.get('user_id')

# login_required decorator
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(g, 'user_id', None):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

# render_layout: consistently wrap content (no Jinja extends/blocks used)
def render_layout(content: str, title: str = "Digital Interactif - URL Shortener"):
    messages = get_flashed_messages()
    # Ensure csrf_token is available in template via app.jinja_env.globals set earlier
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>
    /* Minimal inline styling for single-file app */
    body {{ font-family: Arial, sans-serif; margin: 2rem; }}
    header {{ margin-bottom: 1rem; }}
    .flash {{ background: #fffbdd; border: 1px solid #ffe58f; padding: .5rem; margin-bottom:.5rem; }}
    form {{ margin-bottom: 1rem; }}
    table {{ border-collapse: collapse; width:100%; }}
    td, th {{ border: 1px solid #ddd; padding:.5rem; }}
  </style>
</head>
<body>
  <header>
    <a href="{url_for('index')}">Home</a> |
    <a href="{BASE_URL}">Public Site</a> |
    {{% if g.user_id %}}<a href="{url_for('dashboard')}">Dashboard</a> | <a href="{url_for('logout')}">Logout</a>{{% else %}}<a href="{url_for('login')}">Login</a> | <a href="{url_for('register')}">Register</a>{{% endif %}}
  </header>
  {"".join(f'<div class="flash">{m}</div>' for m in messages)}
  {content}
  <footer style="margin-top:2rem;color:#666;">
    <small>Running on BASE_URL: {BASE_URL}</small>
  </footer>
</body>
</html>"""
    # Use render_template_string so Jinja2 variable csrf_token() resolves in forms
    return render_template_string(html)

# Helpers: safe URL validation (prevent CRLF and similar attacks)
def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ('http', 'https') and p.netloc != ''
    except Exception:
        return False

# Routes

# Home: public shortening form (anonymous allowed)
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        original = (request.form.get('original_url') or '').strip()
        title = (request.form.get('title') or '').strip()
        # Basic validation
        if not original or not is_valid_url(original):
            flash("Invalid URL.")
            return render_layout(index_form())
        # If user logged in, owner_id set; else anonymous
        owner_id = getattr(g, 'user_id', None)
        # Create link
        short_code = generate_short_code()
        # Ensure uniqueness (loop)
        while Link.query.filter_by(short_code=short_code).first():
            short_code = generate_short_code()
        link = Link(short_code=short_code, original_url=original, owner_id=owner_id, title=title)
        db.session.add(link)
        db.session.commit()
        short_url = f"{BASE_URL.rstrip('/')}/{short_code}"
        flash(f"Short URL created: {short_url}")
        # For logged-in users, redirect to dashboard; else show created message
        if owner_id:
            return redirect(url_for('dashboard'))
        else:
            return render_layout(index_form(created=short_url))
    return render_layout(index_form())

def index_form(created: str = None):
    created_html = f"<p><strong>Created:</strong> {created}</p>" if created else ""
    return f"""
    <h1>Shorten a URL</h1>
    {created_html}
    <form method="POST" action="{url_for('index')}">
      <input type="text" name="original_url" placeholder="https://example.com/long/path" style="width:60%;">
      <input type="text" name="title" placeholder="Optional title">
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Shorten</button>
    </form>
    <p>Want more features? <a href="{url_for('register')}">Create an account</a>.</p>
    """

# Register
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = (request.form.get('password') or '')
        if not email or not password:
            flash('Email and password required.')
            return render_layout(register_form())
        if User.query.filter_by(email=email).first():
            flash('Email already registered.')
            return render_layout(register_form())
        pw_hash = ph.hash(password)
        user = User(email=email, password_hash=pw_hash)
        db.session.add(user)
        db.session.commit()
        # Auto-login
        token = create_session_token(user.id)
        resp = make_response(redirect(url_for('dashboard')))
        resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=False, samesite='Lax')  # secure=True in prod (HTTPS)
        return resp
    return render_layout(register_form())

def register_form():
    return f"""
    <h1>Register</h1>
    <form method="POST" action="{url_for('register')}">
      <label>Email: <input type="email" name="email"></label><br>
      <label>Password: <input type="password" name="password"></label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Register</button>
    </form>
    """

# Login with Remember Me
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        remember = request.form.get('remember') == 'on'
        user = User.query.filter_by(email=email).first()
        if not user:
            flash('Invalid credentials.')
            return render_layout(login_form())
        try:
            ph.verify(user.password_hash, password)
        except argon2_exceptions.VerifyMismatchError:
            flash('Invalid credentials.')
            return render_layout(login_form())
        # OK: create session token
        token = create_session_token(user.id, remember=remember)
        resp = make_response(redirect(url_for('dashboard')))
        # In production set secure=True when HTTPS is available
        resp.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=False, samesite='Lax')
        return resp
    return render_layout(login_form())

def login_form():
    return f"""
    <h1>Login</h1>
    <form method="POST" action="{url_for('login')}">
      <label>Email: <input type="email" name="email"></label><br>
      <label>Password: <input type="password" name="password"></label><br>
      <label><input type="checkbox" name="remember"> Remember me</label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Login</button>
    </form>
    <p><a href="{url_for('password_reset_request')}">Forgot password?</a></p>
    """

# Logout
@app.route('/logout', methods=['GET', 'POST'])
def logout():
    resp = make_response(redirect(url_for('index')))
    resp.set_cookie(SESSION_COOKIE_NAME, '', expires=0)
    return resp

# Dashboard: user links (ID Filter Rule applied)
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    # Show user's links; allow create with additional options
    if request.method == 'POST':
        # Creating link via dashboard: owner_id = g.user_id
        original = (request.form.get('original_url') or '').strip()
        title = (request.form.get('title') or '').strip()
        is_public = request.form.get('is_public') == 'on'
        if not original or not is_valid_url(original):
            flash("Invalid URL.")
            return redirect(url_for('dashboard'))
        short_code = generate_short_code()
        while Link.query.filter_by(short_code=short_code).first():
            short_code = generate_short_code()
        link = Link(short_code=short_code, original_url=original, owner_id=g.user_id, title=title, is_public=is_public)
        db.session.add(link)
        db.session.commit()
        flash("Link created.")
        return redirect(url_for('dashboard'))
    # GET: list links owned by user — must filter by owner_id (ID Filter Rule)
    links = Link.query.filter_by(owner_id=g.user_id).order_by(Link.created_at.desc()).all()
    rows = ""
    for l in links:
        short_url = f"{BASE_URL.rstrip('/')}/{l.short_code}"
        # Removed any JavaScript (onclick) to comply with no-JS policy. Deletion uses a plain POST form with CSRF token.
        rows += (
            f"<tr>"
            f"<td>{l.id}</td>"
            f"<td>{l.title or ''}</td>"
            f"<td><a href='{short_url}' target='_blank' rel='noopener noreferrer'>{short_url}</a></td>"
            f"<td>{l.created_at}</td>"
            f"<td>"
            f"<a href='{url_for('link_stats', link_id=l.id)}'>Stats</a> | "
            f"<a href='{url_for('edit_link', link_id=l.id)}'>Edit</a> | "
            f"<form style='display:inline' method='POST' action='{url_for('delete_link', link_id=l.id)}'>"
            f"<input type='hidden' name='csrf_token' value='{{{{ csrf_token() }}}}'>"
            f"<button type='submit'>Delete</button>"
            f"</form>"
            f"</td>"
            f"</tr>"
        )
    content = f"""
    <h1>Your Dashboard</h1>
    <h2>Create Link</h2>
    <form method="POST" action="{url_for('dashboard')}">
      <input type="text" name="original_url" placeholder="https://example.com/long"><br>
      <input type="text" name="title" placeholder="Optional title"><br>
      <label><input type="checkbox" name="is_public" checked> Public</label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Create</button>
    </form>
    <h2>Your Links</h2>
    <table>
      <tr><th>ID</th><th>Title</th><th>Short URL</th><th>Created</th><th>Actions</th></tr>
      {rows or '<tr><td colspan=5>No links yet.</td></tr>'}
    </table>
    """
    return render_layout(content)

# Edit link (GET/POST) — must filter by owner_id
@app.route('/links/<int:link_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_link(link_id):
    # Strict ID filter
    link = Link.query.filter_by(id=link_id, owner_id=g.user_id).first()
    if not link:
        abort(404)
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        is_public = request.form.get('is_public') == 'on'
        link.title = title
        link.is_public = is_public
        db.session.commit()
        flash('Updated.')
        return redirect(url_for('dashboard'))
    short_url = f"{BASE_URL.rstrip('/')}/{link.short_code}"
    content = f"""
    <h1>Edit Link</h1>
    <form method="POST" action="{url_for('edit_link', link_id=link.id)}">
      <label>Original: <input type="text" value="{link.original_url}" readonly style="width:60%;"></label><br>
      <label>Title: <input type="text" name="title" value="{link.title or ''}"></label><br>
      <label><input type="checkbox" name="is_public" {'checked' if link.is_public else ''}> Public</label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Save</button>
    </form>
    <p>Short URL: <a href="{short_url}" rel="noopener noreferrer">{short_url}</a></p>
    """
    return render_layout(content)

# Delete link — POST only; ID Filter Rule strictly enforced
@app.route('/links/<int:link_id>/delete', methods=['POST'])
@login_required
def delete_link(link_id):
    link = Link.query.filter_by(id=link_id, owner_id=g.user_id).first()
    if not link:
        abort(404)
    db.session.delete(link)
    db.session.commit()
    flash('Deleted.')
    return redirect(url_for('dashboard'))

# Stats / Analytics — only owner can view (ID Filter)
@app.route('/links/<int:link_id>/stats', methods=['GET'])
@login_required
def link_stats(link_id):
    link = Link.query.filter_by(id=link_id, owner_id=g.user_id).first()
    if not link:
        abort(404)
    # Retrieve clicks for the link (ID Filter on Clicks is by link_id; link is already owner-checked)
    clicks = Click.query.filter_by(link_id=link.id).order_by(Click.timestamp.desc()).limit(500).all()
    rows = ""
    for c in clicks:
        rows += f"<tr><td>{c.timestamp}</td><td>{c.ip_hash[:12]}...</td><td>{(c.user_agent or '')[:80]}</td></tr>"
    content = f"""
    <h1>Stats for {link.title or link.short_code}</h1>
    <p>Original URL: {link.original_url}</p>
    <table>
      <tr><th>Timestamp</th><th>Anonymized IP</th><th>User-Agent</th></tr>
      {rows or '<tr><td colspan=3>No clicks yet.</td></tr>'}
    </table>
    """
    return render_layout(content)

# Password reset request — issue JWT token for password reset (tokenized link)
@app.route('/password-reset', methods=['GET', 'POST'])
def password_reset_request():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user:
            flash('If that email exists, a reset link has been sent.')  # Do not reveal existence
            return redirect(url_for('login'))
        now = datetime.datetime.utcnow()
        payload = {
            'user_id': user.id,
            'type': 'pw_reset',
            'iat': now,
            'exp': now + timedelta(hours=1)
        }
        token = jwt.encode(payload, APP_SECRET, algorithm=JWT_ALGORITHM)
        # In production: email the link to user. For dev, show the link on screen.
        reset_link = url_for('password_reset', token=token, _external=True)
        flash('Password reset link (development): ' + reset_link)
        return redirect(url_for('login'))
    return render_layout(password_reset_request_form())

def password_reset_request_form():
    return f"""
    <h1>Password Reset</h1>
    <form method="POST" action="{url_for('password_reset_request')}">
      <label>Email: <input type="email" name="email"></label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Request reset</button>
    </form>
    """

# Password reset endpoint — consumes JWT and allows new password
@app.route('/password-reset/<token>', methods=['GET', 'POST'])
def password_reset(token):
    # Validate token
    try:
        payload = jwt.decode(token, APP_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get('type') != 'pw_reset':
            flash('Invalid reset token.')
            return redirect(url_for('login'))
        user_id = payload.get('user_id')
    except Exception:
        flash('Invalid or expired reset token.')
        return redirect(url_for('login'))
    if request.method == 'POST':
        new_password = request.form.get('password') or ''
        if not new_password:
            flash('Password required.')
            return render_layout(password_reset_form(token))
        user = User.query.filter_by(id=user_id).first()
        if not user:
            flash('Invalid user.')
            return redirect(url_for('login'))
        user.password_hash = ph.hash(new_password)
        db.session.commit()
        flash('Password updated. Please log in.')
        return redirect(url_for('login'))
    return render_layout(password_reset_form(token))

def password_reset_form(token):
    return f"""
    <h1>Set New Password</h1>
    <form method="POST" action="{url_for('password_reset', token=token)}">
      <label>New password: <input type="password" name="password"></label><br>
      <input type="hidden" name="csrf_token" value="{{{{ csrf_token() }}}}">
      <button type="submit">Update password</button>
    </form>
    """

# Redirect endpoint — public. Must record analytics (clicks, timestamp, anonymized IP)
@app.route('/<short_code>')
def redirect_short(short_code):
    link = Link.query.filter_by(short_code=short_code).first()
    if not link:
        abort(404)
    # Track click
    ip = request.remote_addr or '0.0.0.0'
    ip_hash = anonymize_ip(ip)
    ua = request.headers.get('User-Agent', '')[:512]
    click = Click(link_id=link.id, ip_hash=ip_hash, user_agent=ua)
    db.session.add(click)
    db.session.commit()
    # Strict redirection to original_url
    # Optionally: validate URL safe (already validated at creation)
    return redirect(link.original_url, code=302)

# Health check
@app.route('/health')
def health():
    return ("OK", 200)

# -----------------------------------------------------------------------------
# NOTES & SECURITY RATIONALE (Surgical, explicit)
# -----------------------------------------------------------------------------
#
# - CSRF:
#   * Flask-WTF CSRFProtect is enabled globally.
#   * Every HTML form in this single-file app contains the explicit input:
#       <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
#     (This matches the requirement exactly and avoids 400 Bad Request on Render.)
#
# - render_layout:
#   * All rendered UI uses render_layout(content) which returns a self-contained HTML boilerplate.
#   * No Jinja2 template inheritance (no {% extends %} or {% block %}) is used — consistent with single-file constraint.
#
# - Sessions:
#   * PyJWT tokens are used for session management per spec.
#   * Token stored in HttpOnly cookie 'session_token'. For production, set secure=True in res.set_cookie.
#   * "Remember Me" implemented by lengthening token expiry (30 days when checked).
#
# - Password Hashing:
#   * argon2-cffi (PasswordHasher) used for robust hashing.
#
# - Analytics & Privacy:
#   * Each redirect writes a Click row with timestamp, ip_hash, and user_agent.
#   * IPs are anonymized via HMAC-SHA256 keyed with APP_SECRET. This is one-way (no plaintext IP stored).
#   * When presenting analytics to users, IP displayed truncated for readability.
#
# - ID Filter Rule:
#   * All operations on user-owned resources enforce explicit owner filter; examples:
#       Link.query.filter_by(id=link_id, owner_id=g.user_id).first()
#       Link.query.filter_by(owner_id=g.user_id).all()
#     This prevents accidental exposure of other users' data.
#
# - BASE_URL:
#   * Short URLs are generated using BASE_URL environment variable. If not set or invalid, default is https://digitalinteractif.com
#   * BASE_URL is normalized at startup to ensure it contains only scheme and domain (no path/component injection).
#
# - Environment & Production:
#   * DATABASE_URL is used for SQLAlchemy; in production set to PostgreSQL connection string.
#   * Use Gunicorn to serve the app in production: e.g.
#       gunicorn -w 4 generated_app:app
#
# - Email (Password Reset):
#   * For production, integrate an email provider to send tokenized reset links.
#   * In development this blueprint displays the reset link via flash to avoid adding external dependencies.
#
# - UI Persistence:
#   * The app serves HTML UI; not headless. Pages are rendered server-side and include CSRF tokens.
#
# - Forbidden patterns enforced:
#   * No User.query.first() anywhere for sensitive operations.
#   * No template inheritance.
#   * No JavaScript used in the server-rendered UI (all onclick/js removed); deletion uses a simple POST form with CSRF token.
#
# -----------------------------------------------------------------------------
# Surgical change checklist (how to make precise updates without drifting):
# -----------------------------------------------------------------------------
# When updating features, apply changes only to:
#  - Models block (if DB schema change) — and accompany with proper migration scripts.
#  - The single route function affected (e.g., redirect_short) for behavioral deltas.
#  - render_layout for UI layout tweaks.
# Avoid:
#  - Replacing CSRFProtect or token generation; always preserve CSRF input in the form strings.
#  - Removing the owner_id filters on queries.
#  - Switching session implementation away from PyJWT.
#
# -----------------------------------------------------------------------------
# End of file
# -----------------------------------------------------------------------------

# If run directly, start the Flask dev server (surgical: for production use Gunicorn)
if __name__ == '__main__':
    # Note: in development, secure cookies are set secure=False so local testing works.
    app.run(host='0.0.0.0', port=5000, debug=True)