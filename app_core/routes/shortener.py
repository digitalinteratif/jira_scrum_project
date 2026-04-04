try:
    from flask import Blueprint, render_template_string, request
except Exception:
    raise

short_bp = Blueprint('short', __name__)

@shortener_bp.route('/shorten', methods=['GET', 'POST'])
def shorten():
    if request.method == 'POST':
        url = request.form['url']
        # create short code...
        return "shortened"
    # Ensure CSRF hidden input is present in the inline form
    return render_template_string("""
        <html><body>
        <form method="post" action="/shorten">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input name="url" />
            <input type="submit" value="Shorten" />
        </form>
        </body></html>
    """)