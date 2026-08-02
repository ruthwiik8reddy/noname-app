from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, abort, send_from_directory
from datetime import datetime, timedelta
import logging
import os
import uuid
import secrets
from werkzeug.utils import secure_filename

from .db_manager import get_db
# ── Orchestrator Pattern ──
# Controllers never call an LLM directly. Every AI-backed feature is routed
# through a service class in src/services/orchestrators/. If you find yourself
# importing a model backend or writing a prompt in this file, it belongs there.
from .services.orchestrators import (
    AssistantOrchestrator,
    EstimateOrchestrator,
    InventoryIntelligenceOrchestrator,
    OrchestratorError,
)
from .services.llm.base import LLMError
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

# Shared orchestrator instances. One per process so their TTL caches actually
# cache — a new instance per request would defeat the point.
_inventory_ai = InventoryIntelligenceOrchestrator()
_estimate_ai  = EstimateOrchestrator()
_assistant_ai = AssistantOrchestrator()

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
        estimate_id    = f.get("estimate_id") or None

        # Auto-create or find customer record
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
             service_id, bay_id, date, time_slot, notes, status, customer_id, estimate_id)
            VALUES (?,?,?,?,?,?,?,?,?,'Pending',?,?)
        """, (sid, customer_name, customer_phone, vehicle_name,
              f["service_id"], f.get("bay_id") or None, f["date"], f["time_slot"],
              f.get("notes", "").strip(), customer_id, estimate_id))
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
    # Stamp completed_at when job is marked done
    if new_status == "Completed":
        conn.execute(
            "UPDATE jobs SET completed_at=date('now') WHERE id=? AND studio_id=?",
            (job_id, sid)
        )
    conn.commit()

    if new_status == "Completed":
        svc = ReminderService(_reminder_db_path())
        svc.schedule_for_completed_job(sid, job_id)

    # ── SMS: notify customer of status change ─────────────────────────────
    _send_job_status_sms(conn, sid, job_id, new_status)

    redirect_to = request.form.get("redirect_to") or url_for("main.jobs_list")
    return redirect(redirect_to)

# ── Inventory & Analytics Dashboard ─────────────────────────────────────────

@bp.route("/inventory")
@staff_or_admin_required
def inventory_dashboard():
    sid = session["studio_id"]
    conn = get_db()

    items = conn.execute(
        "SELECT * FROM inventory_items WHERE studio_id=? ORDER BY category, name",
        (sid,)
    ).fetchall()

    # Calculate analytical summary metrics
    total_items = len(items)
    low_stock_items = [i for i in items if i["quantity"] <= i["reorder_level"]]
    total_valuation_cents = sum(int(i["quantity"] * i["cost_per_unit"]) for i in items)

    # Categories list for filters
    categories = sorted(list(set(i["category"] for i in items)))

    return render_template(
        "inventory.html",
        **sidebar_context(), **auth_context(), active_page="inventory",
        items=items,
        total_items=total_items,
        low_stock_count=len(low_stock_items),
        total_valuation_display=f"${total_valuation_cents / 100:,.2f}",
        categories=categories
    )


@bp.route("/inventory/new", methods=["POST"])
@staff_or_admin_required
def new_inventory_item():
    sid = session["studio_id"]
    conn = get_db()
    f = request.form

    sku = f.get("sku", "").strip().upper()
    name = f.get("name", "").strip()
    category = f.get("category", "General").strip()
    qty = float(f.get("quantity", 0))
    unit = f.get("unit", "units").strip()
    reorder_level = float(f.get("reorder_level", 5))
    cost_dollars = float(f.get("cost_per_unit", 0))
    cost_cents = int(cost_dollars * 100)
    supplier = f.get("supplier", "").strip()

    if sku and name:
        cur = conn.execute("""
            INSERT INTO inventory_items 
            (studio_id, sku, name, category, quantity, unit, reorder_level, cost_per_unit, supplier)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (sid, sku, name, category, qty, unit, reorder_level, cost_cents, supplier))
        
        item_id = cur.lastrowid
        conn.execute("""
            INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason)
            VALUES (?,?,?, 'Initial Stock Entry')
        """, (sid, item_id, qty))
        conn.commit()
        _inventory_ai.invalidate(sid)  # stock changed — next forecast recomputes

    return redirect(url_for("main.inventory_dashboard"))


