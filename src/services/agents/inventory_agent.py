"""
services/agents/inventory_agent.py — watches stock.

Wraps `InventoryIntelligenceOrchestrator` rather than recomputing burn rates.
That matters: if this agent derived its own numbers, the dashboard and the
Analytics page could disagree about when you run out of ceramic coating, and
there would be no way to tell which was right.
"""

from __future__ import annotations

from typing import List

from ..orchestrators.inventory_orchestrator import InventoryIntelligenceOrchestrator
from ..repositories.inventory_repository import InventoryRepository
from .base import BaseAgent, Finding


class InventoryAgent(BaseAgent):
    name = "inventory"
    label = "Inventory Agent"
    description = "Watches burn rate and flags stock that will run out before you can reorder."
    icon = "box"
    default_interval_mins = 120
    responds_to = ("stock_changed", "job_completed")

    def collect(self) -> List[Finding]:
        # One instance, reused — the previous version constructed a second
        # orchestrator for the waste call, throwing away the first one's cache.
        orch = InventoryIntelligenceOrchestrator()

        with self.background(orch):
            data = orch.stock_forecast(self.studio_id, force_refresh=True)
            waste = orch.waste_analysis(self.studio_id, force_refresh=True)

        if data.get("_status", {}).get("degraded") or waste.get("_status", {}).get("degraded"):
            self.mark_degraded()

        findings: List[Finding] = []

        for f in data.get("forecasts", []):
            if f["urgency"] not in ("critical", "warning"):
                continue

            days = f.get("days_to_empty")
            when = f"about {days:g} days ({f['stockout_date']})" if days is not None else "soon"
            severity = "critical" if f["urgency"] == "critical" else "warning"

            findings.append(Finding(
                kind="stock_low",
                severity=severity,
                title=f"{f['name']} runs out in {when}",
                detail=(
                    f"{f['current_qty']:g} {f['unit']} left, burning {f['weekly_burn']:g} "
                    f"{f['unit']}/week. Suggested order: {f['suggested_order_qty']:g} {f['unit']} "
                    f"({f['suggested_order_cost_display']}). Confidence: {f['confidence']}."
                ),
                action_label="View forecast",
                action_url="/analytics/",
                entity_type="inventory_item",
                entity_id=f["item_id"],
                fingerprint=f"stock_low:{f['sku']}",
                data={"sku": f["sku"], "days_to_empty": days, "urgency": f["urgency"]},
            ))

        # Waste is an opportunity, not an alarm — nobody is doing anything wrong.
        for s in waste.get("signals", [])[:4]:
            findings.append(Finding(
                kind="material_waste",
                severity="opportunity",
                title=f"{s['name']} used heavily on some jobs",
                detail=(
                    f"Typical use is {s['median_per_job']:g} {s['unit']} per job, but "
                    f"{len(s['outlier_jobs'])} job(s) used considerably more — about "
                    f"{s['excess_cost_display']} of extra material. Larger vehicles legitimately "
                    f"use more, so this is worth a look rather than a concern."
                ),
                action_label="Review usage",
                action_url="/analytics/",
                entity_type="inventory_item",
                entity_id=s["item_id"],
                fingerprint=f"waste:{s['sku']}",
                data={"excess_cost_cents": s["excess_cost_cents"]},
            ))

        health = data.get("health", {})
        if health.get("health_score") == "Critical":
            findings.append(Finding(
                kind="portfolio_health",
                severity="warning",
                title="Overall stock health is critical",
                detail=(
                    f"{health.get('critical_count', 0)} item(s) are critical and "
                    f"{health.get('stockouts_next_7d', 0)} will run out within 7 days. "
                    f"Restocking everything flagged would cost about "
                    f"{health.get('reorder_cost_display', '—')}."
                ),
                action_label="Open estimator",
                action_url="/analytics/",
                fingerprint="portfolio_health:critical",
            ))

        return findings
