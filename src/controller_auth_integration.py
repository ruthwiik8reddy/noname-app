"""
HOW TO UPDATE controller.py TO USE auth.py
-------------------------------------------
Replace the existing login/logout routes and all @login_required
decorators with the ones below. Everything else stays the same.
"""

# ── At the top of controller.py, replace the imports block with: ──────────────

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from datetime import datetime, timedelta
from .db_manager import get_db
from .auth import (
    attempt_login, set_session, clear_session,
    login_required, admin_required, staff_or_admin_required,
    roles_required, auth_context, sidebar_context
)

bp = Blueprint("main", __name__)

TIME_SLOTS = [
    "8:00 AM","8:30 AM","9:00 AM","9:30 AM","10:00 AM","10:30 AM",
    "11:00 AM","11:30 AM","12:00 PM","12:30 PM","1:00 PM","1:30 PM",
    "2:00 PM","2:30 PM","3:00 PM","3:30 PM","4:00 PM","4:30 PM",
]


# ── Replace login route with: ─────────────────────────────────────────────────

@bp.route("/login", methods=["GET","POST"])
def login():
    # If already logged in, go to dashboard
    if session.get("logged_in"):
        return redirect(url_for("main.dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "Please enter both username and password."
        else:
            conn = get_db()
            payload = attempt_login(username, password, conn)
            if payload:
                set_session(payload)
                # Redirect to 'next' param if present, otherwise dashboard
                next_url = request.args.get("next") or url_for("main.dashboard")
                return redirect(next_url)
            error = "Invalid username or password."

    return render_template("login.html", error=error)


# ── Replace logout route with: ───────────────────────────────────────────────

@bp.route("/logout")
def logout():
    clear_session()
    return redirect(url_for("main.login"))


# ── Example of how to protect routes with roles: ─────────────────────────────

# Admin only:
# @bp.route("/settings")
# @admin_required
# def settings(): ...

# Admin + Staff (not customers):
# @bp.route("/bookings")
# @staff_or_admin_required
# def bookings(): ...

# Specific roles only:
# @bp.route("/reports")
# @roles_required("admin", "general_manager")
# def reports(): ...

# Just logged in (any role):
# @bp.route("/dashboard")
# @login_required
# def dashboard(): ...


# ── In every render_template call, add **auth_context(): ─────────────────────

# Before:
#   return render_template("dashboard.html", **sidebar_context(), jobs=jobs)
#
# After:
#   return render_template("dashboard.html", **sidebar_context(), **auth_context(), jobs=jobs)
#
# This gives every template access to:
#   {{ is_admin }}        → True/False
#   {{ is_staff }}        → True/False
#   {{ is_customer }}     → True/False
#   {{ current_role }}    → "admin" / "staff" / "customer" etc.
#   {{ nav_items }}       → list of nav items this role can see
#   {{ current_name }}    → display name of logged-in user


# ── In templates, use nav_items to show/hide nav: ────────────────────────────
#
# <nav>
#   {% if "dashboard" in nav_items %}
#   <a href="/dashboard">Dashboard</a>
#   {% endif %}
#
#   {% if "bookings" in nav_items %}
#   <a href="/bookings">Bookings</a>
#   {% endif %}
#
#   {% if "staff" in nav_items %}
#   <a href="/staff">Staff</a>
#   {% endif %}
# </nav>
