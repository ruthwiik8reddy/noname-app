"""
auth.py — Authentication & Authorization for Autofiera SaaS
------------------------------------------------------------
Roles (6 total, mapped from spec):
  admin             → Owner/Admin. Full access.
  general_manager   → Same tier as admin, minus staff management.
  service_advisor   → Bookings, estimates, customers, payments.
  technician        → Jobs assigned to them, media upload, client-visible notes only.
  photographer      → Media gallery only.
  customer          → Their own estimates/bookings only.

All staff members live in the `staff` table with a `role` column
holding one of: general_manager, service_advisor, technician, photographer.
Studio owners live in the `studios` table and are always role="admin".
Customers live in the `customers` table and are always role="customer".
"""

import re
import logging
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, redirect, url_for, abort, request

logger = logging.getLogger(__name__)


# ── Password utilities ────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return generate_password_hash(plain, method="pbkdf2:sha256", salt_length=16)


def verify_password(plain: str, stored: str) -> tuple[bool, bool]:
    """Returns (is_valid, needs_rehash). Handles legacy plain-text passwords."""
    if stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
        return check_password_hash(stored, plain), False
    return stored == plain, True


def validate_password_strength(password: str) -> list[str]:
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
    logger.info(f"Entering attempt_login(username={username})")
    username = username.strip().lower()
    password = password.strip()

    # 1. Admin (studio owner)
    studio = conn.execute(
        "SELECT * FROM studios WHERE LOWER(username)=?", (username,)
    ).fetchone()
    if studio:
        valid, needs_rehash = verify_password(password, studio["password"])
        if valid:
            if needs_rehash:
                conn.execute("UPDATE studios SET password=? WHERE id=?",
                             (hash_password(password), studio["id"]))
                conn.commit()
            return {
                "logged_in": True,
                "user_id":   studio["id"],
                "user_type": "admin",
                "role":      "admin",
                "studio_id": studio["id"],
                "studio":    studio["name"],
                "city":      studio["city"],
                "logo":      studio["logo"],
                "owner":     studio["owner"],
                "name":      studio["owner"],
            }

    # 2. Staff (role comes from the staff table itself)
    staff = conn.execute(
        "SELECT st.*, s.name as studio_name, s.city, s.logo, s.owner "
        "FROM staff st JOIN studios s ON st.studio_id = s.id "
        "WHERE LOWER(st.username)=?", (username,)
    ).fetchone()
    if staff:
        valid, needs_rehash = verify_password(password, staff["password"])
        if valid:
            if needs_rehash:
                conn.execute("UPDATE staff SET password=? WHERE id=?",
                             (hash_password(password), staff["id"]))
                conn.commit()
            return {
                "logged_in": True,
                "user_id":   staff["id"],
                # Explicit alias: the dispatch board scopes a technician's queue
                # by staff id, and "user_id" is ambiguous across the three login
                # types (studio id for admins, customer id for customers).
                "staff_id":  staff["id"],
                "user_type": "staff",
                "role":      staff["role"],   # general_manager / service_advisor / technician / photographer
                "studio_id": staff["studio_id"],
                "studio":    staff["studio_name"],
                "city":      staff["city"],
                "logo":      staff["logo"],
                "owner":     staff["owner"],
                "name":      staff["name"],
            }

    # 3. Customer
    customer = conn.execute(
        "SELECT c.*, s.name as studio_name, s.city, s.logo, s.owner "
        "FROM customers c JOIN studios s ON c.studio_id = s.id "
        "WHERE LOWER(c.username)=?", (username,)
    ).fetchone()
    if customer:
        valid, needs_rehash = verify_password(password, customer["password"])
        if valid:
            if needs_rehash:
                conn.execute("UPDATE customers SET password=? WHERE id=?",
                             (hash_password(password), customer["id"]))
                conn.commit()
            return {
                "logged_in": True,
                "user_id":   customer["id"],
                "user_type": "customer",
                "role":      "customer",
                "studio_id": customer["studio_id"],
                "studio":    customer["studio_name"],
                "city":      customer["city"],
                "logo":      customer["logo"],
                "owner":     customer["owner"],
                "name":      customer["name"],
            }

    return None


# ── Session helpers ───────────────────────────────────────────────────────────

def set_session(payload: dict):
    session.clear()
    for key, value in payload.items():
        session[key] = value


def clear_session():
    session.clear()


def current_role() -> str | None:
    return session.get("role")


