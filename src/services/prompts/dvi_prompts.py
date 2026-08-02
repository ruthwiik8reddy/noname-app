"""
services/prompts/dvi_prompts.py — vision prompts for Digital Vehicle Inspection.

Two lessons baked into these templates, both learned the hard way with local
vision models:

1. **Closed vocabulary.** Left open, LLaVA invents defect names ("micro-marring
   haze artifacts"). Given a fixed enum, its output can be mapped straight to a
   price. The enum is the contract between the model and the pricing engine.

2. **Explicit permission to find nothing.** Vision models are eager to please
   and will hallucinate scratches on a flawless panel. Stating that an empty
   list is a valid, expected answer measurably reduces false positives — which
   matters enormously here, because a false positive becomes a bogus charge on
   a real customer's invoice.

Note what the model is NOT asked for: money. It reports what it sees; the
UpchargeCalculator decides what that costs, from the studio's own price list.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

# The closed vocabulary. Every entry maps to a service in pricing/upcharge_calculator.py.
DEFECT_TYPES = [
    "swirl_marks",
    "light_scratch",
    "deep_scratch",
    "rock_chip",
    "dent",
    "oxidation",
    "water_spots",
    "paint_transfer",
    "clear_coat_failure",
    "curb_rash",
    "overspray",
    "trim_fade",
    "glass_etching",
    "interior_stain",
]

PANELS = [
    "front_bumper", "rear_bumper", "hood", "roof", "trunk",
    "front_fender_left", "front_fender_right",
    "door_front_left", "door_front_right",
    "door_rear_left", "door_rear_right",
    "quarter_panel_left", "quarter_panel_right",
    "windshield", "rear_glass", "wheels", "headlights", "interior", "unspecified",
]


class DVIPrompts:

    @staticmethod
    def single_photo(vehicle: str, panel_hint: str = "unspecified") -> str:
        return f"""You are a certified paint inspector at an automotive detailing studio.
Examine this photograph of a {vehicle or 'vehicle'} and report ONLY defects you can actually see.

The technician tagged this photo as: {panel_hint}

DEFECT TYPES — use these exact strings, nothing else:
{json.dumps(DEFECT_TYPES)}

PANEL NAMES — use these exact strings, nothing else:
{json.dumps(PANELS)}

SEVERITY SCALE:
1 = barely visible at an angle in direct light
2 = visible on close inspection
3 = clearly visible at arm's length
4 = obvious from several feet away
5 = severe, affects the vehicle's appearance immediately

CRITICAL RULES:
- Report ONLY what is visible in THIS photograph. Do not guess at what might be elsewhere on the car.
- If the paint looks clean, return an empty findings list. An empty list is a CORRECT and
  expected answer. Do NOT invent defects to seem thorough.
- Reflections, shadows, dust, water droplets and the photographer's own reflection are NOT defects.
- If the image is too dark, blurry or close-cropped to judge, set "image_quality" to "poor"
  and return no findings.
- confidence is your own certainty from 0.0 to 1.0. Be honest — low confidence is useful information.
- Do NOT estimate prices, costs or labour hours. That is not your job.

Return ONLY valid JSON in exactly this schema:
{{
  "image_quality": "good | fair | poor",
  "panel_observed": "one panel name from the list",
  "overall_condition": "excellent | good | fair | poor",
  "findings": [
    {{
      "defect_type": "one value from the defect list",
      "panel": "one value from the panel list",
      "severity": 3,
      "confidence": 0.8,
      "location_note": "where on the panel, e.g. 'lower left near the wheel arch'",
      "description": "one factual sentence describing what you see"
    }}
  ]
}}"""

    @staticmethod
    def inspection_summary(
        vehicle: str,
        service: str,
        findings: List[Dict[str, Any]],
        upcharge_total_display: str,
        photo_count: int,
    ) -> str:
        payload = {
            "vehicle": vehicle,
            "booked_service": service,
            "photos_analyzed": photo_count,
            "findings": findings,
            "recommended_additional_work_total": upcharge_total_display,
        }

        return f"""You are a service advisor at a high-end detailing studio, writing up a
digital vehicle inspection for the customer.

INSPECTION DATA (already priced — do not change any figure):
{json.dumps(payload, indent=2, default=str)}

RULES:
1. Do NOT alter any price. They come from the studio's own price list.
2. Do NOT add defects that are not in the data.
3. Write to the customer: respectful, clear, zero jargon, zero pressure.
4. Explain WHY each recommendation matters in practical terms (protection, resale, appearance) —
   never through fear or urgency tactics.
5. If there are no findings, say the vehicle is in good condition and congratulate them on it.

Return ONLY valid JSON in exactly this schema:
{{
  "customer_summary": "2-4 sentences the customer will read first.",
  "condition_headline": "e.g. 'Good condition with light swirl marks on horizontal panels'",
  "recommendations": [
    {{"service": "...", "reason": "Plain-language explanation of the benefit", "priority": "high | medium | low"}}
  ],
  "technician_notes": ["Short internal note for the technician doing the work"]
}}"""

    @staticmethod
    def repair(bad_output: str, schema_hint: str) -> str:
        return f"""The following was supposed to be valid JSON but could not be parsed.

BROKEN OUTPUT:
{bad_output[:1500]}

Rewrite it as valid JSON matching this schema:
{schema_hint}

Return ONLY the corrected JSON object. No markdown fences, no commentary."""
