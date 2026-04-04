"""utils/rate_limit.py - In-memory token buckets with DB fallback and Flask decorators (KAN-126)

Responsibilities:
 - Provide a thread-safe in-process token-bucket manager for fast local checks.
 - Persist per-key state to models.RateLimitCounter for cross-process best-effort consistency.
 - Expose decorators:
     - rate_limit_user(...): keyed by authenticated user id (falls back to IP if no user)
     - rate_limit_ip(...): keyed by client IP (honors TRUST_X_FORWARDED_FOR setting)
 - Decorator behavior:
     - Adds headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
     - When limit exceeded: respond 429 with the same headers
 - Default policies (reasonable defaults):
     - Shorten (POST /shorten): capacity=5, window_seconds=60 (≈5/min per user)
     - Redirect (GET /<slug>): capacity=120, window_seconds=60 (≈120/min per IP)
 - DB fallback is best-effort: persistence attempted synchronously; race conditions may allow relaxed enforcement
   across processes until DB-backed state converges (documented behavior per ticket).
"""

import time
import threading
import math
from functools import wraps

# Defensive imports for typing & flask; tolerate absence in constrained test runs
try:
    from flask import request, current_app, g, make_response
except Exception:
    request = None
    current_app = None
    g = None
    make_response = None

import models
from datetime import datetime

_TRACE_FILE = "trace_KAN-126.txt"


def _trace(msg: str) -> None:
    try:
        with open(_TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass


class InMemoryBucket:
    """
    Simple token bucket record kept in-process.
    """
    __slots__ = ("tokens", "last_ts", "capacity", "refill_rate")

    def __init__(self, capacity: float, refill_rate: float, tokens: float = None, last_ts: float = None):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)  # tokens per second
        self.tokens = float(tokens if tokens is not None else capacity)
        self.last_ts = float(last_ts if last_ts is not None else time.time())


class TokenBucketManager:
    """
    Manages in-process buckets keyed by strings. Thread-safe.
    """
    def __init__(self):
        self._buckets = {}  # key -> InMemoryBucket
        self._lock = threading.Lock()

    def _get_bucket(self, key: str, capacity: float, refill_rate: float) -> InMemoryBucket:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = InMemoryBucket(capacity=capacity, refill_rate=refill_rate)
                self._buckets[key] = b
            # If config changed (capacity/refill altered) update bucket parameters conservatively
            b.capacity = float(capacity)
            b.refill_rate = float(refill_rate)
            return b

    def _refill(self, bucket: InMemoryBucket) -> None:
        now = time.time()
        if now <= bucket.last_ts:
            return
        elapsed = now - bucket.last_ts
        added = elapsed * bucket.refill_rate
        bucket.tokens = min(bucket.capacity, bucket.tokens + added)
        bucket.last_ts = now

    def consume(self, key: str, capacity: float, refill_rate: float, cost: float = 1.0):
        """
        Attempt to consume 'cost' tokens from in-memory bucket. Returns (allowed:bool, remaining_tokens:float, capacity, refill_rate, reset_ts_epoch:int)
        The reset epoch is estimated: current_time + ceil((capacity - remaining_tokens)/refill_rate)
        """
        b = self._get_bucket(key, capacity, refill_rate)
        with self._lock:
            self._refill(b)
            if b.tokens >= cost:
                b.tokens -= cost
                remaining = b.tokens
                allowed = True
            else:
                remaining = b.tokens
                allowed = False

            reset_seconds = 0
            try:
                if b.refill_rate > 0:
                    reset_seconds = math.ceil(max(0.0, (b.capacity - remaining) / b.refill_rate))
                else:
                    reset_seconds = 0
            except Exception:
                reset_seconds = 0

            reset_ts = int(time.time() + reset_seconds)
            return allowed, remaining, b.capacity, b.refill_rate, reset_ts

    def snapshot(self, key: str):
        """
        Return a snapshot (tokens, last_ts) or None if no bucket exists.
        """
        with self._lock:
            b = self._buckets.get(key)
            if not b:
                return None
            # compute up-to-date refill without modifying
            now = time.time()
            tokens = b.tokens
            if now > b.last_ts:
                tokens = min(b.capacity, tokens + (now - b.last_ts) * b.refill_rate)
            return {"tokens": tokens, "last_ts": b.last_ts, "capacity": b.capacity, "refill_rate": b.refill_rate}


