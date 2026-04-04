from flask import Blueprint, render_template_string, request

# FIX: The variable name must be consistent with the decorators used below
shortener_bp = Blueprint('shortener', __name__)

@shortener_bp.route('/shorten', methods=['GET', 'POST'])
def shorten():
    if request.method == 'POST':
        url = request.form.get('url')
        # create short code logic here...
        return "shortened"
    
    # UI Persistence: Hidden CSRF token is mandatory
    return render_template_string("""
        <html><body>
        <form method="post" action="/shorten">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input name="url" placeholder="Enter URL" required />
            <input type="submit" value="Shorten" />
        </form>
        </body></html>
    """)