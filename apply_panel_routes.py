#!/usr/bin/env python3
"""
apply_panel_routes.py
-----------------------
Run ONCE from project root: python apply_panel_routes.py

Appends the 3D Paint Health inspection routes + constants to
src/controller.py, safely (skips if already applied).
"""
import os

CONTROLLER = "src/controller.py"

CONSTANTS = '''

# ── 3D Paint Health Inspection — constants ───────────────────────────────────

# Panels supported by the BMW X7 model (separable, clickable meshes).
CANONICAL_PANELS = [
    "hood", "body_shell", "trunk",
    "front_bumper", "rear_bumper",
    "front_left_door", "front_right_door",
    "rear_left_door", "rear_right_door",
    "left_fender", "right_fender",
]

PANEL_LABELS = {
    "hood": "Hood",
    "body_shell": "Roof / Body Shell",
    "trunk": "Trunk / Tailgate",
    "front_bumper": "Front Bumper", "rear_bumper": "Rear Bumper",
    "front_left_door": "Front Left Door", "front_right_door": "Front Right Door",
    "rear_left_door": "Rear Left Door", "rear_right_door": "Rear Right Door",
    "left_fender": "Left Fender", "right_fender": "Right Fender",
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
'''


def main():
    if not os.path.exists(CONTROLLER):
        print(f"Error: {CONTROLLER} not found. Run from project root.")
        return

    with open(CONTROLLER) as f:
        content = f.read()

    if "def job_inspection(" in content:
        print("SKIP — panel routes already present in controller.py")
        return

    with open(CONTROLLER, "a") as f:
        f.write(CONSTANTS)

    print("✓ Appended 3D paint inspection routes to controller.py")


if __name__ == "__main__":
    main()
