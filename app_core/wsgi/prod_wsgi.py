"""
wsgi/prod_wsgi.py - Production-like WSGI entrypoint used by CI smoke runner (KAN-133)

This module exposes 'app' WSGI callable for Gunicorn:
  gunicorn wsgi.prod_wsgi:app

It builds the Flask app using create_app() and applies a minimal Prod-ish test_config override
so Gunicorn runs with safe production flags in CI smoke runs.
"""

from app import create_app

# Minimal prod-like config for smoke runs.
# These are intentionally conservative and may be overridden by CI environment variables.
prod_config = {
    "JWT_COOKIE_SECURE": True,
    "JWT_SAMESITE": "Lax",
    "JWT_COOKIE_NAME": "smartlink_jwt",
    # Keep session expiry short for smoke runs
    "JWT_SESSION_EXPIRY_SECONDS": 3600,
    # Limit analytics queries in case smoke runs touch analytics
    "ANALYTICS_MAX_RANGE_DAYS": 30,
    # Allow demo user_id only if explicitly enabled by CI env (off by default)
    "ALLOW_DEMO_USER_ID": False,
    # Enable dev tools rarely
    "ENABLE_DEV_TOOLS": False,
}

# Create Flask app with applied config
app = create_app(test_config=prod_config)

# Expose for Gunicorn
if __name__ == "__main__":
    # For manual testing: run Flask builtin (not used by CI)
    app.run(host="127.0.0.1", port=8000)
--- END FILE: wsgi/prod_wsgi.py ---