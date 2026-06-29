from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, abort, send_from_directory
from datetime import datetime, timedelta
import os
import uuid
import secrets
from werkzeug.utils import secure_filename

from .db_manager import get_db
from .ai_service import AIService
from .reminder_service import ReminderService
from .sms_service import send_tracking_sms, send_status_update_sms
from .config import Config
from .auth import (
    attempt_login, set_session, clear_session,
    login_required, admin_required, staff_or_admin_required,
    auth_context, hash_password, validate_password_strength,
    can, current_role,
)

bp = Blueprint("main", __name__)

TIME_SLOTS = [
    "8:00 AM", "8:30 AM", "9:00 AM", "9:30 AM", "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM", "12:00 PM", "12:30 PM", "1:00 PM", "1:30 PM",
    "2:00 PM", "2:30 PM", "3:00 PM", "3:30 PM", "4:00 PM", "4:30 PM",
]

STAFF_ROLES = ["general_manager", "service_advisor", "technician", "photographer"]
ROLE_LABELS_DISPLAY = {
    "general_manager": "General Manager",
    "service_advisor":  "Service Advisor",
    "technician":       "Technician",
    "photographer":     "Photographer / Content Staff",
}

JOB_STATUSES = ["Pending", "In Progress", "Completed"]

ALLOWED_MEDIA_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "avi"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi"}
UPLOAD_BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "uploads")


def sidebar_context():
    return {
        "studio": session.get("studio"),
        "city":   session.get("city"),
        "logo":   session.get("logo"),
        "owner":  session.get("owner"),
    }


def _reminder_db_path() -> str:
    return Config.DB_PATH


