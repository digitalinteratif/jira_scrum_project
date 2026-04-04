from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'dev'

    # Register blueprints
    try:
        from app_core.routes.home import home_bp
    except Exception:
        home_bp = None
    try:
        from app_core.routes.shortener import shortener_bp
    except Exception:
        shortener_bp = None

    if home_bp:
        app.register_blueprint(home_bp)
    if shortener_bp:
        app.register_blueprint(shortener_bp)

    return app