def is_logged_in() -> bool:
    return session.get("logged_in", False)


def is_admin() -> bool:
    return session.get("role") in ("admin", "general_manager")


def is_customer() -> bool:
    return session.get("role") == "customer"


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Owner/Admin or General Manager only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        if not is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def staff_or_admin_required(f):
    """Any staff role or admin. Customers blocked."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("main.login", next=request.path))
        if session.get("role") == "customer":
            abort(403)
        return f(*args, **kwargs)
    return decorated


def roles_required(*roles):
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


def permission_required(permission: str):
    """Check a specific permission from the PERMISSIONS matrix."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not is_logged_in():
                return redirect(url_for("main.login", next=request.path))
            if not can(session.get("role", ""), permission):
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


# ── Nav visibility per role ────────────────────────────────────────────────────

NAV_PERMISSIONS = {
    "admin": [
        "dashboard", "command", "leads", "agents", "bookings", "estimates", "jobs", "dispatch",
        "tracking", "customers", "staff", "media", "payments",
        "products", "inventory", "analytics", "dvi", "warranties", "settings",
    ],
    "general_manager": [
        "dashboard", "command", "leads", "agents", "bookings", "estimates", "jobs", "dispatch",
        "tracking", "customers", "media", "payments",
        "inventory", "analytics", "dvi", "warranties",
    ],
    "service_advisor": [
        "dashboard", "command", "leads", "bookings", "estimates", "tracking", "customers", "payments",
        "dispatch", "dvi",
    ],
    # Technicians get the capture screen and their own queue, not the analytics
    # dashboard — inventory cost data isn't theirs to see.
    "technician": [
        "dashboard", "command", "jobs", "dispatch", "tracking", "media", "dvi", "inventory",
    ],
    "photographer": [
        "dashboard", "jobs", "tracking", "media", "dvi",
    ],
    "customer": [
        "my_estimates", "my_bookings",
    ],
}


def get_nav_items() -> list[str]:
    role = session.get("role", "")
    return NAV_PERMISSIONS.get(role, [])


# ── Fine-grained action permissions ───────────────────────────────────────────

PERMISSIONS = {
    "admin": {
        "view_internal_notes": True, "edit_staff": True, "delete_anything": True,
        "view_all_jobs": True, "issue_warranty": True, "view_payments": True,
        "process_refund": True, "upload_media": True,
    },
    "general_manager": {
        "view_internal_notes": True, "edit_staff": False, "delete_anything": False,
        "view_all_jobs": True, "issue_warranty": True, "view_payments": True,
        "process_refund": True, "upload_media": True,
    },
    "service_advisor": {
        "view_internal_notes": True, "edit_staff": False, "delete_anything": False,
        "view_all_jobs": False, "issue_warranty": False, "view_payments": True,
        "process_refund": False, "upload_media": False,
    },
    "technician": {
        "view_internal_notes": False, "edit_staff": False, "delete_anything": False,
        "view_all_jobs": False, "issue_warranty": False, "view_payments": False,
        "process_refund": False, "upload_media": True,
    },
    "photographer": {
        "view_internal_notes": False, "edit_staff": False, "delete_anything": False,
        "view_all_jobs": False, "issue_warranty": False, "view_payments": False,
        "process_refund": False, "upload_media": True,
    },
    "customer": {
        "view_internal_notes": False, "edit_staff": False, "delete_anything": False,
        "view_all_jobs": False, "issue_warranty": False, "view_payments": False,
        "process_refund": False, "upload_media": False,
    },
}


def can(role: str, permission: str) -> bool:
    return PERMISSIONS.get(role, {}).get(permission, False)


# ── Template context helper ───────────────────────────────────────────────────

ROLE_LABELS = {
    "admin":            "Owner / Admin",
    "general_manager":  "General Manager",
    "service_advisor":  "Service Advisor",
    "technician":       "Technician",
    "photographer":     "Photographer",
    "customer":         "Customer",
}


def auth_context() -> dict:
    role = current_role()
    return {
        "current_role":         role,
        "current_role_label":   ROLE_LABELS.get(role, role),
        "is_admin":             is_admin(),
        "is_customer":          is_customer(),
        "nav_items":            get_nav_items(),
        "current_name":         session.get("name", ""),
        "can_view_internal_notes": can(role, "view_internal_notes"),
        "can_view_payments":      can(role, "view_payments"),
        "can_upload_media":       can(role, "upload_media"),
        "can_edit_staff":         can(role, "edit_staff"),
    }