@bp.route("/inventory/<int:item_id>/adjust", methods=["POST"])
@staff_or_admin_required
def adjust_inventory_stock(item_id):
    sid = session["studio_id"]
    conn = get_db()
    f = request.form

    change_qty = float(f.get("change_qty", 0))
    reason = f.get("reason", "Manual Adjustment").strip()

    item = conn.execute(
        "SELECT * FROM inventory_items WHERE id=? AND studio_id=?", (item_id, sid)
    ).fetchone()

    if item and change_qty != 0:
        new_qty = max(0.0, item["quantity"] + change_qty)
        conn.execute(
            "UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE id=?",
            (new_qty, item_id)
        )
        conn.execute(
            "INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason) VALUES (?,?,?,?)",
            (sid, item_id, change_qty, reason)
        )
        conn.commit()
        _inventory_ai.invalidate(sid)  # stock changed — next forecast recomputes

    return redirect(url_for("main.inventory_dashboard"))


@bp.route("/inventory/api/ai-analytics", methods=["POST"])
@staff_or_admin_required
def api_inventory_analytics():
    """
    Legacy endpoint, now backed by the Phase 1 orchestrator.

    The old implementation asked a local model to do the arithmetic and fell
    back to a hand-written low-stock list when it failed. The orchestrator
    computes burn rates and stockout dates in Python first, so this endpoint
    now returns real forecasts whether or not Ollama is running.

    The response is remapped to the original key names so the existing
    inventory.html widget keeps working untouched.
    """
    sid = session["studio_id"]
    try:
        result = _inventory_ai.stock_forecast(sid, session.get("studio", ""))
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400

    briefing = result.get("briefing", {})
    health = result.get("health", {})
    forecasts = result.get("forecasts", [])

    return jsonify({
        "health_score": health.get("health_score", "Unknown"),
        "summary": briefing.get("headline", "") + " " + briefing.get("narrative", ""),
        "urgent_reorders": [
            {
                "sku": f["sku"],
                "name": f["name"],
                "current_qty": f["current_qty"],
                "reorder_level": f["reorder_level"],
                "suggested_order_qty": f["suggested_order_qty"],
                "reason": f["rationale"],
            }
            for f in forecasts if f["urgency"] in ("critical", "warning")
        ],
        "consumption_forecast": [
            {
                "category": f["category"],
                "trend": "High" if f["urgency"] == "critical" else "Stable",
                "insight": f["rationale"],
            }
            for f in forecasts[:6]
        ],
        "cost_optimizations": briefing.get("cost_notes", []),
        "_fallback": result.get("_status", {}).get("degraded", False),
        "_detail_url": "/analytics/",
    }), 200


@bp.route("/inventory/api/lookup-sku")
@staff_or_admin_required
def api_lookup_sku():
    """
    AJAX endpoint used by the barcode scanner.
    Checks if a scanned SKU already exists in the studio's inventory.
    """
    sid = session["studio_id"]
    sku = request.args.get("sku", "").strip()
    
    if not sku:
        return jsonify({"error": "No SKU provided"}), 400

    conn = get_db()
    item = conn.execute(
        "SELECT id, name FROM inventory_items WHERE studio_id=? AND sku=?", 
        (sid, sku)
    ).fetchone()

    if item:
        # Product exists! Return the ID and Name so the frontend can open the Adjust modal
        return jsonify({"exists": True, "id": item["id"], "name": item["name"]})
    else:
        # Product doesn't exist. Tell the frontend to open the New Item modal.
        return jsonify({"exists": False, "sku": sku})


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


@bp.route("/estimates/ai")
@staff_or_admin_required
def ai_estimate():
    """Dedicated AI Estimate page — describe the job, AI suggests line items."""
    return render_template("ai_estimate.html",
        **sidebar_context(), **auth_context(), active_page="estimates",
        prefill_vehicle=request.args.get("vehicle", ""),
        prefill_desc=request.args.get("ai_suggestion", ""),
    )


