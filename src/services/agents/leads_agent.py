"""
services/agents/leads_agent.py — watches the sales pipeline.

The scoring is **deterministic Python**, not a model call, for the same reason
the burn-rate maths is: a score that changes between runs on identical data is
worse than no score. A person needs to be able to look at 72/100 and see why.
The model is used only to draft the follow-up message — language, which is what
it's actually good at.

The most valuable thing this agent does is notice silence. A lead that nobody
replied to doesn't generate an event; it just quietly ages. Nothing in the UI
surfaces that until someone thinks to look.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from ..llm.base import LLMError
from ..llm.gate import Priority
from ..orchestrators.base import BaseOrchestrator, OrchestratorError
from ..repositories.lead_repository import LeadRepository
from .base import BaseAgent, Finding

logger = logging.getLogger(__name__)

# Scoring weights. Tunable, visible, and explainable to the person using them.
W_BUDGET_HIGH = 25      # budget_hint above the high-value threshold
W_BUDGET_MED = 12
W_HAS_PHONE = 10        # reachable at all
W_HAS_EMAIL = 5
W_SERVICE_KNOWN = 10    # they told us what they want
W_VEHICLE_KNOWN = 8
W_SOURCE = {            # some channels convert far better than others
    "referral": 22, "walk_in": 18, "phone": 15, "website": 10,
    "booking_form": 12, "whatsapp": 10, "instagram": 5, "manual": 5, "other": 0,
}
W_STAGE = {"new": 0, "contacted": 8, "quoted": 18, "negotiating": 26, "won": 0, "lost": 0}

HIGH_VALUE = 100000     # currency-agnostic: whatever your budget_hint is in
MED_VALUE = 40000

STALE_DAYS = 5
VERY_STALE_DAYS = 12

# Cap on drafted messages per run. Every draft costs inference, and the leads
# worth a tailored message are the high-scoring ones — the rest get the finding
# without the prose.
MAX_DRAFTS = 5


class _Drafter(BaseOrchestrator):
    """
    Drafts follow-up messages for several leads in ONE model call.

    The first version of this looped: one call per stale lead. With eleven
    active leads that was eleven sequential requests — around 88 seconds of
    inference during which every page load, DVI analysis and quote queued
    behind a background job nobody was watching.

    Batching is the fix, not concurrency. Eleven parallel calls would still
    consume the whole GPU; one call producing eleven short messages costs
    roughly the same as one message, because the prompt is shared and the
    output is small.

    It also runs at BACKGROUND priority on the `fast` tier: drafting two
    sentences does not need an 8B model, and nobody is waiting for it.
    """

    priority = int(Priority.BACKGROUND)
    tier = "fast"
    # If we can't get a slot in 20s, something interactive is using the GPU.
    # Give up and emit findings without drafts rather than making a person wait.
    gate_timeout = 20.0

    def draft_batch(self, leads: List[Dict[str, Any]]) -> Dict[int, str]:
        """Returns {lead_id: message}. Missing ids simply got no draft."""
        if not leads:
            return {}

        payload = [
            {
                "id": l["id"],
                "name": l.get("name"),
                "vehicle": l.get("vehicle") or "",
                "interested_in": l.get("service_interest") or "",
                "stage": l.get("status"),
                "days_since_contact": int(l.get("days_quiet") or 0),
            }
            for l in leads
        ]

        prompt = f"""You are a service advisor at a car detailing studio, writing short
follow-up messages to people who enquired and then went quiet.

LEADS:
{json.dumps(payload, indent=2)}

RULES:
1. Write ONE message per lead, 2-3 sentences maximum. These are WhatsApp/SMS
   messages, not emails.
2. Warm and low-pressure. No urgency tactics, no fake scarcity, and never offer
   a discount you were not told to offer.
3. Do NOT invent prices, dates or promises.
4. Reference their specific vehicle or service so it doesn't read as a template.
5. Return a message for every id listed above, and no others.

