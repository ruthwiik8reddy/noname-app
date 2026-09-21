"""
routes/agent_routes.py — the agent control surface.

Thin as ever: session in, runner or repository call, response out. No agent
logic lives here.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request, session

from ..auth import auth_context, roles_required
from ..services.agents import AgentRunner, VehicleDiagnosisOrchestrator, available_agents
from ..services.llm.base import LLMError
from ..services.orchestrators.base import OrchestratorError
from ..services.repositories import AgentRepository

logger = logging.getLogger(__name__)

bp = Blueprint("agents", __name__, url_prefix="/agents")

_runner = AgentRunner()


def _sidebar():
    return {
        "studio": session.get("studio"),
        "city": session.get("city"),
        "logo": session.get("logo"),
        "owner": session.get("owner"),
    }


def _actor() -> str:
    return session.get("name") or session.get("studio") or "staff"


# ── console ───────────────────────────────────────────────────────────────────

@bp.route("/")
@roles_required("admin", "general_manager")
def console():
    studio_id = session["studio_id"]
    repo = AgentRepository()

    # Make sure every registered agent has a settings row so the UI can show it.
    for spec in available_agents():
        repo.ensure_settings(studio_id, spec["name"], spec["default_interval_mins"])

    settings = repo.settings(studio_id)
    last_runs = repo.last_run_per_agent(studio_id)

    agents = []
    for spec in available_agents():
        cfg = settings.get(spec["name"], {})
        run = last_runs.get(spec["name"], {})
        agents.append({
            **spec,
            "enabled": bool(cfg.get("enabled", 1)),
            "interval_mins": cfg.get("interval_mins", spec["default_interval_mins"]),
            "last_run_at": cfg.get("last_run_at"),
            "last_status": run.get("status", "never"),
            "last_findings": run.get("findings_count", 0),
            "last_duration_ms": run.get("duration_ms", 0),
            "last_error": run.get("error", ""),
            "degraded": bool(run.get("degraded", 0)),
            "open_findings": len(repo.open_findings(studio_id, spec["name"], limit=200)),
        })

    return render_template(
        "agents.html",
        **_sidebar(), **auth_context(),
        active_page="agents",
        agents=agents,
        runs=repo.recent_runs(studio_id, 15),
        counts=repo.counts_by_severity(studio_id),
    )


# ── running ───────────────────────────────────────────────────────────────────

@bp.route("/run/<agent_name>", methods=["POST"])
@roles_required("admin", "general_manager")
def run_one(agent_name: str):
    result = _runner.run_agent(session["studio_id"], agent_name, trigger="manual", detail=_actor())
    if result is None:
        return jsonify({"error": f"No agent named '{agent_name}'."}), 404
    return jsonify({
        "agent": result.agent,
        "findings": len(result.findings),
        "degraded": result.degraded,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "summary": result.summary,
    }), (200 if result.ok else 500)


@bp.route("/run-all", methods=["POST"])
@roles_required("admin", "general_manager")
def run_all():
    results = _runner.run_all(session["studio_id"], trigger="manual")
    return jsonify({
        "agents": {
            name: {"findings": len(r.findings), "degraded": r.degraded, "error": r.error}
            for name, r in results.items() if r
        },
        "total_findings": sum(len(r.findings) for r in results.values() if r),
    }), 200


@bp.route("/toggle/<agent_name>", methods=["POST"])
@roles_required("admin", "general_manager")
def toggle(agent_name: str):
    enabled = bool((request.json or {}).get("enabled", True))
    AgentRepository().set_enabled(session["studio_id"], agent_name, enabled)
    return jsonify({"agent": agent_name, "enabled": enabled}), 200


# ── findings ──────────────────────────────────────────────────────────────────

@bp.route("/api/feed")
@roles_required("admin", "general_manager")
def feed():
    repo = AgentRepository()
    studio_id = session["studio_id"]
    return jsonify({
        "findings": repo.feed(studio_id, int(request.args.get("limit", 40))),
        "counts": repo.counts_by_severity(studio_id),
    }), 200


@bp.route("/finding/<int:finding_id>/<action>", methods=["POST"])
@roles_required("admin", "general_manager")
def decide_finding(finding_id: int, action: str):
    if action not in ("dismiss", "resolve", "reopen"):
        return jsonify({"error": "Unknown action."}), 400
    status = {"dismiss": "dismissed", "resolve": "resolved", "reopen": "open"}[action]
    ok = AgentRepository().set_status(session["studio_id"], finding_id, status, _actor())
    return (jsonify({"ok": True, "status": status}), 200) if ok else (jsonify({"error": "Not found."}), 404)


# ── vehicle symptom diagnosis ─────────────────────────────────────────────────

@bp.route("/diagnose", methods=["POST"])
@roles_required("admin", "general_manager")
def diagnose():
    """
    Symptom-based paint triage — the DVI pipeline without photos, for when a
    technician is at the car and typing is faster than shooting.
    """
    payload = request.get_json(silent=True) or {}
    symptoms = (payload.get("symptoms") or "").strip()
    if len(symptoms) < 10:
        return jsonify({"error": "Describe what you're seeing in a little more detail."}), 400

    try:
        result = VehicleDiagnosisOrchestrator().diagnose(
            session["studio_id"], (payload.get("vehicle") or "").strip(), symptoms
        )
        return jsonify(result), 200
    except LLMError as exc:
        return jsonify({"error": str(exc), "ai_offline": True}), 503
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("Vehicle diagnosis failed")
        return jsonify({"error": "Diagnosis failed. Check the server log."}), 500
