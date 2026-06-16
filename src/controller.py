from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from functools import wraps
from datetime import datetime, timedelta
from .db_manager import get_db
from .ai_service import AIService

bp = Blueprint("main", __name__)

TIME_SLOTS = [
    "8:00 AM","8:30 AM","9:00 AM","9:30 AM","10:00 AM","10:30 AM",
    "11:00 AM","11:30 AM","12:00 PM","12:30 PM","1:00 PM","1:30 PM",
    "2:00 PM","2:30 PM","3:00 PM","3:30 PM","4:00 PM","4:30 PM",
]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "logged_in" not in session:
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)
    return decorated


def sidebar_context():
    return {
        "studio": session.get("studio"),
        "city":   session.get("city"),
        "logo":   session.get("logo"),
        "owner":  session.get("owner"),
    }


@bp.route("/")
def index():
    return redirect(url_for("main.dashboard") if "logged_in" in session else url_for("main.login"))


@bp.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        conn = get_db()
        studio = conn.execute(
            "SELECT * FROM studios WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        if studio:
            session.update({
                "logged_in": True,
                "studio_id": studio["id"],
                "studio":    studio["name"],
                "city":      studio["city"],
                "logo":      studio["logo"],
                "owner":     studio["owner"],
            })
            return redirect(url_for("main.dashboard"))
        error = "Wrong username or password."
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


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
            "SELECT COUNT(*) FROM bookings WHERE studio_id=? AND date= ?",
            (sid, datetime.now().strftime("%Y-%m-%d"))
        ).fetchone()[0],
    }
    return render_template("dashboard.html",
        **sidebar_context(), jobs=jobs, bookings=bookings, stats=stats
    )


@bp.route("/bookings")
@login_required
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
    return render_template("bookings.html", **sidebar_context(), bookings=all_bookings)


