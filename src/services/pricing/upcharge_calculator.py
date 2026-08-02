"""
services/pricing/upcharge_calculator.py

Turns vision findings into money — deterministically.

The vision model never sees a price. It reports defect type, panel and severity;
this class decides what that is worth, preferring the studio's *own* services
table and falling back to a default rate card only when no match exists. So
Shine Pro quotes Shine Pro's prices, and the number is reproducible: the same
findings always produce the same quote, and you can explain any line item to a
customer without saying "the AI decided".

Severity scales the price; multiple panels with the same defect are consolidated
into one service line rather than charged repeatedly, because that is how the
work is actually performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Fallback rate card in cents. Used only when the studio has no matching service.
# Deliberately conservative — under-quoting is recoverable, over-quoting is not.
DEFAULT_RATE_CARD: Dict[str, Dict[str, Any]] = {
    "swirl_marks":        {"service": "Paint Correction — Stage 1", "base_cents": 25000, "per_panel_cents": 3500},
    "light_scratch":      {"service": "Paint Correction — Stage 1", "base_cents": 25000, "per_panel_cents": 4000},
    "deep_scratch":       {"service": "Paint Correction — Stage 2", "base_cents": 45000, "per_panel_cents": 9000},
    "clear_coat_failure": {"service": "Paint Correction — Stage 2", "base_cents": 45000, "per_panel_cents": 12000},
    "oxidation":          {"service": "Paint Correction — Stage 1", "base_cents": 25000, "per_panel_cents": 5000},
    "water_spots":        {"service": "Water Spot Removal",         "base_cents": 12000, "per_panel_cents": 2500},
    "paint_transfer":     {"service": "Paint Transfer Removal",     "base_cents": 9000,  "per_panel_cents": 3000},
    "overspray":          {"service": "Overspray Removal",          "base_cents": 18000, "per_panel_cents": 4500},
    "rock_chip":          {"service": "Chip Touch-Up",              "base_cents": 8000,  "per_panel_cents": 2500},
    "dent":               {"service": "Paintless Dent Repair",      "base_cents": 15000, "per_panel_cents": 11000},
    "curb_rash":          {"service": "Wheel Refinishing",          "base_cents": 20000, "per_panel_cents": 9000},
    "glass_etching":      {"service": "Glass Polishing",            "base_cents": 11000, "per_panel_cents": 3500},
    "trim_fade":          {"service": "Trim Restoration",           "base_cents": 9500,  "per_panel_cents": 2500},
    "interior_stain":     {"service": "Interior Deep Clean",        "base_cents": 16000, "per_panel_cents": 0},
}

# Severity 1-5 → price multiplier. A barely-visible swirl is not half a respray.
SEVERITY_MULTIPLIER = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.35, 5: 1.7}

# Below this, we record the finding but do not put a price on it — the model
# isn't sure enough to justify asking a customer for money.
MIN_BILLABLE_CONFIDENCE = 0.45

# Keywords used to match a defect's service name against the studio's own catalog.
CATALOG_HINTS: Dict[str, List[str]] = {
    "swirl_marks":        ["paint correction", "polish", "correction"],
    "light_scratch":      ["paint correction", "polish", "correction"],
    "deep_scratch":       ["paint correction", "stage 2", "compound"],
    "clear_coat_failure": ["paint correction", "stage 2", "respray"],
    "oxidation":          ["paint correction", "polish", "restoration"],
    "water_spots":        ["water spot", "decontamination", "polish"],
    "paint_transfer":     ["decontamination", "polish", "paint correction"],
    "overspray":          ["decontamination", "clay", "overspray"],
    "rock_chip":          ["chip", "touch up", "touch-up", "ppf"],
    "dent":               ["dent", "pdr"],
    "curb_rash":          ["wheel", "rim"],
    "glass_etching":      ["glass", "windshield"],
    "trim_fade":          ["trim", "restoration"],
    "interior_stain":     ["interior", "shampoo", "deep clean"],
}


@dataclass
class UpchargeLine:
    defect_type: str
    service_name: str
    panels: List[str]
    max_severity: int
    avg_confidence: float
    amount_cents: int
    pricing_basis: str          # "studio_catalog" | "default_rate_card" | "not_priced"
    finding_ids: List[int]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "defect_type": self.defect_type,
            "service_name": self.service_name,
            "panels": self.panels,
            "panel_count": len(self.panels),
            "max_severity": self.max_severity,
            "avg_confidence": round(self.avg_confidence, 2),
            "amount_cents": self.amount_cents,
            "amount_display": f"${self.amount_cents / 100:,.2f}",
            "pricing_basis": self.pricing_basis,
            "finding_ids": self.finding_ids,
        }


class UpchargeCalculator:
    """
    Construct with the studio's service catalog so pricing reflects real prices.

    `services` rows come from the `services` table: {name, price} where price is
    in whole dollars (matching the existing schema's convention).
    """

    def __init__(self, services: Optional[List[Dict[str, Any]]] = None):
        self.services = services or []

    # ── catalog matching ──────────────────────────────────────────────────

    def _match_catalog(self, defect_type: str) -> Optional[Dict[str, Any]]:
        hints = CATALOG_HINTS.get(defect_type, [])
        if not hints or not self.services:
            return None
        for hint in hints:  # hints are ordered most- to least-specific
            for svc in self.services:
                if hint in (svc.get("name") or "").lower():
                    return svc
        return None

    # ── pricing ───────────────────────────────────────────────────────────

    def price_finding(self, finding: Dict[str, Any]) -> Dict[str, Any]:
        """Price a single finding. Returns the finding enriched with cost fields."""
        defect = finding.get("defect_type", "unknown")
        severity = max(1, min(int(finding.get("severity", 1) or 1), 5))
        confidence = float(finding.get("confidence", 0) or 0)

        enriched = dict(finding)

        if confidence < MIN_BILLABLE_CONFIDENCE:
            enriched.update(
                suggested_service="",
                suggested_upcharge_cents=0,
                pricing_basis="not_priced",
                pricing_note=f"Confidence {confidence:.0%} is below the {MIN_BILLABLE_CONFIDENCE:.0%} "
                             f"threshold — logged for a human to confirm, not quoted.",
            )
            return enriched

        catalog_hit = self._match_catalog(defect)
        if catalog_hit:
            base_cents = int(float(catalog_hit.get("price") or 0) * 100)
            service_name = catalog_hit.get("name", "")
            basis = "studio_catalog"
        else:
            card = DEFAULT_RATE_CARD.get(defect)
            if not card:
                enriched.update(
                    suggested_service="",
                    suggested_upcharge_cents=0,
                    pricing_basis="not_priced",
                    pricing_note=f"No price configured for defect type '{defect}'.",
                )
                return enriched
            base_cents = int(card["base_cents"])
            service_name = card["service"]
            basis = "default_rate_card"

        amount = int(base_cents * SEVERITY_MULTIPLIER.get(severity, 1.0))
        enriched.update(
            suggested_service=service_name,
            suggested_upcharge_cents=amount,
            pricing_basis=basis,
            pricing_note=(
                f"{service_name} at severity {severity}/5 "
                f"({'studio price list' if basis == 'studio_catalog' else 'standard rate card'})."
            ),
        )
        return enriched

    def consolidate(self, findings: List[Dict[str, Any]]) -> List[UpchargeLine]:
        """
        Group findings by defect type into billable lines. Swirl marks on four
        panels is one correction job priced by area — not four separate charges.
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for f in findings:
            if int(f.get("suggested_upcharge_cents", 0) or 0) <= 0:
                continue
            groups.setdefault(f.get("defect_type", "unknown"), []).append(f)

        lines: List[UpchargeLine] = []
        for defect, group in groups.items():
            panels = sorted({g.get("panel", "unspecified") for g in group})
            max_sev = max(int(g.get("severity", 1) or 1) for g in group)
            avg_conf = sum(float(g.get("confidence", 0) or 0) for g in group) / len(group)

            base = max(int(g.get("suggested_upcharge_cents", 0) or 0) for g in group)
            basis = next((g.get("pricing_basis") for g in group if g.get("pricing_basis")), "default_rate_card")

            # Additional panels bill at the incremental rate, not the full base.
            per_panel = int(DEFAULT_RATE_CARD.get(defect, {}).get("per_panel_cents", 0))
            extra_panels = max(len(panels) - 1, 0)
            amount = base + int(per_panel * extra_panels * SEVERITY_MULTIPLIER.get(max_sev, 1.0))

            lines.append(
                UpchargeLine(
                    defect_type=defect,
                    service_name=group[0].get("suggested_service", ""),
                    panels=panels,
                    max_severity=max_sev,
                    avg_confidence=avg_conf,
                    amount_cents=amount,
                    pricing_basis=basis or "default_rate_card",
                    finding_ids=[int(g["id"]) for g in group if g.get("id")],
                )
            )

        lines.sort(key=lambda line: -line.amount_cents)
        return lines

    @staticmethod
    def condition_score(findings: List[Dict[str, Any]]) -> float:
        """
        0-10 vehicle condition, 10 being flawless. Severity-weighted so one deep
        scratch outranks five faint swirls.
        """
        if not findings:
            return 10.0
        penalty = sum(
            (int(f.get("severity", 1) or 1) ** 1.5) * max(float(f.get("confidence", 0.5) or 0.5), 0.3)
            for f in findings
        )
        return round(max(0.0, 10.0 - min(penalty * 0.6, 10.0)), 1)
