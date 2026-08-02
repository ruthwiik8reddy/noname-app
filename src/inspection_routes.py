"""
inspection_routes.py — Full Autofiera cinematic inspection tracker
-------------------------------------------------------------------
Append to controller.py via apply_inspection_routes.py (or import).

Customer (public, token-auth):
  /customer/tracking/<token>/inspection       cinematic page (video, ring, tracker, SVG car, warranty, AI)
  /customer/tracking/<token>/checklist.json   per-panel data
  /customer/tracking/<token>/warranty.json    warranty (GATED: ready + paid)
  /customer/tracking/<token>/chat             AI concierge

Staff:
  /jobs/<id>/inspection-report                GET/POST rich per-panel entry
  /jobs/<id>/inspection-upload/<panel>        POST photo (before/after)
  /jobs/<id>/payment                          POST toggle payment_received (admin)
"""
import os, json
from flask import request, redirect, url_for, session, jsonify, render_template, Response
from werkzeug.utils import secure_filename

from .db_manager import get_db
from .auth import staff_or_admin_required, admin_required
from . import controller as _c

bp = _c.bp

INSPECTION_PANELS = [
    ("front-bumper","Front Bumper"), ("hood","Hood"), ("windshield","Windshield"),
    ("roof","Roof"), ("rear-glass","Rear Glass"), ("trunk","Trunk"),
    ("door-l","Left Doors"), ("door-r","Right Doors"),
]
PANEL_LABELS = dict(INSPECTION_PANELS)
UPLOAD_BASE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "uploads")


def _updir(sid):
    p = os.path.join(UPLOAD_BASE, f"studio_{sid}", "inspection"); os.makedirs(p, exist_ok=True); return p

def _job_by_token(conn, token):
    return conn.execute("SELECT * FROM jobs WHERE tracking_token=?", (token,)).fetchone()

def _checklist(conn, job_id):
    rows = conn.execute("SELECT * FROM panel_inspections WHERE job_id=?", (job_id,)).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out[d["panel_key"]] = {
            "thickness": d.get("thickness") or "",
            "solution": d.get("solution") or d.get("treatment") or "",
            "notes": d.get("notes") or "",
            "images": json.loads(d.get("images") or "[]"),
            "after_images": json.loads(d.get("after_images") or "[]"),
        }
    return out

def _is_ready(job):
    return (job["status"] or "") in ("Ready", "Completed")

def _is_paid(job):
    try:
        return int(job["payment_received"] or 0) == 1
    except (KeyError, IndexError, TypeError):
        return False


# ═══ CUSTOMER: cinematic page ═══
@bp.route("/customer/tracking/<token>/inspection")
def cust_inspection(token):
    conn = get_db()
    job = _job_by_token(conn, token)
    if not job:
        return render_template("customer_tracking_invalid.html"), 404
    return render_template("customer_inspection.html",
        token=token,
        car=job["car"] or "Your Vehicle",
        reg="",
        name=(job["customer_id"] and _cust_name(conn, job["customer_id"])) or "Valued Client",
        svc=job["service"] or "Ceramic Coating",
        status=job["status"] or "Inspection",
    )

def _cust_name(conn, cid):
    r = conn.execute("SELECT name FROM customers WHERE id=?", (cid,)).fetchone()
    return r["name"] if r else ""


# ═══ CUSTOMER: data APIs ═══
@bp.route("/customer/tracking/<token>/checklist.json")
def cust_checklist(token):
    conn = get_db(); job = _job_by_token(conn, token)
    if not job: return jsonify({}), 404
    return jsonify(_checklist(conn, job["id"]))


@bp.route("/customer/tracking/<token>/warranty.json")
def cust_warranty(token):
    conn = get_db(); job = _job_by_token(conn, token)
    if not job: return jsonify({}), 404
    # ── GATE: only when vehicle is Ready AND payment received ──
    if not (_is_ready(job) and _is_paid(job)):
        return jsonify({"available": False,
                        "reason": "Warranty is issued once the vehicle is ready and payment is complete."})
    w = conn.execute("SELECT * FROM warranties WHERE job_id=?", (job["id"],)).fetchone()
    if w:
        return jsonify({"available": True, "product_name": w["product_name"], "years": w["years"]})
    svc = job["service"] or ""
    years = 7 if "Nano Diamond" in svc else 5 if "Graphene" in svc else 10 if "PPF" in svc else 5 if "Ceramic" in svc else 2
    return jsonify({"available": True, "product_name": svc, "years": years})