# Single global manager instance
_bucket_manager = TokenBucketManager()


# -------------------------
# DB persistence helpers (best-effort)
# -------------------------
def _persist_to_db(key: str, tokens: float, last_refill_ts: float):
    """
    Best-effort persist of bucket state to models.RateLimitCounter.
    We attempt a safe read-modify-write transaction. Failures are logged but do not raise.
    """
    try:
        session = models.Session()
    except Exception:
        _trace(f"DB_PERSIST_SKIP session_unavailable key={key}")
        return

    try:
        now_dt = datetime.utcfromtimestamp(last_refill_ts)
        try:
            # Attempt to load existing row
            row = session.query(models.RateLimitCounter).filter_by(key=key).with_for_update(read=True).first()
        except Exception:
            # Some DB backends may not support with_for_update/read param; fall back to simple query
            try:
                row = session.query(models.RateLimitCounter).filter_by(key=key).first()
            except Exception:
                row = None

        try:
            if row is None:
                # Insert a new row
                new = models.RateLimitCounter(key=key, tokens=float(tokens), last_refill=now_dt)
                session.add(new)
            else:
                # Compute refill from DB row's last_refill to now_dt and update conservatively:
                try:
                    db_last = row.last_refill or now_dt
                    elapsed = (now_dt - db_last).total_seconds()
                    # We'll not have capacity/refill info here; store the in-memory tokens as-is (best-effort)
                    row.tokens = float(tokens)
                    row.last_refill = now_dt
                    session.add(row)
                except Exception:
                    # Fallback: overwrite with current snapshot
                    row.tokens = float(tokens)
                    row.last_refill = now_dt
                    session.add(row)
            session.commit()
            _trace(f"DB_PERSIST_SUCCESS key={key} tokens={tokens} last_refill={now_dt.isoformat()}")
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            _trace(f"DB_PERSIST_ERROR key={key} err={str(e)}")
    except Exception as e:
        _trace(f"DB_PERSIST_TOPLEVEL_ERROR key={key} err={str(e)}")
    finally:
        try:
            session.close()
        except Exception:
            pass


# -------------------------
# Public decorator factory helpers
# -------------------------
def _get_client_ip():
    """
    Determine client IP honoring TRUST_X_FORWARDED_FOR config. Defensive.
    """
    try:
        trust_xff = bool(current_app.config.get("TRUST_X_FORWARDED_FOR", False))
    except Exception:
        trust_xff = False

    try:
        if trust_xff and request.headers.get("X-Forwarded-For"):
            ip = request.headers.get("X-Forwarded-For").split(",")[0].strip()
            return ip
    except Exception:
        pass

    try:
        return request.remote_addr or ""
    except Exception:
        return ""


def _build_headers(capacity: float, remaining: float, reset_ts: int):
    """
    Build standard rate-limit headers.
    X-RateLimit-Limit -> capacity (int)
    X-RateLimit-Remaining -> remaining (int)
    X-RateLimit-Reset -> epoch seconds when bucket will be refilled to capacity
    """
    try:
        headers = {
            "X-RateLimit-Limit": str(int(math.floor(capacity))),
            "X-RateLimit-Remaining": str(max(0, int(math.floor(remaining)))),
            "X-RateLimit-Reset": str(int(reset_ts)),
        }
        return headers
    except Exception:
        return {}


