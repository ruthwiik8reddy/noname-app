"""
services/orchestrators/estimate_orchestrator.py — Phase 4 refactor target.

This exists to delete code from `product_routes.py`, where `_ai_estimate_sqft()`
constructed prompts, called two backends by hand, parsed replies with a regex
and wrote to the cache table — all inside a routes module. That is exactly the
shape the Orchestrator Pattern forbids: a controller that knows what a model is.

The behaviour is preserved (cache → model → body-type fallback) and the
guarantee is strengthened: a vehicle always gets a number, and the caller always
learns which of the three sources produced it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from ..llm.base import LLMError, LLMProvider
from ..repositories.base import BaseRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)

# Body-type priors, used when the model is unavailable or answers nonsensically.
FALLBACK_SQFT: Dict[str, int] = {
    "hatchback": 170, "sedan": 200, "coupe": 185, "convertible": 180,
    "wagon": 220, "crossover": 230, "suv": 245, "truck": 270, "pickup": 270,
    "van": 280, "minivan": 275, "default": 210,
}

# Anything outside this range is a parse error, not a measurement.
MIN_PLAUSIBLE_SQFT = 80
MAX_PLAUSIBLE_SQFT = 600

SQFT_PATTERN = re.compile(
    r"(\d{2,4}(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s*feet|ft2|ft\^?2)", re.IGNORECASE
)
NUMBER_PATTERN = re.compile(r"(\d{2,4}(?:\.\d+)?)")


class EstimateOrchestrator(BaseOrchestrator):

    cache_ttl_seconds = 900

    def __init__(self, repository: Optional[BaseRepository] = None, provider: Optional[LLMProvider] = None):
        super().__init__(provider)
        self.repo = repository or BaseRepository()

    # ── vehicle surface area ──────────────────────────────────────────────

    def vehicle_sqft(self, vehicle: str, studio_id: int) -> Dict[str, Any]:
        """Returns {sqft, source, vehicle}. `source` ∈ cache | ai | fallback."""
        vehicle = (vehicle or "").strip()
        if not vehicle:
            return {"sqft": float(FALLBACK_SQFT["default"]), "source": "fallback", "vehicle": ""}

        cached = self.repo.fetch_one(
            "SELECT total_sqft FROM vehicle_sqft_cache WHERE vehicle=?", (vehicle,)
        )
        if cached:
            return {"sqft": float(cached["total_sqft"]), "source": "cache", "vehicle": vehicle}

        prompt = (
            "You are a paint protection film (PPF) estimator.\n"
            f"Vehicle: {vehicle}\n\n"
            "Estimate the total exterior paintable surface area of this vehicle in square feet "
            "(body panels that would be covered in a full-body PPF wrap; exclude glass and wheels).\n\n"
            "Reference ranges: compact hatchback ~170, sedan ~200, coupe ~185, "
            "midsize SUV ~240, large SUV ~260, pickup truck ~270.\n\n"
            'Return ONLY valid JSON: {"total_sqft": 245}'
        )

        try:
            parsed = self.call_json(prompt, required_keys=("total_sqft",))
            sqft = self._coerce_sqft(parsed.get("total_sqft"))
            if sqft is None:
                sqft = self._parse_sqft(json.dumps(parsed))
            if sqft is not None:
                self.repo.execute(
                    "INSERT OR REPLACE INTO vehicle_sqft_cache (vehicle, total_sqft, source) VALUES (?,?,?)",
                    (vehicle, sqft, "ollama"),
                )
                return {"sqft": sqft, "source": "ai", "vehicle": vehicle}
            logger.info("Model returned an implausible sqft for %r — using body-type prior", vehicle)
        except (LLMError, OrchestratorError) as exc:
            logger.info("sqft estimate for %r degraded to fallback: %s", vehicle, exc)

        return {"sqft": self._body_type_guess(vehicle), "source": "fallback", "vehicle": vehicle}

    @staticmethod
    def _coerce_sqft(value: Any) -> Optional[float]:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return num if MIN_PLAUSIBLE_SQFT <= num <= MAX_PLAUSIBLE_SQFT else None

    @staticmethod
    def _parse_sqft(text: str) -> Optional[float]:
        if not text:
            return None
        cleaned = text.replace(",", "")
        match = SQFT_PATTERN.search(cleaned) or NUMBER_PATTERN.search(cleaned)
        if not match:
            return None
        try:
            value = float(match.group(1))
        except ValueError:
            return None
        return value if MIN_PLAUSIBLE_SQFT <= value <= MAX_PLAUSIBLE_SQFT else None

    @staticmethod
    def _body_type_guess(vehicle: str) -> float:
        lowered = vehicle.lower()
        for key, value in FALLBACK_SQFT.items():
            if key != "default" and key in lowered:
                return float(value)
        return float(FALLBACK_SQFT["default"])

    # ── catalog-grounded estimate ─────────────────────────────────────────

    def draft_estimate(self, studio_id: int, customer_notes: str) -> Dict[str, Any]:
        """
        Turns free-text notes into line items drawn strictly from the studio's
        own service catalog. The model is not allowed to invent a service, and
        prices are re-read from the catalog after generation rather than trusted
        from the model — so a hallucinated price can never reach an invoice.
        """
        catalog = self.repo.fetch_all(
            "SELECT id, name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
        )
        if not catalog:
            raise OrchestratorError(
                "This studio has no services configured yet — add them before generating estimates."
            )

        by_name = {c["name"].lower(): c for c in catalog}
        prompt = f"""You are a service estimator for an automotive detailing studio.
Read the customer's notes and select the appropriate services.

ACTIVE CATALOG (the ONLY services you may select):
{json.dumps([{"name": c["name"], "price": c["price"]} for c in catalog], indent=2)}

RULES:
1. Select ONLY services whose names appear verbatim in the catalog above.
2. Do NOT invent services, and do NOT alter prices.
3. If the notes don't justify any catalog service, return an empty line_items list.

CUSTOMER NOTES: {customer_notes}

Return ONLY valid JSON:
{{
  "estimate_summary": "One or two sentences explaining the recommendation.",
  "line_items": [{{"name": "exact catalog name", "quantity": 1, "reason": "why this service"}}]
}}"""

        parsed = self.call_json(prompt, required_keys=("estimate_summary", "line_items"))

        # Re-price from the catalog — never from the model's output.
        validated: List[Dict[str, Any]] = []
        rejected: List[str] = []
        for item in parsed.get("line_items", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            match = by_name.get(name.lower())
            if not match:
                rejected.append(name)
                continue
            qty = max(1, int(item.get("quantity", 1) or 1))
            unit = float(match["price"])
            validated.append(
                {
                    "name": match["name"],
                    "quantity": qty,
                    "unit_price": unit,
                    "total": round(unit * qty, 2),
                    "reason": str(item.get("reason", ""))[:300],
                }
            )

        if rejected:
            logger.info("Dropped %d off-catalog line item(s): %s", len(rejected), rejected)

        return self.envelope(
            {
                "estimate_summary": parsed.get("estimate_summary", ""),
                "line_items": validated,
                "total_estimated_price": round(sum(i["total"] for i in validated), 2),
                "rejected_items": rejected,
            },
            source="ai",
        )
