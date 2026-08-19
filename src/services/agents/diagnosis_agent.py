"""
services/agents/diagnosis_agent.py — diagnoses two different things.

**Business health** (scheduled): throughput, bottlenecks, idle technicians,
revenue drift, jobs stuck in a state. All computed from the database in Python.

**Vehicle symptoms** (on demand): a technician describes what they're seeing —
"swirls under LED light, some orange peel on the bonnet" — and gets structured
paint-defect triage back. This is the DVI pipeline without photos, for when
someone is standing at the car with a phone and it's faster to type.

The business half deliberately avoids the model entirely. "Maria has three jobs
in progress and Carlos has none" is a counting problem. Asking an 8B model to
count is how you get confident wrong answers about your own operation.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..llm.base import LLMError
from ..orchestrators.base import BaseOrchestrator, OrchestratorError
from ..pricing.upcharge_calculator import UpchargeCalculator
from ..prompts.dvi_prompts import DEFECT_TYPES, PANELS
from ..repositories.base import BaseRepository
from ..repositories.job_repository import JobRepository
from ..repositories.staff_repository import StaffRepository
from .base import BaseAgent, Finding

STUCK_DAYS = 3          # in progress this long without completing
IDLE_THRESHOLD = 0      # technicians with zero open jobs
BACKLOG_RATIO = 2.5     # pending per active technician before it's a bottleneck


class VehicleDiagnosisOrchestrator(BaseOrchestrator):
    """
    Symptom-based paint triage. Same closed vocabulary as the photo pipeline, so
    a typed finding and a photographed one price identically — one source of
    truth for what a defect is worth.
    """

    def diagnose(self, studio_id: int, vehicle: str, symptoms: str) -> Dict[str, Any]:
        services = self.repo.fetch_all(
            "SELECT id, name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
        ) if hasattr(self, "repo") else []

        prompt = f"""You are a paint correction specialist triaging a vehicle from a
technician's written description. You cannot see the car — work only from the words.

VEHICLE: {vehicle or 'unspecified'}
TECHNICIAN'S DESCRIPTION: {symptoms}

DEFECT TYPES — use these exact strings only:
{json.dumps(DEFECT_TYPES)}

PANEL NAMES — use these exact strings only:
{json.dumps(PANELS)}

RULES:
1. Only report defects the description actually supports. Do not speculate about
   panels or problems that were not mentioned.
2. If the description is too vague to judge, return an empty findings list and
   say what additional detail would help.
3. severity is 1-5. confidence is 0.0-1.0 and should be LOWER than a photo-based
   assessment would be, because you are working from a description.
4. Do NOT estimate prices or labour hours.

