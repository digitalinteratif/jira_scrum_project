"""utils/jwt.py - Access token (JWT-like) helpers and DB-backed revocation checks for KAN-129.

Responsibilities:
 - create_access_token(payload, secret, expires_seconds, create_session=True, session_info=...)
     -> returns encoded token string and 'jti' (both encoded token and jti).
     When create_session=True will create models.SessionToken entry with provided session_info.
 - decode_access_token(token, secret) -> returns payload dict including 'jti' and 'iat/exp' checks (raises on invalid/expired)
 - is_revoked(jti) -> consults RevokedToken table and SessionToken.revoked flag to determine revocation status
 - revoke_token(jti, reason=None) -> mark SessionToken.revoked=True (if present) and insert a RevokedToken entry (audit)
 - All DB access uses models.Session() and avoids leaking internals.
 - Writes best-effort traces to trace_KAN-129.txt.
 - Defensive: works with PyJWT when available, or falls back to simple HMAC-compact token consistent with utils.crypto fallback.
"""

import time
import uuid
import hmac
import hashlib
import json
import base64
from datetime import datetime, timedelta

# Defensive PyJWT import (dependency tolerance)
try:
    import jwt as pyjwt
    _has_pyjwt = True
except Exception:
    pyjwt = None
    _has_pyjwt = False

# Defensive Flask import
try:
    from flask import current_app
except Exception:
    current_app = None

# DB models
import models

# Reuse security anonymizer if available for recording ip values (best-effort)
try:
    from utils.security import anonymize_ip as _anonymize_ip
except Exception:
    _anonymize_ip = None

TRACE_FILE = "trace_KAN-129.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def _b64url_decode(s: str) -> bytes:
    padding = '=' * (4 - (len(s) % 4)) if (len(s) % 4) != 0 else ''
    return base64.urlsafe_b64decode(s + padding)

def _now_ts() -> int:
    return int(time.time())

def create_access_token(payload: dict, secret: str, expires_seconds: int = 24 * 3600,
                        create_session: bool = False, session_info: dict = None) -> (str, str):
    """
    Create an access token and optionally create a SessionToken DB row.

    Returns:
      (token_string, jti)

    session_info may include: user_id (int), ip (str), user_agent (str)
    """
    jti = uuid.uuid4().hex
    now = _now_ts()
    body = {"payload": payload, "jti": jti, "iat": now, "exp": now + int(expires_seconds)}
    token = None

    if _has_pyjwt:
        try:
            token = pyjwt.encode(body, secret, algorithm="HS256")
        except Exception as e:
            _trace(f"CREATE_TOKEN_PYJWT_ERROR err={str(e)}")
            token = None

    if not token:
        # fallback compact HMAC token (header.payload.sig)
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        body_b64 = _b64url_encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = f"{header_b64}.{body_b64}".encode("utf-8")
        sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        sig_b64 = _b64url_encode(sig)
        token = f"{header_b64}.{body_b64}.{sig_b64}"

    # Best-effort: persist session metadata if requested
    if create_session:
        try:
            sess = models.Session()
            try:
                user_id = None
                ip_raw = None
                ua = None
                if session_info is not None:
                    user_id = session_info.get("user_id")
                    ip_raw = session_info.get("ip")
                    ua = session_info.get("user_agent")
                # anonymize ip if available
                anonymized_ip = None
                try:
                    if _anonymize_ip is not None and ip_raw:
                        anonymized_ip = _anonymize_ip(remote_addr=ip_raw, x_forwarded_for=None, trust_xff=False)
                    else:
                        anonymized_ip = ip_raw
                except Exception:
                    anonymized_ip = ip_raw
                # create SessionToken
                st = models.SessionToken(
                    jti=jti,
                    user_id=user_id,
                    issued_at=datetime.utcfromtimestamp(now),
                    last_seen=datetime.utcfromtimestamp(now),
                    ip=anonymized_ip,
                    user_agent=(ua or None)[:2000] if ua else None,
                    revoked=False,
                )
                sess.add(st)
                sess.commit()
                _trace(f"SESSION_CREATED jti={jti} user_id={user_id} ip={anonymized_ip}")
            except Exception as e:
                try:
                    sess.rollback()
                except Exception:
                    pass
                _trace(f"SESSION_CREATE_ERROR jti={jti} err={str(e)}")
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
        except Exception:
            # If models.Session not available for any reason, do not break token issuance
            _trace(f"SESSION_CREATE_SKIPPED jti={jti}")

    return token, jti

