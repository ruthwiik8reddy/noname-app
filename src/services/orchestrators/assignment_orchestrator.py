"""
services/orchestrators/assignment_orchestrator.py — Phase 3: Job & Technician Assignment.

No LLM here, and that is the point. Dispatch is a state machine with rules, and
rules belong in code where they can be tested and audited — not in a prompt.
The orchestrator pattern is about centralising *decisions*, not about using AI
everywhere.

Two invariants the board depends on:

* **Transitions are validated, not assumed.** Pending → Completed skips the work.
  Completed → In Progress rewrites history. Both are rejected with a readable
  reason rather than silently written.

* **Nothing starts unowned.** A job cannot move to In Progress without an
  assigned technician — which is precisely the assumption the inventory scanner
  already makes when it attributes material usage to a job in progress.

Every transition and reassignment is appended to an audit trail, so "who had
this car and when" is answerable months later.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..repositories.job_repository import UNASSIGNED, JobRepository
from ..repositories.staff_repository import StaffRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)

PENDING = "Pending"
IN_PROGRESS = "In Progress"
COMPLETED = "Completed"

BOARD_COLUMNS = [PENDING, IN_PROGRESS, COMPLETED]

# The workflow, stated once, in one place.
ALLOWED_TRANSITIONS: Dict[str, set] = {
    PENDING: {IN_PROGRESS},
    IN_PROGRESS: {COMPLETED, PENDING},   # back to Pending = "put it down for now"
    COMPLETED: set(),                    # terminal; reopening is a deliberate override
}

# Statuses that require an owner before they can be entered.
REQUIRES_ASSIGNEE = {IN_PROGRESS}


class TransitionResult(dict):
    """Dict subclass so routes can `jsonify` it directly."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


