"""routes/domains.py - Custom Domain management & verification (KAN-144)

Provides:
 - POST /domains          -> register a custom domain (initiates verification token)
 - GET  /domains          -> list current user's domains
 - GET  /domains/verify/<int:domain_id> -> attempt DNS-based verification and mark domain verified when checks pass
 - DELETE /domains/<int:domain_id> -> delete a custom domain (owner-scoped)

Design:
 - All owner-scoped queries filter by owner_id (ID Filter).
 - DNS interactions use dnspython (dns.resolver) if available; imports wrapped with try/except.
 - Verifies via TXT or CNAME records containing the verification token.
 - Additionally resolves A/AAAA and rejects private/internal addresses.
 - Writes best-effort trace to trace_KAN-144.txt.
 - Uses render_layout for HTML responses (no Jinja extends/blocks).
"""

from flask import Blueprint, request, current_app, g, jsonify, url_for
from utils.templates import render_layout
from datetime import datetime
import models
import time
import secrets
import socket
import ipaddress

TRACE_FILE = "trace_KAN-144.txt"

def _trace(msg: str) -> None:
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{time.time():.6f} {msg}\n")
    except Exception:
        pass

# Defensive import of dnspython resolver (dependency tolerance)
try:
    import dns.resolver as _dns_resolver  # type: ignore
    _has_dns_resolver = True
except Exception:
    _dns_resolver = None
    _has_dns_resolver = False

domains_bp = Blueprint("domains", __name__)

def _require_auth():
    """
    Ensure g.current_user exists OR allow demo user_id when ALLOW_DEMO_USER_ID.
    Returns (user_id, error_response) tuple.
    """
    current_user = getattr(g, "current_user", None)
    if current_user and getattr(current_user, "id", None):
        try:
            return int(current_user.id), None
        except Exception:
            return None, (render_layout("<h1>Unauthorized</h1><p>Invalid authenticated identity.</p>"), 401)

    # allow demo mode for tests/dev when explicitly enabled
    try:
        allow_demo = bool(current_app.config.get("ALLOW_DEMO_USER_ID", False))
    except Exception:
        allow_demo = False
    if allow_demo:
        try:
            user_id = int(request.values.get("user_id", "0"))
            if user_id <= 0:
                raise ValueError("invalid")
            return user_id, None
        except Exception:
            return None, (render_layout("<h1>Unauthorized</h1><p>Demo user_id required in ALLOW_DEMO_USER_ID mode.</p>"), 401)

    return None, (render_layout("<h1>Unauthorized</h1><p>You must be authenticated to manage custom domains.</p>"), 401)


def _is_private_ip(ip_str: str) -> bool:
    """
    Use stdlib ipaddress module to decide if an IP is private / link-local / loopback.
    Conservative: treat parse errors as private for safety.
    """
    try:
        obj = ipaddress.ip_address(ip_str)
        if getattr(obj, "is_loopback", False) or getattr(obj, "is_private", False) or getattr(obj, "is_link_local", False) or getattr(obj, "is_reserved", False):
            return True
        return False
    except Exception:
        # be conservative
        return True


def _resolve_addresses(host: str):
    """
    Return a deduplicated list of IP strings for the host via socket.getaddrinfo.
    On error return [].
    """
    addrs = []
    try:
        if not socket:
            return []
        # socket.getaddrinfo returns tuples where [4][0] is the textual address
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            try:
                addr = info[4][0]
                if addr not in addrs:
                    addrs.append(addr)
            except Exception:
                continue
    except Exception:
        pass
    return addrs