@bp.route("/estimates/new", methods=["GET", "POST"])
@staff_or_admin_required
def new_estimate():
    sid = session["studio_id"]
    conn = get_db()

    if request.method == "POST":
        f = request.form

        # Customer comes from the CRM lookup — pull from DB, not form fields
        customer_id_raw = f.get("customer_id", "").strip()
        customer_name  = ""
        customer_email = ""
        customer_phone = ""
        vehicle        = f.get("vehicle", "").strip()

        if customer_id_raw:
            cust = conn.execute(
                "SELECT name, email, phone FROM customers WHERE id=? AND studio_id=?",
                (customer_id_raw, sid)
            ).fetchone()
            if cust:
                customer_name  = cust["name"]
                customer_email = cust["email"] or ""
                customer_phone = cust["phone"] or ""

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

        # Build human-readable services summary for booking autofill
        services_summary = ", ".join(
            it["name"] + (f" ×{it['qty']}" if it["qty"] > 1 else "")
            for it in items
        )

        cur = conn.execute("""
            INSERT INTO estimates
            (studio_id, customer_name, customer_email, customer_phone,
             vehicle, status, subtotal, tax_percent, tax_amount, total,
             notes, internal_notes, customer_id, services_summary)
            VALUES (?,?,?,?,?,'Draft',?,?,?,?,?,?,?,?)
        """, (sid, customer_name, customer_email, customer_phone, vehicle,
              subtotal, tax_pct, tax_amount, total,
              f.get("notes", "").strip(),
              f.get("internal_notes", "").strip(),
              customer_id_raw or None,
              services_summary))
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

@bp.route("/estimates/api/generate", methods=["POST"])
@staff_or_admin_required
def api_generate_estimate():
    """
    API endpoint for the frontend to hit when a user clicks 'Generate AI Estimate'.
    Expects a JSON payload: {"notes": "Customer wants a ceramic coating on their Tesla..."}
    """
    sid = session["studio_id"]
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip()

    if not notes:
        return jsonify({"error": "Customer notes are required to generate an estimate."}), 400

    # Delegate to the orchestrator. It re-prices every line from the studio's
    # own catalog after generation, so a hallucinated price can't reach an invoice.
    try:
        return jsonify(_estimate_ai.draft_estimate(sid, notes)), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except LLMError as exc:
        # 503, not 500 — the app is healthy, the local model isn't running.
        return jsonify({"error": str(exc), "ai_offline": True}), 503

@bp.route("/estimates/<int:estimate_id>/lookup")
@staff_or_admin_required
def estimate_lookup(estimate_id):
    """
    AJAX — returns estimate data for the new_booking form autofill.
    Returns customer name, phone, vehicle, services summary, and total.
    """
    sid  = session["studio_id"]
    conn = get_db()
    est  = conn.execute(
        "SELECT * FROM estimates WHERE id=? AND studio_id=?", (estimate_id, sid)
    ).fetchone()
    if not est:
        return jsonify({"error": "Estimate not found"}), 404

    return jsonify({
        "id":               est["id"],
        "customer_name":    est["customer_name"],
        "customer_phone":   est["customer_phone"],
        "customer_email":   est["customer_email"],
        "vehicle":          est["vehicle"],
        "services_summary": est["services_summary"] or "",
        "total":            est["total"],
        "total_display":    f"${est['total']/100:.2f}",
        "status":           est["status"],
    })


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


@bp.route("/customers/<int:customer_id>/lookup")
@staff_or_admin_required
def customer_lookup(customer_id):
    """
    AJAX endpoint — returns customer details + their vehicles as JSON.
    Used by new_booking and new_estimate forms to auto-fill fields
    when staff enters a customer ID.
    """
    sid  = session["studio_id"]
    conn = get_db()

    customer = conn.execute(
        "SELECT id, name, phone, email FROM customers WHERE id=? AND studio_id=?",
        (customer_id, sid)
    ).fetchone()

    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    vehicles = conn.execute(
        "SELECT id, make_model, year FROM vehicles WHERE customer_id=? ORDER BY created_at DESC",
        (customer_id,)
    ).fetchall()

    return jsonify({
        "id":       customer["id"],
        "name":     customer["name"],
        "phone":    customer["phone"],
        "email":    customer["email"] or "",
        "vehicles": [
            {"id": v["id"], "make_model": v["make_model"], "year": v["year"] or ""}
            for v in vehicles
        ],
    })


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

    studio_id = session["studio_id"]

    # ── Pricing questions: ground the answer in the real product catalog ──
    try:
        from .estimate_assistant import build_pricing_facts
        facts = build_pricing_facts(studio_id, question)
    except Exception as exc:  # noqa: BLE001 - a broken hook must not break chat
        logging.getLogger(__name__).warning("Pricing hook failed: %s", exc)
        facts = None

    if facts:
        # Hand the model the computed prices and let it only report them.
        grounded = _assistant_ai.pricing_answer(question, facts)
        if grounded:
            return jsonify(grounded)
        # Model unreachable — fall through to the general assistant.

    return jsonify(_assistant_ai.answer(studio_id, question, attachments))