def decode_access_token(token: str, secret: str) -> dict:
    """
    Decode token and validate exp/iat. Returns body dict with 'payload' and 'jti'.
    Raises Exception on invalid/malformed/expired tokens.
    """
    if _has_pyjwt:
        try:
            data = pyjwt.decode(token, secret, algorithms=["HS256"])
            # pyjwt.decode returns the dict we set as body
            return data
        except Exception as e:
            raise Exception("Invalid or expired token: {}".format(str(e)))

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
        # verify expiry/purpose
        if int(body.get("exp", 0)) < _now_ts():
            raise Exception("Token has expired.")
        return body
    except Exception as e:
        raise Exception("Invalid or expired token: {}".format(str(e)))

def is_revoked(jti: str) -> bool:
    """
    Check whether an access token jti has been revoked.

    Consults: RevokedToken table (exists) OR SessionToken.revoked flag.
    Returns True if revoked, False otherwise. Defensive: on DB error, treat as revoked to be safe.
    """
    if not jti:
        return True
    try:
        sess = models.Session()
    except Exception:
        _trace(f"IS_REVOKED_DB_UNAVAILABLE jti={jti}")
        return True

    try:
        # Check explicit revoked tokens audit
        try:
            r = sess.query(models.RevokedToken).filter_by(jti=jti).first()
            if r:
                _trace(f"IS_REVOKED_REVOCATION_FOUND jti={jti}")
                return True
        except Exception:
            # ignore and continue to check session flag
            pass

        try:
            st = sess.query(models.SessionToken).filter_by(jti=jti).first()
            if st:
                if getattr(st, "revoked", False):
                    _trace(f"IS_REVOKED_SESSION_FLAG jti={jti} revoked=True")
                    return True
                # Otherwise not revoked
                _trace(f"IS_REVOKED_SESSION_FLAG jti={jti} revoked=False")
                return False
        except Exception:
            _trace(f"IS_REVOKED_SESSION_CHECK_ERROR jti={jti}")
            return True

        # No record at all: treat as revoked/invalid to be conservative
        _trace(f"IS_REVOKED_NO_RECORD jti={jti} -> treated_revoked")
        return True
    finally:
        try:
            sess.close()
        except Exception:
            pass

def revoke_token(jti: str, reason: str = None) -> bool:
    """
    Revoke a token by:
      - marking SessionToken.revoked=True if present
      - inserting a RevokedToken(jti, revoked_at, reason) row for audit
    Returns True on success (best-effort). If DB unavailable returns False.
    """
    if not jti:
        return False
    try:
        sess = models.Session()
    except Exception:
        _trace(f"REVOKE_DB_UNAVAILABLE jti={jti}")
        return False

    try:
        # Mark SessionToken revoked if exists
        try:
            st = sess.query(models.SessionToken).filter_by(jti=jti).first()
            if st:
                try:
                    st.revoked = True
                    st.last_seen = datetime.utcnow()
                    sess.add(st)
                    sess.commit()
                    _trace(f"REVOKE_SESSION_FLAG_SET jti={jti} user_id={st.user_id}")
                except Exception:
                    try:
                        sess.rollback()
                    except Exception:
                        pass
        except Exception:
            _trace(f"REVOKE_SESSION_LOOKUP_ERROR jti={jti}")

        # Insert audit row (non-unique jti index allows duplicates; if unique desired adapt schema)
        try:
            rv = models.RevokedToken(jti=jti, reason=(reason or None))
            sess.add(rv)
            sess.commit()
            _trace(f"REVOKE_AUDIT_INSERTED jti={jti} reason={reason}")
        except Exception:
            try:
                sess.rollback()
            except Exception:
                pass
            _trace(f"REVOKE_AUDIT_INSERT_ERROR jti={jti}")

        return True
    except Exception as e:
        _trace(f"REVOKE_TOPLEVEL_ERROR jti={jti} err={str(e)}")
        return False
    finally:
        try:
            sess.close()
        except Exception:
            pass
# --- END FILE: utils/jwt.py ---