@domains_bp.route("/domains", methods=["POST"])
def domains_register():
    """
    Register a domain and create a verification token.

    POST params:
      - domain (required): domain to register (e.g., example.com)
      - user_id (optional) allowed only in ALLOW_DEMO_USER_ID mode

    Response:
      - HTML page with verification instructions (TXT/CNAME) and record of created domain.
    """
    user_id, err = _require_auth()
    if err:
        return err

    raw_domain = (request.form.get("domain") or request.json and request.json.get("domain") or "").strip().lower()
    if not raw_domain:
        return render_layout("<h1>Bad Request</h1><p>Missing 'domain' parameter.</p>"), 400

    # Normalize domain: strip surrounding scheme/paths if accidentally included
    # Accept forms like 'https://example.com' by extracting netloc
    try:
        from urllib.parse import urlparse
        p = urlparse(raw_domain if "://" in raw_domain else "http://" + raw_domain)
        domain = (p.netloc or p.path).strip().lower()
        # strip possible trailing slashes
        domain = domain.rstrip("/")
    except Exception:
        domain = raw_domain

    session = models.Session()
    try:
        # Ensure domain is not already taken by someone else
        existing = session.query(models.CustomDomain).filter_by(domain=domain).first()
        if existing:
            # If already owned by this user, show existing status; otherwise reject
            if existing.owner_id != user_id:
                _trace(f"DOMAIN_REGISTER_CONFLICT owner={user_id} domain={domain} existing_owner={existing.owner_id}")
                return render_layout("<h1>Conflict</h1><p>This domain is already registered by another account.</p>"), 409
            # Existing row for same user: re-issue token if not verified
            if not existing.is_verified:
                token = existing.verification_token
                message = f"Domain already registered. Verification still pending. Token preserved."
            else:
                token = existing.verification_token
                message = f"Domain already registered and verified."
            html = f"""
              <h1>Domain Registration</h1>
              <p>{message}</p>
              <p>Domain: <strong>{domain}</strong></p>
              <p>Verified: <strong>{'Yes' if existing.is_verified else 'No'}</p>
              <p>Verification token (keep private): <code>{token}</code></p>
              <p>To verify, add a DNS TXT record on the domain containing the token, or a CNAME/TXT that includes the token; then call <code>GET /domains/verify/{existing.id}</code>.</p>
            """
            _trace(f"DOMAIN_REGISTER_ALREADY_EXISTS user={user_id} domain={domain} verified={existing.is_verified}")
            return render_layout(html)

        # Generate token
        token = secrets.token_urlsafe(32)

        cd = models.CustomDomain(owner_id=user_id, domain=domain, verification_token=token, is_verified=False)
        session.add(cd)
        try:
            session.commit()
            session.refresh(cd)
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            return render_layout("<h1>Database Error</h1><p>Unable to create domain record.</p>"), 500

        # Provide instructions: TXT record for the domain root containing token; we allow CNAME check too.
        instr = f"""
            <h1>Domain Registered</h1>
            <p>Domain: <strong>{domain}</strong></p>
            <p>Verification token: <code>{token}</code></p>
            <h2>DNS Verification Instructions</h2>
            <ol>
              <li>Add a DNS TXT record at the domain root containing the token (example: <code>smartlink-verification={token}</code>), or add a CNAME/TXT record that includes the token.</li>
              <li>After DNS changes propagate (may take up to your DNS TTL), open: <code>GET /domains/verify/{cd.id}</code> to let the system detect the record.</li>
            </ol>
            <p>Note: The verification will fail if the domain resolves to private/internal IP addresses. Ensure A/AAAA records point to public addresses.</p>
        """
        _trace(f"DOMAIN_REGISTERED user={user_id} domain={domain} id={cd.id}")
        return render_layout(instr)
    finally:
        try:
            session.close()
        except Exception:
            pass


@domains_bp.route("/domains", methods=["GET"])
def domains_list():
    """List the authenticated user's domains."""
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        rows = session.query(models.CustomDomain).filter_by(owner_id=user_id).order_by(models.CustomDomain.created_at.desc()).all()
        html = "<h1>Your Custom Domains</h1>"
        if not rows:
            html += "<p>No custom domains registered.</p>"
            return render_layout(html)
        html += "<ul>"
        for r in rows:
            status = "verified" if r.is_verified else "pending"
            verify_link = url_for("domains.verify_domain", domain_id=r.id)
            html += f"<li><strong>{r.domain}</strong> — {status} — <a href='{verify_link}'>Verify now</a></li>"
        html += "</ul>"
        return render_layout(html)
    finally:
        try:
            session.close()
        except Exception:
            pass


