"""
routes/dispatch_routes.py — Phase 3 HTTP surface.

Transition rules live in AssignmentOrchestrator, not here. A handler's only
judgement call is which HTTP status to map an orchestrator result onto.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from ..auth import admin_required, auth_context, can, current_role, staff_or_admin_required
from ..services.orchestrators import AssignmentOrchestrator, OrchestratorError
from ..services.orchestrators.assignment_orchestrator import COMPLETED

logger = logging.getLogger(__name__)

bp = Blueprint("dispatch", __name__, url_prefix="/dispatch")

_orchestrator = AssignmentOrchestrator()


def _sidebar():
    return {
        "studio": session.get("studio"),
        "city": session.get("city"),
        "logo": session.get("logo"),
        "owner": session.get("owner"),
    }


def _actor() -> str:
    return session.get("name") or session.get("studio") or "staff"


def _wants_json() -> bool:
    return request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest"


# ── board ─────────────────────────────────────────────────────────────────────

@bp.route("/")
@staff_or_admin_required
def board():
    studio_id = session["studio_id"]
    role = current_role()
    data = _orchestrator.board(studio_id)

    # Technicians see the same board scoped to their own queue.
    my_queue = []
    if role in ("technician", "photographer"):
        staff_id = session.get("staff_id") or session.get("user_id") or 0
        my_queue = _orchestrator.technician_queue(studio_id, int(staff_id or 0), session.get("name", ""))

    services = _orchestrator.jobs.fetch_all(
        "SELECT name, price FROM services WHERE studio_id=? ORDER BY name", (studio_id,)
    )

    return render_template(
        "dispatch.html",
        **_sidebar(),
        **auth_context(),
        active_page="dispatch",
        board=data,
        my_queue=my_queue,
        services=services,
        can_dispatch=can(role, "view_all_jobs"),
    )


@bp.route("/api/board")
@staff_or_admin_required
def board_json():
    return jsonify(_orchestrator.board(session["studio_id"])), 200


# ── assignment ────────────────────────────────────────────────────────────────

@bp.route("/job/<int:job_id>/assign", methods=["POST"])
@staff_or_admin_required
def assign(job_id: int):
    if not can(current_role(), "view_all_jobs"):
        message = "Your role can't reassign jobs."
        return (jsonify({"ok": False, "message": message}), 403) if _wants_json() else (message, 403)

    payload = request.json if request.is_json else request.form
    raw_staff = (payload or {}).get("staff_id", "")
    staff_id = int(raw_staff) if str(raw_staff).strip().isdigit() else None

    result = _orchestrator.assign(session["studio_id"], job_id, staff_id, _actor())

    if _wants_json():
        return jsonify(result), (200 if result.ok else 400)
    return redirect((payload or {}).get("redirect_to") or url_for("dispatch.board"))


@bp.route("/api/job/<int:job_id>/suggest")
@staff_or_admin_required
def suggest(job_id: int):
    try:
        return jsonify(_orchestrator.suggest_technician(session["studio_id"], job_id)), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 404


# ── transitions ───────────────────────────────────────────────────────────────

@bp.route("/job/<int:job_id>/transition", methods=["POST"])
@staff_or_admin_required
def transition(job_id: int):
    payload = request.json if request.is_json else request.form
    new_status = (payload or {}).get("status", "")
    note = (payload or {}).get("note", "")

    studio_id = session["studio_id"]
    result = _orchestrator.transition(studio_id, job_id, new_status, _actor(), note)

    # Completion side effects (reminders + customer SMS) stay in the existing
    # controller helpers — this route triggers them rather than reimplementing.
    if result.ok and result.get("triggers_completion"):
        _run_completion_hooks(studio_id, job_id)

    if _wants_json():
        return jsonify(result), (200 if result.ok else 409)
    return redirect((payload or {}).get("redirect_to") or url_for("dispatch.board"))


def _run_completion_hooks(studio_id: int, job_id: int) -> None:
    """Reuses the app's existing completion behaviour; failures never block the transition."""
    try:
        from ..config import Config
        from ..reminder_service import ReminderService

        ReminderService(Config.DB_PATH).schedule_for_completed_job(studio_id, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reminder scheduling failed for job %s: %s", job_id, exc)

    try:
        from ..controller import _send_job_status_sms
        from ..db_manager import get_db

        _send_job_status_sms(get_db(), studio_id, job_id, COMPLETED)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Status SMS failed for job %s: %s", job_id, exc)


# ── job detail & creation ─────────────────────────────────────────────────────

@bp.route("/job/<int:job_id>")
@staff_or_admin_required
def job_detail(job_id: int):
    try:
        data = _orchestrator.job_detail(session["studio_id"], job_id)
    except OrchestratorError as exc:
        return render_template("dvi_error.html", **_sidebar(), **auth_context(), message=str(exc)), 404

    return render_template(
        "dispatch_job.html",
        **_sidebar(),
        **auth_context(),
        active_page="dispatch",
        **data,
        technicians=_orchestrator.staff.list_dispatchable(session["studio_id"]),
        can_dispatch=can(current_role(), "view_all_jobs"),
    )


@bp.route("/job/new", methods=["POST"])
@staff_or_admin_required
def create_job():
    if not can(current_role(), "view_all_jobs"):
        return redirect(url_for("dispatch.board"))

    form = request.form
    raw_staff = form.get("staff_id", "")
    try:
        _orchestrator.create_job(
            studio_id=session["studio_id"],
            car=form.get("car", ""),
            service=form.get("service", ""),
            price=int(float(form.get("price", 0) or 0)),
            staff_id=int(raw_staff) if str(raw_staff).strip().isdigit() else None,
            actor=_actor(),
        )
    except (OrchestratorError, ValueError) as exc:
        logger.info("Job creation rejected: %s", exc)

    return redirect(url_for("dispatch.board"))
