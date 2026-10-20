"""
orchestrators/studio_assistant_orchestrator.py — the owner's Studio Assistant.

    owner's words → model (or fallback patterns) → proposed actions
                  → dry run in a rolled-back transaction → previews
                  → owner presses Apply → re-validated + applied atomically

Why a dry run: a plan like "add Ceramic Pro, then raise everything 10%"
has steps that depend on earlier ones. Validating each step against the
live database would wrongly reject step two. Running the whole plan inside
a transaction and rolling it back gives exact previews with zero side effects.

Every request, its plan and its outcome is stored in `owner_requests`, so
there's an audit trail of what the assistant changed and who approved it —
and a list of feature requests it couldn't fulfil.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional

from ...config import Config
from ..assistant import fallback
from ..assistant.actions import ACTIONS, ActionError, catalog, is_missing_info, is_relevant
from ..llm.base import LLMError, LLMProvider
from ..prompts.assistant_prompts import AssistantPrompts
from ..repositories.base import BaseRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)


class StudioAssistantOrchestrator(BaseOrchestrator):

    cache_ttl_seconds = 0

    def __init__(self, provider: Optional[LLMProvider] = None, db_path: Optional[str] = None):
        super().__init__(provider)
        self._db_path = db_path

    # Resolved per call, not at import: the route module builds one instance
    # at import time, before tests (or a CLI) may have pointed Config elsewhere.
    @property
    def db_path(self) -> str:
        return self._db_path or Config.DB_PATH

    @property
    def repo(self) -> BaseRepository:
        return BaseRepository(db_path=self.db_path)

    # ── connections ───────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # A dedicated connection, never the request's: the dry run must be
        # able to roll back without discarding anything else the request did.
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _run(self, studio_id: int, proposed: List[Dict[str, Any]], commit: bool,
             message: str = "") -> List[Dict[str, Any]]:
        """
        Validate and apply each step in order inside one transaction.

        With `message`, each valid step is also checked for relevance to what
        the owner actually asked; unrelated ones are flagged `suspect`.
        """
        conn = self._connect()
        steps: List[Dict[str, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            failed = False
            for raw in proposed[:20]:
                name = str((raw or {}).get("action", "")).strip()
                action = ACTIONS.get(name)
                step = {"action": name or "?", "args": (raw or {}).get("args") or {}, "ok": False,
                        "preview": "", "error": "", "result": "", "suspect": False}
                if not action:
                    step.update(preview=f"Unknown action '{name}'", error="Not something the assistant can do.")
                    failed = True
                else:
                    try:
                        args = action.validate(conn, studio_id, step["args"])
                        step.update(ok=True, preview=action.preview(args))
                        if message and not is_relevant(action, args, message):
                            step["suspect"] = True
                        step["result"] = action.apply(conn, studio_id, args)
                    except ActionError as exc:
                        step.update(preview=action.summary.split(".")[0], error=str(exc))
                        failed = True
                steps.append(step)
            if commit and not failed:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return steps

    # ── context for the model ─────────────────────────────────────────────

    def studio_snapshot(self, studio_id: int) -> Dict[str, Any]:
        r = self.repo
        settings = {row["key"]: row["value"] for row in r.fetch_all(
            "SELECT key, value FROM studio_settings WHERE studio_id=? AND key NOT LIKE 'twilio%'", (studio_id,))}
        return {
            "services": [{"name": s["name"], "price": s["price"], "duration_hours": s["duration_hr"]}
                         for s in r.fetch_all("SELECT name, price, duration_hr FROM services WHERE studio_id=? "
                                              "ORDER BY name", (studio_id,))],
            "bays": [b["name"] for b in r.fetch_all("SELECT name FROM bays WHERE studio_id=? ORDER BY name",
                                                    (studio_id,))],
            "products": [p["name"] for p in r.fetch_all(
                "SELECT name FROM products WHERE studio_id=? AND active=1 ORDER BY name", (studio_id,))],
            "settings": settings,
        }

    # ── public ────────────────────────────────────────────────────────────

    def plan(self, studio_id: int, message: str, actor: str) -> Dict[str, Any]:
        message = (message or "").strip()
        if len(message) < 3:
            raise OrchestratorError("Tell the assistant what you'd like to change.")
        if len(message) > 2000:
            raise OrchestratorError("That's a lot at once — try splitting it into smaller requests.")

        degraded, reason, source = False, "", "ai"
        try:
            parsed = self.call_json(
                AssistantPrompts.plan(message, catalog(), self.studio_snapshot(studio_id)),
                required_keys=("reply", "actions"),
                repair_prompt_builder=AssistantPrompts.repair,
            )
            reply = str(parsed.get("reply") or "").strip()[:600]
            proposed = parsed.get("actions") if isinstance(parsed.get("actions"), list) else []
        except (LLMError, OrchestratorError) as exc:
            logger.info("Assistant planner unavailable, using fallback patterns: %s", exc)
            degraded, reason, source = True, str(exc), "deterministic"
            proposed = fallback.parse(message)
            reply = ("The AI model is offline, so I matched your request against common patterns."
                     if proposed else
                     "The AI model is offline and I couldn't match that request. Try phrasing like "
                     "“add service Ceramic Pro for $899, 6 hours” or “raise all prices by 5%”, "
                     "or try again once the model is running.")

        proposed = [{"action": str(p.get("action", "")), "args": p.get("args") or {}}
                    for p in proposed if isinstance(p, dict)][:20]
        steps = self._run(studio_id, proposed, commit=False, message=message) if proposed else []

        # Steps that only fail for want of a number the owner hasn't given yet
        # aren't errors to show — they're a question. Drop them and ask.
        missing = [s for s in steps if not s["ok"] and is_missing_info(s["error"])]
        if missing:
            proposed = [p for p, s in zip(proposed, steps) if s not in missing]
            steps = self._run(studio_id, proposed, commit=False, message=message) if proposed else []
            if "?" not in reply and not steps:
                fields = sorted({f.replace("_hours", "").replace("_", " ")
                                 for m in missing for f in re.findall(r"'(\w+)'", m["error"])})
                reply = (reply + " " if reply else "") + \
                    f"Before I can do that I need the {', '.join(fields) or 'missing details'}."
        for p, s in zip(proposed, steps):
            p["suspect"] = s["suspect"]
        status = "proposed" if steps else "answered"

        request_id = self.repo.execute(
            "INSERT INTO owner_requests (studio_id, request, reply, plan_json, status, source, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (studio_id, message, reply, json.dumps(proposed), status, source, actor[:80]),
        )
        return self.envelope(
            {"id": request_id, "reply": reply, "steps": self._public(steps), "status": status},
            degraded=degraded, degraded_reason=reason, source=source,
        )

    def apply(self, studio_id: int, request_id: int, selected: Optional[List[int]], actor: str) -> Dict[str, Any]:
        row = self.repo.fetch_one("SELECT * FROM owner_requests WHERE id=? AND studio_id=?", (request_id, studio_id))
        if not row:
            raise OrchestratorError("That request no longer exists.")
        if row["status"] != "proposed":
            raise OrchestratorError(f"This plan was already {row['status']}.")

        proposed = json.loads(row["plan_json"] or "[]")
        if selected is not None:
            proposed = [p for i, p in enumerate(proposed) if i in set(selected)]
        else:
            # Without an explicit choice, never apply a step flagged as unrelated.
            proposed = [p for p in proposed if not p.get("suspect")]
        if not proposed:
            raise OrchestratorError("Select at least one step to apply.")

        steps = self._run(studio_id, proposed, commit=True)
        applied = all(s["ok"] for s in steps)
        has_feature_request = any(s["action"] == "request_feature" and s["ok"] for s in steps)
        status = "applied" if applied else "failed"
        self.repo.execute(
            "UPDATE owner_requests SET status=?, result_json=?, applied_at=datetime('now'), "
            "created_by=COALESCE(NULLIF(created_by,''), ?) WHERE id=? AND studio_id=?",
            (status, json.dumps(self._public(steps)), actor[:80], request_id, studio_id),
        )
        logger.info("Studio assistant request %s for studio %s %s by %s", request_id, studio_id, status, actor)
        return {"id": request_id, "status": status, "steps": self._public(steps),
                "feature_requested": has_feature_request,
                "message": "All changes applied." if applied else
                           "Nothing was changed — fix the step marked in red and try again."}

    def cancel(self, studio_id: int, request_id: int) -> bool:
        return bool(self.repo.execute(
            "UPDATE owner_requests SET status='cancelled' WHERE id=? AND studio_id=? AND status='proposed'",
            (request_id, studio_id)))

    def history(self, studio_id: int, limit: int = 30) -> List[Dict[str, Any]]:
        rows = self.repo.fetch_all(
            "SELECT * FROM owner_requests WHERE studio_id=? ORDER BY id DESC LIMIT ?", (studio_id, limit))
        for r in rows:
            try:
                r["steps"] = json.loads(r.get("result_json") or "[]")
            except json.JSONDecodeError:
                r["steps"] = []
        return rows

    def feature_requests(self, studio_id: int) -> List[Dict[str, Any]]:
        out = []
        for r in self.repo.fetch_all(
                "SELECT id, plan_json, created_at, created_by, status FROM owner_requests "
                "WHERE studio_id=? AND plan_json LIKE '%request_feature%' ORDER BY id DESC LIMIT 50",
                (studio_id,)):
            for step in json.loads(r["plan_json"] or "[]"):
                if step.get("action") == "request_feature" and r["status"] == "applied":
                    out.append({"summary": (step.get("args") or {}).get("summary", ""),
                                "created_at": r["created_at"], "by": r["created_by"]})
        return out

    @staticmethod
    def _public(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"action": s["action"], "ok": s["ok"], "preview": s["preview"],
                 "error": s["error"], "result": s.get("result", ""),
                 "suspect": bool(s.get("suspect")),
                 "warning": ("You didn't ask for this — the assistant may have misunderstood. "
                             "Leave it unticked unless you want it.") if s.get("suspect") else ""}
                for s in steps]
