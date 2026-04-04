"""utils/crypto.py - Hashing and token (JWT-like) helpers with fallbacks."""

import time
import json
import base64
import hmac
import hashlib
from datetime import datetime, timedelta

# Try PyJWT first; if not present, fallback to simple HMAC-SHA256 tokens
try:
    import jwt as pyjwt
    _has_pyjwt = True
except Exception:
    pyjwt = None
    _has_pyjwt = False

# Try argon2 for password hashing; fallback to PBKDF2
try:
    from argon2 import PasswordHasher
    _ph = PasswordHasher()
    _has_argon2 = True
except Exception:
    _ph = None
    _has_argon2 = False

# Defensive import for current_app to support cookie helper
try:
    from flask import current_app
except Exception:
    current_app = None

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_decode(s: str) -> bytes:
    padding = '=' * (4 - (len(s) % 4)) if (len(s) % 4) != 0 else ''
    return base64.urlsafe_b64decode(s + padding)

def hash_password(password: str, pepper: str = None) -> str:
    if _has_argon2:
        try:
            return _ph.hash(password if pepper is None else password + pepper)
        except Exception:
            pass
    # Fallback PBKDF2
    salt = b"static_salt_for_fallback"  # In production use per-user random salt stored with hash
    dk = hashlib.pbkdf2_hmac("sha256", (password + (pepper or "")).encode("utf-8"), salt, 100000)
    return "pbkdf2$" + base64.b64encode(dk).decode("ascii")

def verify_password(password: str, stored_hash: str, pepper: str = None) -> bool:
    if _has_argon2 and stored_hash and stored_hash.startswith("$argon2"):
        try:
            return _ph.verify(stored_hash, password if pepper is None else password + pepper)
        except Exception:
            return False
    if stored_hash.startswith("pbkdf2$"):
        try:
            salt = b"static_salt_for_fallback"
            dk = hashlib.pbkdf2_hmac("sha256", (password + (pepper or "")).encode("utf-8"), salt, 100000)
            return base64.b64encode(dk).decode("ascii") == stored_hash.split("$", 1)[1]
        except Exception:
            return False
    return False

def create_verification_token(payload: dict, purpose: str, secret: str, expires_seconds: int = 24 * 3600) -> str:
    """
    Create a signed token carrying payload + purpose + exp.
    Prefer PyJWT; fallback to simple HMAC-SHA256 compact token.
    """
    now = int(time.time())
    body = {"payload": payload, "purpose": purpose, "iat": now, "exp": now + int(expires_seconds)}
    if _has_pyjwt:
        return pyjwt.encode(body, secret, algorithm="HS256")
    # Fallback
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    body_b64 = _b64url_encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_b64}.{body_b64}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{header_b64}.{body_b64}.{sig_b64}"

def decode_verification_token(token: str, secret: str, expected_purpose: str) -> dict:
    """
    Decode token and verify signature + purpose + expiry.
    Raises Exception on invalid/expired/purpose-mismatch.
    """
    if _has_pyjwt:
        try:
            data = pyjwt.decode(token, secret, algorithms=["HS256"])
        except Exception as e:
            raise Exception("Invalid or expired token: {}".format(str(e)))
        if data.get("purpose") != expected_purpose:
            raise Exception("Token purpose mismatch.")
        if int(data.get("exp", 0)) < int(time.time()):
            raise Exception("Token has expired.")
        return data.get("payload", {})
    # Fallback decode
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise Exception("Malformed token.")
        header_b64, body_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{body_b64}".encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if _b64url_encode(expected_sig) != sig_b64:
            raise Exception("Invalid signature.")
        body_json = _b64url_decode(body_b64).decode("utf-8")
        body = json.loads(body_json)
        if body.get("purpose") != expected_purpose:
            raise Exception("Token purpose mismatch.")
        if int(body.get("exp", 0)) < int(time.time()):
            raise Exception("Token has expired.")
        return body.get("payload", {})
    except Exception as e:
        raise Exception("Invalid or expired token: {}".format(str(e)))

# ---------------------------------------------------------------------
# New helper: attach_jwt_cookie(response, token, max_age=None)
# Centralized cookie setter to ensure consistent Secure/HttpOnly/SameSite attributes.
# Other modules should call utils.crypto.attach_jwt_cookie(resp, token) when issuing JWT cookies.
# Defensive: uses Flask current_app if available; tolerant to absence (best-effort no-raise).
# ---------------------------------------------------------------------
def attach_jwt_cookie(response, token, cookie_name=None, max_age=None, path="/"):
    """
    Attach a JWT cookie to the provided Flask response object using secure defaults.

    Parameters:
      - response: Flask Response instance (or any object with set_cookie method).
      - token: string token value to set as cookie.
      - cookie_name: optional override for cookie name; if omitted use current_app.config['JWT_COOKIE_NAME'] or 'smartlink_jwt'.
      - max_age: optional expiry in seconds for the cookie.
      - path: cookie path (default '/')

    Behavior:
      - Always sets httponly=True per guardrails.
      - secure flag is read from current_app.config['JWT_COOKIE_SECURE'] (default False).
      - samesite is read from current_app.config['JWT_SAMESITE'] (default 'Lax').
      - Silently tolerates failures (do not raise).
    """
    try:
        cfg = {}
        if current_app is not None:
            try:
                cfg = current_app.config or {}
            except Exception:
                cfg = {}
        cname = cookie_name or cfg.get("JWT_COOKIE_NAME", "smartlink_jwt")
        secure_flag = bool(cfg.get("JWT_COOKIE_SECURE", False))
        samesite = cfg.get("JWT_SAMESITE", "Lax") or "Lax"
        # set_cookie may raise in some adapter objects; wrap defensively
        try:
            response.set_cookie(
                cname,
                token,
                httponly=True,
                secure=secure_flag,
                samesite=samesite,
                max_age=max_age,
                path=path,
            )
        except Exception:
            # Fallback: attempt to append a Set-Cookie header manually if response.headers is available.
            try:
                # Build a conservative Set-Cookie header string
                parts = [f"{cname}={token}", "HttpOnly"]
                if secure_flag:
                    parts.append("Secure")
                if samesite:
                    parts.append(f"SameSite={samesite}")
                if max_age:
                    parts.append(f"Max-Age={int(max_age)}")
                if path:
                    parts.append(f"Path={path}")
                header_val = "; ".join(parts)
                # If response has headers dict-like object, append Set-Cookie
                headers = getattr(response, "headers", None)
                if headers is not None:
                    # Multiple Set-Cookie headers: use add if possible
                    try:
                        headers.add("Set-Cookie", header_val)
                    except Exception:
                        # Fallback: set (may override previous); best-effort only
                        headers["Set-Cookie"] = header_val
            except Exception:
                pass
    except Exception:
        # Do not propagate any exceptions from cookie-setting helper
        pass
    return response
# --- END FILE: utils/crypto.py ---