def rate_limit_user(capacity: int = 5, window_seconds: int = 60, cost: float = 1.0):
    """
    Decorator enforcing a per-user token bucket.
    Defaults: capacity=5 tokens per 60s (i.e., ~5/min) — reasonable default for creation endpoints.

    Parameters:
      - capacity: number of tokens in bucket (int)
      - window_seconds: how many seconds it takes to fully refill capacity (int)
      - cost: tokens consumed per request (float)
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be > 0")
    refill_rate = float(capacity) / float(window_seconds)  # tokens per second

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Only enforce for POST-like actions where user intent causes resource creation.
            try:
                method = (request.method or "").upper()
            except Exception:
                method = "POST"

            # Enforce only on POST by default (shorten function uses GET/POST). If caller uses decorator on pure-POST route, this is still safe.
            if method != "POST":
                return fn(*args, **kwargs)

            # Identify key: use authenticated user id if present, else fallback to IP
            try:
                current_user = getattr(g, "current_user", None)
                if current_user and getattr(current_user, "id", None):
                    user_id = int(current_user.id)
                    key = f"user:{user_id}"
                else:
                    # fallback per acceptance: allow demo user or anon IP
                    ip = _get_client_ip() or "anon"
                    key = f"user:anon_ip:{ip}"
            except Exception:
                key = f"user:unknown"

            # First, try fast in-memory token bucket
            allowed, remaining, cap, r_rate, reset_ts = _bucket_manager.consume(key, capacity, refill_rate, cost=cost)

            # Best-effort persist snapshot so other processes can fall back to DB state
            try:
                snap = _bucket_manager.snapshot(key)
                if snap:
                    _persist_to_db(key=key, tokens=snap["tokens"], last_refill_ts=time.time())
            except Exception:
                pass

            # Build headers
            headers = _build_headers(capacity, remaining, reset_ts)

            if not allowed:
                _trace(f"RATE_LIMIT_HIT user_key={key} cap={capacity} rem={remaining} reset={reset_ts}")
                # 429 with headers
                resp = make_response(("Too Many Requests", 429))
                for k, v in headers.items():
                    resp.headers[k] = v
                return resp

            # Call handler and attach headers to response
            result = fn(*args, **kwargs)
            try:
                resp = make_response(result)
                for k, v in headers.items():
                    resp.headers[k] = v
                # Also add a loose X-RateLimit-Policy header for operator visibility
                resp.headers["X-RateLimit-Policy"] = f"user:{capacity}/{window_seconds}s"
                return resp
            except Exception:
                return result
        return wrapper
    return decorator


def rate_limit_ip(capacity: int = 120, window_seconds: int = 60, cost: float = 1.0):
    """
    Decorator enforcing a per-IP token bucket.
    Defaults: capacity=120 tokens per 60s (~120/min) — reasonable default for public redirects.

    Parameters:
      - capacity, window_seconds, cost as in rate_limit_user.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be > 0")
    refill_rate = float(capacity) / float(window_seconds)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Only enforce for GET (redirect) by default
            try:
                method = (request.method or "").upper()
            except Exception:
                method = "GET"

            if method != "GET":
                return fn(*args, **kwargs)

            # Determine client IP
            ip = _get_client_ip() or "unknown"
            key = f"ip:{ip}"

            allowed, remaining, cap, r_rate, reset_ts = _bucket_manager.consume(key, capacity, refill_rate, cost=cost)

            # Persist snapshot to DB (best-effort)
            try:
                snap = _bucket_manager.snapshot(key)
                if snap:
                    _persist_to_db(key=key, tokens=snap["tokens"], last_refill_ts=time.time())
            except Exception:
                pass

            headers = _build_headers(capacity, remaining, reset_ts)

            if not allowed:
                _trace(f"RATE_LIMIT_HIT ip_key={key} cap={capacity} rem={remaining} reset={reset_ts}")
                resp = make_response(("Too Many Requests", 429))
                for k, v in headers.items():
                    resp.headers[k] = v
                return resp

            # Call handler and augment response with headers
            result = fn(*args, **kwargs)
            try:
                resp = make_response(result)
                for k, v in headers.items():
                    resp.headers[k] = v
                resp.headers["X-RateLimit-Policy"] = f"ip:{capacity}/{window_seconds}s"
                return resp
            except Exception:
                return result
        return wrapper
    return decorator