@domains_bp.route("/domains/verify/<int:domain_id>", methods=["GET"])
def verify_domain(domain_id: int):
    """
    Attempt DNS verification for a given domain id (owner-scoped).
    Checks:
      - TXT records for the domain that contain the verification token
      - CNAME records for the domain that contain the verification token
      - Also resolves A/AAAA addresses and rejects the verification if any resolved IP is private/internal
    """
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        domain_row = session.query(models.CustomDomain).filter_by(id=domain_id, owner_id=user_id).first()
        if not domain_row:
            # check whether domain exists at all to decide 404 vs 403
            exists = session.query(models.CustomDomain).filter_by(id=domain_id).first()
            if exists:
                _trace(f"DOMAIN_VERIFY_FORBIDDEN user={user_id} domain_id={domain_id}")
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to verify this domain.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>Domain record not found.</p>"), 404

        if domain_row.is_verified:
            return render_layout(f"<h1>Already Verified</h1><p>{domain_row.domain} is already verified.</p>")

        domain = domain_row.domain
        token = domain_row.verification_token

        if not _has_dns_resolver:
            _trace(f"DOMAIN_VERIFY_UNAVAILABLE_DNSLIB user={user_id} domain={domain}")
            return render_layout("<h1>DNS Resolver Unavailable</h1><p>The server cannot perform DNS verification because dnspython is not installed. Please contact support or install the dns resolver dependency.</p>"), 503

        verified = False
        details = []

        # 1) Check TXT records for token
        try:
            try:
                answers = _dns_resolver.resolve(domain, "TXT")
            except Exception:
                # Some DNS servers respond differently (use query), fallback to resolver.resolve may raise NXDOMAIN
                answers = []
            txts = []
            for a in answers:
                try:
                    # each answer may be a sequence of bytes pieces; join and decode
                    if hasattr(a, "strings"):
                        raw = b"".join(a.strings)
                        txts.append(raw.decode("utf-8", "ignore"))
                    else:
                        # fallback: to_text()
                        txts.append(str(a).strip('"'))
                except Exception:
                    try:
                        txts.append(str(a))
                    except Exception:
                        pass
            for t in txts:
                if token in t:
                    verified = True
                    details.append("found token in TXT")
                    break
        except Exception as e:
            _trace(f"DOMAIN_VERIFY_TXT_ERROR domain={domain} err={str(e)}")

        # 2) If not found in TXT, check CNAME
        if not verified:
            try:
                try:
                    cname_answers = _dns_resolver.resolve(domain, "CNAME")
                except Exception:
                    cname_answers = []
                cnames = []
                for c in cname_answers:
                    try:
                        # rdata.target is common representation
                        if hasattr(c, "target"):
                            cnames.append(str(c.target).rstrip("."))
                        else:
                            cnames.append(str(c))
                    except Exception:
                        try:
                            cnames.append(str(c))
                        except Exception:
                            pass
                for cval in cnames:
                    if token in cval:
                        verified = True
                        details.append("found token in CNAME")
                        break
            except Exception as e:
                _trace(f"DOMAIN_VERIFY_CNAME_ERROR domain={domain} err={str(e)}")

        # 3) If found token in DNS records, ensure domain does not resolve to private IPs
        if verified:
            try:
                addrs = _resolve_addresses(domain)
                if not addrs:
                    # absence of addresses is suspicious but may be okay (CNAME-only workflows). To be conservative:
                    # allow verification if DNS record clearly contains token, but log the lack of A/AAAA resolution.
                    _trace(f"DOMAIN_VERIFY_NO_A_RECORDS domain={domain} verified_by_dns_but_no_addrs details={details}")
                else:
                    for ip in addrs:
                        if _is_private_ip(ip):
                            # Reject verification if any resolved address is private/internal
                            verified = False
                            details.append(f"resolve_to_private_ip:{ip}")
                            _trace(f"DOMAIN_VERIFY_REJECT_PRIVATE domain={domain} ip={ip}")
                            break
            except Exception as e:
                _trace(f"DOMAIN_VERIFY_RESOLVE_ERROR domain={domain} err={str(e)}")
                # conservative: reject verification if IP inspection fails
                verified = False
                details.append("ip_inspection_failed")

        # 4) Persist verification result
        if verified:
            try:
                domain_row.is_verified = True
                domain_row.verified_at = datetime.utcnow()
                session.add(domain_row)
                session.commit()
                _trace(f"DOMAIN_VERIFIED user={user_id} domain={domain} domain_id={domain_id} details={details}")
                return render_layout(f"<h1>Verified</h1><p>The domain <strong>{domain}</strong> has been verified and will be used for short links.</p>")
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                _trace(f"DOMAIN_VERIFY_COMMIT_ERROR domain={domain} err={str(e)}")
                return render_layout("<h1>Database Error</h1><p>Could not persist verification state.</p>"), 500

        # Not verified — present helpful diagnostics
        _trace(f"DOMAIN_VERIFY_FAILED user={user_id} domain={domain} domain_id={domain_id} details={details}")
        diag = "<h1>Verification Not Detected</h1><p>The verification token was not found in DNS records for the domain or the domain resolved to a private address.</p>"
        diag += "<p>Expected token: <code>{}</code></p>".format(token)
        diag += "<p>Tips:</p><ul>"
        diag += "<li>Ensure you added a TXT record at the domain root that contains the token (example value: <code>smartlink-verification={}</code>).</li>".format(token)
        diag += "<li>Wait for DNS propagation (TTL). Try again in a few minutes.</li>"
        diag += "<li>If you used a CNAME, ensure it contains the token or points to a host that contains the token in the target name.</li>"
        diag += "</ul>"
        return render_layout(diag), 422
    finally:
        try:
            session.close()
        except Exception:
            pass


@domains_bp.route("/domains/<int:domain_id>", methods=["DELETE"])
def delete_domain(domain_id: int):
    """Delete a custom domain (owner-scoped)."""
    user_id, err = _require_auth()
    if err:
        return err

    session = models.Session()
    try:
        cd = session.query(models.CustomDomain).filter_by(id=domain_id, owner_id=user_id).first()
        if not cd:
            exists = session.query(models.CustomDomain).filter_by(id=domain_id).first()
            if exists:
                _trace(f"DOMAIN_DELETE_FORBIDDEN user={user_id} domain_id={domain_id}")
                return render_layout("<h1>Forbidden</h1><p>You do not have permission to delete this domain.</p>"), 403
            else:
                return render_layout("<h1>Not Found</h1><p>Domain not found.</p>"), 404
        try:
            session.delete(cd)
            session.commit()
            _trace(f"DOMAIN_DELETED user={user_id} domain={cd.domain} domain_id={domain_id}")
            return render_layout("<h1>Deleted</h1><p>Custom domain removed.</p>")
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            _trace(f"DOMAIN_DELETE_ERROR user={user_id} domain_id={domain_id} err={str(e)}")
            return render_layout("<h1>Database Error</h1><p>Unable to delete domain.</p>"), 500
    finally:
        try:
            session.close()
        except Exception:
            pass
--- END FILE: routes/domains.py ---