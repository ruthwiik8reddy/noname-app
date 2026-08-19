"""
services/agents/runtime.py — how agents actually get executed.

Three entry points, one execution path:

  * **Manual** — someone presses "Run now".
  * **Scheduled** — a background thread ticks every minute and runs whatever is due.
  * **Triggered** — something happened (stock moved, lead created) and an agent
    that cares about that event runs immediately.

All three funnel through `AgentRunner.run_agent`, so a run is recorded and
findings are persisted identically no matter what caused it.

**On the scheduler being a thread.** This is a single-process Flask app with a
SQLite database, so a thread is the honest fit — no broker, no worker fleet, no
extra thing to keep alive. It is deliberately conservative: it skips entirely
under Flask's reloader parent process (which would otherwise give you two
schedulers), holds a lock so runs can't overlap, and catches everything. If this
app ever runs under multiple workers, this needs to become a real task queue —
several processes each running the same agent would produce duplicate work and
contend for SQLite writes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Type

from ..repositories.agent_repository import AgentRepository
from .base import BaseAgent, AgentResult
from .diagnosis_agent import DiagnosisAgent
from .estimate_agent import EstimateAgent
from .inventory_agent import InventoryAgent
from .leads_agent import LeadsAgent

logger = logging.getLogger(__name__)

REGISTRY: Dict[str, Type[BaseAgent]] = {
    InventoryAgent.name: InventoryAgent,
    LeadsAgent.name: LeadsAgent,
    EstimateAgent.name: EstimateAgent,
    DiagnosisAgent.name: DiagnosisAgent,
}

SCHEDULER_TICK_SECONDS = 60
_scheduler_started = False
_run_lock = threading.Lock()


def available_agents() -> List[Dict[str, Any]]:
    return [cls.describe() for cls in REGISTRY.values()]


class AgentRunner:
    """Executes agents and persists the results."""

    def __init__(self, repository: Optional[AgentRepository] = None):
        self.repo = repository or AgentRepository()

    def run_agent(
        self, studio_id: int, agent_name: str, trigger: str = "manual", detail: str = ""
    ) -> Optional[AgentResult]:
        cls = REGISTRY.get(agent_name)
        if not cls:
            logger.warning("Unknown agent requested: %s", agent_name)
            return None

        self.repo.ensure_settings(studio_id, agent_name, cls.default_interval_mins)
        run_id = self.repo.start_run(studio_id, agent_name, trigger, detail)

        result = cls(studio_id).run()

        if result.ok:
            self.repo.save_findings(studio_id, agent_name, run_id, result.findings)
            # Anything this agent no longer reports is considered fixed.
            self.repo.resolve_missing(
                studio_id, agent_name, [f.fingerprint for f in result.findings]
            )

        self.repo.finish_run(
            run_id,
            status="ok" if result.ok else "failed",
            findings_count=len(result.findings),
            duration_ms=result.duration_ms,
            degraded=result.degraded,
            error=result.error,
        )
        self.repo.touch_last_run(studio_id, agent_name)

        logger.info(
            "Agent %s (%s) for studio %s: %d finding(s) in %dms%s",
            agent_name, trigger, studio_id, len(result.findings), result.duration_ms,
            " [degraded]" if result.degraded else "",
        )
        return result

    def run_all(self, studio_id: int, trigger: str = "manual") -> Dict[str, AgentResult]:
        return {
            name: self.run_agent(studio_id, name, trigger)
            for name in REGISTRY
        }

    def run_due(self, studio_id: int) -> List[str]:
        for name, cls in REGISTRY.items():
            self.repo.ensure_settings(studio_id, name, cls.default_interval_mins)
        due = self.repo.due_agents(studio_id)
        for name in due:
            self.run_agent(studio_id, name, trigger="scheduled")
        return due


# ── Trigger bus ───────────────────────────────────────────────────────────────

class TriggerBus:
    """
    Fire-and-forget event dispatch.

    Triggers run on a background thread so a technician marking a job complete
    never waits for an agent — and never sees a 500 because one failed.
    """

    @staticmethod
    def emit(studio_id: int, event: str, detail: str = "", synchronous: bool = False) -> List[str]:
        interested = [n for n, c in REGISTRY.items() if event in c.responds_to]
        if not interested:
            return []

        def _work() -> None:
            runner = AgentRunner()
            for name in interested:
                try:
                    runner.run_agent(studio_id, name, trigger=f"event:{event}", detail=detail)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Triggered agent %s failed on %s: %s", name, event, exc)

        if synchronous:
            _work()
        else:
            threading.Thread(target=_work, daemon=True, name=f"trigger-{event}").start()
        return interested


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _studio_ids() -> List[int]:
    from ..repositories.base import BaseRepository
    return [int(r["id"]) for r in BaseRepository().fetch_all("SELECT id FROM studios")]


def _loop(app: Any) -> None:
    logger.info("Agent scheduler started (tick %ss)", SCHEDULER_TICK_SECONDS)
    # Let the app finish booting before the first sweep.
    time.sleep(15)
    while True:
        try:
            if _run_lock.acquire(blocking=False):
                try:
                    with app.app_context():
                        runner = AgentRunner()
                        for studio_id in _studio_ids():
                            due = runner.run_due(studio_id)
                            if due:
                                logger.info("Scheduled run for studio %s: %s", studio_id, due)
                finally:
                    _run_lock.release()
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.warning("Scheduler tick failed: %s", exc)
        time.sleep(SCHEDULER_TICK_SECONDS)


def start_scheduler(app: Any) -> bool:
    """
    Start the background scheduler. Returns True if it started.

    Skipped when AGENT_SCHEDULER=0, and skipped in the Flask reloader's parent
    process — otherwise every code change would leave a second scheduler running.
    """
    global _scheduler_started
    if _scheduler_started:
        return False
    if str(os.getenv("AGENT_SCHEDULER", "1")).lower() in ("0", "false", "no"):
        logger.info("Agent scheduler disabled via AGENT_SCHEDULER=0")
        return False
    # Under `flask run --reload`, only the child process has this set.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False

    _scheduler_started = True
    threading.Thread(target=_loop, args=(app,), daemon=True, name="agent-scheduler").start()
    return True