@bp.route("/bookings/new", methods=["GET","POST"])
@login_required
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
            bays = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
            dates = [(datetime.now().date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
            return render_template("new_booking.html", **sidebar_context(),
                services=services, bays=bays, dates=dates, time_slots=TIME_SLOTS,
                errors=errors, form=f)
        conn.execute("""
            INSERT INTO bookings
            (studio_id, customer_name, customer_phone, vehicle,
             service_id, bay_id, date, time_slot, notes, status)
            VALUES (?,?,?,?,?,?,?,?,?,'Pending')
        """, (sid, f["customer_name"].strip(), f["customer_phone"].strip(),
              f["vehicle"].strip(), f["service_id"],
              f.get("bay_id") or None, f["date"], f["time_slot"],
              f.get("notes", "").strip()))
        conn.commit()
        return redirect(url_for("main.bookings"))
    services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
    bays = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
    dates = [(datetime.now().date() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
    return render_template("new_booking.html", **sidebar_context(),
        services=services, bays=bays, dates=dates, time_slots=TIME_SLOTS,
        errors=[], form={})


@bp.route("/bookings/slots")
@login_required
def available_slots():
    sid = session["studio_id"]
    date = request.args.get("date")
    bay_id = request.args.get("bay_id")
    conn = get_db()
    booked = conn.execute(
        "SELECT time_slot FROM bookings WHERE studio_id=? AND date=? AND bay_id=? AND status != 'Cancelled'",
        (sid, date, bay_id)
    ).fetchall()
    return jsonify({"booked": [r["time_slot"] for r in booked]})


@bp.route("/bookings/<int:booking_id>/status", methods=["POST"])
@login_required
def update_booking_status(booking_id):
    conn = get_db()
    conn.execute(
        "UPDATE bookings SET status=? WHERE id=? AND studio_id=?",
        (request.form.get("status"), booking_id, session["studio_id"])
    )
    conn.commit()
    return redirect(url_for("main.bookings"))


@bp.route("/estimates")
@login_required
def estimates():
    sid = session["studio_id"]
    conn = get_db()
    all_estimates = conn.execute(
        "SELECT * FROM estimates WHERE studio_id=? ORDER BY created_at DESC", (sid,)
    ).fetchall()
    return render_template("estimates.html", **sidebar_context(), estimates=all_estimates)


@bp.route("/estimates/new", methods=["GET","POST"])
@login_required
def new_estimate():
    sid = session["studio_id"]
    conn = get_db()
    if request.method == "POST":
        f = request.form
        names = f.getlist("item_name")
        descs = f.getlist("item_desc")
        quantities = f.getlist("item_qty")
        unit_prices = f.getlist("item_price")
        items = []
        for i in range(len(names)):
            if names[i].strip() and unit_prices[i].strip():
                qty = int(quantities[i] or 1)
                price = int(float(unit_prices[i] or 0) * 100)
                items.append({
                    "name": names[i].strip(),
                    "desc": descs[i].strip() if i < len(descs) else "",
                    "qty": qty,
                    "price": price,
                    "total": qty * price,
                })
        subtotal = sum(it["total"] for it in items)
        tax_pct = float(f.get("tax_percent", 8.5))
        tax_amount = int(subtotal * tax_pct / 100)
        total = subtotal + tax_amount
        cur = conn.execute("""
            INSERT INTO estimates
            (studio_id, customer_name, customer_email, customer_phone,
             vehicle, status, subtotal, tax_percent, tax_amount, total,
             notes, internal_notes)
            VALUES (?,?,?,?,?,'Draft',?,?,?,?,?,?)
        """, (sid, f.get("customer_name", "").strip(), f.get("customer_email", "").strip(),
              f.get("customer_phone", "").strip(), f.get("vehicle", "").strip(),
              subtotal, tax_pct, tax_amount, total,
              f.get("notes", "").strip(), f.get("internal_notes", "").strip()))
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
    return render_template("new_estimate.html", **sidebar_context(), services=services)


@bp.route("/estimates/<int:estimate_id>")
@login_required
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
    return render_template("estimate_detail.html", **sidebar_context(), est=est, items=items)


@bp.route("/estimates/<int:estimate_id>/send", methods=["POST"])
@login_required
def send_estimate(estimate_id):
    conn = get_db()
    conn.execute(
        "UPDATE estimates SET status='Sent' WHERE id=? AND studio_id=?",
        (estimate_id, session["studio_id"])
    )
    conn.commit()
    return redirect(url_for("main.estimate_detail", estimate_id=estimate_id))


@bp.route("/assistant")
@login_required
def assistant():
    return render_template("assistant.html", **sidebar_context())


@bp.route("/assistant/query", methods=["POST"])
@login_required
def assistant_query():
    data = request.get_json(silent=True) or request.form
    question = (data.get("question") if isinstance(data, dict) else None) or request.form.get("question", "")
    attachments = []
    if isinstance(data, dict):
        attachments = data.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [item.strip() for item in attachments.split(",") if item.strip()]
    question = question.strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400

    service = AIService()
    answer = service.answer(session["studio_id"], question, attachments)
    return jsonify(answer.as_dict())


@bp.route("/estimates/<int:estimate_id>/approve", methods=["GET","POST"])
def approve_estimate(estimate_id):
    conn = get_db()
    est = conn.execute(
        "SELECT e.*, s.name as studio_name FROM estimates e JOIN studios s ON e.studio_id=s.id WHERE e.id=?",
        (estimate_id,)
    ).fetchone()
    if not est:
        return "Estimate not found", 404
    if request.method == "POST":
        action = request.form.get("action")
        signature = request.form.get("signature", "").strip()
        if action == "approve" and signature:
            conn.execute(
                "UPDATE estimates SET status='Approved', signature=?, approved_at=datetime('now') WHERE id=?",
                (signature, estimate_id)
            )
        elif action == "revision":
            conn.execute(
                "UPDATE estimates SET status='Revision Requested' WHERE id=?",
                (estimate_id,)
            )
        conn.commit()
        return render_template("approve_success.html", est=est, revision=(action == "revision"))
    items = conn.execute(
        "SELECT * FROM estimate_items WHERE estimate_id=?", (estimate_id,)
    ).fetchall()
    return render_template("approve_estimate.html", est=est, items=items)
