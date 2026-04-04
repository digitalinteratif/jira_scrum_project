from flask import Blueprint, request

home_bp = Blueprint("home", __name__)

def render_layout(content_html: str, **ctx) -> str:
    return (
        "<!doctype html><html><head>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Digital Interactif</title>"
        "</head><body>"
        f"{content_html}"
        "</body></html>"
    )

@home_bp.route('/')
def index():
    return render_layout('<main class="p-6"><h1>Home</h1></main>')

@home_bp.route('/login')
def login():
    return render_layout('<main class="p-6"><h1>Login</h1></main>')

@home_bp.route('/register')
def register():
    return render_layout('<main class="p-6"><h1>Register</h1></main>')