# ── Account ───────────────────────────────────────────────────────────────────

# Subscription plan definitions — extend these when you add paid tiers
PLANS = {
    "free": {
        "name":        "Free",
        "description": "Everything you need to get started — no credit card required.",
        "status":      "Active",
        "features": [
            "Unlimited bookings",
            "Job tracking & DVI",
            "Customer CRM",
            "Estimates & approvals",
            "Before/after media gallery",
            "AI Assistant",
            "Live customer tracking link",
            "Warranty card PDF",
            "SMS (bring your own Twilio)",
        ],
    },
    # Future tiers go here — e.g. "pro", "enterprise"
}


def _get_studio_plan(studio_id: int) -> dict:
    """Return the current plan for a studio. Extend this when billing is added."""
    # Currently all studios are on the free plan.
    # When you add a `plan` column to the studios table, look it up here.
    return PLANS["free"]


@bp.route("/account")
@login_required
def account():
    sid  = session["studio_id"]
    role = current_role()
    conn = get_db()

    # Twilio config — read from env so it reflects whatever is live
    twilio_sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from  = os.getenv("TWILIO_FROM_NUMBER", "")
    app_base_url = os.getenv("APP_BASE_URL", "")

    # Mask the auth token — show last 4 chars only for confirmation
    twilio_token_masked = ("•" * 20 + twilio_token[-4:]) if twilio_token else ""

    plan = _get_studio_plan(sid)

    return render_template("account.html",
        **sidebar_context(), **auth_context(), active_page="account",
        plan_name=plan["name"],
        plan_description=plan["description"],
        plan_status=plan["status"],
        plan_features=plan["features"],
        twilio_sid=twilio_sid,
        twilio_token_masked=twilio_token_masked,
        twilio_from=twilio_from,
        app_base_url=app_base_url,
        flash_ok=request.args.get("saved"),
        flash_err=request.args.get("error"),
    )


@bp.route("/account/sms-config", methods=["POST"])
@admin_required
def account_sms_config():
    """
    Save Twilio credentials to the .env file.
    Only admins can reach this route.
    """
    f = request.form

    twilio_sid   = f.get("twilio_sid", "").strip()
    twilio_token = f.get("twilio_token", "").strip()
    twilio_from  = f.get("twilio_from", "").strip()
    app_base_url = f.get("app_base_url", "").rstrip("/").strip()

    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    try:
        # Read existing .env
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        else:
            lines = []

        # Keys we manage — map env key → new value
        updates = {
            "TWILIO_ACCOUNT_SID": twilio_sid,
            "TWILIO_FROM_NUMBER": twilio_from,
            "APP_BASE_URL":       app_base_url,
        }
        # Only update auth token if a new one was supplied (not masked placeholder)
        if twilio_token and not twilio_token.startswith("•"):
            updates["TWILIO_AUTH_TOKEN"] = twilio_token

        # Update lines in-place, add missing keys at the end
        handled = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                handled.add(key)
            else:
                new_lines.append(line)

        for key, val in updates.items():
            if key not in handled:
                new_lines.append(f"{key}={val}\n")

        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(new_lines)

        # Also update os.environ for the current process so SMS works immediately
        # without restarting the server
        import os as _os
        for key, val in updates.items():
            _os.environ[key] = val

        return redirect(url_for("main.account") + "?saved=SMS+settings+saved+successfully")

    except Exception as exc:
        return redirect(url_for("main.account") + f"?error=Save+failed:+{exc}")


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

