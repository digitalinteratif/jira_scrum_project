"""routes/shortener.py - URL shortening blueprint, public redirection, and QR generation (KAN-113/114/115/119/126/146).

Surgical update (KAN-146):
 - Adds endpoint GET /<slug>/qr that returns a QR image (PNG or SVG) encoding the canonical short URL.
 - Implements an in-memory cache keyed by slug+target checksum with TTL and simple eviction.
 - Adds download link for the QR code to the short-url creation success pages.
 - Dependency tolerant: tries to use 'qrcode' (PNG/SVG), falls back to 'segno' if present, otherwise returns a lightweight SVG placeholder.
 - Records best-effort trace to trace_KAN-146.txt for Architectural Memory.
 - Changes are localized to this module only (surgical).
"""

from flask import Blueprint, request, current_app, redirect, url_for, g, make_response
from utils.templates import render_layout
from sqlalchemy.exc import IntegrityError
import models
from datetime import datetime

# New imports for QR generation + caching
import io
import threading
import time
import hashlib
import base64
import html as _html

# Shortener utilities (prefer import from utils.shortener; fallback minimal implementations already exist in repo)
try:
    from utils.shortener import (
        generate_slug,
        validate_custom_slug,
        find_unique_slug,
        suggest_alternatives,
        UniqueSlugGenerationError,
    )
except Exception:
    # Defensive fallback - mirrors earlier lightweight replacements to avoid breaking app creation in constrained test runs
    def generate_slug(length=8, **kwargs):
        import time, hashlib
        return hashlib.sha1(str(time.time()).encode("utf-8")).hexdigest()[:length]

    def validate_custom_slug(slug):
        return isinstance(slug, str) and 0 < len(slug) <= 255

    def find_unique_slug(session, length=8, max_retries=5, **kwargs):
        return generate_slug(length)

    def suggest_alternatives(base_slug, count=5, length=8, session=None):
        return [f"{base_slug}-{i}" for i in range(1, count + 1)]

    class UniqueSlugGenerationError(Exception):
        pass

# Try to import URL validation/normalization from utils.validation as requested by KAN-113.
# If absent, provide a conservative fallback implementation here.
try:
    from utils.validation import validate_and_normalize_url
except Exception:
    # Fallback validator/normalizer: conservative checks and IDNA-encoding of hostname.
    from urllib.parse import urlparse, urlunparse, quote, unquote

    def validate_and_normalize_url(raw_url: str) -> str:
        """
        Validate and normalize a target URL.

        Rules:
          - Must have http or https scheme.
          - Netloc must be present.
          - IDNA-encode the hostname portion to support internationalized domain names.
          - Preserve path/query/fragment; ensure no CRLF injection.
        Returns:
          - normalized absolute URL string
        Raises:
          - ValueError on invalid input
        """
        if not isinstance(raw_url, str):
            raise ValueError("URL must be a string.")

        url = raw_url.strip()
        if not url:
            raise ValueError("Empty URL provided.")

        # Defend against CRLF injection
        if "\n" in url or "\r" in url:
            raise ValueError("Invalid characters in URL.")

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            # If scheme missing but URL looks like host-only, reject (require explicit http/https for clarity)
            raise ValueError("URL must start with http:// or https://")

        if not parsed.netloc:
            raise ValueError("URL must include a network location.")

        # IDNA-encode hostname (preserve port if present)
        # parsed.netloc may include username/password and port; handle simply for common cases.
        netloc = parsed.netloc
        userinfo = ""
        hostport = netloc

        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)

        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            # If port is not numeric, treat as part of host (very uncommon) -> attempt to keep as-is
            try:
                int(port)
                port_part = f":{port}"
            except Exception:
                host = hostport
                port_part = ""
        else:
            host = hostport
            port_part = ""

        try:
            host_idna = host.encode("idna").decode("ascii")
        except Exception:
            # If IDNA fails, reject input
            raise ValueError("Invalid hostname in URL.")

        normalized_netloc = host_idna + port_part
        if userinfo:
            normalized_netloc = f"{userinfo}@{normalized_netloc}"

        # Rebuild normalized URL; ensure path/query/fragment are safely quoted
        path = quote(unquote(parsed.path), safe="/%:@[]!$&'()*+,;=")
        query = quote(unquote(parsed.query), safe="=&?/")
        fragment = quote(unquote(parsed.fragment), safe="")
        return urlunparse((scheme, normalized_netloc, path, parsed.params, query, fragment))


# Import rate limiting decorators (KAN-126). Defensive import to avoid breaking constrained test runs.
try:
    from utils.rate_limit import rate_limit_user, rate_limit_ip
except Exception:
    # Provide no-op decorators as fallback (ensures app still runs if utils.rate_limit missing)
    def rate_limit_user(*args, **kwargs):
        def _d(fn):
            return fn
        return _d

    def rate_limit_ip(*args, **kwargs):
        def _d(fn):
            return fn
        return _d


