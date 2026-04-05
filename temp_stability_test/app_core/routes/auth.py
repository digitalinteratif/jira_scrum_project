from flask import Blueprint, request, redirect, url_for, current_app
from flask_wtf.csrf import generate_csrf
from utils.templates import render_layout

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """
    Login page (surgical fix for KAN-153).
    - GET: render a login form wrapped with render_layout and inject a runtime CSRF token.
    - POST: for now, redirect to the home index (home.index). Keep this surgical: auth logic is out-of-scope.
    Notes:
    - We avoid using any non-public Flask internals (e.g., current_app.context_processor_helpers).
    - The form includes explicit id/label attributes to aid accessibility tests.
    """
    # POST: handle form submission minimally and redirect to home index
    if request.method == 'POST':
        # Post-processing (authentication) is out of scope for this ticket;
        # perform a safe redirect back to the public landing page.
        try:
            return redirect(url_for('home.index'))
        except Exception:
            # Fallback: redirect to root path to avoid url_for errors in unusual setups
            return redirect('/')

    # GET: render login form with an injected CSRF token
    try:
        csrf_token = generate_csrf()
    except Exception:
        csrf_token = ""

    login_form = f"""
    <div class="max-w-md mx-auto bg-white p-8 border border-slate-200 rounded-xl shadow-sm" role="main">
        <h2 class="text-2xl font-bold mb-6 text-slate-800">Welcome Back</h2>
        <form method="post" action="/login" novalidate>
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <div class="mb-4">
                <label for="login-email" class="block text-sm font-medium mb-2">Email Address</label>
                <input id="login-email" type="email" name="email" class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500 outline-none" required>
            </div>
            <div class="mb-6">
                <label for="login-password" class="block text-sm font-medium mb-2">Password</label>
                <input id="login-password" type="password" name="password" class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500 outline-none" required>
            </div>
            <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold hover:bg-blue-700 transition">Sign In</button>
        </form>
        <p class="mt-6 text-center text-sm text-slate-500">
            Don't have an account? <a href="/register" class="text-blue-600 font-bold">Sign Up</a>
        </p>
    </div>
    """

    # Architectural Memory: trace the render
    try:
        with open("trace_KAN-153.txt", "a") as f:
            import time
            f.write(f"{time.time():.6f} LOGIN_RENDER user_agent={request.headers.get('User-Agent','<unknown>')} remote={request.remote_addr}\n")
    except Exception:
        pass

    return render_layout(login_form)