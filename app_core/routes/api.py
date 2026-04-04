"""routes/api.py - Programmatic API endpoints (KAN-145)

Provides:
 - POST /api/shorten : create a short URL programmatically using an API key

Design:
 - Auth via API Key (Authorization: Bearer <key>, X-API-Key header, or ?api_key= query param)
 - Decorator @api_key_required sets g.api_key and g.api_user (lightweight), writes trace_KAN-145.txt
 - Enforces per-key rate limit (capacity/window_seconds) using utils.rate_limit token-buckets (best-effort)
 - Accepts JSON payload and returns JSON responses, including rate-limit headers on both success and 429
 - Reuses models.create_shorturl and utils.validation.validate_and_normalize_url for core logic
 - On slug conflicts returns 400 with suggestions
 - Writes best-effort traces to trace_KAN-145.txt for Architectural Memory
"""

from flask import Blueprint, request, jsonify, g, current_app, make_response
import time
import math
from datetime import datetime
import models

api_bp = Blueprint("api", __name__)

TRACE_FILE = "trace_KAN-145.txt"

def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


# Defensive imports of helpers we rely on; fall back where reasonable
try:
    from utils.validation import validate_and_normalize_url
except Exception:
    # fallback to shortener module's validator if utils.validation absent
    try:
        from utils.shortener import validate_and_normalize_url  # type: ignore
    except Exception:
        validate_and_normalize_url = None  # handler will defensively error

try:
    from utils.shortener import suggest_alternatives
except Exception:
    def suggest_alternatives(base_slug, count=5, length=8, session=None):
        return [f"{base_slug}-{i}" for i in range(1, count + 1)]


# Use existing rate_limit module internals for token buckets; defensive about availability
try:
    from utils import rate_limit as rl_mod  # type: ignore
    _bucket_manager = getattr(rl_mod, "_bucket_manager", None)
    _persist_to_db = getattr(rl_mod, "_persist_to_db", None)
except Exception:
    _bucket_manager = None
    _persist_to_db = None


def _build_rate_limit_headers(capacity: float, remaining: float, reset_ts: int):
    try:
        return {
            "X-RateLimit-Limit": str(int(math.floor(capacity))),
            "X-RateLimit-Remaining": str(max(0, int(math.floor(remaining)))),
            "X-RateLimit-Reset": str(int(reset_ts)),
        }
    except Exception:
        return {}