Return ONLY valid JSON:
{{
  "assessment": "One or two sentences summarising the likely condition.",
  "needs_photos": true,
  "clarifying_questions": ["What would help you judge this better"],
  "findings": [
    {{"defect_type": "...", "panel": "...", "severity": 3, "confidence": 0.5,
      "description": "what you believe is happening and why"}}
  ]
}}"""

        parsed = self.call_json(
            prompt, required_keys=("assessment", "findings", "clarifying_questions")
        )

        calc = UpchargeCalculator(services)
        priced, total = [], 0
        for item in parsed.get("findings", []):
            if not isinstance(item, dict):
                continue
            defect = str(item.get("defect_type", "")).lower().replace(" ", "_")
            if defect not in DEFECT_TYPES:
                continue   # outside the vocabulary means we can't price it — drop it
            item["defect_type"] = defect
            enriched = calc.price_finding(item)
            total += int(enriched.get("suggested_upcharge_cents", 0) or 0)
            priced.append(enriched)

        return self.envelope({
            "vehicle": vehicle,
            "assessment": parsed.get("assessment", ""),
            "needs_photos": bool(parsed.get("needs_photos", True)),
            "clarifying_questions": parsed.get("clarifying_questions", []),
            "findings": priced,
            "estimated_total_cents": total,
            "estimated_total_display": f"${total / 100:,.2f}",
            "condition_score": UpchargeCalculator.condition_score(priced),
        }, source="ai")

    @property
    def repo(self) -> BaseRepository:
        if not hasattr(self, "_repo"):
            self._repo = BaseRepository()
        return self._repo


class DiagnosisAgent(BaseAgent):
    name = "diagnosis"
    label = "Diagnosis Agent"
    description = "Diagnoses studio bottlenecks, and triages vehicle symptoms on demand."
    icon = "stethoscope"
    default_interval_mins = 240
    responds_to = ("job_completed", "job_assigned")

    def collect(self) -> List[Finding]:
        jobs = JobRepository()
        staff = StaffRepository()
        base = BaseRepository()
        findings: List[Finding] = []

        # ── Jobs stuck in progress ──
        # `started_at` arrives with migrate_phase2. On a database that predates
        # it we simply skip this check rather than crash — an agent that only
        # works on a fully-migrated schema is an agent that breaks on upgrade.
        stuck = base.fetch_all(
            """
            SELECT id, car, service, technician,
                   CAST(julianday('now') - julianday(started_at) AS INTEGER) AS days_open
            FROM jobs
            WHERE studio_id=? AND status='In Progress' AND started_at != ''
              AND julianday('now') - julianday(started_at) >= ?
            ORDER BY days_open DESC
            """,
            (self.studio_id, STUCK_DAYS),
        ) if base.column_exists("jobs", "started_at") else []
        for job in stuck:
            findings.append(Finding(
                kind="job_stuck",
                severity="warning" if job["days_open"] < 7 else "critical",
                title=f"{job['car']} has been in progress for {job['days_open']} days",
                detail=(
                    f"{job['service']} with {job['technician'] or 'nobody assigned'}. "
                    f"Either it needs attention or the status needs updating — a job that "
                    f"sits open distorts throughput and technician load figures."
                ),
                action_label="Open job",
                action_url=f"/dispatch/job/{job['id']}",
                entity_type="job",
                entity_id=job["id"],
                fingerprint=f"job_stuck:{job['id']}",
                data={"days_open": job["days_open"]},
            ))

        # ── Unassigned backlog vs capacity ──
        counts = jobs.counts_by_status(self.studio_id)
        pending = counts.get("Pending", 0)
        techs = staff.technicians(self.studio_id)
        if techs and pending > len(techs) * BACKLOG_RATIO:
            findings.append(Finding(
                kind="backlog",
                severity="warning",
                title=f"{pending} jobs pending against {len(techs)} technician(s)",
                detail=(
                    f"That's roughly {pending / len(techs):.1f} queued jobs per technician. "
                    f"Either capacity is short this week or work isn't being dispatched."
                ),
                action_label="Open dispatch",
                action_url="/dispatch/",
                fingerprint="backlog:pending",
                data={"pending": pending, "technicians": len(techs)},
            ))

        # ── Idle technicians while work waits ──
        if pending > 0 and base.column_exists("jobs", "assigned_staff_id"):
            load = jobs.active_load(self.studio_id)
            idle = [t for t in techs if int(load.get(int(t["id"]), {}).get("open_jobs", 0) or 0) == IDLE_THRESHOLD]
            if idle and len(idle) < len(techs):
                names = ", ".join(t["name"] for t in idle[:4])
                findings.append(Finding(
                    kind="idle_capacity",
                    severity="opportunity",
                    title=f"{names} {'has' if len(idle) == 1 else 'have'} no assigned work",
                    detail=f"There {'is' if pending == 1 else 'are'} {pending} pending job(s) "
                           f"waiting while {len(idle)} technician(s) sit idle.",
                    action_label="Assign work",
                    action_url="/dispatch/",
                    fingerprint="idle_capacity:" + ",".join(str(t["id"]) for t in idle),
                ))

        # ── Unassigned jobs ──
        has_fk = base.column_exists("jobs", "assigned_staff_id")
        unassigned = int(base.fetch_scalar(
            "SELECT COUNT(*) FROM jobs WHERE studio_id=? AND status='Pending' AND ("
            + ("assigned_staff_id IS NULL AND " if has_fk else "")
            + "(technician IS NULL OR technician='Unassigned'))",
            (self.studio_id,), default=0,
        ))
        if unassigned:
            findings.append(Finding(
                kind="unassigned_jobs",
                severity="warning" if unassigned > 2 else "info",
                title=f"{unassigned} job(s) have no technician",
                detail="A job can't be started until it's assigned, and material usage is "
                       "logged against whoever owns it.",
                action_label="Assign now",
                action_url="/dispatch/",
                fingerprint="unassigned_jobs",
                data={"count": unassigned},
            ))

        # ── Revenue drift ──
        this_month = float(base.fetch_scalar(
            "SELECT COALESCE(SUM(price),0) FROM jobs WHERE studio_id=? AND status='Completed' "
            "AND completed_at >= date('now','start of month')", (self.studio_id,), default=0))
        last_month = float(base.fetch_scalar(
            "SELECT COALESCE(SUM(price),0) FROM jobs WHERE studio_id=? AND status='Completed' "
            "AND completed_at >= date('now','start of month','-1 month') "
            "AND completed_at < date('now','start of month')", (self.studio_id,), default=0))
        if last_month > 0:
            delta = (this_month - last_month) / last_month * 100
            if delta <= -25:
                findings.append(Finding(
                    kind="revenue_drop",
                    severity="warning",
                    title=f"Completed revenue is down {abs(delta):.0f}% on last month",
                    detail=(
                        f"${this_month:,.0f} so far this month against ${last_month:,.0f} last month. "
                        f"Note this counts completed jobs only, so early in the month it will "
                        f"naturally look low."
                    ),
                    action_label="View jobs",
                    action_url="/jobs",
                    fingerprint="revenue_drop",
                    data={"this_month": this_month, "last_month": last_month},
                ))

        return findings
