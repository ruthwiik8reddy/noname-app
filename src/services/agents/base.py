"""
services/agents/base.py — the Agent contract.

An **orchestrator** answers a question when a page asks it.
An **agent** goes looking for questions nobody asked.

That is the whole distinction, and it drives the design. An orchestrator returns
a payload to a controller and is done. An agent runs on a schedule or a trigger,
with no user waiting, and produces **findings** — durable claims with a severity
and a suggested action, written to the database so a dashboard can show them
later and a person can dismiss them.

Agents do not replace orchestrators; they use them. `InventoryAgent` calls
`InventoryIntelligenceOrchestrator` rather than re-deriving burn rates, because
duplicating that logic is how two parts of a product start disagreeing about
what is true.

Three rules every agent follows:

1. **Findings are idempotent.** Each carries a `fingerprint` — a stable string
   derived from what it's about, not when it ran. Re-running updates the
   existing row rather than stacking a hundred copies of "Ceramic Coating is
   low". Without this, a 15-minute schedule buries the user in a week.

2. **An agent must never break a page.** Failures are caught, recorded on the
   run, and logged. A crashed agent shows as a failed run, not a 500.

3. **Degradation is not failure.** If the model is down, an agent still emits
   whatever its deterministic layer found. Fewer words, same numbers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ordered worst-first; the dashboard sorts on this.
SEVERITIES = ("critical", "warning", "opportunity", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    """
    One thing an agent noticed.

    `fingerprint` identifies the *observation*, not the run: "ceramic coating is
    below reorder" is the same finding today and tomorrow. Include the subject
    (SKU, lead id) but never a timestamp.
    """

    kind: str
    title: str
    severity: str = "info"
    detail: str = ""
    action_label: str = ""
    action_url: str = ""
    entity_type: str = ""
    entity_id: Optional[int] = None
    fingerprint: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            self.severity = "info"
        if not self.fingerprint:
            basis = f"{self.kind}|{self.entity_type}|{self.entity_id}|{self.title}"
            self.fingerprint = hashlib.sha1(basis.encode()).hexdigest()[:20]

    def as_row(self, studio_id: int, agent: str, run_id: Optional[int]) -> tuple:
        return (
            studio_id, run_id, agent, self.kind, self.severity, self.title, self.detail,
            self.action_label, self.action_url, self.entity_type, self.entity_id,
            self.fingerprint, json.dumps(self.data, default=str),
        )


@dataclass
class AgentResult:
    agent: str
    findings: List[Finding]
    degraded: bool = False
    error: str = ""
    duration_ms: int = 0
    summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class BaseAgent(ABC):
    """Subclasses implement `collect()`. Everything else is handled here."""

    # Identity and scheduling defaults — overridden per subclass.
    name: str = "agent"
    label: str = "Agent"
    description: str = ""
    icon: str = "sparkle"
    default_interval_mins: int = 60

    # Trigger names this agent responds to (see agents/triggers.py).
    responds_to: tuple = ()

    def __init__(self, studio_id: int):
        self.studio_id = studio_id

    # ── the one method subclasses write ───────────────────────────────────

    @abstractmethod
    def collect(self) -> List[Finding]:
        """
        Look at the studio and return what's worth saying.

        Returning `[]` is a valid, common result and means "nothing to report".
        Do not manufacture findings to look busy — an agent that cries wolf gets
        ignored, and an ignored agent is worse than no agent.
        """

    # ── lifecycle ─────────────────────────────────────────────────────────

    def run(self) -> AgentResult:
        """Execute `collect()` with timing, degradation tracking and error capture."""
        started = time.time()
        self._degraded = False
        try:
            findings = self.collect() or []
        except Exception as exc:  # noqa: BLE001 - an agent must never break its caller
            logger.warning("Agent %s failed: %s\n%s", self.name, exc, traceback.format_exc(limit=3))
            return AgentResult(
                agent=self.name,
                findings=[],
                error=str(exc)[:400],
                duration_ms=int((time.time() - started) * 1000),
            )

        findings.sort(key=lambda f: SEVERITY_RANK.get(f.severity, 9))
        return AgentResult(
            agent=self.name,
            findings=findings,
            degraded=getattr(self, "_degraded", False),
            duration_ms=int((time.time() - started) * 1000),
            summary=self.summarize(findings),
        )

    @staticmethod
    @contextmanager
    def background(*orchestrators: Any):
        """
        Run borrowed orchestrators at background priority for the duration.

        An agent reusing `InventoryIntelligenceOrchestrator` would otherwise
        inherit its INTERACTIVE priority and compete with the person who is
        actually looking at a page. Borrowing the logic must not mean borrowing
        the urgency.
        """
        from ..llm.gate import Priority

        saved = [(o, o.priority, o.gate_timeout) for o in orchestrators]
        for o in orchestrators:
            o.priority = int(Priority.BACKGROUND)
            o.gate_timeout = 25.0
        try:
            yield
        finally:
            for o, prio, gt in saved:
                o.priority, o.gate_timeout = prio, gt

    def mark_degraded(self) -> None:
        """Call when the model was unreachable but deterministic output survived."""
        self._degraded = True

    def summarize(self, findings: List[Finding]) -> str:
        if not findings:
            return "Nothing to report."
        worst = findings[0].severity
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{n} {sev}" for sev, n in counts.items()]
        return f"{len(findings)} finding(s) — {', '.join(parts)}. Most urgent: {worst}."

    # ── descriptor, for the settings UI ───────────────────────────────────

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        return {
            "name": cls.name,
            "label": cls.label,
            "description": cls.description,
            "icon": cls.icon,
            "default_interval_mins": cls.default_interval_mins,
            "responds_to": list(cls.responds_to),
        }