def _get_api_key_from_request():
    """
    Extract API key from Authorization header (Bearer), X-API-Key header, or api_key query param.
    Returns the raw key string or None.
    """
    # Try Authorization header: Bearer <key>
    auth = request.headers.get("Authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    # X-API-Key header
    xk = request.headers.get("X-API-Key") or request.headers.get("X-Api-Key")
    if xk:
        return xk.strip()
    # Query param
    q = request.args.get("api_key") or request.values.get("api_key")
    if q:
        return q.strip()
    return None


def _get_user_verified_custom_host(session, user_id: int):
    """
    Small helper to prefer the user's verified custom domain similar to routes.shortener's helper.
    """
    try:
        cd = session.query(models.CustomDomain).filter_by(owner_id=user_id, is_verified=True).order_by(models.CustomDomain.created_at.desc()).first()
        if cd and getattr(cd, "domain", None):
            return cd.domain.lower()
    except Exception:
        pass
    return None


def _apply_bucket(key_str: str, capacity: int, window_seconds: int, cost: float = 1.0):
    """
    Attempt to consume a token from in-memory bucket managed by utils.rate_limit.
    Returns (allowed: bool, headers: dict).
    Side-effect: persist snapshot to DB via rl_mod._persist_to_db best-effort when available.
    """
    # Defensive defaults
    if capacity is None or capacity <= 0:
        capacity = int(current_app.config.get("API_DEFAULT_RATE_LIMIT_CAPACITY", 60))
    if window_seconds is None or window_seconds <= 0:
        window_seconds = int(current_app.config.get("API_DEFAULT_RATE_LIMIT_WINDOW_SECONDS", 60))

    try:
        if _bucket_manager is None:
            # No in-process manager available -> fallback to allowing (conservative: allow) but trace
            _trace(f"RATE_LIMIT_UNAVAILABLE key={key_str} allow_by_default")
            headers = _build_rate_limit_headers(capacity, capacity, int(time.time() + window_seconds))
            return True, headers

        refill_rate = float(capacity) / float(window_seconds)
        allowed, remaining, cap, refill_rate_used, reset_ts = _bucket_manager.consume(key_str, capacity, refill_rate, cost=cost)

        # best-effort persistence
        try:
            snap = _bucket_manager.snapshot(key_str)
            if snap and _persist_to_db is not None:
                try:
                    _persist_to_db(key=key_str, tokens=snap["tokens"], last_refill_ts=time.time())
                except Exception:
                    pass
        except Exception:
            pass

        headers = _build_rate_limit_headers(capacity, remaining, reset_ts)
        return bool(allowed), headers
    except Exception as e:
        _trace(f"RATE_LIMIT_ERROR key={key_str} err={str(e)}")
        # On internal errors prefer conservative behavior (allow) but still return nominal headers
        headers = _build_rate_limit_headers(capacity, capacity, int(time.time() + window_seconds))
        return True, headers


def api_key_required(func):
    """
    Decorator to authenticate requests using an API key.

    Sets:
      - g.api_key -> models.APIKey instance (DB-backed) if available
      - g.api_user -> lightweight object with 'id' and 'email' attributes for downstream code that expects g.current_user-like structure.
      - g.api_user_id -> integer owner id

    Responses:
      - 401 JSON on missing/invalid key
      - 403 if key revoked
      - 429 if key rate-limited (with rate-limit headers)
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        key_raw = _get_api_key_from_request()
        if not key_raw:
            _trace(f"API_AUTH_MISSING remote={request.remote_addr} path={request.path}")
            return jsonify({"error": "missing_api_key"}), 401

        session = models.Session()
        try:
            # Owner-scoped fetch not needed here; we simply look up key by its string
            api_key = session.query(models.APIKey).filter_by(key=key_raw).first()
            if not api_key:
                _trace(f"API_AUTH_INVALID provided_key_preview={(key_raw[:8] + '...') if len(key_raw) > 8 else key_raw} remote={request.remote_addr}")
                return jsonify({"error": "invalid_api_key"}), 401

            if getattr(api_key, "revoked", False):
                _trace(f"API_AUTH_REVOKED api_key_id={api_key.id} user_id={api_key.user_id}")
                return jsonify({"error": "api_key_revoked"}), 403

            # Set g.api_key & g.api_user (lightweight)
            g.api_key = api_key
            # set a small light user object with id & email to enable code that expects g.current_user-like shape
            class _ApiUserObj:
                def __init__(self, uid, email):
                    self.id = uid
                    self.email = email
            try:
                user = session.query(models.User).filter_by(id=api_key.user_id).first()
                uid = user.id if user else int(api_key.user_id)
                email = user.email if user and getattr(user, "email", None) else ""
            except Exception:
                uid = int(api_key.user_id)
                email = ""
            g.api_user = _ApiUserObj(uid, email)
            # For compatibility with "ID Filter" patterns, also populate g.api_user_id
            g.api_user_id = int(api_key.user_id)

            # Apply per-key rate limit: key name we use is "api_key:<id>"
            key_name = f"api_key:{api_key.id}"
            cap = int(api_key.rate_limit_capacity) if getattr(api_key, "rate_limit_capacity", None) else int(current_app.config.get("API_DEFAULT_RATE_LIMIT_CAPACITY", 60))
            win = int(api_key.rate_limit_window_seconds) if getattr(api_key, "rate_limit_window_seconds", None) else int(current_app.config.get("API_DEFAULT_RATE_LIMIT_WINDOW_SECONDS", 60))
            allowed, headers = _apply_bucket(key_name, cap, win, cost=1.0)
            # Attach headers to response in case of both allowed and blocked outcomes
            if not allowed:
                _trace(f"API_RATE_LIMIT_EXCEEDED api_key_id={api_key.id} user_id={api_key.user_id} cap={cap} window={win}")
                resp = make_response(jsonify({"error": "rate_limited"}), 429)
                for hk, hv in headers.items():
                    resp.headers[hk] = hv
                return resp
            # Save the headers on g for handlers to attach (optional)
            g.rate_limit_headers = headers
            _trace(f"API_AUTH_SUCCESS api_key_id={api_key.id} user_id={api_key.user_id} remote={request.remote_addr}")
            return func(*args, **kwargs)
        finally:
            try:
                session.close()
            except Exception:
                pass
    return wrapper


@api_bp.route("/shorten", methods=["POST"])
@api_key_required
def api_shorten():
    """
    POST /api/shorten
    Content-Type: application/json
    Request JSON:
      {
        "target_url": "https://example.com/...",
        "slug": "optional-custom-slug",
        "is_custom": true|false,
        "expire_at": "2026-01-01T12:00:00Z"    # optional ISO-8601 string or 'YYYY-MM-DD HH:MM' server-legacy format
      }

    Responses:
      - 201 Created with JSON on success:
          {
            "id": 123,
            "slug": "abcd1234",
            "short_url": "https://.../abcd1234",
            "target_url": "https://example.com/..."
          }
      - 400 Bad Request for invalid payload / duplicate slug (includes suggestions)
      - 401/403 for auth problems (handled earlier)
      - 429 if rate-limited (headers present)
    """
    # Attach best-effort rate-limit headers if present from decorator
    try:
        rl_headers = getattr(g, "rate_limit_headers", {}) or {}
    except Exception:
        rl_headers = {}

    # Parse JSON body
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        resp = make_response(jsonify({"error": "invalid_json"}), 400)
        for k, v in rl_headers.items():
            resp.headers[k] = v
        return resp

    target_raw = (data.get("target_url") or "").strip()
    if not target_raw:
        resp = make_response(jsonify({"error": "missing_target_url"}), 400)
        for k, v in rl_headers.items():
            resp.headers[k] = v
        return resp

    # Normalize target using canonical validator
    if validate_and_normalize_url is None:
        resp = make_response(jsonify({"error": "server_validation_unavailable"}), 500)
        for k, v in rl_headers.items():
            resp.headers[k] = v
        return resp

    try:
        normalized_target = validate_and_normalize_url(target_raw)
    except Exception as e:
        resp = make_response(jsonify({"error": "invalid_target_url", "detail": str(e)}), 400)
        for k, v in rl_headers.items():
            resp.headers[k] = v
        return resp

    # Slug & flags
    slug = (data.get("slug") or "").strip()
    is_custom = bool(data.get("is_custom", False))
    expire_at_raw = (data.get("expire_at") or "").strip()
    expire_at = None
    if expire_at_raw:
        # Try ISO first then fallback to 'YYYY-MM-DD HH:MM'
        try:
            # Python 3.11+ supports fromisoformat for many ISO forms; this will accept "YYYY-MM-DDTHH:MM:SS" etc.
            expire_at = datetime.fromisoformat(expire_at_raw)
        except Exception:
            try:
                expire_at = datetime.strptime(expire_at_raw, "%Y-%m-%d %H:%M")
            except Exception:
                resp = make_response(jsonify({"error": "invalid_expire_at", "detail": "expected ISO-8601 or 'YYYY-MM-DD HH:MM'"}), 400)
                for k, v in rl_headers.items():
                    resp.headers[k] = v
                return resp

    # Create using models.create_shorturl and a DB session
    session = models.Session()
    try:
        api_key = getattr(g, "api_key", None)
        if not api_key:
            resp = make_response(jsonify({"error": "unauthenticated"}), 401)
            for k, v in rl_headers.items():
                resp.headers[k] = v
            return resp

        user_id = int(api_key.user_id)

        # If client provided slug and flagged as custom, validate then attempt direct create
        if slug:
            # We rely on models.create_shorturl to check uniqueness (DuplicateSlugError)
            try:
                new_short = models.create_shorturl(session, user_id=user_id, target_url=normalized_target, slug=slug, is_custom=bool(is_custom), expire_at=expire_at)
            except models.DuplicateSlugError:
                # Provide suggestions
                try:
                    suggestions = suggest_alternatives(slug, count=5, length=8, session=session)
                except Exception:
                    suggestions = []
                resp_body = {"error": "slug_conflict", "slug": slug, "suggestions": suggestions}
                resp = make_response(jsonify(resp_body), 400)
                for k, v in rl_headers.items():
                    resp.headers[k] = v
                _trace(f"API_SHORTEN_SLUG_CONFLICT api_key_id={api_key.id} user_id={user_id} slug={slug} suggestions={suggestions}")
                return resp
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                resp = make_response(jsonify({"error": "db_error", "detail": str(e)}), 500)
                for k, v in rl_headers.items():
                    resp.headers[k] = v
                _trace(f"API_SHORTEN_DB_ERROR api_key_id={api_key.id} user_id={user_id} err={str(e)}")
                return resp

            # Success response
            # Prefer verified custom domain for owner if available (for full short_url construction)
            custom_host = _get_user_verified_custom_host(session, user_id)
            if custom_host:
                scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
                short_path = f"{scheme}://{custom_host}/{new_short.slug}"
            else:
                try:
                    # build absolute URL using request.host_url as fallback
                    host_url = request.url_root.rstrip("/")
                    short_path = f"{host_url}/{new_short.slug}"
                except Exception:
                    short_path = new_short.slug

            body = {
                "id": new_short.id,
                "slug": new_short.slug,
                "short_url": short_path,
                "target_url": new_short.target_url,
            }
            resp = make_response(jsonify(body), 201)
            for k, v in rl_headers.items():
                resp.headers[k] = v
            _trace(f"API_SHORTEN_CREATED api_key_id={api_key.id} user_id={user_id} short_id={new_short.id} slug={new_short.slug}")
            return resp

        # Auto-generated slug path: use deterministic option if provided by payload (not required by spec)
        # Use models.create_shorturl via an atomic reservation callback is the most robust approach.
        # For simplicity here, try find a unique slug by using a simple generate/reserve loop via models.create_shorturl.
        # We'll use a conservative approach: try a few randomized attempts
        from utils.shortener import generate_slug, UniqueSlugGenerationError  # defensive import

        attempt = 0
        max_attempts = 8
        created = None
        last_err = None
        while attempt < max_attempts and created is None:
            cand = None
            # Deterministic hint: if client requested 'deterministic' flag use hashed source
            if data.get("deterministic"):
                # Use HMAC-like deterministic slug if utils.shortener supports it; else fallback to generate_slug without deterministic_source
                try:
                    cand = generate_slug(length=8, deterministic_source=normalized_target, secret=current_app.config.get("JWT_SECRET", ""))
                except Exception:
                    cand = generate_slug(length=8)
            else:
                cand = generate_slug(length=8)

            try:
                created = models.create_shorturl(session, user_id=user_id, target_url=normalized_target, slug=cand, is_custom=False, expire_at=expire_at)
                break
            except models.DuplicateSlugError as e:
                last_err = e
                attempt += 1
                continue
            except Exception as e:
                # DB or unexpected error -> abort
                try:
                    session.rollback()
                except Exception:
                    pass
                resp = make_response(jsonify({"error": "db_error", "detail": str(e)}), 500)
                for k, v in rl_headers.items():
                    resp.headers[k] = v
                _trace(f"API_SHORTEN_DB_ERROR_CAND api_key_id={api_key.id} user_id={user_id} err={str(e)}")
                return resp

        if created is None:
            # After retries, return helpful message and suggestions
            suggestions = []
            try:
                suggestions = suggest_alternatives("link", count=5, session=session)
            except Exception:
                suggestions = []
            resp = make_response(jsonify({"error": "slug_generation_failed", "detail": "unable to generate unique slug", "suggestions": suggestions}), 500)
            for k, v in rl_headers.items():
                resp.headers[k] = v
            _trace(f"API_SHORTEN_GENERATION_FAILED api_key_id={api_key.id} user_id={user_id} attempts={attempt} last_err={str(last_err)}")
            return resp

        # Success: created contains the new ShortURL
        custom_host = _get_user_verified_custom_host(session, user_id)
        if custom_host:
            scheme = current_app.config.get("CUSTOM_DOMAIN_DEFAULT_SCHEME", "https")
            short_path = f"{scheme}://{custom_host}/{created.slug}"
        else:
            try:
                host_url = request.url_root.rstrip("/")
                short_path = f"{host_url}/{created.slug}"
            except Exception:
                short_path = created.slug

        body = {
            "id": created.id,
            "slug": created.slug,
            "short_url": short_path,
            "target_url": created.target_url,
        }
        resp = make_response(jsonify(body), 201)
        for k, v in rl_headers.items():
            resp.headers[k] = v
        _trace(f"API_SHORTEN_CREATED_AUTO api_key_id={api_key.id} user_id={user_id} short_id={created.id} slug={created.slug}")
        return resp
    finally:
        try:
            session.close()
        except Exception:
            pass

Notes & guardrails
- All database operations filter ownership by providing user_id from the API key (ID Filter rule).
- No HTML responses are returned; JSON only.
- CSRF is not required for JSON programmatic endpoints (API keys serve auth).
- The decorator writes trace_KAN-145.txt entries for auth/limit/create events per Architectural Memory mandate.
--- END FILE ---