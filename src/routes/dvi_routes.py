"""
routes/dvi_routes.py — Phase 2 HTTP surface.

Handlers move bytes and session values; the orchestrator does the thinking.
The vision model is never mentioned in this file.
"""

from __future__ import annotations

import logging
import os

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from ..auth import auth_context, can, current_role, staff_or_admin_required
from ..services.llm.base import LLMError
from ..services.orchestrators import DVIOrchestrator, OrchestratorError
from ..services.prompts.dvi_prompts import PANELS
from ..services.repositories import DVIRepository, JobRepository

logger = logging.getLogger(__name__)

bp = Blueprint("dvi", __name__, url_prefix="/dvi")

_orchestrator = DVIOrchestrator()


def _sidebar():
    return {
        "studio": session.get("studio"),
        "city": session.get("city"),
        "logo": session.get("logo"),
        "owner": session.get("owner"),
    }


def _actor() -> str:
    return session.get("name") or session.get("studio") or "staff"


# ── inspection list ───────────────────────────────────────────────────────────

@bp.route("/")
@staff_or_admin_required
def index():
    studio_id = session["studio_id"]
    repo = DVIRepository()
    jobs = JobRepository().fetch_all(
        "SELECT id, car, service, status, technician FROM jobs "
        "WHERE studio_id=? AND status IN ('Pending','In Progress') ORDER BY id DESC",
        (studio_id,),
    )
    return render_template(
        "dvi_index.html",
        **_sidebar(),
        **auth_context(),
        active_page="dvi",
        inspections=repo.list_inspections(studio_id),
        open_jobs=jobs,
        ai_online=_orchestrator.ai_available(),
    )


# ── capture ───────────────────────────────────────────────────────────────────

@bp.route("/job/<int:job_id>")
@staff_or_admin_required
def capture(job_id: int):
    studio_id = session["studio_id"]
    try:
        context = _orchestrator.start_inspection(studio_id, job_id, _actor())
    except OrchestratorError as exc:
        return render_template("dvi_error.html", **_sidebar(), **auth_context(), message=str(exc)), 404

    inspection = context["inspection"]
    repo = DVIRepository()
    return render_template(
        "dvi_capture.html",
        **_sidebar(),
        **auth_context(),
        active_page="dvi",
        job=context["job"],
        inspection=inspection,
        photos=repo.photos(inspection["id"]),
        panels=PANELS,
        ai_online=_orchestrator.ai_available(),
    )


@bp.route("/inspection/<int:inspection_id>/photos", methods=["POST"])
@staff_or_admin_required
def upload_photos(inspection_id: int):
    if not can(current_role(), "upload_media"):
        return jsonify({"error": "Your role can't upload inspection photos."}), 403

    files = request.files.getlist("photos") or request.files.getlist("photo")
    if not files:
        return jsonify({"error": "No photos were attached."}), 400

    try:
        result = _orchestrator.add_photos(
            studio_id=session["studio_id"],
            inspection_id=inspection_id,
            files=files,
            panel=request.form.get("panel", "unspecified"),
            actor=_actor(),
        )
        return jsonify(result), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("DVI upload failed for inspection %s", inspection_id)
        return jsonify({"error": "Upload failed. Check the server log."}), 500


@bp.route("/photo/<int:studio_id>/<path:filename>")
@staff_or_admin_required
def serve_photo(studio_id: int, filename: str):
    # Tenant isolation: a session may only read its own studio's uploads.
    if studio_id != session.get("studio_id"):
        abort(403)
    directory = _orchestrator.upload_dir(studio_id)
    if not os.path.exists(os.path.join(directory, filename)):
        abort(404)
    return send_from_directory(directory, filename)


@bp.route("/photo/<int:photo_id>/delete", methods=["POST"])
@staff_or_admin_required
def delete_photo(photo_id: int):
    studio_id = session["studio_id"]
    repo = DVIRepository()
    photo = repo.get_photo(studio_id, photo_id)
    if not photo:
        return jsonify({"error": "Photo not found."}), 404

    repo.delete_photo(studio_id, photo_id)
    try:
        if photo.get("filepath") and os.path.exists(photo["filepath"]):
            os.remove(photo["filepath"])
    except OSError as exc:
        logger.warning("Could not remove DVI file from disk: %s", exc)
    return jsonify({"ok": True}), 200


# ── analysis ──────────────────────────────────────────────────────────────────

@bp.route("/inspection/<int:inspection_id>/analyze", methods=["POST"])
@staff_or_admin_required
def analyze(inspection_id: int):
    reanalyze = (request.json or {}).get("reanalyze", False) if request.is_json else False
    try:
        payload = _orchestrator.analyze(
            studio_id=session["studio_id"], inspection_id=inspection_id, reanalyze=bool(reanalyze)
        )
        return jsonify(payload), 200
    except LLMError as exc:
        # 503, not 500: the app is fine, the local model isn't running.
        return jsonify({"error": str(exc), "ai_offline": True}), 503
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("DVI analysis failed for inspection %s", inspection_id)
        return jsonify({"error": "Analysis failed. Check the server log."}), 500


@bp.route("/inspection/<int:inspection_id>")
@staff_or_admin_required
def report(inspection_id: int):
    try:
        data = _orchestrator.report(session["studio_id"], inspection_id)
    except OrchestratorError as exc:
        return render_template("dvi_error.html", **_sidebar(), **auth_context(), message=str(exc)), 404

    return render_template(
        "dvi_report.html",
        **_sidebar(),
        **auth_context(),
        active_page="dvi",
        **data,
    )


@bp.route("/inspection/<int:inspection_id>/report.json")
@staff_or_admin_required
def report_json(inspection_id: int):
    try:
        return jsonify(_orchestrator.report(session["studio_id"], inspection_id)), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 404


# ── decisions & conversion ────────────────────────────────────────────────────

@bp.route("/finding/<int:finding_id>/decision", methods=["POST"])
@staff_or_admin_required
def decide(finding_id: int):
    decision = (request.json or request.form or {}).get("decision", "")
    try:
        ok = _orchestrator.decide(session["studio_id"], finding_id, decision, _actor())
        if not ok:
            return jsonify({"error": "Finding not found."}), 404
        return jsonify({"ok": True, "decision": decision}), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/inspection/<int:inspection_id>/to-estimate", methods=["POST"])
@staff_or_admin_required
def to_estimate(inspection_id: int):
    if not can(current_role(), "view_payments"):
        return jsonify({"error": "Your role can't create estimates."}), 403
    try:
        result = _orchestrator.to_estimate(session["studio_id"], inspection_id, _actor())
        result["redirect"] = url_for("main.estimate_detail", estimate_id=result["estimate_id"])
        return jsonify(result), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("DVI→estimate conversion failed for inspection %s", inspection_id)
        return jsonify({"error": "Could not create the estimate."}), 500
