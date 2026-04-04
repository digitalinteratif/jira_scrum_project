from flask import Blueprint, render_template_string, request, redirect, url_for, current_app

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # FIX: Redirect to 'index' (root), not 'home.index'
        return redirect(url_for('index'))
    
    # FIX: Use string-based content with render_layout to ensure UI consistency
    login_form = """
    <div class="max-w-md mx-auto bg-white p-8 border border-slate-200 rounded-xl shadow-sm">
        <h2 class="text-2xl font-bold mb-6 text-slate-800">Welcome Back</h2>
        <form method="post" action="/login">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="mb-4">
                <label class="block text-sm font-medium mb-2">Email Address</label>
                <input type="email" name="email" class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500 outline-none" required>
            </div>
            <div class="mb-6">
                <label class="block text-sm font-medium mb-2">Password</label>
                <input type="password" name="password" class="w-full p-3 border rounded-lg focus:ring-2 focus:ring-blue-500 outline-none" required>
            </div>
            <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold hover:bg-blue-700 transition">Sign In</button>
        </form>
        <p class="mt-6 text-center text-sm text-slate-500">
            Don't have an account? <a href="/register" class="text-blue-600 font-bold">Sign Up</a>
        </p>
    </div>
    """
    
    # Get the layout helper from the application's context processor
    render_layout = current_app.context_processor_helpers['render_layout']
    return render_layout(login_form)