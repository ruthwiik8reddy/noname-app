from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, abort
from datetime import datetime, timedelta
from .db_manager import get_db
from .ai_service import AIService
from .auth import (
    attempt_login, set_session, clear_session,
    login_required, admin_required, staff_or_admin_required,
    auth_context,
)

bp = Blueprint("main", __name__)

TIME_SLOTS = [
    "8:00 AM","8:30 AM","9:00 AM","9:30 AM","10:00 AM","10:30 AM",
    "11:00 AM","11:30 AM","12:00 PM","12:30 PM","1:00 PM","1:30 PM",
    "2:00 PM","2:30 PM","3:00 PM","3:30 PM","4:00 PM","4:30 PM",
]


def sidebar_context():
    return {
        "studio": session.get("studio"),
        "city":   session.get("city"),
        "logo":   session.get("logo"),
        "owner":  session.get("owner"),
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    return redirect(url_for("main.dashboard") if session.get("logged_in") else url_for("main.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in — go straight to dashboard
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
                # Respect ?next= redirect param
                next_url = request.args.get("next") or url_for("main.dashboard")
                return redirect(next_url)
            error = "Invalid username or password."

    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    clear_session()
    return redirect(url_for("main.login"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@bp.route("/dashboard")
@login_required
def dashboard():
    sid = session["studio_id"]
    conn = get_db()
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE studio_id=? ORDER BY id DESC", (sid,)
    ).fetchall()
    bookings = conn.execute("""
        SELECT b.*, s.name AS service_name, s.price AS service_price
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE b.studio_id=? ORDER BY b.date DESC LIMIT 5
    """, (sid,)).fetchall()
    stats = {
        "total":     len(jobs),
        "progress":  sum(1 for j in jobs if j["status"] == "In Progress"),
        "pending":   sum(1 for j in jobs if j["status"] == "Pending"),
        "completed": sum(1 for j in jobs if j["status"] == "Completed"),
        "revenue":   sum(j["price"] for j in jobs if j["status"] == "Completed"),
        "bookings_today": conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE studio_id=? AND date=?",
            (sid, datetime.now().strftime("%Y-%m-%d"))
        ).fetchone()[0],
    }
    return render_template("dashboard.html",
        **sidebar_context(), **auth_context(),
        jobs=jobs, bookings=bookings, stats=stats
    )


# ── Bookings ──────────────────────────────────────────────────────────────────

@bp.route("/bookings")
@staff_or_admin_required
def bookings():
    sid = session["studio_id"]
    conn = get_db()
    all_bookings = conn.execute("""
        SELECT b.*, s.name AS service_name, s.price AS service_price,
               bay.name AS bay_name
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        LEFT JOIN bays bay ON b.bay_id = bay.id
        WHERE b.studio_id=? ORDER BY b.date DESC, b.time_slot
    """, (sid,)).fetchall()
    return render_template("bookings.html",
        **sidebar_context(), **auth_context(), bookings=all_bookings
    )


@bp.route("/bookings/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_booking():
    sid = session["studio_id"]
    conn = get_db()
    if request.method == "POST":
        f = request.form
        errors = []
        if not f.get("customer_name", "").strip(): errors.append("Customer name is required.")
        if not f.get("customer_phone", "").strip(): errors.append("Phone is required.")
        if not f.get("vehicle", "").strip(): errors.append("Vehicle is required.")
        if not f.get("service_id", "").strip(): errors.append("Please select a service.")
        if not f.get("date", "").strip(): errors.append("Please select a date.")
        if not f.get("time_slot", "").strip(): errors.append("Please select a time slot.")
        if errors:
            services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
            bays     = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
            dates    = [(datetime.now().date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
            return render_template("new_booking.html",
                **sidebar_context(), **auth_context(),
                services=services, bays=bays, dates=dates,
                time_slots=TIME_SLOTS, errors=errors, form=f)
        # Auto-create or find customer record (CRM)
        customer_name  = f["customer_name"].strip()
        customer_phone = f["customer_phone"].strip()
        vehicle_name   = f["vehicle"].strip()

        existing_customer = conn.execute(
            "SELECT id FROM customers WHERE studio_id=? AND phone=?",
            (sid, customer_phone)
        ).fetchone()

        if existing_customer:
            customer_id = existing_customer["id"]
        else:
            username = f"cust_{sid}_{customer_phone}".replace(" ", "")
            cur = conn.execute(
                "INSERT INTO customers (studio_id, name, phone, email, username, password) "
                "VALUES (?,?,?,?,?,?)",
                (sid, customer_name, customer_phone, "", username, "")
            )
            customer_id = cur.lastrowid

        # Auto-add vehicle if this customer doesn't already have it on file
        existing_vehicle = conn.execute(
            "SELECT id FROM vehicles WHERE customer_id=? AND make_model=?",
            (customer_id, vehicle_name)
        ).fetchone()
        if not existing_vehicle:
            conn.execute(
                "INSERT INTO vehicles (studio_id, customer_id, make_model) VALUES (?,?,?)",
                (sid, customer_id, vehicle_name)
            )

        conn.execute("""
            INSERT INTO bookings
            (studio_id, customer_name, customer_phone, vehicle,
             service_id, bay_id, date, time_slot, notes, status, customer_id)
            VALUES (?,?,?,?,?,?,?,?,?,'Pending',?)
        """, (sid, customer_name, customer_phone,
              vehicle_name, f["service_id"],
              f.get("bay_id") or None, f["date"], f["time_slot"],
              f.get("notes", "").strip(), customer_id))
        conn.commit()
        return redirect(url_for("main.bookings"))
    services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
    bays     = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
    dates    = [(datetime.now().date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
    return render_template("new_booking.html",
        **sidebar_context(), **auth_context(),
        services=services, bays=bays, dates=dates,
        time_slots=TIME_SLOTS, errors=[], form={})


@bp.route("/bookings/slots")
@login_required
def available_slots():
    sid    = session["studio_id"]
    date   = request.args.get("date")
    bay_id = request.args.get("bay_id")
    conn   = get_db()
    booked = conn.execute(
        "SELECT time_slot FROM bookings "
        "WHERE studio_id=? AND date=? AND bay_id=? AND status != 'Cancelled'",
        (sid, date, bay_id)
    ).fetchall()
    return jsonify({"booked": [r["time_slot"] for r in booked]})


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@staff_or_admin_required
def update_booking_status(booking_id):
    conn = get_db()
    conn.execute(
        "UPDATE bookings SET status=? WHERE id=? AND studio_id=?",
        (request.form.get("status"), booking_id, session["studio_id"])
    )
    conn.commit()
    return redirect(url_for("main.bookings"))


# ── Estimates ─────────────────────────────────────────────────────────────────

@bp.route("/estimates")
@staff_or_admin_required
def estimates():
    sid = session["studio_id"]
    conn = get_db()
    all_estimates = conn.execute(
        "SELECT * FROM estimates WHERE studio_id=? ORDER BY created_at DESC", (sid,)
    ).fetchall()
    return render_template("estimates.html",
        **sidebar_context(), **auth_context(), estimates=all_estimates
    )


@bp.route("/estimates/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_estimate():
    sid = session["studio_id"]
    conn = get_db()
    if request.method == "POST":
        f           = request.form
        names       = f.getlist("item_name")
        descs       = f.getlist("item_desc")
        quantities  = f.getlist("item_qty")
        unit_prices = f.getlist("item_price")
        items = []
        for i in range(len(names)):
            if names[i].strip() and unit_prices[i].strip():
                qty   = int(quantities[i] or 1)
                price = int(float(unit_prices[i] or 0) * 100)
                items.append({
                    "name":  names[i].strip(),
                    "desc":  descs[i].strip() if i < len(descs) else "",
                    "qty":   qty,
                    "price": price,
                    "total": qty * price,
                })
        subtotal   = sum(it["total"] for it in items)
        tax_pct    = float(f.get("tax_percent", 8.5))
        tax_amount = int(subtotal * tax_pct / 100)
        total      = subtotal + tax_amount
        cur = conn.execute("""
            INSERT INTO estimates
            (studio_id, customer_name, customer_email, customer_phone,
             vehicle, status, subtotal, tax_percent, tax_amount, total,
             notes, internal_notes)
            VALUES (?,?,?,?,?,'Draft',?,?,?,?,?,?)
        """, (sid,
              f.get("customer_name", "").strip(),
              f.get("customer_email", "").strip(),
              f.get("customer_phone", "").strip(),
              f.get("vehicle", "").strip(),
              subtotal, tax_pct, tax_amount, total,
              f.get("notes", "").strip(),
              f.get("internal_notes", "").strip()))
        estimate_id = cur.lastrowid
        for it in items:
            conn.execute("""
                INSERT INTO estimate_items
                (estimate_id, name, description, quantity, unit_price, total)
                VALUES (?,?,?,?,?,?)
            """, (estimate_id, it["name"], it["desc"], it["qty"], it["price"], it["total"]))
        conn.commit()
        return redirect(url_for("main.estimate_detail", estimate_id=estimate_id))
    services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
    return render_template("new_estimate.html",
        **sidebar_context(), **auth_context(), services=services
    )


@bp.route("/estimates/<int:estimate_id>")
@staff_or_admin_required
def estimate_detail(estimate_id):
    sid = session["studio_id"]
    conn = get_db()
    est = conn.execute(
        "SELECT * FROM estimates WHERE id=? AND studio_id=?", (estimate_id, sid)
    ).fetchone()
    if not est:
        return "Estimate not found", 404
    items = conn.execute(
        "SELECT * FROM estimate_items WHERE estimate_id=?", (estimate_id,)
    ).fetchall()
    return render_template("estimate_detail.html",
        **sidebar_context(), **auth_context(), est=est, items=items
    )


@bp.route("/estimates/<int:estimate_id>/send", methods=["POST"])
@staff_or_admin_required
def send_estimate(estimate_id):
    conn = get_db()
    conn.execute(
        "UPDATE estimates SET status='Sent' WHERE id=? AND studio_id=?",
        (estimate_id, session["studio_id"])
    )
    conn.commit()
    return redirect(url_for("main.estimate_detail", estimate_id=estimate_id))


@bp.route("/estimates/<int:estimate_id>/approve", methods=["GET", "POST"])
def approve_estimate(estimate_id):
    """Public route — no login required. Customer opens this link."""
    conn = get_db()
    est = conn.execute(
        "SELECT e.*, s.name as studio_name FROM estimates e "
        "JOIN studios s ON e.studio_id=s.id WHERE e.id=?",
        (estimate_id,)
    ).fetchone()
    if not est:
        return "Estimate not found", 404
    if request.method == "POST":
        action    = request.form.get("action")
        signature = request.form.get("signature", "").strip()
        if action == "approve" and signature:
            conn.execute(
                "UPDATE estimates SET status='Approved', signature=?, "
                "approved_at=datetime('now') WHERE id=?",
                (signature, estimate_id)
            )
        elif action == "revision":
            conn.execute(
                "UPDATE estimates SET status='Revision Requested' WHERE id=?",
                (estimate_id,)
            )
        conn.commit()
        return render_template("approve_success.html",
            est=est, revision=(action == "revision"))
    items = conn.execute(
        "SELECT * FROM estimate_items WHERE estimate_id=?", (estimate_id,)
    ).fetchall()
    return render_template("approve_estimate.html", est=est, items=items)


# ── AI Assistant ──────────────────────────────────────────────────────────────

@bp.route("/assistant")
@login_required
def assistant():
    return render_template("assistant.html",
        **sidebar_context(), **auth_context()
    )


@bp.route("/assistant/query", methods=["POST"])
@login_required
def assistant_query():
    data        = request.get_json(silent=True) or {}
    question    = (data.get("question") or request.form.get("question", "")).strip()
    attachments = data.get("attachments") or []

    if isinstance(attachments, str):
        attachments = [a.strip() for a in attachments.split(",") if a.strip()]

    if not question:
        return jsonify({"error": "Question is required."}), 400

    service = AIService()
    answer  = service.answer(session["studio_id"], question, attachments)
    return jsonify(answer.as_dict())


# ── Staff Management ─────────────────────────────────────────────────────────

from .auth import hash_password, validate_password_strength, admin_required

STAFF_ROLES = ["general_manager", "service_advisor", "technician", "photographer"]
ROLE_LABELS_DISPLAY = {
    "general_manager": "General Manager",
    "service_advisor": "Service Advisor",
    "technician":      "Technician",
    "photographer":    "Photographer / Content Staff",
}


@bp.route("/staff")
@admin_required
def staff_list():
    sid = session["studio_id"]
    conn = get_db()
    all_staff = conn.execute(
        "SELECT * FROM staff WHERE studio_id=? ORDER BY name", (sid,)
    ).fetchall()
    return render_template("staff.html",
        **sidebar_context(), **auth_context(),
        staff=all_staff, role_labels=ROLE_LABELS_DISPLAY
    )


@bp.route("/staff/new", methods=["GET", "POST"])
@admin_required
def new_staff():
    sid = session["studio_id"]
    conn = get_db()
    errors = []

    if request.method == "POST":
        f = request.form
        name     = f.get("name", "").strip()
        role     = f.get("role", "").strip()
        username = f.get("username", "").strip().lower()
        password = f.get("password", "").strip()

        if not name: errors.append("Name is required.")
        if role not in STAFF_ROLES: errors.append("Please select a valid role.")
        if not username: errors.append("Username is required.")

        pw_errors = validate_password_strength(password) if password else ["Password is required."]
        errors.extend(pw_errors)

        # Check username uniqueness across staff table
        if username:
            existing = conn.execute(
                "SELECT id FROM staff WHERE LOWER(username)=?", (username,)
            ).fetchone()
            if existing:
                errors.append("That username is already taken.")

        if not errors:
            conn.execute(
                "INSERT INTO staff (studio_id, name, role, username, password) "
                "VALUES (?,?,?,?,?)",
                (sid, name, role, username, hash_password(password))
            )
            conn.commit()
            return redirect(url_for("main.staff_list"))

    return render_template("new_staff.html",
        **sidebar_context(), **auth_context(),
        roles=STAFF_ROLES, role_labels=ROLE_LABELS_DISPLAY, errors=errors
    )


@bp.route("/staff/<int:staff_id>/deactivate", methods=["POST"])
@admin_required
def deactivate_staff(staff_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM staff WHERE id=? AND studio_id=?",
        (staff_id, session["studio_id"])
    )
    conn.commit()
    return redirect(url_for("main.staff_list"))


# ── Media Gallery ─────────────────────────────────────────────────────────────

import os
import uuid
from werkzeug.utils import secure_filename
from .auth import can, current_role

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "avi"}
VIDEO_EXTENSIONS    = {"mp4", "mov", "avi"}
UPLOAD_BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "uploads")


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _media_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    return "video" if ext in VIDEO_EXTENSIONS else "image"


def _studio_upload_dir(studio_id: int) -> str:
    path = os.path.join(UPLOAD_BASE, f"studio_{studio_id}")
    os.makedirs(path, exist_ok=True)
    return path


@bp.route("/media")
@login_required
def media_gallery():
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    job_filter = request.args.get("job_id")

    query = """
        SELECT m.*, j.car, j.service
        FROM media m
        LEFT JOIN jobs j ON m.job_id = j.id
        WHERE m.studio_id=?
    """
    params = [sid]

    # Technicians/Photographers only see jobs they're assigned to
    if not can(role, "view_all_jobs") and role in ("technician", "photographer"):
        query += " AND j.technician = ?"
        params.append(session.get("name", ""))

    if job_filter:
        query += " AND m.job_id = ?"
        params.append(job_filter)

    query += " ORDER BY m.uploaded_at DESC"

    items = conn.execute(query, params).fetchall()

    jobs = conn.execute(
        "SELECT id, car, service FROM jobs WHERE studio_id=? ORDER BY id DESC", (sid,)
    ).fetchall()

    return render_template("media.html",
        **sidebar_context(), **auth_context(),
        items=items, jobs=jobs, selected_job=job_filter
    )


@bp.route("/media/upload", methods=["GET", "POST"])
@login_required
def upload_media():
    sid  = session["studio_id"]
    role = current_role()

    if not can(role, "upload_media") and role not in ("admin", "general_manager"):
        abort(403)

    conn = get_db()
    errors = []

    if request.method == "POST":
        job_id  = request.form.get("job_id") or None
        stage   = request.form.get("stage", "before")
        caption = request.form.get("caption", "").strip()
        files   = request.files.getlist("files")

        if stage not in ("before", "during", "after"):
            errors.append("Invalid stage selected.")
        if not files or all(f.filename == "" for f in files):
            errors.append("Please select at least one file.")

        if not errors:
            upload_dir = _studio_upload_dir(sid)
            saved = 0
            for f in files:
                if f and f.filename and _allowed_file(f.filename):
                    ext = f.filename.rsplit(".", 1)[1].lower()
                    unique_name = f"{uuid.uuid4().hex}.{ext}"
                    f.save(os.path.join(upload_dir, unique_name))
                    conn.execute("""
                        INSERT INTO media
                        (studio_id, job_id, stage, filename, original_name,
                         media_type, caption, uploaded_by)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (sid, job_id, stage, unique_name,
                          secure_filename(f.filename),
                          _media_type(f.filename), caption,
                          session.get("name", "")))
                    saved += 1
            conn.commit()
            if saved:
                return redirect(url_for("main.media_gallery"))
            errors.append("No valid files were uploaded (allowed: images and videos).")

    jobs = conn.execute(
        "SELECT id, car, service FROM jobs WHERE studio_id=? ORDER BY id DESC", (sid,)
    ).fetchall()

    return render_template("upload_media.html",
        **sidebar_context(), **auth_context(),
        jobs=jobs, errors=errors
    )


@bp.route("/media/<int:media_id>/delete", methods=["POST"])
@login_required
def delete_media(media_id):
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    item = conn.execute(
        "SELECT * FROM media WHERE id=? AND studio_id=?", (media_id, sid)
    ).fetchone()
    if not item:
        return "Not found", 404

    # Only admin/GM or the uploader themselves can delete
    if not can(role, "delete_anything") and item["uploaded_by"] != session.get("name", ""):
        abort(403)

    filepath = os.path.join(_studio_upload_dir(sid), item["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)

    conn.execute("DELETE FROM media WHERE id=?", (media_id,))
    conn.commit()
    return redirect(url_for("main.media_gallery"))


@bp.route("/uploads/studio_<int:studio_id>/<filename>")
def serve_upload(studio_id, filename):
    """Serve uploaded media — checks the requester belongs to that studio."""
    if session.get("studio_id") != studio_id and not session.get("logged_in"):
        abort(403)
    from flask import send_from_directory
    return send_from_directory(_studio_upload_dir(studio_id), filename)


# ── Customer CRM ──────────────────────────────────────────────────────────────

@bp.route("/customers")
@staff_or_admin_required
def customers_list():
    sid = session["studio_id"]
    conn = get_db()
    search = request.args.get("q", "").strip()

    query = """
        SELECT c.*,
               (SELECT COUNT(*) FROM vehicles v WHERE v.customer_id = c.id) as vehicle_count,
               (SELECT COUNT(*) FROM bookings b WHERE b.customer_id = c.id) as booking_count
        FROM customers c
        WHERE c.studio_id = ?
    """
    params = [sid]

    if search:
        query += " AND (c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like])

    query += " ORDER BY c.created_at DESC"

    all_customers = conn.execute(query, params).fetchall()
    return render_template("customers.html",
        **sidebar_context(), **auth_context(),
        customers=all_customers, search=search
    )


@bp.route("/customers/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_customer():
    sid = session["studio_id"]
    conn = get_db()
    errors = []

    if request.method == "POST":
        f = request.form
        name  = f.get("name", "").strip()
        phone = f.get("phone", "").strip()
        email = f.get("email", "").strip()
        notes = f.get("notes", "").strip()
        vehicle_make_model = f.get("vehicle_make_model", "").strip()

        if not name:  errors.append("Customer name is required.")
        if not phone: errors.append("Phone number is required.")

        if not errors:
            existing = conn.execute(
                "SELECT id FROM customers WHERE studio_id=? AND phone=?", (sid, phone)
            ).fetchone()
            if existing:
                customer_id = existing["id"]
            else:
                username = f"cust_{sid}_{phone}".replace(" ", "")
                cur = conn.execute(
                    "INSERT INTO customers (studio_id, name, phone, email, notes, username, password) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (sid, name, phone, email, notes, username, "")
                )
                customer_id = cur.lastrowid

            if vehicle_make_model:
                conn.execute(
                    "INSERT INTO vehicles (studio_id, customer_id, make_model) VALUES (?,?,?)",
                    (sid, customer_id, vehicle_make_model)
                )

            conn.commit()
            return redirect(url_for("main.customer_detail", customer_id=customer_id))

    return render_template("new_customer.html",
        **sidebar_context(), **auth_context(), errors=errors
    )


@bp.route("/customers/<int:customer_id>")
@staff_or_admin_required
def customer_detail(customer_id):
    sid = session["studio_id"]
    conn = get_db()

    customer = conn.execute(
        "SELECT * FROM customers WHERE id=? AND studio_id=?", (customer_id, sid)
    ).fetchone()
    if not customer:
        return "Customer not found", 404

    vehicles = conn.execute(
        "SELECT * FROM vehicles WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)
    ).fetchall()

    bookings = conn.execute("""
        SELECT b.*, s.name as service_name FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE b.customer_id=? ORDER BY b.date DESC
    """, (customer_id,)).fetchall()

    estimates = conn.execute(
        "SELECT * FROM estimates WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)
    ).fetchall()

    return render_template("customer_detail.html",
        **sidebar_context(), **auth_context(),
        customer=customer, vehicles=vehicles,
        bookings=bookings, estimates=estimates
    )


@bp.route("/customers/<int:customer_id>/vehicles/new", methods=["POST"])
@staff_or_admin_required
def add_vehicle(customer_id):
    sid = session["studio_id"]
    conn = get_db()
    f = request.form

    make_model = f.get("make_model", "").strip()
    if make_model:
        conn.execute("""
            INSERT INTO vehicles (studio_id, customer_id, make_model, year, color, license_plate, vin)
            VALUES (?,?,?,?,?,?,?)
        """, (sid, customer_id, make_model,
              f.get("year", "").strip(), f.get("color", "").strip(),
              f.get("license_plate", "").strip(), f.get("vin", "").strip()))
        conn.commit()

    return redirect(url_for("main.customer_detail", customer_id=customer_id))