@bp.route("/customer/tracking/<token>/chat", methods=["POST"])
def cust_chat(token):
    conn = get_db(); job = _job_by_token(conn, token)
    if not job: return jsonify({"reply": "Vehicle not found."}), 404
    msg = (request.get_json(silent=True) or {}).get("message", "").strip()
    if not msg: return jsonify({"reply": "Please ask a question."})
    try:
        from .services.orchestrators import AssistantOrchestrator

        reply = AssistantOrchestrator().concierge_reply(
            studio_id=job["studio_id"],
            vehicle=job["car"] or "vehicle",
            service=job["service"] or "detailing",
            panel_data=_checklist(conn, job["id"]),
            message=msg,
        )
    except Exception:  # noqa: BLE001 - a customer-facing page must never 500 here
        reply = ("Your vehicle is receiving our full detailing treatment. "
                 "Please contact the studio for specifics.")
    return jsonify({"reply": reply})


# ═══ STAFF: rich per-panel entry ═══
@bp.route("/jobs/<int:job_id>/inspection-report", methods=["GET", "POST"])
@staff_or_admin_required
def inspection_report(job_id):
    sid = session["studio_id"]; conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, sid)).fetchone()
    if not job: return "Job not found", 404

    if request.method == "POST":
        f = request.form
        for key, _label in INSPECTION_PANELS:
            thickness = f.get(f"{key}__thickness", "").strip()
            solution  = f.get(f"{key}__solution", "").strip()
            notes     = f.get(f"{key}__notes", "").strip()
            if thickness or solution or notes:
                conn.execute("""
                    INSERT INTO panel_inspections
                      (studio_id, job_id, panel_key, thickness, solution, notes, updated_at)
                    VALUES (?,?,?,?,?,?, datetime('now'))
                    ON CONFLICT(job_id, panel_key) DO UPDATE SET
                      thickness=excluded.thickness, solution=excluded.solution,
                      notes=excluded.notes, updated_at=datetime('now')
                """, (sid, job_id, key, thickness, solution, notes))
        conn.commit()
        return redirect(url_for("main.inspection_report", job_id=job_id))

    return render_template("staff_inspection_report.html",
        **_c.sidebar_context(), **_c.auth_context(),
        job=job, panels=INSPECTION_PANELS, data=_checklist(conn, job_id),
        active_page="jobs")


# ═══ STAFF: photo upload (before/after) ═══
@bp.route("/jobs/<int:job_id>/inspection-upload/<panel>", methods=["POST"])
@staff_or_admin_required
def inspection_upload(job_id, panel):
    sid = session["studio_id"]; conn = get_db()
    job = conn.execute("SELECT id FROM jobs WHERE id=? AND studio_id=?", (job_id, sid)).fetchone()
    if not job: return jsonify({"error": "not found"}), 404

    file = request.files.get("photo")
    tag  = request.form.get("tag", "before")  # before | after
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400

    fn = secure_filename(f"{job_id}_{panel}_{tag}_{file.filename}")
    file.save(os.path.join(_updir(sid), fn))
    url = f"/static/uploads/studio_{sid}/inspection/{fn}"

    row = conn.execute("SELECT images, after_images FROM panel_inspections WHERE job_id=? AND panel_key=?",
                       (job_id, panel)).fetchone()
    images = json.loads(row["images"]) if row and row["images"] else []
    after  = json.loads(row["after_images"]) if row and row["after_images"] else []
    images.append(url)
    if tag == "after": after.append(url)

    conn.execute("""
        INSERT INTO panel_inspections (studio_id, job_id, panel_key, images, after_images, updated_at)
        VALUES (?,?,?,?,?, datetime('now'))
        ON CONFLICT(job_id, panel_key) DO UPDATE SET
          images=excluded.images, after_images=excluded.after_images, updated_at=datetime('now')
    """, (sid, job_id, panel, json.dumps(images), json.dumps(after)))
    conn.commit()
    return jsonify({"ok": True, "url": url, "tag": tag})


# ═══ ADMIN: mark payment received (the warranty gate) ═══
@bp.route("/jobs/<int:job_id>/payment", methods=["POST"])
@staff_or_admin_required
def toggle_payment(job_id):
    sid = session["studio_id"]; conn = get_db()
    received = 1 if request.form.get("payment_received") == "on" else 0
    conn.execute("UPDATE jobs SET payment_received=?, payment_status=? WHERE id=? AND studio_id=?",
                 (received, "paid" if received else "unpaid", job_id, sid))
    conn.commit()
    return redirect(request.form.get("redirect_to") or url_for("main.inspection_report", job_id=job_id))
