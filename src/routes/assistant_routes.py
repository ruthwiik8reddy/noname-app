"""
routes/assistant_routes.py — the Studio Assistant.

Owner / general manager only: it changes the studio's menu, prices and
settings. Every change is previewed first and only applied on confirmation.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request, session

from ..auth import admin_required, auth_context
from ..services.llm.base import LLMError
from ..services.orchestrators import OrchestratorError, StudioAssistantOrchestrator

logger = logging.getLogger(__name__)

bp = Blueprint("assistant", __name__, url_prefix="/assistant")

_assistant = StudioAssistantOrchestrator()

EXAMPLES = [
    "Add a service called Ceramic Pro 5-Year for $1,199, takes 8 hours",
    "Raise all prices by 7%, rounded to the nearest $5",
    "Set our sales tax to 9.5% and make invoices due in 7 days",
    "Add a Bay 4 and rename Bay 1 to Paint Booth",
    "We want customers to be able to book online from Instagram",
]


def _sidebar():
    return {"studio": session.get("studio"), "city": session.get("city"),
            "logo": session.get("logo"), "owner": session.get("owner")}


def _actor() -> str:
    return session.get("name") or session.get("studio") or "owner"


@bp.route("/")
@admin_required
def console():
    sid = session["studio_id"]
    return render_template(
        "assistant.html", **_sidebar(), **auth_context(), active_page="assistant",
        history=_assistant.history(sid), feature_requests=_assistant.feature_requests(sid),
        examples=EXAMPLES,
    )


@bp.route("/plan", methods=["POST"])
@admin_required
def plan():
    message = ((request.get_json(silent=True) or {}).get("message") or "").strip()
    try:
        return jsonify(_assistant.plan(session["studio_id"], message, _actor())), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 400
    except LLMError as exc:
        return jsonify({"error": str(exc), "ai_offline": True}), 503
    except Exception:  # noqa: BLE001
        logger.exception("Studio assistant planning failed")
        return jsonify({"error": "Something went wrong planning that. Check the server log."}), 500


@bp.route("/requests/<int:request_id>/apply", methods=["POST"])
@admin_required
def apply(request_id: int):
    payload = request.get_json(silent=True) or {}
    selected = payload.get("steps")
    if selected is not None:
        if not isinstance(selected, list) or not all(isinstance(i, int) for i in selected):
            return jsonify({"error": "steps must be a list of step numbers"}), 400
    try:
        return jsonify(_assistant.apply(session["studio_id"], request_id, selected, _actor())), 200
    except OrchestratorError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception:  # noqa: BLE001
        logger.exception("Studio assistant apply failed for request %s", request_id)
        return jsonify({"error": "Nothing was changed — something went wrong. Check the server log."}), 500


@bp.route("/requests/<int:request_id>/cancel", methods=["POST"])
@admin_required
def cancel(request_id: int):
    ok = _assistant.cancel(session["studio_id"], request_id)
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "Not found or already handled."}), 404)
