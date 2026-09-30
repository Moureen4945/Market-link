import functools
import re

from flask import session, redirect, url_for, abort
from werkzeug.security import generate_password_hash, check_password_hash

from db import query, execute, get_db

PHONE_RE = re.compile(r"^2547\d{8}$")


def current_user():
    return session.get("user")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    """Decorator: only lets the given roles into a dashboard route."""
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user()["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def dashboard_url_for(role):
    return {
        "customer": "customer_dashboard",
        "broker": "broker_dashboard",
        "seller": "seller_dashboard",
        "admin": "admin_dashboard",
    }.get(role, "login")


def login_user(email, password):
    user = query(
        "SELECT * FROM users WHERE email = %(email)s AND is_active = TRUE",
        {"email": email}, fetch="one",
    )
    if user and check_password_hash(user["password_hash"], password):
        user = dict(user)
        user.pop("password_hash")
        session["user"] = user
        return user
    return None


def register_user(name, email, phone, password, role):
    if role not in ("customer", "broker", "seller"):
        return False, "Invalid account type selected."
    if not PHONE_RE.match(phone):
        return False, "Phone must be in the format 2547XXXXXXXX."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    existing = query(
        "SELECT id FROM users WHERE email = %(email)s OR phone = %(phone)s",
        {"email": email, "phone": phone}, fetch="one",
    )
    if existing:
        return False, "An account with that email or phone already exists."

    password_hash = generate_password_hash(password)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (full_name, email, phone, password_hash, role)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (name, email, phone, password_hash, role),
            )
            user_id = cur.fetchone()["id"]

            if role == "seller":
                cur.execute(
                    "INSERT INTO seller_profiles (user_id, company_name) VALUES (%s, %s)",
                    (user_id, name),
                )
            elif role == "broker":
                cur.execute("INSERT INTO broker_profiles (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        return True, "Account created. You can now log in."
    except Exception as e:
        conn.rollback()
        return False, f"Registration failed: {e}"


def logout_user():
    session.clear()
