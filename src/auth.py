"""
auth.py — Authentication & Authorization for Autofiera SaaS
------------------------------------------------------------
Roles:
  admin     → Studio owner. Full access to everything.
  staff     → Technician / advisor. Access scoped to their studio.
  customer  → End customer. Can only view their own estimates & approvals.

All passwords are stored as Werkzeug pbkdf2:sha256 hashes.
Plain-text passwords in the DB will be auto-detected and migrated
on first successful login (backward compatible with the existing seed).

Session keys set after login:
  logged_in   : True
  user_id     : int
  user_type   : "admin" | "staff" | "customer"
  role        : "admin" | "staff" | "customer"  (alias for templates)
  studio_id   : int
  studio      : str  (studio name)
  city        : str
  logo        : str
  owner       : str
  name        : str  (display name of logged-in user)
"""

import re
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, abort, request

# ── Password utilities ────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Return a secure pbkdf2:sha256 hash of the password."""
    return generate_password_hash(plain, method="pbkdf2:sha256", salt_length=16)


def verify_password(plain: str, stored: str) -> bool:
    """
    Verify a password against a stored hash.
    Also handles plain-text passwords still in DB (legacy / seed data).
    Returns (is_valid, needs_rehash).
    """
    if stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
        return check_password_hash(stored, plain), False
    # Plain-text fallback for seed data
    return stored == plain, True


# ── Password strength validation ─────────────────────────────────────────────

def validate_password_strength(password: str) -> list[str]:
    """
    Returns a list of error messages.
    Empty list means the password is strong enough.
    """
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"\d", password):
        errors.append("Password must contain at least one number.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-]", password):
        errors.append("Password must contain at least one special character.")
    return errors


# ── Login logic ───────────────────────────────────────────────────────────────

def attempt_login(username: str, password: str, conn) -> dict | None:
    """
    Try to authenticate against admin (studios), staff, or customer tables.
    Returns a session payload dict on success, None on failure.
    Also rehashes plain-text passwords transparently.
    """
    username = username.strip().lower()
    password = password.strip()

    # ── 1. Try admin (studio owner) ──
    studio = conn.execute(
        "SELECT * FROM studios WHERE LOWER(username)=?", (username,)
    ).fetchone()

    if studio:
        valid, needs_rehash = verify_password(password, studio["password"])
        if valid:
            if needs_rehash:
                _rehash_studio_password(conn, studio["id"], password)
            return {
                "logged_in":  True,
                "user_id":    studio["id"],
                "user_type":  "admin",
                "role":       "admin",
                "studio_id":  studio["id"],
                "studio":     studio["name"],
                "city":       studio["city"],
                "logo":       studio["logo"],
                "owner":      studio["owner"],
                "name":       studio["owner"],
            }

    # ── 2. Try staff ──
    staff = conn.execute(
        "SELECT st.*, s.name as studio_name, s.city, s.logo, s.owner "
        "FROM staff st JOIN studios s ON st.studio_id = s.id "
        "WHERE LOWER(st.username)=?", (username,)
    ).fetchone()

    if staff:
        valid, needs_rehash = verify_password(password, staff["password"])
        if valid:
            if needs_rehash:
                _rehash_staff_password(conn, staff["id"], password)
            return {
                "logged_in":  True,
                "user_id":    staff["id"],
                "user_type":  "staff",
                "role":       staff["role"],
                "studio_id":  staff["studio_id"],
                "studio":     staff["studio_name"],
                "city":       staff["city"],
                "logo":       staff["logo"],
                "owner":      staff["owner"],
                "name":       staff["name"],
            }

    # ── 3. Try customer ──
    customer = conn.execute(
        "SELECT c.*, s.name as studio_name, s.city, s.logo, s.owner "
        "FROM customers c JOIN studios s ON c.studio_id = s.id "
        "WHERE LOWER(c.username)=?", (username,)
    ).fetchone()

    if customer:
        valid, needs_rehash = verify_password(password, customer["password"])
        if valid:
            if needs_rehash:
                _rehash_customer_password(conn, customer["id"], password)
            return {
                "logged_in":  True,
                "user_id":    customer["id"],
                "user_type":  "customer",
                "role":       "customer",
                "studio_id":  customer["studio_id"],
                "studio":     customer["studio_name"],
                "city":       customer["city"],
                "logo":       customer["logo"],
                "owner":      customer["owner"],
                "name":       customer["name"],
            }

    return None


