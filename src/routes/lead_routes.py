"""
routes/lead_routes.py — the sales pipeline UI, plus the command dashboard.

The dashboard lives here rather than in `controller.py` because it is mostly a
composition of agent findings and pipeline state — the two newest things — and
the old dashboard route stays untouched at `/dashboard` for anyone who prefers it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from ..auth import auth_context, staff_or_admin_required
from ..services.agents import TriggerBus
from ..services.agents.leads_agent import LeadsAgent
from ..services.repositories import AgentRepository, LeadRepository
from ..services.repositories.base import BaseRepository
from ..services.repositories.lead_repository import PIPELINE, SOURCES

logger = logging.getLogger(__name__)

bp = Blueprint("leads", __name__)


def _sidebar():
    return {
        "studio": session.get("studio"),
        "city": session.get("city"),
        "logo": session.get("logo"),
        "owner": session.get("owner"),
    }


def _actor() -> str:
    return session.get("name") or session.get("studio") or "staff"


# ══════════════════════════════════════════════════════════════════════════════
# Command dashboard
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/command")
@staff_or_admin_required
def command_center():
    studio_id = session["studio_id"]
    base = BaseRepository()

    jobs = {
        r["status"]: int(r["n"]) for r in base.fetch_all(
            "SELECT status, COUNT(*) n FROM jobs WHERE studio_id=? GROUP BY status", (studio_id,)
        )
    }
    revenue_month = float(base.fetch_scalar(
        "SELECT COALESCE(SUM(price),0) FROM jobs WHERE studio_id=? AND status='Completed' "
        "AND completed_at >= date('now','start of month')", (studio_id,), default=0))

    # 14-day completion trend — the sparkline on the dashboard.
    trend = base.fetch_all(
        """
        SELECT date(completed_at) AS day, COUNT(*) AS jobs, COALESCE(SUM(price),0) AS revenue
        FROM jobs
        WHERE studio_id=? AND status='Completed' AND completed_at >= date('now','-13 days')
        GROUP BY date(completed_at) ORDER BY day
        """,
        (studio_id,),
    )

    return render_template(
        "command.html",
        **_sidebar(), **auth_context(),
        active_page="command",
        jobs=jobs,
        revenue_month=revenue_month,
        trend=trend,
        lead_stats=LeadRepository().stats(studio_id),
        counts=AgentRepository().counts_by_severity(studio_id),
    )


@bp.route("/command/api/pulse")
@staff_or_admin_required
def pulse():
    """Everything the dashboard refreshes on a timer, in one round trip."""
    studio_id = session["studio_id"]
    base = BaseRepository()
    agents = AgentRepository()
    leads = LeadRepository()

    return jsonify({
        "findings": agents.feed(studio_id, 15),
        "counts": agents.counts_by_severity(studio_id),
        "jobs": {
            r["status"]: int(r["n"]) for r in base.fetch_all(
                "SELECT status, COUNT(*) n FROM jobs WHERE studio_id=? GROUP BY status", (studio_id,)
            )
        },
        "leads": leads.stats(studio_id),
        "hot_leads": leads.active(studio_id)[:5],
        "stale_leads": leads.stale(studio_id, 5)[:5],
        "last_runs": agents.last_run_per_agent(studio_id),
    }), 200


# ══════════════════════════════════════════════════════════════════════════════
# Leads
# ══════════════════════════════════════════════════════════════════════════════

@bp.route("/leads")
@staff_or_admin_required
def index():
    studio_id = session["studio_id"]
    repo = LeadRepository()
    return render_template(
        "leads.html",
        **_sidebar(), **auth_context(),
        active_page="leads",
        pipeline=repo.pipeline(studio_id),
        stages=PIPELINE,
        sources=SOURCES,
        stats=repo.stats(studio_id),
        by_source=repo.by_source(studio_id),
        stale=repo.stale(studio_id, 5),
    )


@bp.route("/leads/<int:lead_id>")
@staff_or_admin_required
def detail(lead_id: int):
    studio_id = session["studio_id"]
    repo = LeadRepository()
    lead = repo.get(studio_id, lead_id)
    if not lead:
        return render_template("dvi_error.html", **_sidebar(), **auth_context(),
                               message="Lead not found"), 404

    score, reason = LeadsAgent.score(lead)
    return render_template(
        "lead_detail.html",
        **_sidebar(), **auth_context(),
        active_page="leads",
        lead=lead,
        events=repo.events(studio_id, lead_id),
        stages=PIPELINE,
        score=score,
        score_reason=reason,
        findings=[
            f for f in AgentRepository().open_findings(studio_id, "leads")
            if f.get("entity_id") == lead_id
        ],
    )


@bp.route("/leads/new", methods=["POST"])
@staff_or_admin_required
def create():
    studio_id = session["studio_id"]
    lead_id = LeadRepository().create(studio_id, request.form.to_dict(), _actor())
    # Let the leads agent score and triage the new arrival in the background.
    TriggerBus.emit(studio_id, "lead_created", f"lead {lead_id}")
    return redirect(url_for("leads.detail", lead_id=lead_id))


@bp.route("/leads/<int:lead_id>/status", methods=["POST"])
@staff_or_admin_required
def set_status(lead_id: int):
    payload = request.json if request.is_json else request.form
    status = (payload or {}).get("status", "")
    ok = LeadRepository().update_status(session["studio_id"], lead_id, status, _actor())
    if request.is_json:
        return (jsonify({"ok": True, "status": status}) if ok
                else (jsonify({"error": "Invalid stage."}), 400))
    return redirect(url_for("leads.detail", lead_id=lead_id))


@bp.route("/leads/<int:lead_id>/contact", methods=["POST"])
@staff_or_admin_required
def log_contact(lead_id: int):
    payload = request.json if request.is_json else request.form
    detail = (payload or {}).get("detail", "Contacted")
    LeadRepository().log_contact(session["studio_id"], lead_id, detail, _actor())
    if request.is_json:
        return jsonify({"ok": True}), 200
    return redirect(url_for("leads.detail", lead_id=lead_id))


@bp.route("/leads/<int:lead_id>/convert", methods=["POST"])
@staff_or_admin_required
def convert(lead_id: int):
    customer_id = LeadRepository().convert(session["studio_id"], lead_id, _actor())
    if not customer_id:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({"ok": True, "customer_id": customer_id,
                    "redirect": f"/customers/{customer_id}"}), 200


# ── bulk import ───────────────────────────────────────────────────────────────

# "Priya, +91 98450 11223, Audi Q7, full PPF" — one enquiry per line, in the
# order people naturally write them down.
LINE = re.compile(r"^\s*(?P<name>[^,]+?)\s*[,;]\s*(?P<rest>.+)$")
PHONE = re.compile(r"(\+?\d[\d\s\-()]{6,}\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def parse_pasted(text: str, source: str) -> List[Dict[str, Any]]:
    """
    Deliberately simple, deliberately not an LLM call.

    Parsing "name, phone, vehicle" is a regex problem. Sending contact details
    to a model to split on commas would be slower, less predictable, and would
    push customer data through inference for no benefit.
    """
    rows: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) < 3:
            continue

        phone = PHONE.search(line)
        email = EMAIL.search(line)
        cleaned = line
        for m in (phone, email):
            if m:
                cleaned = cleaned.replace(m.group(0), "")

        parts = [p.strip() for p in re.split(r"[,;|]", cleaned) if p.strip()]
        if not parts:
            continue

        rows.append({
            "name": parts[0][:80],
            "phone": (phone.group(1).strip() if phone else ""),
            "email": (email.group(0) if email else ""),
            "vehicle": parts[1][:80] if len(parts) > 1 else "",
            "service_interest": parts[2][:80] if len(parts) > 2 else "",
            "notes": f"Imported: {line[:200]}",
            "source": source,
        })
    return rows


@bp.route("/leads/import", methods=["POST"])
@staff_or_admin_required
def bulk_import():
    payload = request.json or {}
    text = (payload.get("text") or "").strip()
    source = payload.get("source", "whatsapp")
    if not text:
        return jsonify({"error": "Paste some enquiries first."}), 400

    rows = parse_pasted(text, source if source in SOURCES else "other")
    if not rows:
        return jsonify({"error": "Couldn't read any enquiries. Try one per line, "
                                 "starting with the person's name."}), 400

    studio_id = session["studio_id"]
    result = LeadRepository().bulk_import(studio_id, rows, _actor())
    TriggerBus.emit(studio_id, "lead_created", f"bulk import of {result['created']}")
    return jsonify(result), 200


@bp.route("/leads/api/preview-import", methods=["POST"])
@staff_or_admin_required
def preview_import():
    """Show what the parser found before anything is written."""
    payload = request.json or {}
    rows = parse_pasted((payload.get("text") or "").strip(), payload.get("source", "whatsapp"))
    return jsonify({"rows": rows[:25], "count": len(rows)}), 200
