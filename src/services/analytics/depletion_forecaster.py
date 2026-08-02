"""
services/analytics/depletion_forecaster.py

Pure arithmetic. No LLM, no I/O, no Flask — give it rows, get back numbers.

Why this module exists at all: a 3B or 8B model running on a laptop cannot be
trusted to divide 4.5 litres by 0.31 litres/day and tell you the truth. Ask it
to, and it will confidently invent a date. So the split is strict:

    Python decides WHAT IS TRUE   →  burn rate, stockout date, confidence
    The model decides HOW TO SAY IT →  prioritisation, phrasing, buying advice

If Ollama is down, the numbers still render. You lose the prose, not the
forecast. That is the difference between a feature and a demo.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Optional

# Recent behaviour predicts next week better than last month's average, but a
# 7-day window alone is noisy in a shop that details 4 cars a week. Blend them.
SHORT_WINDOW_DAYS = 7
LONG_WINDOW_DAYS = 30
SHORT_WEIGHT = 0.6
LONG_WEIGHT = 0.4

URGENCY_CRITICAL_DAYS = 7
URGENCY_WARNING_DAYS = 21

# A single job using >2.2x the studio's own median for that product is worth a look.
WASTE_MULTIPLIER = 2.2
WASTE_MIN_SAMPLES = 4


@dataclass
class ItemForecast:
    item_id: int
    sku: str
    name: str
    category: str
    unit: str
    current_qty: float
    reorder_level: float
    cost_per_unit_cents: int

    daily_burn: float = 0.0
    weekly_burn: float = 0.0
    per_job_usage: float = 0.0
    days_to_reorder: Optional[float] = None
    days_to_empty: Optional[float] = None
    stockout_date: Optional[str] = None
    reorder_date: Optional[str] = None
    suggested_order_qty: float = 0.0
    suggested_order_cost_cents: int = 0
    urgency: str = "ok"                 # critical | warning | ok | idle
    confidence: str = "low"             # high | medium | low | none
    observation_days: int = 0
    active_days: int = 0
    total_consumed: float = 0.0
    below_reorder: bool = False
    rationale: str = ""

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["suggested_order_cost_display"] = f"${self.suggested_order_cost_cents / 100:,.2f}"
        return data


@dataclass
class WasteSignal:
    item_id: int
    sku: str
    name: str
    unit: str
    median_per_job: float
    outlier_jobs: List[Dict[str, Any]] = field(default_factory=list)
    excess_units: float = 0.0
    excess_cost_cents: int = 0
    sample_size: int = 0

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["excess_cost_display"] = f"${self.excess_cost_cents / 100:,.2f}"
        return data


def _parse_day(value: str) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


class DepletionForecaster:
    """Turns movement logs into stockout dates. Deterministic and unit-testable."""

    def __init__(self, today: Optional[date] = None):
        self.today = today or date.today()

    # ── main entry point ──────────────────────────────────────────────────

    def forecast_all(
        self,
        items: List[Dict[str, Any]],
        daily_rows: List[Dict[str, Any]],
        first_seen: Optional[Dict[int, str]] = None,
        jobs_completed: int = 0,
        job_window_days: int = 30,
        expected_jobs_next_7d: float = 0.0,
    ) -> List[ItemForecast]:
        """
        items       — inventory_items rows
        daily_rows  — {item_id, day, qty} aggregates of negative movements
        first_seen  — {item_id: earliest log timestamp}, for confidence scoring
        """
        by_item: Dict[int, Dict[date, float]] = {}
        for row in daily_rows:
            day = _parse_day(row.get("day", ""))
            if day is None:
                continue
            by_item.setdefault(int(row["item_id"]), {})[day] = float(row.get("qty") or 0)

        first_seen = first_seen or {}
        forecasts = [
            self._forecast_item(
                item,
                by_item.get(int(item["id"]), {}),
                _parse_day(first_seen.get(int(item["id"]), "")),
                jobs_completed,
                job_window_days,
                expected_jobs_next_7d,
            )
            for item in items
        ]

        urgency_rank = {"critical": 0, "warning": 1, "ok": 2, "idle": 3}
        forecasts.sort(
            key=lambda f: (
                urgency_rank.get(f.urgency, 4),
                f.days_to_empty if f.days_to_empty is not None else 9_999,
            )
        )
        return forecasts

    # ── per-item ──────────────────────────────────────────────────────────

    def _forecast_item(
        self,
        item: Dict[str, Any],
        daily: Dict[date, float],
        first_seen: Optional[date],
        jobs_completed: int,
        job_window_days: int,
        expected_jobs_next_7d: float,
    ) -> ItemForecast:
        qty = float(item.get("quantity") or 0)
        reorder = float(item.get("reorder_level") or 0)
        cost = int(item.get("cost_per_unit") or 0)

        fc = ItemForecast(
            item_id=int(item["id"]),
            sku=item.get("sku", ""),
            name=item.get("name", ""),
            category=item.get("category", "General"),
            unit=item.get("unit", "units"),
            current_qty=round(qty, 2),
            reorder_level=reorder,
            cost_per_unit_cents=cost,
            below_reorder=qty <= reorder,
        )

        short_total, short_days = self._window_stats(daily, SHORT_WINDOW_DAYS)
        long_total, long_days = self._window_stats(daily, LONG_WINDOW_DAYS)
        fc.total_consumed = round(long_total, 2)
        fc.active_days = len([d for d, q in daily.items() if q > 0])

        # How much history do we actually have? A product added yesterday must not
        # be forecast as though we watched it for a month.
        earliest = first_seen or (min(daily) if daily else None)
        fc.observation_days = max(1, (self.today - earliest).days) if earliest else 0

        # A product first stocked 3 days ago must be divided by 3, not by 30 —
        # otherwise a brand-new item always looks like it is barely moving.
        effective_short = min(short_days, max(fc.observation_days, 1))
        effective_long = min(long_days, max(fc.observation_days, 1))
        short_rate = short_total / effective_short if short_total > 0 else 0.0
        long_rate = long_total / effective_long if long_total > 0 else 0.0

        if short_rate > 0 and long_rate > 0:
            fc.daily_burn = SHORT_WEIGHT * short_rate + LONG_WEIGHT * long_rate
        else:
            fc.daily_burn = short_rate or long_rate

        # Demand-adjust: if the diary is busier than the period we measured, scale up.
        if jobs_completed > 0 and long_total > 0:
            fc.per_job_usage = round(long_total / jobs_completed, 3)
            observed_jobs_per_day = jobs_completed / max(job_window_days, 1)
            upcoming_jobs_per_day = expected_jobs_next_7d / 7.0 if expected_jobs_next_7d else 0.0
            if observed_jobs_per_day > 0 and upcoming_jobs_per_day > 0:
                demand_ratio = upcoming_jobs_per_day / observed_jobs_per_day
                # Clamp: a booking spike shouldn't triple the forecast on thin data.
                fc.daily_burn *= max(0.5, min(demand_ratio, 2.0))

        fc.daily_burn = round(fc.daily_burn, 4)
        fc.weekly_burn = round(fc.daily_burn * 7, 2)

        if fc.daily_burn > 0:
            fc.days_to_empty = round(qty / fc.daily_burn, 1)
            headroom = qty - reorder
            fc.days_to_reorder = round(max(headroom, 0) / fc.daily_burn, 1) if headroom > 0 else 0.0
            fc.stockout_date = (self.today + timedelta(days=math.floor(fc.days_to_empty))).isoformat()
            fc.reorder_date = (self.today + timedelta(days=math.floor(fc.days_to_reorder))).isoformat()

        fc.confidence = self._confidence(fc)
        fc.urgency = self._urgency(fc)
        fc.suggested_order_qty, fc.suggested_order_cost_cents = self._suggest_order(fc)
        fc.rationale = self._rationale(fc)
        return fc

    def _window_stats(self, daily: Dict[date, float], days: int) -> tuple[float, int]:
        """
        Total usage over a window, plus the number of days that total is spread across.

        Subtle but important: today is a PARTIAL day. Counting it in the
        denominator before it has finished divides a full window's usage by one
        more day than actually elapsed, which understates the burn rate by
        roughly 1/window. Understating burn means telling a studio they have
        more time than they do — the one direction this must never err in.

        So the denominator is the `days` complete days ending yesterday, and
        today is only counted once it has usage logged against it.
        """
        start = self.today - timedelta(days=days)
        total = sum(q for d, q in daily.items() if start <= d <= self.today)
        denominator = days + (1 if self.today in daily else 0)
        return total, denominator

    def _confidence(self, fc: ItemForecast) -> str:
        if fc.daily_burn <= 0:
            return "none"
        if fc.observation_days >= 21 and fc.active_days >= 5:
            return "high"
        if fc.observation_days >= 10 and fc.active_days >= 3:
            return "medium"
        return "low"

    def _urgency(self, fc: ItemForecast) -> str:
        if fc.daily_burn <= 0:
            # Sitting below reorder with no movement is still a buying decision.
            return "warning" if fc.below_reorder else "idle"
        if fc.current_qty <= 0:
            return "critical"
        if fc.days_to_empty is not None and fc.days_to_empty <= URGENCY_CRITICAL_DAYS:
            return "critical"
        if fc.below_reorder:
            return "critical"
        if fc.days_to_reorder is not None and fc.days_to_reorder <= URGENCY_WARNING_DAYS:
            return "warning"
        return "ok"

    def _suggest_order(self, fc: ItemForecast) -> tuple[float, int]:
        """Order enough to cover 30 days of burn plus the reorder buffer."""
        if fc.daily_burn <= 0:
            if fc.below_reorder:
                qty = max(fc.reorder_level * 2 - fc.current_qty, 0)
                return round(qty, 2), int(qty * fc.cost_per_unit_cents)
            return 0.0, 0
        target = fc.daily_burn * 30 + fc.reorder_level
        qty = max(target - fc.current_qty, 0)
        qty = math.ceil(qty * 10) / 10
        return qty, int(qty * fc.cost_per_unit_cents)

    def _rationale(self, fc: ItemForecast) -> str:
        if fc.daily_burn <= 0:
            return (
                f"No recorded usage in the last {LONG_WINDOW_DAYS} days"
                + (" — but stock is at or below the reorder level." if fc.below_reorder else ".")
            )
        window = f"{fc.observation_days}d of history, {fc.active_days} active days"
        base = (
            f"Burning {fc.daily_burn:g} {fc.unit}/day ({fc.weekly_burn:g}/week) — {window}. "
            f"{fc.current_qty:g} {fc.unit} left"
        )
        if fc.days_to_empty is not None:
            base += f" ≈ {fc.days_to_empty:g} days, empty around {fc.stockout_date}"
        if fc.per_job_usage:
            base += f". Averaging {fc.per_job_usage:g} {fc.unit} per completed job"
        return base + "."

    # ── waste detection ───────────────────────────────────────────────────

    def detect_waste(
        self,
        items: List[Dict[str, Any]],
        per_job_usage: Dict[int, Dict[int, float]],
    ) -> List[WasteSignal]:
        """
        Flags jobs that consumed unusually much of a product, measured against
        the studio's own median for that product. Comparing a shop to itself —
        rather than to an industry number — is what keeps this from being noise.
        """
        item_index = {int(i["id"]): i for i in items}
        signals: List[WasteSignal] = []

        for item_id, jobs in per_job_usage.items():
            item = item_index.get(item_id)
            if not item or len(jobs) < WASTE_MIN_SAMPLES:
                continue

            amounts = [q for q in jobs.values() if q > 0]
            if len(amounts) < WASTE_MIN_SAMPLES:
                continue

            baseline = median(amounts)
            if baseline <= 0:
                continue

            threshold = baseline * WASTE_MULTIPLIER
            outliers = [
                {
                    "job_id": job_id,
                    "used": round(qty, 2),
                    "expected": round(baseline, 2),
                    "excess": round(qty - baseline, 2),
                    "multiple": round(qty / baseline, 1),
                }
                for job_id, qty in sorted(jobs.items(), key=lambda kv: -kv[1])
                if qty > threshold
            ]
            if not outliers:
                continue

            excess = sum(o["excess"] for o in outliers)
            signals.append(
                WasteSignal(
                    item_id=item_id,
                    sku=item.get("sku", ""),
                    name=item.get("name", ""),
                    unit=item.get("unit", "units"),
                    median_per_job=round(baseline, 2),
                    outlier_jobs=outliers[:10],
                    excess_units=round(excess, 2),
                    excess_cost_cents=int(excess * int(item.get("cost_per_unit") or 0)),
                    sample_size=len(amounts),
                )
            )

        signals.sort(key=lambda s: -s.excess_cost_cents)
        return signals

    # ── roll-up ───────────────────────────────────────────────────────────

    @staticmethod
    def portfolio_health(forecasts: Iterable[ItemForecast]) -> Dict[str, Any]:
        items = list(forecasts)
        total = len(items)
        if not total:
            return {
                "health_score": "No Data",
                "total_items": 0,
                "critical_count": 0,
                "warning_count": 0,
                "reorder_cost_cents": 0,
                "reorder_cost_display": "$0.00",
                "stockouts_next_7d": 0,
                "stockouts_next_30d": 0,
            }

        critical = [f for f in items if f.urgency == "critical"]
        warning = [f for f in items if f.urgency == "warning"]
        reorder_cost = sum(f.suggested_order_cost_cents for f in items)

        critical_ratio = len(critical) / total
        warning_ratio = (len(critical) + len(warning)) / total
        if critical_ratio > 0.25:
            score = "Critical"
        elif warning_ratio > 0.35 or critical:
            score = "Attention Needed"
        else:
            score = "Good"

        return {
            "health_score": score,
            "total_items": total,
            "critical_count": len(critical),
            "warning_count": len(warning),
            "idle_count": len([f for f in items if f.urgency == "idle"]),
            "reorder_cost_cents": reorder_cost,
            "reorder_cost_display": f"${reorder_cost / 100:,.2f}",
            "stockouts_next_7d": len(
                [f for f in items if f.days_to_empty is not None and f.days_to_empty <= 7]
            ),
            "stockouts_next_30d": len(
                [f for f in items if f.days_to_empty is not None and f.days_to_empty <= 30]
            ),
        }
