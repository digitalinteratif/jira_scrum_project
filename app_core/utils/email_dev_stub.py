"""utils/email_dev_stub.py - Development stub for outbound emails.
This module records sent verification tokens/URLs for local dev and unit tests.
"""

_sent_emails = []

def send_verification_email(to_email: str, verification_url: str, token: str = None):
    """
    Development stub: record the intended email. In production, replace with real email sender.
    """
    entry = {
        "to": to_email,
        "verification_url": verification_url,
        "token": token,
        "sent_at": __import__("time").time(),
    }
    _sent_emails.append(entry)
    # Also write to a trace log for architectural memory (non-blocking)
    try:
        with open("trace_KAN-110.txt", "a") as f:
            f.write("EMAIL_SENT {}\n".format(entry))
    except Exception:
        pass

def get_sent_emails():
    return list(_sent_emails)

def pop_last_email():
    return _sent_emails.pop() if _sent_emails else None