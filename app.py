from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = "autofiera-secret-key"
DB_PATH = "studios.db"

TIME_SLOTS = [
    "8:00 AM","8:30 AM","9:00 AM","9:30 AM","10:00 AM","10:30 AM",
    "11:00 AM","11:30 AM","12:00 PM","12:30 PM","1:00 PM","1:30 PM",
    "2:00 PM","2:30 PM","3:00 PM","3:30 PM","4:00 PM","4:30 PM",
]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "logged_in" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def sidebar_context():
    """Common context passed to every template for the sidebar."""
    return {
        "studio": session.get("studio"),
        "city":   session.get("city"),
        "logo":   session.get("logo"),
        "owner":  session.get("owner"),
    }

# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "logged_in" in session else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        conn = get_db()
        studio = conn.execute(
            "SELECT * FROM studios WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        conn.close()
        if studio:
            session.update({
                "logged_in": True,
                "studio_id": studio["id"],
                "studio":    studio["name"],
                "city":      studio["city"],
                "logo":      studio["logo"],
                "owner":     studio["owner"],
            })
            return redirect(url_for("dashboard"))
        error = "Wrong username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    sid = session["studio_id"]
    conn = get_db()
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE studio_id=? ORDER BY id DESC", (sid,)
    ).fetchall()
    # Recent bookings for this studio only
    bookings = conn.execute("""
        SELECT b.*, s.name AS service_name, s.price AS service_price
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        WHERE b.studio_id=?
        ORDER BY b.date DESC, b.time_slot DESC
        LIMIT 5
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
    conn.close()
    return render_template("dashboard.html",
        **sidebar_context(), jobs=jobs, bookings=bookings, stats=stats
    )

# ── Bookings ─────────────────────────────────────────────────────────────────

@app.route("/bookings")
@login_required
def bookings():
    sid = session["studio_id"]
    conn = get_db()
    # Only bookings belonging to THIS studio
    all_bookings = conn.execute("""
        SELECT b.*, s.name AS service_name, s.price AS service_price,
               bay.name AS bay_name
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        LEFT JOIN bays bay ON b.bay_id = bay.id
        WHERE b.studio_id=?
        ORDER BY b.date DESC, b.time_slot
    """, (sid,)).fetchall()
    conn.close()
    return render_template("bookings.html",
        **sidebar_context(), bookings=all_bookings
    )

@app.route("/bookings/new", methods=["GET","POST"])
@login_required
def new_booking():
    sid = session["studio_id"]
    conn = get_db()

    if request.method == "POST":
        f = request.form
        customer_name  = f.get("customer_name","").strip()
        customer_phone = f.get("customer_phone","").strip()
        vehicle        = f.get("vehicle","").strip()
        service_id     = f.get("service_id","").strip()
        bay_id         = f.get("bay_id","").strip() or None
        date           = f.get("date","").strip()
        time_slot      = f.get("time_slot","").strip()
        notes          = f.get("notes","").strip()

        # Server-side validation
        errors = []
        if not customer_name:  errors.append("Customer name is required.")
        if not customer_phone: errors.append("Phone number is required.")
        if not vehicle:        errors.append("Vehicle is required.")
        if not service_id:     errors.append("Please select a service.")
        if not date:           errors.append("Please select a date.")
        if not time_slot:      errors.append("Please select a time slot.")

        if errors:
            # Re-render form with errors
            services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
            bays     = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
            today    = datetime.now().date()
            dates    = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
            conn.close()
            return render_template("new_booking.html",
                **sidebar_context(),
                services=services, bays=bays,
                dates=dates, time_slots=TIME_SLOTS,
                errors=errors, form=f
            )

        # Verify service belongs to this studio (security check)
        svc = conn.execute(
            "SELECT id FROM services WHERE id=? AND studio_id=?", (service_id, sid)
        ).fetchone()
        if not svc:
            conn.close()
            return "Invalid service", 400

        conn.execute("""
            INSERT INTO bookings
            (studio_id, customer_name, customer_phone, vehicle,
             service_id, bay_id, date, time_slot, notes, status)
            VALUES (?,?,?,?,?,?,?,?,?,'Pending')
        """, (sid, customer_name, customer_phone, vehicle,
              service_id, bay_id, date, time_slot, notes))
        conn.commit()
        conn.close()
        return redirect(url_for("bookings"))

    # GET — load form data for THIS studio only
    services = conn.execute("SELECT * FROM services WHERE studio_id=?", (sid,)).fetchall()
    bays     = conn.execute("SELECT * FROM bays WHERE studio_id=?", (sid,)).fetchall()
    today    = datetime.now().date()
    dates    = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
    conn.close()

    return render_template("new_booking.html",
        **sidebar_context(),
        services=services, bays=bays,
        dates=dates, time_slots=TIME_SLOTS,
        errors=[], form={}
    )

@app.route("/bookings/slots")
@login_required
def available_slots():
    sid    = session["studio_id"]
    date   = request.args.get("date")
    bay_id = request.args.get("bay_id")
    conn   = get_db()
    booked = conn.execute(
        """SELECT time_slot FROM bookings
           WHERE studio_id=? AND date=? AND bay_id=? AND status != 'Cancelled'""",
        (sid, date, bay_id)
    ).fetchall()
    conn.close()
    return jsonify({"booked": [r["time_slot"] for r in booked]})

@app.route("/bookings/<int:booking_id>/status", methods=["POST"])
@login_required
def update_booking_status(booking_id):
    status = request.form.get("status")
    conn = get_db()
    # Only update if this booking belongs to the logged-in studio
    conn.execute(
        "UPDATE bookings SET status=? WHERE id=? AND studio_id=?",
        (status, booking_id, session["studio_id"])
    )
    conn.commit()
    conn.close()
    return redirect(url_for("bookings"))

if __name__ == "__main__":
    app.run(debug=True, port=5055)
