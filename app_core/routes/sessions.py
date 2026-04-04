"""routes/sessions.py - Session management UI (KAN-129)

Provides:
 - GET  /sessions                 -> list session tokens owned by current user
 - POST /sessions/<jti>/revoke    -> revoke the specified session token (owner-scoped)

Behavior & constraints:
 - All queries for user-owned data apply the "ID Filter" rule.
 - CSRF token included in all POST forms.
 - When revoking the current session token (jti matching cookie), the cookie is cleared and user redirected to login.
 - Calls utils.jwt.revoke_token to mark token revoked and insert audit row.
 - Writes best-effort trace to trace_KAN-129.txt.
"""

from flask import Blueprint, request, current_app, g, url_for, redirect, make_response
from utils.templates import render_layout
import models
from datetime import datetime

sessions_bp = Blueprint("sessions", __name__)

TRACE_FILE = "trace_KAN-129.txt"

def _trace(msg: str):
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass

def _require_auth():
    current_user = getattr(g, "current_user", None)
    if not current_user or not getattr(current_user, "id", None):
        return None, (render_layout("<h1>Unauthorized</h1><p>You must be authenticated to view sessions.</p>"), 401)
    try:
        return int(current_user.id), None
    except Exception:
        return None, (render_layout("<h1>Unauthorized</h1><p>Invalid authenticated identity.</p>"), 401)

@sessions_bp.route("/sessions", methods=["GET"])
def sessions_index():
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        rows = session.query(models.SessionToken).filter_by(user_id=user_id).order_by(models.SessionToken.issued_at.desc()).all()

        # CSRF token for revoke forms
        try:
            from flask_wtf.csrf import generate_csrf
            csrf_token = generate_csrf()
        except Exception:
            csrf_token = ""

        # Build simple table
        html = "<h1>Your Active Sessions</h1>"
        if not rows:
            html += "<p>No recorded sessions for your account.</p>"
            return render_layout(html)

        html += "<table style='width:100%; border-collapse: collapse;'>"
        html += "<tr><th>JTI (short)</th><th>Issued At (UTC)</th><th>Last Seen (UTC)</th><th>IP</th><th>User Agent</th><th>Revoked</th><th>Actions</th></tr>"
        for r in rows:
            jti_short = (r.jti[:12] + "...") if r.jti else ""
            issued = r.issued_at or ""
            last_seen = r.last_seen or ""
            ip = r.ip or ""
            ua = (r.user_agent or "")[:200]
            revoked = "Yes" if getattr(r, "revoked", False) else "No"
            revoke_action = ""
            if not getattr(r, "revoked", False):
                # Revoke form (POST)
                revoke_action = f"""
                  <form method="post" action="/sessions/{r.jti}/revoke" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{csrf_token}">
                    <button type="submit" onclick="return confirm('Revoke this session?');">Revoke</button>
                  </form>
                """
            html += f"<tr style='border-top:1px solid #ddd;'><td style='font-family:monospace'>{jti_short}</td><td>{issued}</td><td>{last_seen}</td><td>{ip}</td><td>{ua}</td><td>{revoked}</td><td>{revoke_action}</td></tr>"
        html += "</table>"
        _trace(f"SESSIONS_VIEW user_id={user_id} count={len(rows)}")
        return render_layout(html)
    finally:
        try:
            session.close()
        except Exception:
            pass

@sessions_bp.route("/sessions/<string:jti>/revoke", methods=["POST"])
def sessions_revoke(jti):
    user_id, err = _require_auth()
    if err:
        return err

    db = models.Session()
    try:
        st = db.query(models.SessionToken).filter_by(jti=jti, user_id=user_id).first()
        if not st:
            # Differentiate 403 vs 404
            exists = db.query(models.SessionToken).filter_by(jti=jti).first()
            if exists:
                _trace(f"SESSIONS_REVOKE_FORBIDDEN user_id={user_id} jti={jti}")
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to revoke that session.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>Session token not found.</p>"), 404

        # Attempt to revoke via utils.jwt.revoke_token
        try:
            from utils.jwt import revoke_token
            reason = f"User requested revoke via sessions UI by user_id={user_id}"
            ok = revoke_token(jti, reason=reason)
        except Exception as e:
            _trace(f"SESSIONS_REVOKE_REVOKE_ERROR user_id={user_id} jti={jti} err={str(e)}")
            ok = False

        # If revoke succeeded, update local DB row (defensive: revoke_token should have done this)
        try:
            st.revoked = True
            db.add(st)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        _trace(f"SESSIONS_REVOKED user_id={user_id} jti={jti} ok={ok}")

        # If the user revoked their current session, clear cookie and redirect to login
        try:
            # Determine current JWT cookie jti
            cookie_name = current_app.config.get("JWT_COOKIE_NAME", "smartlink_jwt")
            token = request.cookies.get(cookie_name)
            if token:
                try:
                    from utils.jwt import decode_access_token
                    body = decode_access_token(token, secret=current_app.config.get("JWT_SECRET", ""))
                    cur_jti = body.get("jti")
                    if cur_jti == jti:
                        # Clear cookie via app helper if available
                        resp = make_response(redirect(url_for("auth.login")))
                        try:
                            if hasattr(current_app, "set_jwt_cookie"):
                                current_app.set_jwt_cookie(resp, "", max_age=0, path="/")
                            else:
                                # fallback to setting empty cookie with max_age=0
                                resp.set_cookie(cookie_name, "", httponly=True, max_age=0, path="/")
                        except Exception:
                            pass
                        _trace(f"SESSIONS_REVOKE_CLEARED_COOKIE user_id={user_id} jti={jti}")
                        return resp
                except Exception:
                    # Any decode issues -> continue to normal redirect
                    pass
        except Exception:
            pass

        return redirect(url_for("sessions.sessions_index"))
    finally:
        try:
            db.close()
        except Exception:
            pass
# --- END FILE: routes/sessions.py ---