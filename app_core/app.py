import os
import logging
import traceback
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
from dotenv import load_dotenv
from sqlalchemy import MetaData

# Load environment variables
load_dotenv()

# --- 1. DATABASE CONFIGURATION ---
naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
metadata = MetaData(naming_convention=naming_convention)
db = SQLAlchemy(metadata=metadata)
csrf = CSRFProtect()

def create_app():
    """Application Factory to initialize the modular service."""
    app = Flask(__name__)

    # --- 2. DATABASE PATH RESILIENCY ---
    db_url = os.environ.get("DATABASE_URL", "sqlite:///shortener.db")
    if db_url.startswith("sqlite:////data/"):
        if not os.path.exists("/data"):
            app.logger.warning("Storage disk /data not found. Falling back to local sqlite.")
            db_url = "sqlite:///shortener.db"

    # --- 3. CONFIGURATION ---
    app.config.update({
        "SECRET_KEY": os.environ.get("APP_SECRET", "dev-secret-key-999"),
        "SQLALCHEMY_DATABASE_URI": db_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "BASE_URL": os.environ.get("BASE_URL", "https://digitalinteractif.com"),
        "DATA_RETENTION_DAYS": int(os.environ.get("DATA_RETENTION_DAYS", 90)),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SECURE": True if os.environ.get("BASE_URL") and "https" in os.environ.get("BASE_URL") else False,
    })

    # --- 4. EXTENSIONS ---
    db.init_app(app)
    csrf.init_app(app)
    # Disable forced HTTPS in local Docker environment
    force_https = os.environ.get("FLASK_ENV") != "development"
    Talisman(app, content_security_policy=None, force_https=force_https)

    # --- 5. MODULAR ROUTE REGISTRATION ---
    # We must fail-fast on blueprint import/registration errors per project guardrails.
    # Do not swallow exceptions here: surface them so CI and developers see the root cause.
    try:
        from app_core.routes.home import home_bp
        app.register_blueprint(home_bp)
        app.logger.info("SUCCESS: Home Blueprint registered.")
    except Exception as e:
        # Log full traceback then re-raise so create_app fails fast.
        app.logger.error("CRITICAL FAIL: Home Blueprint failed to import/register.\n%s", traceback.format_exc())
        raise

    try:
        from app_core.routes.auth import auth_bp
        app.register_blueprint(auth_bp)
        app.logger.info("SUCCESS: Auth Blueprint registered.")
    except Exception as e:
        app.logger.error("CRITICAL FAIL: Auth Blueprint failed to import/register.\n%s", traceback.format_exc())
        raise

    try:
        from app_core.routes.shortener import shortener_bp
        app.register_blueprint(shortener_bp)
        app.logger.info("SUCCESS: Shortener Blueprint registered.")
    except Exception as e:
        app.logger.error("CRITICAL FAIL: Shortener Blueprint failed to import/register.\n%s", traceback.format_exc())
        raise

    # --- 6. DATABASE INITIALIZATION ---
    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            # Database initialization errors are fatal during app bootstrap; surface them.
            app.logger.error("Database creation failed during app startup.\n%s", traceback.format_exc())
            raise

    return app

# --- CORRECT PLACEMENT ---
# Gunicorn looks for this 'app' variable. It must be defined AFTER create_app().
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))