@bp.route("/customer/tracking/<token>/warranty.pdf")
def customer_warranty_pdf(token):
    """
    Public route — no login required.
    Generates and streams the warranty card as a PDF using xhtml2pdf.
    Only works when the job is Completed. Returns 403 otherwise.
    """
    from io import BytesIO
    from xhtml2pdf import pisa
    from flask import Response
    from datetime import datetime

    conn = get_db()
    row = conn.execute(
        """
        SELECT j.*, s.name as studio_name, s.city as studio_city,
               s.owner as studio_owner, s.logo as studio_logo
        FROM jobs j
        JOIN studios s ON j.studio_id = s.id
        WHERE j.tracking_token = ?
        """,
        (token,)
    ).fetchone()

    if not row:
        return "Not found", 404

    if row["status"] != "Completed":
        return "Warranty card is only available once the job is completed.", 403

    # Resolve customer info if linked
    customer = {"name": "", "phone": ""}
    if row["customer_id"]:
        cust = conn.execute(
            "SELECT name, phone FROM customers WHERE id=?", (row["customer_id"],)
        ).fetchone()
        if cust:
            customer = {"name": cust["name"], "phone": cust["phone"]}

    # Format completed date
    completed_raw = row["completed_at"] or ""
    try:
        completed_date = datetime.strptime(completed_raw, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        completed_date = datetime.now().strftime("%B %d, %Y")

    # Price display
    price_cents = row["price"] or 0
    price_display = f"${price_cents / 100:.2f}" if price_cents >= 100 else f"${price_cents}.00"

    # Warranty reference = first 12 chars of token, uppercase
    warranty_id = token[:12].upper()

    # Tracking URL
    tracking_url = url_for("main.customer_tracking", token=token, _external=True)

    studio = {
        "name":  row["studio_name"],
        "city":  row["studio_city"],
        "owner": row["studio_owner"],
    }

    html_string = render_template(
        "warranty_card.html",
        job=row,
        studio=studio,
        customer=customer,
        completed_date=completed_date,
        price_display=price_display,
        warranty_id=warranty_id,
        tracking_url=tracking_url,
    )

    buf = BytesIO()
    pisa_status = pisa.CreatePDF(html_string, dest=buf)
    if pisa_status.err:
        return "PDF generation failed", 500

    buf.seek(0)
    filename = f"warranty-{warranty_id}.pdf"
    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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
        "warranty_url":   url_for("main.customer_warranty_pdf", token=token) if status == "Completed" else None,
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


# ── 3D Paint Health Inspection — constants ───────────────────────────────────

CANONICAL_PANELS = [
    "hood", "roof", "front_bumper", "rear_bumper",
    "front_left_door", "front_right_door",
    "rear_left_door", "rear_right_door",
    "left_fender", "right_fender",
    "left_quarter", "right_quarter",
    "trunk",
]

PANEL_LABELS = {
    "hood": "Hood", "roof": "Roof",
    "front_bumper": "Front Bumper", "rear_bumper": "Rear Bumper",
    "front_left_door": "Front Left Door", "front_right_door": "Front Right Door",
    "rear_left_door": "Rear Left Door", "rear_right_door": "Rear Right Door",
    "left_fender": "Left Fender", "right_fender": "Right Fender",
    "left_quarter": "Left Quarter Panel", "right_quarter": "Right Quarter Panel",
    "trunk": "Trunk / Tailgate",
}

HEALTH_RATINGS = ["excellent", "good", "fair", "poor"]


def _get_panel_inspections(conn, job_id):
    rows = conn.execute(
        "SELECT * FROM panel_inspections WHERE job_id=?", (job_id,)
    ).fetchall()
    by_key = {r["panel_key"]: dict(r) for r in rows}
    result = {}
    for key in CANONICAL_PANELS:
        result[key] = by_key.get(key, {
            "panel_key": key, "health_rating": "", "treatment": "",
            "notes": "", "before_photo": "", "after_photo": "",
        })
    return result


def _serialize_panels(conn, studio_id, job_id):
    panels = _get_panel_inspections(conn, job_id)
    out = []
    for key in CANONICAL_PANELS:
        p = panels[key]
        out.append({
            "panel_key":     key,
            "label":         PANEL_LABELS[key],
            "health_rating": p["health_rating"] or "",
            "treatment":     p["treatment"] or "",
            "notes":         p["notes"] or "",
            "before_photo":  f"/uploads/studio_{studio_id}/{p['before_photo']}" if p["before_photo"] else "",
            "after_photo":   f"/uploads/studio_{studio_id}/{p['after_photo']}" if p["after_photo"] else "",
        })
    return out


@bp.route("/jobs/<int:job_id>/inspection", methods=["GET", "POST"])
@staff_or_admin_required
def job_inspection(job_id):
    sid  = session["studio_id"]
    conn = get_db()
    job = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, sid)
    ).fetchone()
    if not job:
        return "Job not found", 404

    if request.method == "POST":
        f = request.form
        for key in CANONICAL_PANELS:
            health    = f.get(f"{key}_health", "").strip()
            treatment = f.get(f"{key}_treatment", "").strip()
            notes     = f.get(f"{key}_notes", "").strip()
            before    = f.get(f"{key}_before", "").strip()
            after     = f.get(f"{key}_after", "").strip()
            if health or treatment or notes or before or after:
                conn.execute("""
                    INSERT INTO panel_inspections
                    (studio_id, job_id, panel_key, health_rating, treatment, notes, before_photo, after_photo, updated_at)
                    VALUES (?,?,?,?,?,?,?,?, datetime('now'))
                    ON CONFLICT(job_id, panel_key) DO UPDATE SET
                        health_rating=excluded.health_rating,
                        treatment=excluded.treatment,
                        notes=excluded.notes,
                        before_photo=excluded.before_photo,
                        after_photo=excluded.after_photo,
                        updated_at=datetime('now')
                """, (sid, job_id, key, health or "good", treatment, notes, before, after))
        conn.commit()
        return redirect(url_for("main.job_inspection", job_id=job_id))

    panels = _get_panel_inspections(conn, job_id)
    media = conn.execute(
        "SELECT id, filename, stage, caption FROM media WHERE job_id=? ORDER BY uploaded_at DESC",
        (job_id,)
    ).fetchall()
    return render_template("job_inspection.html",
        **sidebar_context(), **auth_context(),
        job=job, panels=panels, panel_labels=PANEL_LABELS,
        canonical_panels=CANONICAL_PANELS, health_ratings=HEALTH_RATINGS,
        media=media, active_page="jobs",
    )