class AssignmentOrchestrator(BaseOrchestrator):

    def __init__(
        self,
        job_repository: Optional[JobRepository] = None,
        staff_repository: Optional[StaffRepository] = None,
    ):
        super().__init__(provider=None)
        self.jobs = job_repository or JobRepository()
        self.staff = staff_repository or StaffRepository()

    # ── board ─────────────────────────────────────────────────────────────

    def board(self, studio_id: int) -> Dict[str, Any]:
        columns = self.jobs.board(studio_id, BOARD_COLUMNS)
        technicians = self.staff.list_dispatchable(studio_id)
        load = self.jobs.active_load(studio_id)

        roster = []
        for tech in technicians:
            stats = load.get(int(tech["id"]), {})
            roster.append(
                {
                    **tech,
                    "open_jobs": int(stats.get("open_jobs", 0) or 0),
                    "in_progress": int(stats.get("in_progress", 0) or 0),
                    "committed_hours": round(float(stats.get("committed_hours", 0) or 0), 1),
                }
            )
        roster.sort(key=lambda t: (t["in_progress"], t["open_jobs"], t["name"]))

        unassigned = [
            j for j in columns.get(PENDING, [])
            if not j.get("assigned_staff_id") and (j.get("technician") or UNASSIGNED) == UNASSIGNED
        ]

        return {
            "columns": columns,
            "column_order": BOARD_COLUMNS,
            "technicians": roster,
            "counts": {status: len(jobs) for status, jobs in columns.items()},
            "unassigned_count": len(unassigned),
            "allowed_transitions": {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()},
        }

    # ── assignment ────────────────────────────────────────────────────────

    def assign(self, studio_id: int, job_id: int, staff_id: Optional[int], actor: str) -> TransitionResult:
        job = self.jobs.get(studio_id, job_id)
        if not job:
            return TransitionResult(ok=False, error="not_found", message="Job not found.")

        if job["status"] == COMPLETED:
            return TransitionResult(
                ok=False,
                error="job_completed",
                message="This job is already completed and can't be reassigned.",
            )

        if staff_id in (None, 0, ""):
            self.jobs.assign(studio_id, job_id, None, UNASSIGNED)
            self.jobs.record_assignment(studio_id, job_id, None, UNASSIGNED, actor)
            return TransitionResult(
                ok=True, job_id=job_id, staff_id=None, staff_name=UNASSIGNED,
                message="Job returned to the unassigned pool.",
            )

        member = self.staff.get(studio_id, int(staff_id))
        if not member:
            return TransitionResult(
                ok=False, error="staff_not_found", message="That staff member isn't in this studio."
            )

        previous = job.get("technician") or UNASSIGNED
        self.jobs.assign(studio_id, job_id, int(staff_id), member["name"])
        self.jobs.record_assignment(studio_id, job_id, int(staff_id), member["name"], actor)

        logger.info("Job %s assigned to %s by %s", job_id, member["name"], actor)
        return TransitionResult(
            ok=True,
            job_id=job_id,
            staff_id=int(staff_id),
            staff_name=member["name"],
            previous_technician=previous,
            message=f"Assigned to {member['name']}.",
        )

    # ── transitions ───────────────────────────────────────────────────────

    def transition(
        self, studio_id: int, job_id: int, new_status: str, actor: str, note: str = "", force: bool = False
    ) -> TransitionResult:
        job = self.jobs.get(studio_id, job_id)
        if not job:
            return TransitionResult(ok=False, error="not_found", message="Job not found.")

        current = job.get("status") or PENDING
        if new_status not in BOARD_COLUMNS:
            return TransitionResult(
                ok=False, error="unknown_status", message=f"'{new_status}' isn't a valid job status."
            )

        if current == new_status:
            return TransitionResult(
                ok=True, job_id=job_id, status=new_status, unchanged=True,
                message=f"Job is already {new_status}.",
            )

        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed and not force:
            return TransitionResult(
                ok=False,
                error="invalid_transition",
                message=(
                    f"Can't move a job from {current} to {new_status}."
                    + (f" Allowed from {current}: {', '.join(sorted(allowed))}." if allowed
                       else f" {current} is a terminal state.")
                ),
                current_status=current,
                allowed=sorted(allowed),
            )

        has_owner = bool(job.get("assigned_staff_id")) or (job.get("technician") or UNASSIGNED) != UNASSIGNED
        if new_status in REQUIRES_ASSIGNEE and not has_owner:
            return TransitionResult(
                ok=False,
                error="unassigned",
                message="Assign a technician before starting this job — "
                        "material usage is logged against whoever owns it.",
                current_status=current,
            )

        self.jobs.set_status(studio_id, job_id, new_status)
        self.jobs.record_history(studio_id, job_id, current, new_status, actor, note)
        logger.info("Job %s: %s → %s by %s", job_id, current, new_status, actor)

        return TransitionResult(
            ok=True,
            job_id=job_id,
            status=new_status,
            previous_status=current,
            technician=job.get("technician"),
            triggers_completion=(new_status == COMPLETED),
            message=f"Job moved to {new_status}.",
        )

    # ── suggestion ────────────────────────────────────────────────────────

    def suggest_technician(self, studio_id: int, job_id: int) -> Dict[str, Any]:
        """
        Ranks technicians by a transparent score, so a dispatcher can see *why*
        someone was suggested and disagree with it. Deliberately not an LLM call:
        "who is free" is a counting problem, and a wrong answer here misroutes
        real work.
        """
        job = self.jobs.get(studio_id, job_id)
        if not job:
            raise OrchestratorError("Job not found")

        technicians = self.staff.list_dispatchable(studio_id)
        if not technicians:
            return {"job_id": job_id, "suggestions": [], "message": "No dispatchable staff in this studio."}

        load = self.jobs.active_load(studio_id)
        history = self.jobs.completion_stats(studio_id, days=180)
        service = job.get("service", "")

        ranked: List[Dict[str, Any]] = []
        for tech in technicians:
            sid = int(tech["id"])
            stats = load.get(sid, {})
            open_jobs = int(stats.get("open_jobs", 0) or 0)
            in_progress = int(stats.get("in_progress", 0) or 0)
            hours = float(stats.get("committed_hours", 0) or 0)

            tech_history = history.get(sid, {"total": 0, "by_service": {}})
            service_reps = int(tech_history["by_service"].get(service, 0))

            # Availability dominates; relevant experience breaks ties.
            score = 100.0
            score -= in_progress * 25
            score -= max(open_jobs - in_progress, 0) * 8
            score -= min(hours, 40) * 1.2
            score += min(service_reps, 10) * 4
            if tech.get("role") != "technician":
                score -= 15  # a GM can take a car, but shouldn't be first choice

            reasons = []
            if in_progress == 0:
                reasons.append("no job currently in progress")
            else:
                reasons.append(f"{in_progress} job(s) already in progress")
            if service_reps:
                reasons.append(f"has completed {service_reps} × {service}")
            if hours:
                reasons.append(f"{hours:g}h already committed")

            ranked.append(
                {
                    "staff_id": sid,
                    "name": tech["name"],
                    "role": tech.get("role", ""),
                    "score": round(score, 1),
                    "open_jobs": open_jobs,
                    "in_progress": in_progress,
                    "committed_hours": round(hours, 1),
                    "service_experience": service_reps,
                    "reason": "; ".join(reasons),
                }
            )

        ranked.sort(key=lambda r: -r["score"])
        return {
            "job_id": job_id,
            "service": service,
            "vehicle": job.get("car", ""),
            "suggestions": ranked,
            "top_pick": ranked[0] if ranked else None,
        }

    # ── job creation & detail ─────────────────────────────────────────────

    def create_job(
        self,
        studio_id: int,
        car: str,
        service: str,
        price: int = 0,
        staff_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        if not car.strip() or not service.strip():
            raise OrchestratorError("A job needs both a vehicle and a service")

        job_id = self.jobs.create(studio_id, car.strip(), service.strip(), price, customer_id)
        self.jobs.record_history(studio_id, job_id, None, PENDING, actor, "Job created")

        assignment = None
        if staff_id:
            assignment = self.assign(studio_id, job_id, int(staff_id), actor)

        return {"job_id": job_id, "assignment": assignment}

    def job_detail(self, studio_id: int, job_id: int) -> Dict[str, Any]:
        job = self.jobs.get(studio_id, job_id)
        if not job:
            raise OrchestratorError("Job not found")
        current = job.get("status") or PENDING
        return {
            "job": job,
            "status_history": self.jobs.history(studio_id, job_id),
            "assignment_history": self.jobs.assignment_history(studio_id, job_id),
            "allowed_next": sorted(ALLOWED_TRANSITIONS.get(current, set())),
        }

    def technician_queue(self, studio_id: int, staff_id: int, name: str) -> List[Dict[str, Any]]:
        return self.jobs.list_for_technician(studio_id, staff_id, name)
