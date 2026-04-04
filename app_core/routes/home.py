from flask import Blueprint, render_template_string

# This blueprint provides the 'home' namespace required for 'home.index' redirects
home_bp = Blueprint('home', __name__)

def render_layout(content_body):
    """
    The master UI wrapper helper. 
    Centralizing this here ensures UI consistency across all blueprints.
    """
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
                    <a href="/register" class="bg-blue-600 text-white px-4 py-2 rounded-full text-sm font-bold shadow-md hover:bg-blue-700 transition">Get Started</a>
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

@home_bp.route('/')
def index():
    """Main landing page for digitalinteractif.com"""
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
    return render_layout(content)

@home_bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page, moved to home blueprint for root-level access."""
    content = """
    <div class="max-w-md mx-auto bg-white p-8 border border-slate-200 rounded-xl shadow-sm">
        <h2 class="text-2xl font-bold mb-6">Create your account</h2>
        <form method="post" action="/register">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="mb-4">
                <label class="block text-sm font-medium mb-2">Full Name</label>
                <input type="text" name="name" class="w-full p-3 border rounded-lg" required>
            </div>
            <div class="mb-4">
                <label class="block text-sm font-medium mb-2">Email Address</label>
                <input type="email" name="email" class="w-full p-3 border rounded-lg" required>
            </div>
            <div class="mb-6">
                <label class="block text-sm font-medium mb-2">Password</label>
                <input type="password" name="password" class="w-full p-3 border rounded-lg" required>
            </div>
            <button type="submit" class="w-full bg-blue-600 text-white p-3 rounded-lg font-bold shadow-md hover:bg-blue-700">Sign Up</button>
        </form>
    </div>
    """
    return render_layout(content)