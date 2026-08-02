"""
services/prompts/inventory_prompts.py

Prompts live apart from orchestration so they can be tweaked, diffed and
regression-tested without touching control flow.

The governing rule in every template below: **the model is handed finished
arithmetic and forbidden from redoing it.** Small local models are good at
prioritising and explaining; they are bad at division. We only ask for what
they're good at.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List


class InventoryPrompts:

    STOCK_SYSTEM = (
        "You are an inventory analyst for a high-end automotive detailing studio. "
        "You write short, concrete operational briefings for a studio manager who is busy. "
        "You never invent numbers."
    )

    @staticmethod
    def stock_briefing(
        studio_name: str,
        health: Dict[str, Any],
        forecasts: List[Dict[str, Any]],
        job_volume: Dict[str, Any],
        max_items: int = 12,
    ) -> str:
        """
        Every figure here was computed in Python. The model's only job is to
        turn the table into prose a manager will act on.
        """
        trimmed = [
            {
                "sku": f["sku"],
                "name": f["name"],
                "category": f["category"],
                "on_hand": f"{f['current_qty']} {f['unit']}",
                "burn_per_week": f"{f['weekly_burn']} {f['unit']}",
                "days_until_empty": f["days_to_empty"],
                "runs_out_on": f["stockout_date"],
                "urgency": f["urgency"],
                "confidence": f["confidence"],
                "suggested_order": f"{f['suggested_order_qty']} {f['unit']}",
            }
            for f in forecasts[:max_items]
        ]

        payload = {
            "studio": studio_name,
            "portfolio_health": health,
            "items": trimmed,
            "workload": job_volume,
        }

        return f"""{InventoryPrompts.STOCK_SYSTEM}

Below is a PRE-CALCULATED inventory forecast. The arithmetic is already done and verified.

DATA:
{json.dumps(payload, indent=2, default=str)}

ABSOLUTE RULES:
1. Do NOT recalculate, re-derive, or alter any number. Quote them exactly as given.
2. Do NOT mention any SKU that does not appear in the data above.
3. If `confidence` is "low", say the estimate is early and based on limited history.
4. Be specific. "Order more coating" is useless; "Order 2.5 L of Ceramic Coating before Nov 14" is useful.
5. Write for a manager on a workshop floor: plain sentences, no filler, no preamble.

Return ONLY valid JSON in exactly this schema:
{{
  "headline": "One sentence a manager could read in three seconds.",
  "narrative": "2-4 sentences connecting stock levels to the current job workload.",
  "priority_actions": [
    {{"sku": "...", "action": "Order 2.5 L before Nov 14", "why": "Runs out mid-week with 6 coatings booked"}}
  ],
  "watch_list": [
    {{"sku": "...", "note": "Usage climbing — recheck next week"}}
  ],
  "cost_notes": ["Short observation about spend or ordering efficiency"]
}}"""

    @staticmethod
    def waste_briefing(
        studio_name: str,
        signals: List[Dict[str, Any]],
        job_volume: Dict[str, Any],
    ) -> str:
        payload = {"studio": studio_name, "waste_signals": signals[:8], "workload": job_volume}

        return f"""{InventoryPrompts.STOCK_SYSTEM}

Below are statistically detected material-usage outliers. Each was found by comparing a job's
consumption against THIS STUDIO'S OWN median for that product — not an industry benchmark.

DATA:
{json.dumps(payload, indent=2, default=str)}

ABSOLUTE RULES:
1. Do NOT recalculate any figure. Quote the given numbers exactly.
2. An outlier is NOT proof of waste. A large SUV, a heavily oxidised repaint, or a correction
   job legitimately consumes more. Frame every item as "worth checking", never as an accusation.
3. Never name or blame a technician, even if you can infer one.
4. If the sample size is small, say the signal is weak.

Return ONLY valid JSON in exactly this schema:
{{
  "headline": "One sentence summary of material efficiency.",
  "narrative": "2-3 sentences on what the pattern suggests.",
  "checks": [
    {{"sku": "...", "job_id": 12, "suggestion": "Worth confirming the vehicle size on this job"}}
  ],
  "process_tips": ["Concrete, low-effort suggestion to reduce material loss"]
}}"""

    @staticmethod
    def repair(bad_output: str, schema_hint: str) -> str:
        """Second-chance prompt when the first reply won't parse."""
        return f"""The following was supposed to be valid JSON but could not be parsed.

BROKEN OUTPUT:
{bad_output[:1500]}

Rewrite it as valid JSON matching this schema:
{schema_hint}

Return ONLY the corrected JSON object. No markdown fences, no commentary."""
