import os
import logging
import traceback
import importlib
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
from dotenv import load_dotenv
from sqlalchemy import MetaData

# Load environment variables from .env
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

TRACE_FILE = "trace_KAN-150.txt"


def _trace(msg: str):
    """Best-effort append to trace_KAN-150.txt for Architectural Memory."""
    try:
        with open(TRACE_FILE, "a") as f:
            f.write(f"{msg}\n")
    except Exception:
        # never raise from tracing
        pass


def _import_blueprint_from_candidates(module_candidates, attr_name):
    """
    Try importing a blueprint attribute name from a list of module import paths.
    Returns (blueprint_obj, used_module_path) on success, or (None, None) on failure.
    Writes trace entries for attempts.
    """
    for mod_path in module_candidates:
        try:
            mod = importlib.import_module(mod_path)
            if hasattr(mod, attr_name):
                _trace(f"IMPORT_OK module={mod_path} attr={attr_name}")
                return getattr(mod, attr_name), mod_path
            else:
                _trace(f"IMPORT_NOATTR module={mod_path} missing_attr={attr_name}")
        except Exception as e:
            _trace(f"IMPORT_FAIL module={mod_path} err={str(e)}")
            # continue trying alternatives
            continue
    return None, None


def create_app(test_config=None):
    """Application Factory to initialize the modular service."""
    app = Flask(__name__)

    # --- 2. DATABASE PATH RESILIENCY ---
    db_url = os.environ.get("DATABASE_URL", "sqlite:///shortener.db")
    if db_url.startswith("sqlite:////data/"):
        if not os.path.exists("/data"):
            app.logger.warning("Storage disk /data not found. Falling back to local sqlite.")
            db_url = "sqlite:///shortener.db"

    # --- 3. CONFIGURATION ---
    # Allow callers/tests to pass test_config dict to override settings cleanly (surgical pattern used in tests)
    cfg = {
        "SECRET_KEY": os.environ.get("APP_SECRET", "dev-secret-1234567890"),
        "SQLALCHEMY_DATABASE_URI": db_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "BASE_URL": os.environ.get("BASE_URL", "https://digitalinteractif.com"),
        "DATA_RETENTION_DAYS": int(os.environ.get("DATA_RETENTION_DAYS", 90)),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SECURE": True if os.environ.get("BASE_URL") and "https" in os.environ.get("BASE_URL") else False,
    }
    # Merge with app.config or test_config
    app.config.update(cfg)
    if test_config and isinstance(test_config, dict):
        app.config.update(test_config)

    # --- 4. EXTENSIONS ---
    db.init_app(app)
    csrf.init_app(app)
    Talisman(app, content_security_policy=None)

    # --- 5. MODULAR ROUTE REGISTRATION ---
    # We use a tolerant import strategy so the codebase can live under either 'app_core.routes.*' or top-level 'routes.*'.
    blueprint_specs = [
        # (logical name, attribute name on module, list of candidate module import paths)
        ("health", "health_bp", ["app_core.routes.health", "routes.health", "health"]),
        ("home", "home_bp", ["app_core.routes.home", "routes.home", "home"]),
        ("auth", "auth_bp", ["app_core.routes.auth", "routes.auth", "auth"]),
        ("shortener", "shortener_bp", ["app_core.routes.shortener", "routes.shortener", "shortener"]),
        # Add more blueprints here if you want them registered automatically with the same tolerant import logic.
    ]

    for logical_name, attr_name, candidates in blueprint_specs:
        try:
            bp, used = _import_blueprint_from_candidates(candidates, attr_name)
            if bp is not None:
                app.register_blueprint(bp)
                app.logger.info(f"SUCCESS: Registered blueprint '{logical_name}' from '{used}'.")
                _trace(f"BLUEPRINT_REGISTERED name={logical_name} module={used}")
            else:
                app.logger.warning(f"SKIP: Could not find blueprint '{logical_name}' in candidates: {candidates}")
                _trace(f"BLUEPRINT_MISSING name={logical_name} candidates={candidates}")
        except Exception:
            app.logger.error(f"CRITICAL FAIL: {logical_name} Blueprint failed to register.\n{traceback.format_exc()}")
            _trace(f"BLUEPRINT_REGISTER_ERROR name={logical_name} err={traceback.format_exc()}")

    # --- 6. DATABASE INITIALIZATION ---
    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            app.logger.error(f"Database creation failed: {e}")
            _trace(f"DB_CREATE_ERROR err={str(e)}")

    return app


# The Gunicorn entry point
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))