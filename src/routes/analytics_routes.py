"""
routes/analytics_routes.py — Phase 1 HTTP surface.

Note what this file does NOT contain: no prompt strings, no `requests` import,
no JSON repair, no burn-rate arithmetic. Every handler does the same four
things — read session, call an orchestrator, handle one error class, return.
That is the Orchestrator Pattern holding.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request, session

from ..auth import auth_context, permission_required, staff_or_admin_required
from ..services.llm.base import LLMError
from ..services.orchestrators import InventoryIntelligenceOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)

bp = Blueprint("analytics", __name__, url_prefix="/analytics")

# One instance per process: the TTL cache is only useful if it's shared.
_orchestrator = InventoryIntelligenceOrchestrator()


def _sidebar():
    return {
        "studio": session.get("studio"),
        "city": session.get("city"),
        "logo": session.get("logo"),
        "owner": session.get("owner"),
    }


@bp.route("/")
@permission_required("view_payments")
def dashboard():
    return render_template(
        "analytics.html",
        **_sidebar(),
        **auth_context(),
        active_page="analytics",
        ai_online=_orchestrator.ai_available(),
    )


@bp.route("/api/stock-forecast", methods=["GET", "POST"])
@permission_required("view_payments")
def stock_forecast():
    studio_id = session["studio_id"]
    force = request.args.get("refresh") == "1" or (request.json or {}).get("refresh") if request.is_json else False
    try:
        payload = _orchestrator.stock_forecast(
            studio_id=studio_id,
            studio_name=session.get("studio", ""),
            force_refresh=bool(force),
        )
        return jsonify(payload), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("Stock forecast failed for studio %s", studio_id)
        return jsonify({"error": "Could not build the forecast. Check the server log."}), 500


@bp.route("/api/waste", methods=["GET", "POST"])
@permission_required("view_payments")
def waste_analysis():
    studio_id = session["studio_id"]
    force = request.args.get("refresh") == "1"
    try:
        return jsonify(
            _orchestrator.waste_analysis(
                studio_id=studio_id, studio_name=session.get("studio", ""), force_refresh=force
            )
        ), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("Waste analysis failed for studio %s", studio_id)
        return jsonify({"error": "Could not run the waste analysis."}), 500


@bp.route("/api/item/<int:item_id>")
@permission_required("view_payments")
def item_detail(item_id: int):
    try:
        return jsonify(_orchestrator.item_detail(session["studio_id"], item_id)), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.route("/api/health")
@staff_or_admin_required
def ai_health():
    """Lets the UI show an honest 'local AI offline' badge instead of spinning."""
    from ..services.llm.factory import LLMProviderFactory

    from ..services.llm.gate import GATE

    provider = LLMProviderFactory.text_provider()
    try:
        info = provider.describe()
    except Exception as exc:  # noqa: BLE001
        info = {"provider": "unknown", "available": False, "error": str(exc)}

    # Queue depth and wait times make contention visible. If avg_wait_ms climbs,
    # either raise concurrency to match the backend or reduce agent frequency.
    info["gate"] = GATE.stats()
    return jsonify(info), 200


def invalidate_studio_cache(studio_id: int) -> None:
    """Called by inventory mutations elsewhere so the next view recomputes."""
    _orchestrator.invalidate(studio_id)
