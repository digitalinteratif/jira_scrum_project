from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import random
import string

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///local_app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    verified = db.Column(db.Boolean, default=False)

class URL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(2048), nullable=False)
    short_url = db.Column(db.String(6), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Helper Functions
def generate_short_url():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=6))

def send_email(to, subject, body):
    print(f"Mock Email Sent to {to}: {subject}\n{body}")

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        urls = URL.query.filter_by(user_id=user.id).all()
        return render_template_string('''
            <h1>Welcome {{ user.username }}</h1>
            <form action="{{ url_for('shorten_url') }}" method="post">
                <input type="url" name="original_url" placeholder="Enter URL to shorten" required>
                <button type="submit">Shorten</button>
            </form>
            <h2>Your Shortened URLs</h2>
            <ul>
                {% for url in urls %}
                    <li><a href="{{ url_for('redirect_short_url', short_url=url.short_url) }}">{{ url.short_url }}</a> - {{ url.original_url }}</li>
                {% endfor %}
            </ul>
            <a href="{{ url_for('logout') }}">Logout</a>
        ''', user=user, urls=urls)
    return render_template_string('''
        <h1>URL Shortener</h1>
        <a href="{{ url_for('register') }}">Register</a> | <a href="{{ url_for('login') }}">Login</a>
    ''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        if User.query.filter_by(email=email).first():
            return "Email already registered."
        new_user = User(username=username, email=email, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        verification_code = str(uuid.uuid4())
        send_email(email, "Verify your email", f"Your verification code is {verification_code}")
        return "Registration successful! Check your email for verification."
    return render_template_string('''
        <h1>Register</h1>
        <form method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Register</button>
        </form>
    ''')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            return redirect(url_for('index'))
        return "Invalid credentials."
    return render_template_string('''
        <h1>Login</h1>
        <form method="post">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
    ''')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))

@app.route('/shorten', methods=['POST'])
def shorten_url():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    original_url = request.form['original_url']
    short_url = generate_short_url()
    new_url = URL(original_url=original_url, short_url=short_url, user_id=session['user_id'])
    db.session.add(new_url)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/<short_url>')
def redirect_short_url(short_url):
    url = URL.query.filter_by(short_url=short_url).first_or_404()
    return redirect(url.original_url)

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        if user:
            reset_code = str(uuid.uuid4())
            send_email(email, "Password Reset", f"Your password reset code is {reset_code}")
            return "Password reset email sent."
        return "Email not found."
    return render_template_string('''
        <h1>Reset Password</h1>
        <form method="post">
            <input type="email" name="email" placeholder="Email" required>
            <button type="submit">Send Reset Email</button>
        </form>
    ''')

@app.route('/reset_password/<reset_code>', methods=['GET', 'POST'])
def complete_reset_password(reset_code):
    if request.method == 'POST':
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        if new_password == confirm_password:
            # This is a mock implementation, in reality, you would verify the reset_code
            user = User.query.first()  # Mock: Assume the first user is resetting password
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            return "Password reset successful."
        return "Passwords do not match."
    return render_template_string('''
        <h1>Complete Password Reset</h1>
        <form method="post">
            <input type="password" name="new_password" placeholder="New Password" required>
            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
            <button type="submit">Reset Password</button>
        </form>
    ''')

# Initialize Database
with app.app_context():
    db.create_all()

# Run the app
if __name__ == '__main__':
    app.run(debug=True)