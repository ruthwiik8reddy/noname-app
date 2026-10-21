"""
services/tracking_stages.py — the detailing stages a customer sees on their tracking link.

Jobs keep their three operational statuses (Pending / In Progress /
Completed). While a car is in progress, staff can say *where* it is —
washing, paint correction, coating, curing — and the customer's live
timeline follows. Pending and Ready come from the status itself, so the
timeline can never claim a car is ready before the job is completed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

# (name, what the customer is told). Order is the order of the work.
STAGES: List[tuple] = [
    ("Pending", "Scheduled and waiting."),
    ("Washing", "Deep cleaning and decontamination."),
    ("Correction", "Paint leveling and polishing."),
    ("Coating", "Applying protection layer."),
    ("Curing", "Bonding under IR lamps."),
    ("Ready", "Your vehicle is ready for delivery!"),
]
NAMES = [name for name, _ in STAGES]
WORK_STAGES = NAMES[1:-1]          # the ones staff pick while a job is in progress


def stage_index(job: Mapping[str, Any]) -> int:
    status = (job.get("status") or "").strip().lower()
    if status == "completed":
        return len(STAGES) - 1
    if status == "in progress":
        stage = job.get("tracking_stage") or ""
        return NAMES.index(stage) if stage in WORK_STAGES else 1
    return 0


def timeline(job: Mapping[str, Any]) -> List[Dict[str, Any]]:
    current = stage_index(job)
    return [{"name": name, "desc": desc,
             "state": "completed" if i < current else "active" if i == current else "upcoming"}
            for i, (name, desc) in enumerate(STAGES)]