def _rehash_studio_password(conn, studio_id: int, plain: str):
    conn.execute(
        "UPDATE studios SET password=? WHERE id=?",
        (hash_password(plain), studio_id)
    )
    conn.commit()


def _rehash_staff_password(conn, staff_id: int, plain: str):
    conn.execute(
        "UPDATE staff SET password=? WHERE id=?",
        (hash_password(plain), staff_id)
    )
    conn.commit()


def _rehash_customer_password(conn, customer_id: int, plain: str):
    conn.execute(
        "UPDATE customers SET password=? WHERE id=?",
        (hash_password(plain), customer_id)
    )
    conn.commit()


# ── Session helpers ───────────────────────────────────────────────────────────

def set_session(payload: dict):
    """Write the login payload into the Flask session."""
    session.clear()
    for key, value in payload.items():
        session[key] = value


def clear_session():
    session.clear()


def current_role() -> str | None:
    return session.get("role")


def current_user_type() -> str | None:
    return session.get("user_type")


def is_logged_in() -> bool:
    return session.get("logged_in", False)


def is_admin() -> bool:
    return session.get("role") == "admin"


def is_staff() -> bool:
    return session.get("user_type") == "staff"


def is_customer() -> bool:
    return session.get("role") == "customer"


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    """Redirect to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Allow only admin role. Others get 403."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        if not is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def staff_or_admin_required(f):
    """Allow admin or staff. Customers get 403."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        if session.get("role") not in ("admin", "staff", "technician",
                                        "service_advisor", "general_manager",
                                        "photographer"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def customer_required(f):
    """Allow only customer role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        if not is_customer():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def roles_required(*roles):
    """
    Flexible role check. Pass any combination of roles.
    Usage:
        @roles_required("admin", "general_manager")
        def my_route(): ...
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_logged_in():
                return redirect(url_for("main.login", next=request.path))
            if session.get("role") not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Role-based nav visibility ─────────────────────────────────────────────────

# Defines which nav items each role can see.
# Used in templates via: {{ nav_items }}
NAV_PERMISSIONS = {
    "admin": [
        "dashboard", "bookings", "estimates",
        "jobs", "customers", "staff",
        "warranties", "settings",
    ],
    "general_manager": [
        "dashboard", "bookings", "estimates",
        "jobs", "customers", "warranties",
    ],
    "service_advisor": [
        "dashboard", "bookings", "estimates", "customers",
    ],
    "staff": [
        "dashboard", "bookings", "estimates", "customers",
    ],
    "technician": [
        "dashboard", "jobs",
    ],
    "photographer": [
        "dashboard", "jobs",
    ],
    "customer": [
        "my_estimates", "my_bookings",
    ],
}


def get_nav_items() -> list[str]:
    """Return the list of nav items the current user can see."""
    role = session.get("role", "")
    return NAV_PERMISSIONS.get(role, [])


# ── Template context helper ───────────────────────────────────────────────────

def auth_context() -> dict:
    """
    Inject into every render_template call alongside sidebar_context().
    Gives templates access to role checks without extra logic.
    Usage in controller:
        return render_template("page.html", **sidebar_context(), **auth_context())
    """
    return {
        "current_role":     current_role(),
        "current_user_type": current_user_type(),
        "is_admin":         is_admin(),
        "is_staff":         is_staff(),
        "is_customer":      is_customer(),
        "nav_items":        get_nav_items(),
        "current_name":     session.get("name", ""),
    }
