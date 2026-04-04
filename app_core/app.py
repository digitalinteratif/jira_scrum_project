import os
from flask import Flask, render_template_string, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
from dotenv import load_dotenv
from sqlalchemy import MetaData

# Load environment variables
load_dotenv()

# Centralized MetaData naming convention to prevent 'index already exists' errors
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
    app = Flask(__name__)

    # Configuration updated via dictionary to prevent SyntaxErrors
    app.config.update({
        "SECRET_KEY": os.environ.get("APP_SECRET", "dev-secret-key-12345"),
        "SQLALCHEMY_DATABASE_URI": os.environ.get("DATABASE_URL", "sqlite:///shortener.db"),
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "BASE_URL": os.environ.get("BASE_URL", "https://digitalinteractif.com"),
        "DATA_RETENTION_DAYS": int(os.environ.get("DATA_RETENTION_DAYS", 90)),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SECURE": True if os.environ.get("BASE_URL") and "https" in os.environ.get("BASE_URL") else False,
    })

    # Initialize extensions
    db.init_app(app)
    csrf.init_app(app)
    
    # Security Headers (Enforces HTTPS on digitalinteractif.com)
    # CSP is set to None for initial deployment compatibility with Tailwind CDN
    Talisman(app, content_security_policy=None) 

    # UI Persistence: Global Layout Wrapper
    @app.context_processor
    def utility_processor():
        def render_layout(content_body):
            """Wraps content strings into the consistent HTML5 boilerplate."""
            html_template = f"""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>URL Shortener | digitalinteractif.com</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-50 text-gray-900 font-sans">
                <nav class="bg-white border-b p-4 shadow-sm">
                    <div class="container mx-auto flex justify-between items-center">
                        <a href="/" class="text-xl font-bold text-blue-600">URL Shortener</a>
                        <div>
                            <a href="/login" class="px-4 py-2 text-sm">Login</a>
                            <a href="/register" class="bg-blue-600 text-white px-4 py-2 rounded text-sm">Sign Up</a>
                        </div>
                    </div>
                </nav>
                <main class="container mx-auto mt-10 p-4">
                    {content_body}
                </main>
                <footer class="mt-20 border-t p-10 text-center text-gray-500 text-sm">
                    &copy; 2026 digitalinteractif.com - High-Performance Redirection
                </footer>
            </body>
            </html>
            """
            return render_template_string(html_template)
        return dict(render_layout=render_layout)

    # Register Blueprints (Modular Architecture)
    try:
        from app_core.routes.auth import auth_bp
        from app_core.routes.shortener import shortener_bp
        app.register_blueprint(auth_bp)
        app.register_blueprint(shortener_bp)
    except ImportError as e:
        app.logger.warning(f"Blueprint import failed: {e}")

    with app.app_context():
        db.create_all()

    return app

# The Gunicorn entry point
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))