# ── Auth ──────────────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    return redirect(url_for("main.dashboard") if session.get("logged_in") else url_for("main.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
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
        **sidebar_context(), **auth_context(), active_page="dashboard",
        jobs=jobs, bookings=bookings, stats=stats,
        job_statuses=JOB_STATUSES,
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
        **sidebar_context(), **auth_context(), active_page="bookings", bookings=all_bookings
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
                **sidebar_context(), **auth_context(), active_page="bookings",
                services=services, bays=bays, dates=dates,
                time_slots=TIME_SLOTS, errors=errors, form=f)

        customer_name  = f["customer_name"].strip()
        customer_phone = f["customer_phone"].strip()
        vehicle_name   = f["vehicle"].strip()

        # Auto-create or find customer record (CRM integration)
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
        """, (sid, customer_name, customer_phone, vehicle_name,
              f["service_id"], f.get("bay_id") or None, f["date"], f["time_slot"],
              f.get("notes", "").strip(), customer_id))
        conn.commit()
        return redirect(url_for("main.bookings"))

    services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
    bays     = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
    dates    = [(datetime.now().date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
    return render_template("new_booking.html",
        **sidebar_context(), **auth_context(), active_page="bookings",
        services=services, bays=bays, dates=dates,
        time_slots=TIME_SLOTS, errors=[], form={}
    )


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


# ── Jobs ──────────────────────────────────────────────────────────────────────

@bp.route("/jobs")
@staff_or_admin_required
def jobs_list():
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    query  = "SELECT * FROM jobs WHERE studio_id=?"
    params = [sid]

    if not can(role, "view_all_jobs") and role in ("technician", "photographer"):
        query += " AND technician = ?"
        params.append(session.get("name", ""))

    query += " ORDER BY id DESC"
    jobs = conn.execute(query, params).fetchall()

    return render_template("jobs.html",
        **sidebar_context(), **auth_context(), active_page="jobs",
        jobs=jobs, job_statuses=JOB_STATUSES
    )


@bp.route("/tracking")
@staff_or_admin_required
def tracking():
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    query  = "SELECT * FROM jobs WHERE studio_id=?"
    params = [sid]
    if not can(role, "view_all_jobs") and role in ("technician", "photographer"):
        query += " AND technician = ?"
        params.append(session.get("name", ""))
    query += " ORDER BY id DESC"

    rows = conn.execute(query, params).fetchall()
    jobs = []
    for row in rows:
        status = row["status"]
        progress_index = JOB_STATUSES.index(status) if status in JOB_STATUSES else 0
        job = dict(row)
        job["progress_index"] = progress_index
        job["dvi_url"] = url_for("main.tracking_dvi", job_id=row["id"])
        jobs.append(job)

    return render_template("tracking.html",
        **sidebar_context(), **auth_context(), active_page="tracking",
        jobs=jobs, job_statuses=JOB_STATUSES
    )


@bp.route("/tracking/job/<int:job_id>/dvi")
@staff_or_admin_required
def tracking_dvi(job_id):
    sid = session["studio_id"]
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND studio_id=?",
        (job_id, sid)
    ).fetchone()
    if not job:
        return "Job not found", 404

    return render_template("tracking_dvi.html",
        **sidebar_context(), **auth_context(), active_page="tracking",
        job=job, job_statuses=JOB_STATUSES
    )


@bp.route("/jobs/<int:job_id>/status", methods=["POST"])
@staff_or_admin_required
def update_job_status(job_id):
    sid = session["studio_id"]
    new_status = request.form.get("status")
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status=? WHERE id=? AND studio_id=?",
        (new_status, job_id, sid)
    )
    conn.commit()

    if new_status == "Completed":
        svc = ReminderService(_reminder_db_path())
        svc.schedule_for_completed_job(sid, job_id)

    # ── SMS: notify customer of status change ─────────────────────────────
    _send_job_status_sms(conn, sid, job_id, new_status)

    redirect_to = request.form.get("redirect_to") or url_for("main.jobs_list")
    return redirect(redirect_to)


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
        **sidebar_context(), **auth_context(), active_page="estimates", estimates=all_estimates
    )


@bp.route("/estimates/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_estimate():
    sid = session["studio_id"]
    conn = get_db()

    if request.method == "POST":
        f = request.form
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
        **sidebar_context(), **auth_context(), active_page="estimates", services=services
    )


@bp.route("/estimates/<int:estimate_id>")
@staff_or_admin_required
def estimate_detail(estimate_id):
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    est = conn.execute(
        "SELECT * FROM estimates WHERE id=? AND studio_id=?", (estimate_id, sid)
    ).fetchone()
    if not est:
        return "Estimate not found", 404

    items = conn.execute(
        "SELECT * FROM estimate_items WHERE estimate_id=?", (estimate_id,)
    ).fetchall()

    notes = get_notes_for(conn, sid, role, estimate_id=estimate_id)

    return render_template("estimate_detail.html",
        **sidebar_context(), **auth_context(), active_page="estimates", est=est, items=items, notes=notes
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


# ── Notes (internal vs client) ────────────────────────────────────────────────

def get_notes_for(conn, studio_id, role, job_id=None, estimate_id=None, booking_id=None):
    query  = "SELECT * FROM notes WHERE studio_id=?"
    params = [studio_id]

    if job_id:
        query += " AND job_id=?"
        params.append(job_id)
    if estimate_id:
        query += " AND estimate_id=?"
        params.append(estimate_id)
    if booking_id:
        query += " AND booking_id=?"
        params.append(booking_id)

    if not can(role, "view_internal_notes"):
        query += " AND note_type='client'"

    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()


@bp.route("/notes/add", methods=["POST"])
@staff_or_admin_required
def add_note():
    sid  = session["studio_id"]
    role = current_role()
    f    = request.form

    note_type   = f.get("note_type", "internal")
    content     = f.get("content", "").strip()
    job_id      = f.get("job_id") or None
    estimate_id = f.get("estimate_id") or None
    booking_id  = f.get("booking_id") or None
    redirect_to = f.get("redirect_to") or url_for("main.dashboard")

    if note_type == "internal" and not can(role, "view_internal_notes"):
        note_type = "client"

    if content:
        conn = get_db()
        conn.execute("""
            INSERT INTO notes
            (studio_id, job_id, estimate_id, booking_id, note_type,
             content, author_name, author_role)
            VALUES (?,?,?,?,?,?,?,?)
        """, (sid, job_id, estimate_id, booking_id, note_type,
              content, session.get("name", ""), role))
        conn.commit()

    return redirect(redirect_to)


@bp.route("/notes/<int:note_id>/delete", methods=["POST"])
@staff_or_admin_required
def delete_note(note_id):
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    note = conn.execute(
        "SELECT * FROM notes WHERE id=? AND studio_id=?", (note_id, sid)
    ).fetchone()
    if not note:
        return "Not found", 404

    if not can(role, "delete_anything") and note["author_name"] != session.get("name", ""):
        abort(403)

    conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    conn.commit()
    redirect_to = request.form.get("redirect_to") or url_for("main.dashboard")
    return redirect(redirect_to)


# ── Staff Management ──────────────────────────────────────────────────────────

@bp.route("/staff")
@admin_required
def staff_list():
    sid = session["studio_id"]
    conn = get_db()
    all_staff = conn.execute(
        "SELECT * FROM staff WHERE studio_id=? ORDER BY name", (sid,)
    ).fetchall()
    return render_template("staff.html",
        **sidebar_context(), **auth_context(), active_page="staff",
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
        **sidebar_context(), **auth_context(), active_page="staff",
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

def _allowed_media_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_MEDIA_EXTENSIONS


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
        **sidebar_context(), **auth_context(), active_page="media",
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
                if f and f.filename and _allowed_media_file(f.filename):
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
        **sidebar_context(), **auth_context(), active_page="media",
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
    # Allow logged-in staff, or requests that carry a valid tracking token
    # (customer DVI pages load media this way — no staff session needed).
    if not session.get("logged_in"):
        token = request.args.get("token", "").strip()
        if token:
            conn = get_db()
            job = conn.execute(
                "SELECT id FROM jobs WHERE tracking_token=? AND studio_id=?",
                (token, studio_id)
            ).fetchone()
            if not job:
                abort(403)
        else:
            abort(403)
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
        **sidebar_context(), **auth_context(), active_page="customers",
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
        **sidebar_context(), **auth_context(), active_page="customers", errors=errors
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
        **sidebar_context(), **auth_context(), active_page="customers",
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


# ── Follow-up Reminders ──────────────────────────────────────────────────────

@bp.route("/reminders")
@staff_or_admin_required
def reminders_list():
    sid = session["studio_id"]
    svc = ReminderService(_reminder_db_path())
    due      = svc.get_due_reminders(sid)
    upcoming = svc.get_upcoming_reminders(sid)
    return render_template("reminders.html",
        **sidebar_context(), **auth_context(), active_page="reminders",
        due=due, upcoming=upcoming
    )


@bp.route("/reminders/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_reminder():
    sid = session["studio_id"]
    conn = get_db()
    errors = []

    if request.method == "POST":
        f = request.form
        customer_id = f.get("customer_id", "").strip()
        message     = f.get("message", "").strip()
        due_date    = f.get("due_date", "").strip()

        if not customer_id: errors.append("Please select a customer.")
        if not message:     errors.append("Please enter a message.")
        if not due_date:    errors.append("Please select a due date.")

        if not errors:
            svc = ReminderService(_reminder_db_path())
            svc.create_manual_reminder(sid, int(customer_id), message, due_date)
            return redirect(url_for("main.reminders_list"))

    customers = conn.execute(
        "SELECT id, name, phone FROM customers WHERE studio_id=? ORDER BY name", (sid,)
    ).fetchall()
    return render_template("new_reminder.html",
        **sidebar_context(), **auth_context(), active_page="reminders",
        customers=customers, errors=errors
    )


@bp.route("/reminders/<int:reminder_id>/send", methods=["POST"])
@staff_or_admin_required
def send_reminder(reminder_id):
    sid = session["studio_id"]
    svc = ReminderService(_reminder_db_path())
    svc.mark_sent(sid, reminder_id)
    return redirect(url_for("main.reminders_list"))


@bp.route("/reminders/<int:reminder_id>/dismiss", methods=["POST"])
@staff_or_admin_required
def dismiss_reminder(reminder_id):
    sid = session["studio_id"]
    svc = ReminderService(_reminder_db_path())
    svc.dismiss(sid, reminder_id)
    return redirect(url_for("main.reminders_list"))


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


# ── Tracking token helpers ─────────────────────────────────────────────────────

def _generate_tracking_token() -> str:
    """Generate a secure, URL-safe tracking token for a job."""
    return secrets.token_urlsafe(20)


def _get_job_customer(conn, job) -> dict | None:
    """
    Return customer info for a job.
    Prefers jobs.customer_id (direct FK).
    Falls back to vehicle name match for legacy jobs.
    """
    customer_id = job["customer_id"] if "customer_id" in job.keys() else None

    if customer_id:
        row = conn.execute(
            "SELECT id, name, phone FROM customers WHERE id=?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None

    # Legacy fallback: match by vehicle name
    row = conn.execute(
        """
        SELECT c.id, c.name, c.phone
        FROM vehicles v
        JOIN customers c ON v.customer_id = c.id
        WHERE v.make_model = ?
        LIMIT 1
        """,
        (job["car"],)
    ).fetchone()
    return dict(row) if row else None


def _send_job_status_sms(conn, studio_id: int, job_id: int, new_status: str) -> None:
    """Send an SMS status update if the job has a linked customer with a phone number."""
    job = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id)
    ).fetchone()
    if not job:
        return

    token = job["tracking_token"] if "tracking_token" in job.keys() else None
    if not token:
        return

    customer = _get_job_customer(conn, job)
    if not customer or not customer.get("phone"):
        return

    send_status_update_sms(
        phone=customer["phone"],
        job_token=token,
        new_status=new_status,
        customer_name=customer.get("name", ""),
    )


# ── Staff: manually send tracking SMS for a job ───────────────────────────────

@bp.route("/jobs/<int:job_id>/send-tracking-sms", methods=["POST"])
@staff_or_admin_required
def send_job_tracking_sms(job_id):
    """
    Staff-triggered: send (or re-send) the tracking link to the customer via SMS.
    Accessible from the job board or tracking page.
    """
    sid  = session["studio_id"]
    conn = get_db()

    job = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, sid)
    ).fetchone()
    if not job:
        return "Job not found", 404

    # Ensure a token exists (safe to call on legacy jobs)
    token = job["tracking_token"] if "tracking_token" in job.keys() else None
    if not token:
        token = _generate_tracking_token()
        conn.execute(
            "UPDATE jobs SET tracking_token=? WHERE id=?", (token, job_id)
        )
        conn.commit()

    customer = _get_job_customer(conn, job)
    if not customer or not customer.get("phone"):
        # Redirect back with a notice — no phone on file
        redirect_to = request.form.get("redirect_to") or url_for("main.jobs_list")
        return redirect(redirect_to)

    send_tracking_sms(
        phone=customer["phone"],
        job_token=token,
        customer_name=customer.get("name", ""),
    )

    redirect_to = request.form.get("redirect_to") or url_for("main.jobs_list")
    return redirect(redirect_to)


# ── Public customer-facing tracking routes (NO staff login required) ──────────

@bp.route("/customer/tracking/<token>/status")
def customer_tracking_status(token):
    """
    Public JSON endpoint — no login required.
    Polled by the customer page every 10 s to get live status updates
    without reloading the page or sending a new SMS link.
    """
    conn = get_db()
    job = conn.execute(
        "SELECT status FROM jobs WHERE tracking_token = ?", (token,)
    ).fetchone()

    if not job:
        return jsonify({"error": "not_found"}), 404

    status = job["status"]
    progress_index = JOB_STATUSES.index(status) if status in JOB_STATUSES else 0
    total = len(JOB_STATUSES)

    status_messages = {
        "Pending":     "Your vehicle has been received and is queued for service. Our team will begin work soon.",
        "In Progress": "Work is currently underway on your vehicle. We'll update you when it's ready.",
        "Completed":   "✓ Your vehicle is ready! Please contact us to arrange pickup.",
    }

    return jsonify({
        "status":         status,
        "progress_index": progress_index,
        "progress_pct":   round((progress_index + 1) / total * 100, 1),
        "statuses":       JOB_STATUSES,
        "message":        status_messages.get(status, f"Status: {status}"),
    })


@bp.route("/customer/tracking/<token>")
def customer_tracking(token):
    """
    Public route — no login required.
    Customer opens this link from their SMS.
    Shows live progress for their single job.
    """
    conn = get_db()
    job = conn.execute(
        "SELECT j.*, s.name as studio_name, s.city as studio_city, s.logo as studio_logo "
        "FROM jobs j "
        "JOIN studios s ON j.studio_id = s.id "
        "WHERE j.tracking_token = ?",
        (token,)
    ).fetchone()

    if not job:
        return render_template("customer_tracking_invalid.html"), 404

    status = job["status"]
    progress_index = JOB_STATUSES.index(status) if status in JOB_STATUSES else 0
    dvi_url = url_for("main.customer_dvi", token=token)

    return render_template(
        "customer_tracking.html",
        job=job,
        progress_index=progress_index,
        job_statuses=JOB_STATUSES,
        dvi_url=dvi_url,
        token=token,
    )


@bp.route("/customer/tracking/<token>/dvi")
def customer_dvi(token):
    """
    Public route — no login required.
    Shows DVI (Digital Vehicle Inspection) details for the job.
    """
    conn = get_db()
    job = conn.execute(
        "SELECT j.*, s.name as studio_name, s.city as studio_city, s.logo as studio_logo "
        "FROM jobs j "
        "JOIN studios s ON j.studio_id = s.id "
        "WHERE j.tracking_token = ?",
        (token,)
    ).fetchone()

    if not job:
        return render_template("customer_tracking_invalid.html"), 404

    status = job["status"]
    progress_index = JOB_STATUSES.index(status) if status in JOB_STATUSES else 0

    # Load client-visible notes only (no internal notes exposed)
    notes = conn.execute(
        "SELECT * FROM notes WHERE job_id=? AND note_type='client' ORDER BY created_at DESC",
        (job["id"],)
    ).fetchall()

    # Load media (before/during/after photos) for this job
    media = conn.execute(
        "SELECT * FROM media WHERE job_id=? ORDER BY uploaded_at DESC",
        (job["id"],)
    ).fetchall()

    tracking_url = url_for("main.customer_tracking", token=token)

    return render_template(
        "customer_dvi.html",
        job=job,
        progress_index=progress_index,
        job_statuses=JOB_STATUSES,
        notes=notes,
        media=media,
        tracking_url=tracking_url,
        token=token,
    )