def _write_trace_113(entry: str):
    """
    Append a trace line for KAN-113 architectural memory. Non-blocking best-effort.
    """
    try:
        with open("trace_KAN-113.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass


# New trace helper for KAN-114
def _write_trace_114(entry: str):
    try:
        with open("trace_KAN-114.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass


# New trace helper for KAN-115 (click event tracking)
def _write_trace_115(entry: str):
    try:
        with open("trace_KAN-115.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass


# Trace helper for KAN-119 (expiry & cleanup)
def _write_trace_119(entry: str):
    try:
        with open("trace_KAN-119.txt", "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {entry}\n")
    except Exception:
        pass


shortener_bp = Blueprint("shortener", __name__)


def _get_user_verified_custom_host_db(session, user_id: int):
    """
    Return a string host (no scheme) for the user's preferred verified domain, or None if none found.
    Preference: most recently created verified domain.
    """
    try:
        cd = session.query(models.CustomDomain).filter_by(owner_id=user_id, is_verified=True).order_by(models.CustomDomain.created_at.desc()).first()
        if cd and getattr(cd, "domain", None):
            return cd.domain.lower()
    except Exception:
        pass
    return None


# -------------------------
# QR generation & cache (KAN-146)
# -------------------------
# Dependency-tolerant imports: try qrcode (PNG + SVG), then segno, else fallback placeholder generator.
_qr_lib_qrcode = None
_qr_lib_segno = None
try:
    import qrcode  # type: ignore
    import qrcode.image.svg as qrcode_svg  # type: ignore
    _qr_lib_qrcode = qrcode
except Exception:
    _qr_lib_qrcode = None

if _qr_lib_qrcode is None:
    try:
        import segno  # type: ignore
        _qr_lib_segno = segno
    except Exception:
        _qr_lib_segno = None

# In-memory cache for generated QR images
_qr_cache = {}  # key -> {data: bytes, content_type: str, etag:str, expires_at: float, created_at: float}
_qr_cache_lock = threading.Lock()

def _qr_cache_cleanup(max_items: int):
    """
    Evict expired entries and trim to max_items by oldest created_at.
    """
    try:
        now = time.time()
        with _qr_cache_lock:
            # Remove expired
            keys_to_delete = [k for k, v in _qr_cache.items() if v.get("expires_at", 0) <= now]
            for k in keys_to_delete:
                try:
                    del _qr_cache[k]
                except Exception:
                    pass
            # Enforce max_items by removing oldest entries
            if max_items and len(_qr_cache) > max_items:
                items = sorted(_qr_cache.items(), key=lambda kv: kv[1].get("created_at", 0))
                for k, _ in items[: max(0, len(_qr_cache) - max_items)]:
                    try:
                        del _qr_cache[k]
                    except Exception:
                        pass
    except Exception:
        # never raise
        pass

def _qr_cache_get(key: str):
    now = time.time()
    with _qr_cache_lock:
        entry = _qr_cache.get(key)
        if not entry:
            return None
        if entry.get("expires_at", 0) <= now:
            # expired
            try:
                del _qr_cache[key]
            except Exception:
                pass
            return None
        return entry

def _qr_cache_set(key: str, data: bytes, content_type: str, ttl_seconds: int, max_items: int):
    now = time.time()
    etag = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
    entry = {
        "data": data,
        "content_type": content_type,
        "etag": etag,
        "expires_at": now + int(ttl_seconds),
        "created_at": now,
    }
    with _qr_cache_lock:
        _qr_cache[key] = entry
    # best-effort cleanup (trim)
    try:
        _qr_cache_cleanup(max_items)
    except Exception:
        pass
    return entry

def _generate_qr_bytes(text: str, fmt: str = "png", box_size: int = 10, border: int = 4):
    """
    Generate QR image bytes for 'text' in either 'png' or 'svg' format.
    Dependency-tolerant:
     - Prefer qrcode (PIL + qrcode.image.svg) when available.
     - Else try segno to produce PNG/SVG.
     - Else produce a simple SVG placeholder containing the text (non-scannable).
    Returns (bytes, content_type)
    """
    fmt = (fmt or "png").lower()
    # 1) qrcode lib
    if _qr_lib_qrcode is not None:
        try:
            if fmt == "svg":
                factory = qrcode.image.svg.SvgImage  # type: ignore[attr-defined]
                img = _qr_lib_qrcode.make(text, image_factory=factory, box_size=box_size, border=border)
                bio = io.BytesIO()
                # img is a PilImage-like or SvgImage; save() writes xml/text to buffer
                img.save(bio)
                return bio.getvalue(), "image/svg+xml"
            else:
                # PNG path
                img = _qr_lib_qrcode.make(text, box_size=box_size, border=border)
                bio = io.BytesIO()
                try:
                    img.save(bio, format="PNG")
                except TypeError:
                    # In some qrcode versions, img is PIL.Image.Image and .save accepts no format param
                    img.save(bio)
                return bio.getvalue(), "image/png"
        except Exception:
            # fall through to next lib
            pass

    # 2) segno lib
    if _qr_lib_segno is not None:
        try:
            q = _qr_lib_segno.make(text, error='h')
            bio = io.BytesIO()
            if fmt == "svg":
                q.save(bio, kind='svg', xmldecl=False)
                return bio.getvalue(), "image/svg+xml"
            else:
                q.save(bio, kind='png', scale=1)
                return bio.getvalue(), "image/png"
        except Exception:
            pass

    # 3) Fallback: simple SVG placeholder (not a real QR). This ensures endpoint returns an image
    try:
        esc = _html.escape(text)
        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400" role="img" aria-label="QR code placeholder">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <rect x="20" y="20" width="360" height="360" fill="#eee" stroke="#ccc" stroke-width="2"/>
  <text x="200" y="200" font-family="Arial, Helvetica, sans-serif" font-size="12" fill="#444" text-anchor="middle">
    QR generation unavailable
  </text>
  <text x="200" y="220" font-family="Arial, Helvetica, sans-serif" font-size="10" fill="#666" text-anchor="middle">
    {esc}
  </text>
</svg>"""
        return svg.encode("utf-8"), "image/svg+xml"
    except Exception:
        # As ultimate fallback, return empty PNG bytes (should be non-empty per acceptance criteria; avoid)
        return b"", "application/octet-stream"

def _build_qr_cache_key(slug: str, short_url: str, fmt: str) -> str:
    h = hashlib.sha256()
    h.update(slug.encode("utf-8"))
    h.update(b"|")
    h.update(short_url.encode("utf-8"))
    h.update(b"|")
    h.update(fmt.encode("utf-8"))
    return h.hexdigest()

# Best-effort trace writer for this ticket
def _trace_146(msg: str):
    try:
        with open("trace_KAN-146.txt", "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

# -------------------------
# Shorten endpoint (GET/POST) - preserved behavior with QR links added to success pages
# -------------------------
@shortener_bp.route("/shorten", methods=["GET", "POST"])
@rate_limit_user(capacity=5, window_seconds=60)  # per-user creation rate: default 5 per 60s
def shorten():
    """
    GET: Render a form to create a short URL.
    POST: Create ShortURL row using canonical models.create_shorturl helper and enforce ID Filter
    by setting user_id to the authenticated user's id.
    After creating a short link we include QR download links (PNG/SVG).
    """
    if request.method == "GET":
        # CSRF token generation (rendered into form)
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        # Determine whether demo user_id is allowed (defaults to False to enforce auth)
        allow_demo = bool(current_app.config.get("ALLOW_DEMO_USER_ID", False))

        demo_note = ""
        demo_user_field = ""
        if allow_demo:
            demo_note = "<p><em>Demo mode: you may optionally provide a user_id (for local testing only).</em></p>"
            demo_user_field = '<label for="shorten-user_id">User ID: <input id="shorten-user_id" type="number" name="user_id" required></label><br/>'
        else:
            demo_note = "<p><em>This route requires authentication. Submit as an authenticated user.</em></p>"

        html = f"""
        <h1>Create Short URL</h1>
        <form method="post" action="/shorten" novalidate>
          {demo_user_field}
          <label for="shorten-target_url">Target URL</label>
          <input id="shorten-target_url" type="url" name="target_url" required style="width:100%" aria-required="true">
          <label for="shorten-slug">Slug (optional)</label>
          <input id="shorten-slug" type="text" name="slug" maxlength="255" aria-describedby="slug-help">
          <div id="slug-help" class="sr-only" style="display:none;">Allowed characters: A-Z a-z 0-9 _ -</div>
          <label for="shorten-is_custom"><input id="shorten-is_custom" type="checkbox" name="is_custom" value="1"> Custom Slug</label>
          <label for="shorten-deterministic"><input id="shorten-deterministic" type="checkbox" name="deterministic" value="1"> Deterministic (generate from target+secret)</label>
          <label for="shorten-expire">Expire At (YYYY-MM-DD HH:MM, optional UTC)</label>
          <input id="shorten-expire" type="text" name="expire_at">
          <input type="hidden" name="csrf_token" value="{csrf_token}">
          <button type="submit">Create</button>
        </form>
        {demo_note}
        <p>Note: If you are authenticated, your logged-in identity will be used as the owner of the short URL.</p>
        """
        return render_layout(html)

    # POST - process creation
    session = models.Session()
    try:
        # Determine current user: prefer g.current_user (auth required), fallback to demo user_id only if allowed
        current_user = getattr(g, "current_user", None)
        if current_user and getattr(current_user, "id", None):
            user_id = int(current_user.id)
        else:
            # If no current_user, check whether demo user_id is permitted
            allow_demo = bool(current_app.config.get("ALLOW_DEMO_USER_ID", False))
            if allow_demo:
                try:
                    user_id = int(request.form.get("user_id", "0"))
                except Exception:
                    return render_layout("<p>Invalid or missing user_id.</p>"), 400
            else:
                # Strict auth-required behavior
                return render_layout("<h1>Unauthorized</h1><p>You must be authenticated to create a short URL.</p>"), 401

        # Validate that user exists (ID Filter mindset)
        user = session.query(models.User).filter_by(id=user_id).first()
        if not user:
            return render_layout("<p>User not found.</p>"), 404

        # Target URL validation & normalization
        raw_target = request.form.get("target_url", "").strip()
        if not raw_target:
            return render_layout("<p>Missing target URL.</p>"), 400

        try:
            target_url = validate_and_normalize_url(raw_target)
        except ValueError as ve:
            return render_layout(f"<h1>Invalid URL</h1><p>{str(ve)}</p>"), 400
        except Exception as e:
            # Defensive catch-all
            return render_layout(f"<h1>Invalid URL</h1><p>{str(e)}</p>"), 400

        # Slug and flags
        slug = request.form.get("slug", "").strip()
        is_custom = bool(request.form.get("is_custom"))
        deterministic = bool(request.form.get("deterministic"))
        expire_at_raw = request.form.get("expire_at", "").strip()
        expire_at = None
        if expire_at_raw:
            try:
                expire_at = datetime.strptime(expire_at_raw, "%Y-%m-%d %H:%M")
            except Exception:
                return render_layout("<p>expire_at must be in 'YYYY-MM-DD HH:MM' UTC format.</p>"), 400

        # If a slug is provided by user, validate it using utils.shortener.validate_custom_slug
        if slug:
            if not validate_custom_slug(slug):
                return render_layout("<h1>Invalid Slug</h1><p>Your custom slug contains invalid characters or is too long. Allowed: A-Z a-z 0-9 _ - (1-255 chars).</p>"), 400

            # Truncate defensively (DB constraint)
            if len(slug) > 255:
                slug = slug[:255]

            # Attempt direct creation (user-provided slug path)
            try:
                new_short = models.create_shorturl(session, user_id=user_id, target_url=target_url, slug=slug, is_custom=True, expire_at=expire_at)
            except models.DuplicateSlugError:
                # Suggest alternatives using suggestion utility
                try:
                    suggestions = suggest_alternatives(slug, count=5, length=8, session=session)
                except Exception:
                    suggestions = []
                sug_html = ""
                if suggestions:
                    for s in suggestions:
                        short_path = url_for("shortener.redirect_slug", slug=s, _external=True)
                        sug_html += f"<li>{s} -> <a href=\"{short_path}\">{short_path}</a></li>"
                    sug_html = "<ul>" + sug_html + "</ul>"
                else:
                    sug_html = "<p>No suggestions available. Try a different slug or leave blank to auto-generate.</p>"

                return render_layout(f"<h1>Slug Conflict</h1><p>The slug '{slug}' is already in use.</p><h2>Suggestions</h2>{sug_html}"), 400
            except IntegrityError as e:
                # Unexpected DB integrity error; rollback and report
                session.rollback()
                return render_layout(f"<h1>Database Error</h1><p>{str(e)}</p>"), 500
            except Exception as e:
                session.rollback()
                return render_layout(f"<h1>Error</h1><p>{str(e)}</p>"), 500

            # Success: show created short URL (accessible copy control)
            try:
                _write_trace_113(f"SHORTURL_CREATED user_id={user_id} slug={new_short.slug} id={new_short.id}")
            except Exception:
                pass

            # Prefer verified custom domain for owner if available
            custom_host = None
            try:
                custom_host = _get_user_verified_custom_host_db(session, user_id)
            except Exception:
                custom_host = None

            if custom_host:
                scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                short_path = f"{scheme}://{custom_host}/{new_short.slug}"
            else:
                short_path = url_for("shortener.redirect_slug", slug=new_short.slug, _external=True)

            # New: QR download link (PNG default) and SVG alternative
            qr_png_link = f"{short_path.rstrip('/')}/qr?format=png"
            qr_svg_link = f"{short_path.rstrip('/')}/qr?format=svg"

            html = f"""
            <h1>Short URL Created</h1>
            <p>Slug: <strong>{new_short.slug}</strong></p>
            <p>Target: <a href="{new_short.target_url}">{new_short.target_url}</a></p>
            <div class="shortlink-row" aria-label="Short link">
              <label for="shortlink-input">Short Link</label>
              <input id="shortlink-input" class="shortlink-input" type="text" value="{short_path}" readonly aria-readonly="true">
              <button id="copy-shortlink" class="copy-btn" type="button" aria-label="Copy short link" tabindex="0">Copy</button>
            </div>
            <p>QR Codes: <a href="{qr_png_link}" download>Download PNG</a> | <a href="{qr_svg_link}" download>Download SVG</a></p>
            <p>Owner (user_id): {new_short.user_id}</p>
            <script>
            (function(){{
              try {{
                var btn = document.getElementById('copy-shortlink');
                var input = document.getElementById('shortlink-input');
                if (btn && input) {{
                  btn.addEventListener('click', function(){{
                    try {{
                      if (navigator.clipboard && navigator.clipboard.writeText) {{
                        navigator.clipboard.writeText(input.value).then(function(){{
                          window.__smartlinkAnnounce('Short link copied to clipboard');
                          btn.textContent = 'Copied';
                          setTimeout(function(){{ btn.textContent = 'Copy'; }}, 2000);
                        }}, function(){{ fallbackCopy(); }});
                      }} else {{
                        fallbackCopy();
                      }}
                    }} catch (e) {{ fallbackCopy(); }}
                    function fallbackCopy() {{
                      try {{
                        input.select();
                        var ok = document.execCommand('copy');
                        window.__smartlinkAnnounce(ok ? 'Short link copied to clipboard' : 'Copy failed');
                        btn.textContent = ok ? 'Copied' : 'Copy';
                        setTimeout(function(){{ btn.textContent = 'Copy'; }}, 2000);
                      }} catch (e) {{
                        window.__smartlinkAnnounce('Copy not supported in this browser');
                      }}
                    }}
                  }});
                  // keyboard support: Enter and Space activate the copy button
                  btn.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter' || e.key === ' ') {{
                      e.preventDefault();
                      btn.click();
                    }}
                  }});
                }}
              }} catch (e) {{ /* Do not break page */ }}
            }})();
            </script>
            """
            return render_layout(html)

        # No slug provided: generate and atomically reserve a unique slug using find_unique_slug with reserve_callback.
        def _reserve_callback(candidate_slug: str, **kwargs):
            # models.create_shorturl signature: (session, user_id: int, target_url: str, slug: str, is_custom: bool = False, expire_at=None)
            # It will raise DuplicateSlugError on uniqueness conflict.
            return models.create_shorturl(session, user_id=user_id, target_url=target_url, slug=candidate_slug, is_custom=False, expire_at=expire_at)

        # Determine deterministic parameters if requested
        deterministic_source = None
        secret = None
        if deterministic:
            deterministic_source = target_url
            secret = current_app.config.get("JWT_SECRET") or current_app.config.get("SECRET_KEY") or ""

        try:
            found_slug = find_unique_slug(
                session=session,
                length=8,
                max_retries=10,
                deterministic_source=deterministic_source,
                secret=secret,
                reserve_callback=_reserve_callback,
                reserve_kwargs=None,
            )
        except UniqueSlugGenerationError as e:
            return render_layout(f"<h1>Slug Generation Failed</h1><p>{str(e)}</p>"), 500
        except Exception as e:
            # Unexpected error from reserve_callback (re-raise from find_unique_slug) or DB issues
            try:
                session.rollback()
            except Exception:
                pass
            return render_layout(f"<h1>Error</h1><p>{str(e)}</p>"), 500

        # Fetch the created row to display (create_shorturl should've returned and inserted row)
        short = session.query(models.ShortURL).filter_by(slug=found_slug, user_id=user_id).first()
        # It's possible the row exists but belongs to another user (shouldn't happen because create_shorturl set user_id),
        # but ensure we respect ID Filter when showing owner-specific info.
        if not short:
            # Fallback: try to fetch by slug alone (public) to display target if present
            public_short = session.query(models.ShortURL).filter_by(slug=found_slug).first()
            if public_short:
                # Prefer verified custom host if present
                custom_host = None
                try:
                    custom_host = _get_user_verified_custom_host_db(session, user_id)
                except Exception:
                    custom_host = None

                if custom_host:
                    scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                    short_path = f"{scheme}://{custom_host}/{found_slug}"
                else:
                    short_path = url_for("shortener.redirect_slug", slug=found_slug, _external=True)

                qr_png_link = f"{short_path.rstrip('/')}/qr?format=png"
                qr_svg_link = f"{short_path.rstrip('/')}/qr?format=svg"

                html = f"""
                <h1>Short URL Created (best-effort)</h1>
                <p>Slug: <strong>{found_slug}</strong></p>
                <p>Short Link: <a href="{short_path}">{short_path}</a></p>
                <p>QR Codes: <a href="{qr_png_link}" download>Download PNG</a> | <a href="{qr_svg_link}" download>Download SVG</a></p>
                <p>Note: The DB row was not found under your user_id after reservation; please check logs.</p>
                """
                return render_layout(html)
            else:
                custom_host = None
                try:
                    custom_host = _get_user_verified_custom_host_db(session, user_id)
                except Exception:
                    custom_host = None

                if custom_host:
                    scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                    short_path = f"{scheme}://{custom_host}/{found_slug}"
                else:
                    short_path = url_for("shortener.redirect_slug", slug=found_slug, _external=True)

                qr_png_link = f"{short_path.rstrip('/')}/qr?format=png"
                qr_svg_link = f"{short_path.rstrip('/')}/qr?format=svg"

                html = f"""
                <h1>Short URL Created</h1>
                <p>Slug: <strong>{found_slug}</strong></p>
                <p>Short Link: <a href="{short_path}">{short_path}</a></p>
                <p>QR Codes: <a href="{qr_png_link}" download>Download PNG</a> | <a href="{qr_svg_link}" download>Download SVG</a></p>
                <p>Note: The DB row was not found after reservation; please check logs.</p>
                """
                return render_layout(html)
        else:
            try:
                _write_trace_113(f"SHORTURL_CREATED user_id={user_id} slug={short.slug} id={short.id}")
            except Exception:
                pass

            # Prefer verified custom domain for owner if available
            custom_host = None
            try:
                custom_host = _get_user_verified_custom_host_db(session, user_id)
            except Exception:
                custom_host = None

            if custom_host:
                scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                short_path = f"{scheme}://{custom_host}/{short.slug}"
            else:
                short_path = url_for("shortener.redirect_slug", slug=short.slug, _external=True)

            qr_png_link = f"{short_path.rstrip('/')}/qr?format=png"
            qr_svg_link = f"{short_path.rstrip('/')}/qr?format=svg"

            html = f"""
            <h1>Short URL Created</h1>
            <p>Slug: <strong>{short.slug}</strong></p>
            <p>Target: <a href="{short.target_url}">{short.target_url}</a></p>
            <div class="shortlink-row" aria-label="Short link">
              <label for="shortlink-input">Short Link</label>
              <input id="shortlink-input" class="shortlink-input" type="text" value="{short_path}" readonly aria-readonly="true">
              <button id="copy-shortlink" class="copy-btn" type="button" aria-label="Copy short link" tabindex="0">Copy</button>
            </div>
            <p>QR Codes: <a href="{qr_png_link}" download>Download PNG</a> | <a href="{qr_svg_link}" download>Download SVG</a></p>
            <p>Owner (user_id): {short.user_id}</p>
            <script>
            (function(){{
              try {{
                var btn = document.getElementById('copy-shortlink');
                var input = document.getElementById('shortlink-input');
                if (btn && input) {{
                  btn.addEventListener('click', function(){{
                    try {{
                      if (navigator.clipboard && navigator.clipboard.writeText) {{
                        navigator.clipboard.writeText(input.value).then(function(){{
                          window.__smartlinkAnnounce('Short link copied to clipboard');
                          btn.textContent = 'Copied';
                          setTimeout(function(){{ btn.textContent = 'Copy'; }}, 2000);
                        }}, function(){{ fallbackCopy(); }});
                      }} else {{
                        fallbackCopy();
                      }}
                    }} catch (e) {{ fallbackCopy(); }}
                    function fallbackCopy() {{
                      try {{
                        input.select();
                        var ok = document.execCommand('copy');
                        window.__smartlinkAnnounce(ok ? 'Short link copied to clipboard' : 'Copy failed');
                        btn.textContent = ok ? 'Copied' : 'Copy';
                        setTimeout(function(){{ btn.textContent = 'Copy'; }}, 2000);
                      }} catch (e) {{
                        window.__smartlinkAnnounce('Copy not supported in this browser');
                      }}
                    }}
                  }});
                  // keyboard support: Enter and Space activate the copy button
                  btn.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter' || e.key === ' ') {{
                      e.preventDefault();
                      btn.click();
                    }}
                  }});
                }}
              }} catch (e) {{ /* Do not break page */ }}
            }})();
            </script>
            """
            return render_layout(html)
    finally:
        try:
            session.close()
        except Exception:
            pass


# -------------------------
# QR endpoint (KAN-146)
# -------------------------
@shortener_bp.route("/<slug>/qr", methods=["GET"])
def qr_for_slug(slug: str):
    """
    Public endpoint returning a QR image encoding the canonical short URL.

    Behavior:
      - Public lookup by slug.
      - If not found -> 404 HTML page (no image).
      - Query param: ?format=png|svg (case-insensitive). If absent, prefer Accept header:
            - If Accept contains 'image/svg+xml' and not 'image/png', choose svg
            - Otherwise, default to png.
      - Caching:
          - In-memory cache keyed by sha256(slug|short_url|fmt)
          - TTL configurable via app.config["QR_CACHE_TTL_SECONDS"] (default 3600)
          - Max items configurable via app.config["QR_CACHE_MAX_ITEMS"] (default 1024)
      - Conditional GET:
          - Supports If-None-Match using ETag (sha256 digest of image bytes)
          - Returns 304 when ETag matches
      - Response headers include Cache-Control, Expires, ETag, Content-Type
      - Traces requests to trace_KAN-146.txt
    """
    session = models.Session()
    try:
        # Lookup short URL
        short = session.query(models.ShortURL).filter_by(slug=slug).first()
        if not short:
            return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # Build canonical short URL: prefer owner's verified custom domain, else use external url_for
        try:
            custom_host = _get_user_verified_custom_host_db(session, short.user_id)
        except Exception:
            custom_host = None
        try:
            if custom_host:
                scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                canonical = f"{scheme}://{custom_host}/{slug}"
            else:
                # request.url_root is the host root of this request; use url_for to build absolute redirect URL
                canonical = url_for("shortener.redirect_slug", slug=slug, _external=True)
        except Exception:
            # fallback
            canonical = f"/{slug}"

        # Determine requested format
        fmt_q = (request.args.get("format") or "").strip().lower()
        accept = (request.headers.get("Accept") or "").lower()
        if fmt_q in ("png", "svg"):
            fmt = fmt_q
        else:
            # Heuristic: if Accept explicitly prefers svg, choose svg; else png
            if "image/svg+xml" in accept and "image/png" not in accept:
                fmt = "svg"
            else:
                fmt = "png"

        # Cache configuration
        ttl = int(current_app.config.get("QR_CACHE_TTL_SECONDS", 3600))
        max_items = int(current_app.config.get("QR_CACHE_MAX_ITEMS", 1024))

        # Compute cache key and check cache
        cache_key = _build_qr_cache_key(slug, canonical, fmt)
        entry = _qr_cache_get(cache_key)
        if entry:
            # Conditional GET support
            client_etag = request.headers.get("If-None-Match")
            if client_etag and client_etag.strip().strip('"') == entry.get("etag"):
                # Not modified
                _trace_146(f"QR_IF_NONE_MATCH 304 slug={slug} fmt={fmt}")
                resp = make_response("", 304)
                resp.headers["ETag"] = f'"{entry.get("etag")}"'
                return resp

            # Return cached bytes
            data = entry.get("data") or b""
            content_type = entry.get("content_type") or ("image/png" if fmt == "png" else "image/svg+xml")
            etag = entry.get("etag")
            resp = make_response(data)
            resp.headers["Content-Type"] = content_type
            resp.headers["Content-Length"] = str(len(data))
            resp.headers["Cache-Control"] = f"public, max-age={ttl}"
            resp.headers["Expires"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + ttl))
            resp.headers["ETag"] = f'"{etag}"'
            _trace_146(f"QR_CACHE_HIT slug={slug} fmt={fmt} etag={etag}")
            return resp

        # Not cached: generate
        try:
            img_bytes, content_type = _generate_qr_bytes(canonical, fmt=fmt)
        except Exception as e:
            _trace_146(f"QR_GEN_ERROR slug={slug} err={str(e)}")
            img_bytes, content_type = b"", "application/octet-stream"

        # Ensure non-empty payload per acceptance criteria; fallback placeholder ensures some bytes for SVG
        if not img_bytes:
            # If generation failed completely, return service unavailable
            _trace_146(f"QR_EMPTY_GENERATION slug={slug} fmt={fmt}")
            return render_layout("<h1>QR Generation Unavailable</h1><p>Unable to generate QR code at this time.</p>"), 503

        # Compute etag & cache
        _qr_entry = _qr_cache_set(cache_key, img_bytes, content_type, ttl_seconds=ttl, max_items=max_items)
        etag = _qr_entry.get("etag")

        # Conditional GET: If client provided If-None-Match that equals the new ETag, return 304
        client_etag = request.headers.get("If-None-Match")
        if client_etag and client_etag.strip().strip('"') == etag:
            _trace_146(f"QR_GEN_IF_NONE_MATCH 304 slug={slug} fmt={fmt} etag={etag}")
            resp = make_response("", 304)
            resp.headers["ETag"] = f'"{etag}"'
            return resp

        # Return the generated image bytes
        resp = make_response(img_bytes)
        resp.headers["Content-Type"] = content_type
        resp.headers["Content-Length"] = str(len(img_bytes))
        resp.headers["Cache-Control"] = f"public, max-age={ttl}"
        resp.headers["Expires"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + ttl))
        resp.headers["ETag"] = f'"{etag}"'

        _trace_146(f"QR_SERVED slug={slug} fmt={fmt} etag={etag} cache_key={cache_key}")
        return resp
    finally:
        try:
            session.close()
        except Exception:
            pass


# -------------------------
# Public redirect route (KAN-114 + KAN-115 click tracking + KAN-119 expiry handling + KAN-126 rate-limit)
# -------------------------
@shortener_bp.route("/<slug>", methods=["GET"])
@rate_limit_ip(capacity=120, window_seconds=60)  # per-IP redirect rate: default 120 per 60s
def redirect_public_root(slug):
    session = models.Session()
    try:
        # Public lookup by slug
        short = session.query(models.ShortURL).filter_by(slug=slug).first()
        if not short:
            return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # Expiry / inactive check: consider both expire_at and is_active flag
        now = datetime.utcnow()
        is_expired = False
        try:
            if (short.expire_at is not None) and (short.expire_at < now):
                is_expired = True
            if getattr(short, "is_active", True) is False:
                is_expired = True
        except Exception:
            # Defensive: if any attribute access fails, prefer to treat as expired to be safe
            is_expired = True

        if is_expired:
            try:
                code = int(current_app.config.get("EXPIRED_REDIRECT_STATUS", 410))
            except Exception:
                code = 410
            # Do NOT record ClickEvent per acceptance criteria
            try:
                _write_trace_119(f"REDIRECT_BLOCKED_EXPIRED slug={slug} short_id={short.id} expire_at={short.expire_at} is_active={getattr(short, 'is_active', None)}")
            except Exception:
                pass
            return render_layout("<h1>Link Expired</h1><p>This short link has expired.</p>"), code

        # Normalize/validate target using canonical function; if invalid, treat as not found (do not record click)
        try:
            normalized_target = validate_and_normalize_url(short.target_url)
        except Exception:
            # Defensive: do not expose internal error details
            return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        # Resolve anonymized IP using utils.security.anonymize_ip; be tolerant of missing module.
        try:
            from utils.security import anonymize_ip as _anonymize_ip
        except Exception:
            # Fallback anonymizer (conservative, minimal). Keeps last octet zeroed for IPv4,
            # zeros last hextet for IPv6-ish strings. This is a defensive fallback.
            def _anonymize_ip(remote_addr: str = None, x_forwarded_for: str = None, trust_xff: bool = False) -> str:
                ip = ""
                if trust_xff and x_forwarded_for:
                    # Take the left-most entry in X-Forwarded-For as client IP
                    try:
                        ip = x_forwarded_for.split(",")[0].strip()
                    except Exception:
                        ip = x_forwarded_for or remote_addr or ""
                else:
                    ip = remote_addr or ""

                if not ip:
                    return ""

                # IPv4 mask
                if ip.count(".") == 3:
                    parts = ip.split(".")
                    try:
                        parts[-1] = "0"
                        return ".".join(parts)
                    except Exception:
                        return ip
                # IPv6-ish mask (very conservative)
                if ":" in ip:
                    parts = ip.split(":")
                    try:
                        parts[-1] = "0000"
                        return ":".join(parts)
                    except Exception:
                        return ip
                return ip

        # Determine whether to trust X-Forwarded-For
        trust_xff = bool(current_app.config.get("TRUST_X_FORWARDED_FOR", False))
        xff = request.headers.get("X-Forwarded-For", "")
        remote_addr = request.remote_addr or ""
        anonymized = ""
        try:
            anonymized = _anonymize_ip(remote_addr=remote_addr, x_forwarded_for=xff, trust_xff=trust_xff)
        except Exception:
            anonymized = ""

        # Per acceptance criteria: if anonymized IP is empty/malformed -> persist NULL
        anonymized_db_val = anonymized if anonymized else None

        # Prepare ClickEvent
        try:
            click = models.ClickEvent(
                short_url_id=short.id,
                anonymized_ip=anonymized_db_val,
                user_agent=(request.headers.get("User-Agent", "") or None)[:2000],
                referrer=(request.headers.get("Referer", "") or None)[:2000],
                occurred_at=datetime.utcnow(),
            )
            # Persist click and (optionally) increment hit_count atomically within session
            session.add(click)
            try:
                # Increment hit_count in-memory
                short.hit_count = (short.hit_count or 0) + 1
                session.add(short)
            except Exception:
                # If increment fails for any reason, do not block click recording
                pass

            session.commit()
            # Trace after successful commit
            try:
                _write_trace_115(f"CLICK_PERSISTED slug={slug} short_id={short.id} occurred_at={click.occurred_at} anonymized_ip={anonymized_db_val}")
            except Exception:
                pass
        except Exception:
            # Defensive: on any DB error, rollback and continue with redirect (do not block user experience)
            try:
                session.rollback()
            except Exception:
                pass

        # Architectural trace
        try:
            _write_trace_114(f"SHORTURL_REDIRECT slug={slug} short_id={short.id} target={normalized_target} anonymized_ip={anonymized}")
        except Exception:
            pass

        # Redirect using configured status code
        try:
            code = int(current_app.config.get("REDIRECT_STATUS_CODE", 302))
        except Exception:
            code = 302
        return redirect(normalized_target, code=code)
    finally:
        try:
            session.close()
        except Exception:
            pass


# -------------------------
# Legacy /s/<slug> redirect (unchanged behavior)
# -------------------------
@shortener_bp.route("/s/<slug>", methods=["GET"])
def redirect_slug(slug):
    """
    Public redirection endpoint (legacy /s/<slug>). Finds ShortURL by slug and redirects to target_url.
    - Does not expose owner information.
    - Enforces expire_at (if set) and is_active flag; returns configured expired status if expired or not found.
    """
    session = models.Session()
    try:
        # Query by slug (public lookup)
        short = session.query(models.ShortURL).filter_by(slug=slug).first()
        if not short:
            return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        now = datetime.utcnow()
        is_expired = False
        try:
            if (short.expire_at is not None) and (short.expire_at < now):
                is_expired = True
            if getattr(short, "is_active", True) is False:
                is_expired = True
        except Exception:
            is_expired = True

        if is_expired:
            try:
                code = int(current_app.config.get("EXPIRED_REDIRECT_STATUS", 410))
            except Exception:
                code = 410
            try:
                _write_trace_119(f"LEGACY_REDIRECT_BLOCKED_EXPIRED slug={slug} short_id={short.id} expire_at={short.expire_at} is_active={getattr(short, 'is_active', None)}")
            except Exception:
                pass
            return render_layout("<h1>Link Expired</h1><p>This short link has expired.</p>"), code

        try:
            _write_trace_114(f"SHORTURL_REDIRECT_LEGACY slug={slug} target_url={short.target_url}")
        except Exception:
            pass

        try:
            # Attempt to normalize target prior to redirect; if validation fails, return 404
            normalized = validate_and_normalize_url(short.target_url)
        except Exception:
            return render_layout("<h1>Not Found</h1><p>The requested link does not exist.</p>"), 404

        return redirect(normalized, code=302)
    finally:
        try:
            session.close()
        except Exception:
            pass


# -------------------------
# Utility demo listing endpoint (unchanged)
# -------------------------
@shortener_bp.route("/shorten/list", methods=["GET"])
def list_user_links():
    """
    Demo endpoint to list short URLs for a given user_id (simulates authenticated user's dashboard).
    Requires query param: user_id
    Shows canonical usage of ID Filter when querying user-owned resources.
    """
    try:
        user_id = int(request.args.get("user_id", "0"))
    except Exception:
        return render_layout("<p>Invalid or missing user_id query parameter.</p>"), 400

    session = models.Session()
    try:
        user = session.query(models.User).filter_by(id=user_id).first()
        if not user:
            return render_layout("<p>User not found.</p>"), 404

        items = session.query(models.ShortURL).filter_by(user_id=user_id).order_by(models.ShortURL.created_at.desc()).all()
        rows_html = "<ul>"
        for i in items:
            short_path = url_for("shortener.redirect_slug", slug=i.slug, _external=True)
            rows_html += f"<li>{i.slug} -> <a href=\"{i.target_url}\">{i.target_url}</a> (short: <a href=\"{short_path}\">{short_path}</a>)</li>"
        rows_html += "</ul>"
        return render_layout(f"<h1>Short URLs for user {user_id}</h1>{rows_html}")
    finally:
        try:
            session.close()
        except Exception:
            pass
--- END FILE ---