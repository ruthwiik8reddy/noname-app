"""
services/orchestrators/assistant_orchestrator.py — Phase 4 refactor target.

Absorbs three direct-LLM call sites that were living in route modules:

  * `controller.assistant_query()`  — built prompts and called GeminiBackend /
    OllamaBackend by hand, inside a Flask handler.
  * `inspection_routes.cust_chat()` — constructed a concierge prompt inline.
  * the studio-context loading previously duplicated in `ai_service.UserMemoryStore`.

Behaviour is preserved, including the important bit: when the pricing hook
produces real catalog figures, the model is handed those figures and told to
report them. It is never asked to compute a price. That was the correct instinct
in the original code — it just belonged in a service, not a controller.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..llm.base import LLMError, LLMProvider, VisionLLMProvider
from ..repositories.base import BaseRepository
from .base import BaseOrchestrator

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are the AI assistant built into Autofiera, a detailing studio management platform. "
    "You help studio owners and staff with questions about their business, services and "
    "industry knowledge. Be concise, practical and professional. When you reference studio "
    "data, say where it came from. If you don't know something, say so plainly."
)

# Words that suggest the person is asking about something they can see.
VISUAL_TRIGGERS = (
    "photo", "image", "picture", "look at", "see this", "swirl", "scratch",
    "looks like", "show me", "this panel", "paint condition",
)


class AssistantOrchestrator(BaseOrchestrator):

    cache_ttl_seconds = 60

    def __init__(self, repository: Optional[BaseRepository] = None, provider: Optional[LLMProvider] = None):
        super().__init__(provider)
        self.repo = repository or BaseRepository()

    # ── studio context ────────────────────────────────────────────────────

    def build_context(self, studio_id: int, question: str) -> Dict[str, Any]:
        """Only load what the question actually needs — local context windows are small."""
        lowered = question.lower()
        context: Dict[str, Any] = {}

        profile = self.repo.fetch_one(
            "SELECT name, city, owner FROM studios WHERE id=?", (studio_id,)
        )
        if profile:
            context["studio"] = profile

        if any(w in lowered for w in ("job", "car", "vehicle", "tech", "status", "progress")):
            context["recent_jobs"] = self.repo.fetch_all(
                "SELECT car, service, status, technician, price FROM jobs "
                "WHERE studio_id=? ORDER BY id DESC LIMIT 6",
                (studio_id,),
            )

        if any(w in lowered for w in ("service", "package", "price", "cost", "offer", "quote")):
            context["services"] = self.repo.fetch_all(
                "SELECT name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
            )

        if any(w in lowered for w in ("booking", "appointment", "schedule", "slot", "diary")):
            context["upcoming_bookings"] = self.repo.fetch_all(
                "SELECT customer_name, vehicle, date, time_slot, status FROM bookings "
                "WHERE studio_id=? AND date >= date('now') ORDER BY date LIMIT 5",
                (studio_id,),
            )

        if any(w in lowered for w in ("stock", "inventory", "run out", "reorder", "supply", "product")):
            context["low_stock"] = self.repo.fetch_all(
                "SELECT sku, name, quantity, unit, reorder_level FROM inventory_items "
                "WHERE studio_id=? AND quantity <= reorder_level LIMIT 10",
                (studio_id,),
            )

        if any(w in lowered for w in ("estimate", "invoice", "total", "revenue")):
            context["recent_estimates"] = self.repo.fetch_all(
                "SELECT customer_name, total, status FROM estimates "
                "WHERE studio_id=? ORDER BY created_at DESC LIMIT 5",
                (studio_id,),
            )

        return context

    @staticmethod
    def _build_prompt(question: str, context: Dict[str, Any]) -> str:
        parts = [SYSTEM, ""]
        if context:
            parts.append("STUDIO CONTEXT (live data from this studio's database):")
            parts.append(json.dumps(context, indent=2, default=str))
            parts.append("")
            parts.append(
                "Use the context above when it is relevant. Do not invent jobs, prices, "
                "customers or stock levels that do not appear in it."
            )
            parts.append("")
        parts.append(f"Question: {question}")
        parts.append("Answer:")
        return "\n".join(parts)

    # ── general answer ────────────────────────────────────────────────────

    def answer(
        self, studio_id: int, question: str, attachments: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Returns the same shape the old `AIAnswer.as_dict()` produced, so existing
        templates and JS keep working unchanged:
            {answer, reasoning, sources, evidence}
        """
        attachments = [a for a in (attachments or []) if a]
        context = self.build_context(studio_id, question)
        prompt = self._build_prompt(question, context)

        wants_vision = bool(attachments) or any(t in question.lower() for t in VISUAL_TRIGGERS)
        evidence = [
            {"attachment": a, "type": "image", "description": f"Attached: {a.rsplit('/', 1)[-1]}"}
            for a in attachments
        ]

        if attachments and wants_vision:
            try:
                provider = self.provider
                if isinstance(provider, VisionLLMProvider):
                    result = provider.complete_vision(prompt, attachments, json_mode=False)
                    return {
                        "answer": result.text,
                        "reasoning": "Analysed locally with the vision model.",
                        "sources": [f"ollama:{result.model}"],
                        "evidence": evidence,
                    }
            except LLMError as exc:
                logger.info("Vision path unavailable, falling back to text: %s", exc)

        try:
            result = self.provider.complete(prompt)
            return {
                "answer": result.text,
                "reasoning": "Answered locally from your studio's data.",
                "sources": [f"ollama:{result.model}"],
                "evidence": evidence,
            }
        except LLMError as exc:
            logger.info("Assistant unavailable: %s", exc)
            return {
                "answer": (
                    "The local AI model isn't reachable right now. Start it with `ollama serve`, "
                    "then try again — nothing else in the app is affected."
                ),
                "reasoning": str(exc),
                "sources": ["none"],
                "evidence": evidence,
                "degraded": True,
            }

    # ── grounded pricing answer ───────────────────────────────────────────

    def pricing_answer(self, question: str, facts: str) -> Optional[Dict[str, Any]]:
        """
        `facts` are prices already computed from the product catalog. The model
        may only restate them. Returns None if the model is unreachable, so the
        caller can fall through to the general assistant.
        """
        prompt = (
            "You are the AI assistant for a car detailing studio. "
            "A staff member asked a pricing question.\n\n"
            f"{facts}\n\n"
            f"Staff question: {question}\n\n"
            "Report the PRICE only, in one or two short sentences. The figures above are already "
            "correct — repeat them exactly and do NOT recalculate. No cost breakdown, no "
            "material/labour/markup figures, no calculation steps, no disclaimers, no extra advice."
        )
        try:
            result = self.provider.complete(prompt)
            return {
                "answer": result.text,
                "reasoning": "Quoted directly from your product catalog.",
                "sources": ["catalog", f"ollama:{result.model}"],
                "evidence": [],
            }
        except LLMError as exc:
            logger.info("Pricing answer unavailable: %s", exc)
            return None

    # ── customer-facing concierge ─────────────────────────────────────────

    def concierge_reply(
        self, studio_id: int, vehicle: str, service: str, panel_data: Dict[str, Any], message: str
    ) -> str:
        """Used by the public vehicle-tracking page. Always returns something safe to show."""
        prompt = (
            f"You are the concierge for a detailing studio, speaking to the owner of a {vehicle} "
            f"currently having {service} work done.\n\n"
            f"Panel inspection data: {json.dumps(panel_data, default=str)[:2000]}\n\n"
            f"Customer question: {message}\n\n"
            "Answer warmly in 2-3 sentences. Only state facts present in the data above. "
            "Never quote a price, never promise a completion time, and never speculate about "
            "work that has not been recorded."
        )
        try:
            return self.provider.complete(prompt).text
        except LLMError as exc:
            logger.info("Concierge reply unavailable: %s", exc)
            return (
                "Your vehicle is currently receiving our full detailing treatment. "
                "For specifics, please contact the studio directly and the team will be happy to help."
            )