@bp.route("/jobs/<int:job_id>/panels.json")
@staff_or_admin_required
def job_panels_json(job_id):
    sid  = session["studio_id"]
    conn = get_db()
    job = conn.execute(
        "SELECT id FROM jobs WHERE id=? AND studio_id=?", (job_id, sid)
    ).fetchone()
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"panels": _serialize_panels(conn, sid, job_id)})


@bp.route("/customer/tracking/<token>/panels.json")
def customer_panels_json(token):
    conn = get_db()
    job = conn.execute(
        "SELECT id, studio_id FROM jobs WHERE tracking_token=?", (token,)
    ).fetchone()
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"panels": _serialize_panels(conn, job["studio_id"], job["id"])})


# ── Full inspection tracker (video, SVG car, warranty gate, AI) ──
from . import inspection_routes  # noqa: E402,F401


# ── Products catalog + AI sqft estimator ──
from . import product_routes  # noqa: E402,F401





@bp.route('/api/jobs/log-material', methods=['POST'])
@staff_or_admin_required
def log_job_material():
    sid = session.get("studio_id")
    username = session.get("username")
    
    data = request.json
    sku = data.get('sku')
    qty_used = float(data.get('quantity_used', 0))

    if not sku or qty_used <= 0:
        return jsonify({"error": "Valid SKU and quantity are required."}), 400

    conn = get_db()
    
    # 1. Map the session username to the technician's full name 
    # (Because your 'jobs' table tracks techs by their full name, e.g., 'Maria Lopez')
    staff_member = conn.execute(
        "SELECT name FROM staff WHERE username=? AND studio_id=?", 
        (username, sid)
    ).fetchone()
    
    if not staff_member:
        return jsonify({"error": "Technician profile not found."}), 403
        
    tech_name = staff_member["name"]

    # 2. Locate the Technician's Active Job
    active_job = conn.execute(
        "SELECT id FROM jobs WHERE studio_id=? AND technician=? AND status='In Progress'",
        (sid, tech_name)
    ).fetchone()

    if not active_job:
        return jsonify({"error": "You do not have an active 'In Progress' job."}), 400

    # 3. Locate the Inventory Item
    item = conn.execute(
        "SELECT id, name, unit, quantity FROM inventory_items WHERE studio_id=? AND sku=?",
        (sid, sku)
    ).fetchone()
    
    if not item:
        return jsonify({"error": "Product not found in inventory."}), 404

    # 4. Deduct Stock & Record Usage
    new_qty = item["quantity"] - qty_used
    
    # Subtract from global inventory
    conn.execute(
        "UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE id=? AND studio_id=?",
        (new_qty, item["id"], sid)
    )
    
    # Link material to the job
    conn.execute(
        "INSERT INTO job_materials (studio_id, job_id, inventory_id, technician, quantity_used) VALUES (?, ?, ?, ?, ?)",
        (sid, active_job["id"], item["id"], tech_name, qty_used)
    )
    
    # Keep your inventory_logs accurate!
    conn.execute(
        "INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason) VALUES (?, ?, ?, ?)",
        (sid, item["id"], -qty_used, f"Point of use scan for Job #{active_job['id']}")
    )
    
    conn.commit()

    return jsonify({
        "success": True, 
        "message": f"Logged {qty_used} {item['unit']} of {item['name']} to Job #{active_job['id']}",
        "new_stock_level": new_qty
    })