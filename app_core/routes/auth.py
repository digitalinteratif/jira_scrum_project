try:
    from flask import Blueprint, render_template, request, redirect, url_for
except Exception:
    # Dependency missing: raise so the import-time failure is explicit
    raise

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # process login
        return redirect(url_for('home.index'))
    # Use render_template instead of deprecated render_to_response
    return render_template('auth/login.html', next=request.args.get('next'))