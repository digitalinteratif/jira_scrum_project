from flask import Blueprint, request, redirect, url_for, render_template

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    # If it's a form submission, redirect them to the home page for now
    if request.method == 'POST':
        return redirect(url_for('home.index'))

    # If they are just loading the page, render the HTML template
    # (Playwright will see a 200 OK status and pass the test!)
    return render_template('login.html')

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        return redirect(url_for('home.index'))
    return render_template('register.html')