Return ONLY valid JSON:
{{"messages": [{{"id": 1, "message": "..."}}]}}"""

        result = self.call_json(prompt, required_keys=("messages",))
        out: Dict[int, str] = {}
        for item in result.get("messages", []):
            if not isinstance(item, dict):
                continue
            try:
                out[int(item["id"])] = str(item.get("message", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue
        return out


class LeadsAgent(BaseAgent):
    name = "leads"
    label = "Leads Agent"
    description = "Scores enquiries, spots leads going cold, and drafts follow-ups."
    icon = "users"
    default_interval_mins = 180
    responds_to = ("lead_created", "booking_created")

    def collect(self) -> List[Finding]:
        repo = LeadRepository()
        findings: List[Finding] = []

        # ── 1. Rescore everything active ──
        for lead in repo.active(self.studio_id):
            score, reason = self.score(lead)
            repo.set_score(self.studio_id, lead["id"], score, reason)

        # ── 2. Hot leads that nobody has contacted ──
        for lead in repo.active(self.studio_id):
            score, reason = self.score(lead)
            if score >= 60 and lead["status"] == "new":
                findings.append(Finding(
                    kind="hot_lead",
                    severity="opportunity",
                    title=f"{lead['name']} is a strong lead and hasn't been contacted",
                    detail=(
                        f"Score {score}/100. {reason} "
                        f"Interested in {lead.get('service_interest') or 'unspecified work'}"
                        + (f" for their {lead['vehicle']}" if lead.get("vehicle") else "") + "."
                    ),
                    action_label="Open lead",
                    action_url=f"/leads/{lead['id']}",
                    entity_type="lead",
                    entity_id=lead["id"],
                    fingerprint=f"hot_lead:{lead['id']}",
                    data={"score": score},
                ))

        # ── 3. Leads going cold ──
        stale = repo.stale(self.studio_id, STALE_DAYS)

        # Draft for the highest-scoring few only, in a single batched call.
        # Drafting for every cold lead is wasted inference: the ones worth
        # chasing personally are the ones worth a tailored message.
        drafts: Dict[int, str] = {}
        if stale:
            ranked = sorted(stale, key=lambda l: -self.score(l)[0])[:MAX_DRAFTS]
            try:
                drafts = _Drafter().draft_batch(ranked)
            except (LLMError, OrchestratorError) as exc:
                logger.info("Follow-up drafting unavailable: %s", exc)
                self.mark_degraded()

        for lead in stale:
            days = int(lead.get("days_quiet") or 0)
            severity = "warning" if days >= VERY_STALE_DAYS else "info"
            score, _ = self.score(lead)
            draft = drafts.get(int(lead["id"]), "")

            detail = (
                f"No contact for {days} days while at the '{lead['status']}' stage. "
                f"Lead score {score}/100."
            )
            detail += f"\n\nSuggested message:\n{draft}" if draft else " Worth a quick call or message."

            findings.append(Finding(
                kind="lead_cold",
                severity=severity,
                title=f"{lead['name']} has gone quiet ({days} days)",
                detail=detail,
                action_label="Follow up",
                action_url=f"/leads/{lead['id']}",
                entity_type="lead",
                entity_id=lead["id"],
                fingerprint=f"lead_cold:{lead['id']}",
                data={"days_quiet": days, "draft": draft},
            ))

        # ── 4. Channel performance ──
        sources = [s for s in repo.by_source(self.studio_id) if int(s["total"]) >= 4]
        for s in sources:
            total, won = int(s["total"]), int(s["won"] or 0)
            rate = won / total * 100 if total else 0
            if rate >= 50:
                findings.append(Finding(
                    kind="channel_strong",
                    severity="opportunity",
                    title=f"{s['source'].replace('_', ' ').title()} converts at {rate:.0f}%",
                    detail=f"{won} of {total} leads from this channel became customers. "
                           f"Worth putting more effort here.",
                    action_label="View leads",
                    action_url="/leads",
                    fingerprint=f"channel_strong:{s['source']}",
                ))

        return findings

    # ── deterministic scoring ─────────────────────────────────────────────

    @staticmethod
    def score(lead: Dict[str, Any]) -> tuple[int, str]:
        """
        Returns (0-100, human-readable reason). Same input always gives the same
        output, and every point is attributable — a score you can't explain is a
        score nobody will trust.
        """
        pts = 0
        why: List[str] = []

        budget = int(lead.get("budget_hint") or 0)
        if budget >= HIGH_VALUE:
            pts += W_BUDGET_HIGH
            why.append("high-value job")
        elif budget >= MED_VALUE:
            pts += W_BUDGET_MED
            why.append("mid-value job")

        source = lead.get("source", "manual")
        src_pts = W_SOURCE.get(source, 0)
        if src_pts:
            pts += src_pts
            why.append(f"came via {source.replace('_', ' ')}")

        stage_pts = W_STAGE.get(lead.get("status", "new"), 0)
        if stage_pts:
            pts += stage_pts
            why.append(f"already {lead['status']}")

        if (lead.get("phone") or "").strip():
            pts += W_HAS_PHONE
        else:
            why.append("no phone number")
        if (lead.get("email") or "").strip():
            pts += W_HAS_EMAIL
        if (lead.get("service_interest") or "").strip():
            pts += W_SERVICE_KNOWN
        else:
            why.append("service not specified")
        if (lead.get("vehicle") or "").strip():
            pts += W_VEHICLE_KNOWN

        score = max(0, min(pts, 100))
        return score, ("Scored on: " + ", ".join(why) + "." if why else "Limited information available.")
