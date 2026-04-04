import os
import logging
import traceback
from flask import Flask, render_template_string, jsonify
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
        "SECRET_KEY": os.environ.get("APP_SECRET", "dev-secret-1234567890"),
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
    Talisman(app, content_security_policy=None)

    # --- 5. UI PERSISTENCE (Global Layout) ---
    @app.context_processor
    def utility_processor():
        def render_layout(content_body):
            html_template = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>URL Shortener | digitalinteractif.com</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-slate-50 text-slate-900 font-sans">
                <nav class="bg-white border-b border-slate-200 p-4 shadow-sm">
                    <div class="container mx-auto flex justify-between items-center">
                        <a href="/" class="text-2xl font-black text-blue-600">URL.CO</a>
                        <div class="space-x-4">
                            <a href="/login" class="text-sm hover:text-blue-600">Log In</a>
                            <a href="/register" class="bg-blue-600 text-white px-4 py-2 rounded-full text-sm font-bold hover:bg-blue-700 transition">Get Started</a>
                        </div>
                    </div>
                </nav>
                <main class="container mx-auto mt-12 px-4 max-w-5xl">
                    {content_body}
                </main>
                <footer class="mt-20 border-t p-10 text-center text-gray-400 text-xs uppercase tracking-widest">
                    &copy; 2026 digitalinteractif.com
                </footer>
            </body>
            </html>
            """
            return render_template_string(html_template)
        return dict(render_layout=render_layout)

    # --- 6. ROOT ROUTE ---
    @app.route('/')
    def index():
        content = """
        <div class="text-center py-20">
            <h1 class="text-5xl font-extrabold mb-6 text-slate-800">Simplify your links.</h1>
            <p class="text-xl text-slate-500 mb-10">Professional URL shortening and analytics for digitalinteractif.com</p>
            <div class="flex justify-center gap-4">
                <a href="/register" class="bg-blue-600 text-white px-8 py-3 rounded-lg font-bold shadow-lg hover:bg-blue-700 transition">Create Free Account</a>
                <a href="/login" class="bg-white border border-slate-300 px-8 py-3 rounded-lg font-bold hover:bg-slate-50 transition">Sign In</a>
            </div>
        </div>
        """
        layout_func = utility_processor()['render_layout']
        return layout_func(content)

    # --- 7. DEBUG ROUTE (Helpful to see what is loaded) ---
    @app.route('/debug-routes')
    def list_routes():
        import urllib
        output = []
        for rule in app.url_map.iter_rules():
            methods = ','.join(rule.methods)
            line = urllib.parse.unquote(f"{rule.endpoint:50s} {methods:20s} {rule}")
            output.append(line)
        return "<pre>" + "\n".join(output) + "</pre>"

    # --- 8. MODULAR ROUTE REGISTRATION ---
    # These blocks are likely failing due to syntax errors in auth.py and shortener.py.
    # The traceback.format_exc() will print the EXACT line of the error to Render logs.
    try:
        from app_core.routes.auth import auth_bp
        app.register_blueprint(auth_bp)
        app.logger.info("SUCCESS: Auth Blueprint registered.")
    except Exception as e:
        app.logger.error(f"CRITICAL FAIL: Auth Blueprint could not be registered.\n{traceback.format_exc()}")

    try:
        from app_core.routes.shortener import shortener_bp
        app.register_blueprint(shortener_bp)
        app.logger.info("SUCCESS: Shortener Blueprint registered.")
    except Exception as e:
        app.logger.error(f"CRITICAL FAIL: Shortener Blueprint could not be registered.\n{traceback.format_exc()}")

    # --- 9. DATABASE INITIALIZATION ---
    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            app.logger.error(f"Database creation failed: {e}")

    return app

# The Gunicorn entry point
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))