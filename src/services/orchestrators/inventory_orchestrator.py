"""
services/orchestrators/inventory_orchestrator.py — Phase 1: the AI Estimator.

Pipeline:

    SQLite (inventory_items + inventory_logs)
        ↓  InventoryRepository
    Raw movement history
        ↓  DepletionForecaster        ← pure Python, always runs
    Burn rates, stockout dates, reorder quantities, waste outliers
        ↓  InventoryPrompts + local Ollama   ← optional
    A manager-readable briefing

The forecast is computed BEFORE the model is consulted and is returned whether
or not the model answers. Ollama being down degrades the prose, never the
numbers — the response shape is identical either way, so the UI never branches.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..analytics.depletion_forecaster import DepletionForecaster
from ..llm.base import LLMError, LLMProvider
from ..prompts.inventory_prompts import InventoryPrompts
from ..repositories.inventory_repository import InventoryRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)

STOCK_SCHEMA_HINT = (
    '{"headline": "...", "narrative": "...", "priority_actions": [], "watch_list": [], "cost_notes": []}'
)
WASTE_SCHEMA_HINT = '{"headline": "...", "narrative": "...", "checks": [], "process_tips": []}'


class InventoryIntelligenceOrchestrator(BaseOrchestrator):

    cache_ttl_seconds = 300

    def __init__(
        self,
        repository: Optional[InventoryRepository] = None,
        forecaster: Optional[DepletionForecaster] = None,
        provider: Optional[LLMProvider] = None,
    ):
        super().__init__(provider)
        self.repo = repository or InventoryRepository()
        self.forecaster = forecaster or DepletionForecaster()

    # ── stock forecast ────────────────────────────────────────────────────

    def stock_forecast(
        self, studio_id: int, studio_name: str = "", force_refresh: bool = False
    ) -> Dict[str, Any]:
        cache_key = (studio_id, "stock_forecast")
        if not force_refresh:
            hit = self.cached(cache_key)
            if hit:
                return hit

        items = self.repo.list_items(studio_id)
        if not items:
            return self.envelope(
                {
                    "health": DepletionForecaster.portfolio_health([]),
                    "forecasts": [],
                    "briefing": {
                        "headline": "No inventory items yet.",
                        "narrative": "Add products on the Inventory page and log usage — "
                                     "forecasts appear once there's movement to learn from.",
                        "priority_actions": [],
                        "watch_list": [],
                        "cost_notes": [],
                    },
                    "job_volume": self.repo.job_volume(studio_id),
                },
                degraded=False,
                source="deterministic",
            )

        # ── deterministic layer (always runs) ──
        daily = self.repo.daily_consumption(studio_id, days=60)
        first_seen = self.repo.first_movement_dates(studio_id)
        volume = self.repo.job_volume(studio_id, days=30)

        forecasts = self.forecaster.forecast_all(
            items=items,
            daily_rows=daily,
            first_seen=first_seen,
            jobs_completed=volume["completed_jobs"],
            job_window_days=volume["window_days"],
            expected_jobs_next_7d=volume["bookings_next_14d"] / 2.0,
        )
        health = DepletionForecaster.portfolio_health(forecasts)
        forecast_dicts = [f.as_dict() for f in forecasts]

        base_payload = {
            "health": health,
            "forecasts": forecast_dicts,
            "job_volume": volume,
            "valuation_cents": self.repo.total_valuation_cents(studio_id),
        }

        # ── AI narrative layer (optional) ──
        try:
            briefing = self.call_json(
                InventoryPrompts.stock_briefing(
                    studio_name=studio_name or f"Studio {studio_id}",
                    health=health,
                    forecasts=forecast_dicts,
                    job_volume=volume,
                ),
                required_keys=("headline", "narrative", "priority_actions", "watch_list", "cost_notes"),
                repair_prompt_builder=InventoryPrompts.repair,
                schema_hint=STOCK_SCHEMA_HINT,
            )
            base_payload["briefing"] = briefing
            payload = self.envelope(base_payload, source="ai")
        except (LLMError, OrchestratorError) as exc:
            logger.info("Stock briefing degraded to deterministic mode: %s", exc)
            base_payload["briefing"] = self._fallback_briefing(forecasts, health)
            payload = self.envelope(
                base_payload,
                degraded=True,
                degraded_reason=str(exc),
                source="deterministic",
            )

        return self.store(cache_key, payload)

    def _fallback_briefing(self, forecasts: List[Any], health: Dict[str, Any]) -> Dict[str, Any]:
        """
        Written from the same numbers the model would have received. Not an
        error message — a real, useful answer that happens to lack prose polish.
        """
        critical = [f for f in forecasts if f.urgency == "critical"][:6]
        warning = [f for f in forecasts if f.urgency == "warning"][:6]

        if critical:
            soonest = min(critical, key=lambda f: f.days_to_empty if f.days_to_empty is not None else 1e9)
            when = (
                f"{soonest.name} runs out in about {soonest.days_to_empty:g} days ({soonest.stockout_date})"
                if soonest.days_to_empty is not None
                else f"{soonest.name} is at or below its reorder level"
            )
            headline = f"{len(critical)} item(s) need ordering now — {when}."
        elif warning:
            headline = f"{len(warning)} item(s) approaching their reorder point."
        else:
            headline = "Stock levels are healthy across all tracked items."

        return {
            "headline": headline,
            "narrative": (
                f"Portfolio health: {health['health_score']}. "
                f"{health['stockouts_next_7d']} item(s) are projected to run out within 7 days and "
                f"{health['stockouts_next_30d']} within 30. Estimated restock cost: "
                f"{health['reorder_cost_display']}."
            ),
            "priority_actions": [
                {
                    "sku": f.sku,
                    "action": f"Order {f.suggested_order_qty:g} {f.unit} of {f.name}"
                              + (f" before {f.reorder_date}" if f.reorder_date else ""),
                    "why": f.rationale,
                }
                for f in critical
            ],
            "watch_list": [{"sku": f.sku, "note": f.rationale} for f in warning],
            "cost_notes": [
                f"Restocking every flagged item would cost about {health['reorder_cost_display']}."
            ],
            "_generated_by": "deterministic_fallback",
        }

    # ── waste analysis ────────────────────────────────────────────────────

    def waste_analysis(
        self, studio_id: int, studio_name: str = "", force_refresh: bool = False
    ) -> Dict[str, Any]:
        cache_key = (studio_id, "waste_analysis")
        if not force_refresh:
            hit = self.cached(cache_key)
            if hit:
                return hit

        items = self.repo.list_items(studio_id)
        per_job = self.repo.per_job_usage(studio_id, days=90)
        signals = self.forecaster.detect_waste(items, per_job)
        signal_dicts = [s.as_dict() for s in signals]
        volume = self.repo.job_volume(studio_id, days=30)

        total_excess_cents = sum(s.excess_cost_cents for s in signals)
        base_payload = {
            "signals": signal_dicts,
            "total_excess_cents": total_excess_cents,
            "total_excess_display": f"${total_excess_cents / 100:,.2f}",
            "jobs_analyzed": len({j for jobs in per_job.values() for j in jobs}),
            "job_volume": volume,
        }

        if not signals:
            base_payload["briefing"] = {
                "headline": "No unusual material usage detected.",
                "narrative": (
                    "Every product with enough job history is being consumed within a normal range "
                    "of this studio's own median. Note that at least four jobs per product are needed "
                    "before outlier detection produces a meaningful signal."
                ),
                "checks": [],
                "process_tips": [],
            }
            return self.store(cache_key, self.envelope(base_payload, source="deterministic"))

        try:
            briefing = self.call_json(
                InventoryPrompts.waste_briefing(
                    studio_name=studio_name or f"Studio {studio_id}",
                    signals=signal_dicts,
                    job_volume=volume,
                ),
                required_keys=("headline", "narrative", "checks", "process_tips"),
                repair_prompt_builder=InventoryPrompts.repair,
                schema_hint=WASTE_SCHEMA_HINT,
            )
            base_payload["briefing"] = briefing
            payload = self.envelope(base_payload, source="ai")
        except (LLMError, OrchestratorError) as exc:
            logger.info("Waste briefing degraded to deterministic mode: %s", exc)
            base_payload["briefing"] = {
                "headline": f"{len(signals)} product(s) show higher-than-usual usage on some jobs.",
                "narrative": (
                    f"Excess consumption above this studio's own median accounts for roughly "
                    f"{base_payload['total_excess_display']} across the last 90 days. "
                    f"Larger vehicles and correction-heavy work legitimately use more product — "
                    f"treat these as questions, not findings."
                ),
                "checks": [
                    {
                        "sku": s.sku,
                        "job_id": s.outlier_jobs[0]["job_id"] if s.outlier_jobs else None,
                        "suggestion": (
                            f"{s.name}: used {s.outlier_jobs[0]['used']:g} {s.unit} vs a median of "
                            f"{s.median_per_job:g} — confirm the vehicle size and scope."
                        ) if s.outlier_jobs else f"Review {s.name} usage.",
                    }
                    for s in signals[:6]
                ],
                "process_tips": [
                    "Record product usage at the point of use rather than at the end of the day.",
                    "Log vehicle size against each job so per-job comparisons stay fair.",
                ],
                "_generated_by": "deterministic_fallback",
            }
            payload = self.envelope(
                base_payload, degraded=True, degraded_reason=str(exc), source="deterministic"
            )

        return self.store(cache_key, payload)

    # ── single-item drilldown ─────────────────────────────────────────────

    def item_detail(self, studio_id: int, item_id: int) -> Dict[str, Any]:
        item = self.repo.get_item(studio_id, item_id)
        if not item:
            raise OrchestratorError(f"Inventory item {item_id} not found for this studio")

        daily = [r for r in self.repo.daily_consumption(studio_id, 90) if int(r["item_id"]) == item_id]
        first_seen = self.repo.first_movement_dates(studio_id)
        volume = self.repo.job_volume(studio_id, 30)

        forecast = self.forecaster.forecast_all(
            items=[item],
            daily_rows=daily,
            first_seen=first_seen,
            jobs_completed=volume["completed_jobs"],
            job_window_days=volume["window_days"],
            expected_jobs_next_7d=volume["bookings_next_14d"] / 2.0,
        )[0]

        return self.envelope(
            {
                "item": item,
                "forecast": forecast.as_dict(),
                "history": [{"day": r["day"], "qty": float(r["qty"])} for r in daily],
                "recent_events": self.repo.consumption_events(studio_id, 30)[-25:],
            },
            source="deterministic",
        )

    def invalidate(self, studio_id: int) -> None:
        """Called after any stock movement so the next view recomputes."""
        self.invalidate_studio(studio_id)
