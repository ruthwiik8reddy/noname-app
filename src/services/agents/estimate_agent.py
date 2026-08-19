"""
services/agents/estimate_agent.py — watches quoting and pricing.

Estimates leak revenue quietly. A quote sent three weeks ago that nobody chased,
a DVI with accepted findings that never became an estimate, a service priced
below what the materials cost — none of these generate an alert anywhere, and
none are visible unless somebody goes looking.

Everything here is arithmetic over your own catalog. No model call at all: the
findings are either true or they aren't, and a language model can only make that
less certain.
"""

from __future__ import annotations

from typing import List

from ..repositories.base import BaseRepository
from .base import BaseAgent, Finding

STALE_ESTIMATE_DAYS = 7
DEAD_ESTIMATE_DAYS = 21


class EstimateAgent(BaseAgent):
    name = "estimate"
    label = "Estimate Agent"
    description = "Chases unanswered quotes and surfaces work that was found but never billed."
    icon = "receipt"
    default_interval_mins = 240
    responds_to = ("estimate_created", "dvi_analyzed")

    def collect(self) -> List[Finding]:
        repo = BaseRepository()
        findings: List[Finding] = []

        # ── 1. Sent estimates with no response ──
        stale = repo.fetch_all(
            """
            SELECT id, customer_name, vehicle, total, status,
                   CAST(julianday('now') - julianday(created_at) AS INTEGER) AS days_old
            FROM estimates
            WHERE studio_id=? AND status IN ('Sent','Draft')
              AND julianday('now') - julianday(created_at) >= ?
            ORDER BY days_old DESC LIMIT 20
            """,
            (self.studio_id, STALE_ESTIMATE_DAYS),
        )
        for est in stale:
            days = int(est["days_old"] or 0)
            dead = days >= DEAD_ESTIMATE_DAYS
            findings.append(Finding(
                kind="estimate_stale",
                severity="warning" if dead else "opportunity",
                title=f"{est['customer_name']}'s quote is {days} days old",
                detail=(
                    f"${(est['total'] or 0) / 100:,.2f} for {est['vehicle'] or 'their vehicle'}, "
                    f"still marked '{est['status']}'. "
                    + ("Likely cold — worth closing out or one last call."
                       if dead else "A short follow-up often converts these.")
                ),
                action_label="Open estimate",
                action_url=f"/estimates/{est['id']}",
                entity_type="estimate",
                entity_id=est["id"],
                fingerprint=f"estimate_stale:{est['id']}",
                data={"days_old": days, "total_cents": est["total"]},
            ))

        # ── 2. Inspections that found billable work but never got quoted ──
        if repo.table_exists("dvi_inspections"):
            orphaned = repo.fetch_all(
                """
                SELECT d.id, d.total_upcharge_cents, j.car,
                       (SELECT COUNT(*) FROM dvi_findings f
                         WHERE f.inspection_id = d.id AND f.decision='accepted') AS accepted
                FROM dvi_inspections d
                LEFT JOIN jobs j ON j.id = d.job_id
                WHERE d.studio_id=? AND d.estimate_id IS NULL
                  AND d.status='analyzed' AND d.total_upcharge_cents > 0
                ORDER BY d.total_upcharge_cents DESC LIMIT 10
                """,
                (self.studio_id,),
            )
            for insp in orphaned:
                amount = int(insp["total_upcharge_cents"] or 0)
                accepted = int(insp["accepted"] or 0)
                findings.append(Finding(
                    kind="dvi_unquoted",
                    severity="opportunity",
                    title=f"Inspection on {insp['car'] or 'a vehicle'} found "
                          f"${amount / 100:,.2f} of unquoted work",
                    detail=(
                        f"{accepted} finding(s) accepted but no estimate was created."
                        if accepted else
                        "Findings were recorded but none accepted yet — worth reviewing "
                        "with the customer."
                    ),
                    action_label="Open inspection",
                    action_url=f"/dvi/inspection/{insp['id']}",
                    entity_type="dvi_inspection",
                    entity_id=insp["id"],
                    fingerprint=f"dvi_unquoted:{insp['id']}",
                    data={"amount_cents": amount, "accepted": accepted},
                ))

        # ── 3. Services never quoted ──
        # NOTE: an earlier version of this agent compared each service price
        # against the *average* product cost-per-sqft across the whole catalog,
        # multiplied by a nominal 200 sqft. That is meaningless — it charged a
        # $250 interior detail with the material cost of a full-body PPF wrap,
        # and consequently flagged every single service as "priced thin".
        #
        # An agent that flags everything is an agent nobody reads. A real margin
        # check needs per-service material mapping (which product, how much),
        # and that data doesn't exist yet. Until it does, saying nothing is the
        # honest option.
        unquoted = repo.fetch_all(
            """
            SELECT s.id, s.name, s.price
            FROM services s
            WHERE s.studio_id=? AND s.price > 0
              AND NOT EXISTS (
                SELECT 1 FROM estimate_items ei
                JOIN estimates e ON e.id = ei.estimate_id AND e.studio_id = s.studio_id
                WHERE ei.name = s.name
              )
              AND NOT EXISTS (
                SELECT 1 FROM jobs j WHERE j.studio_id = s.studio_id AND j.service = s.name
              )
            """,
            (self.studio_id,),
        ) if repo.table_exists("estimate_items") else []

        if len(unquoted) >= 2:
            names = ", ".join(u["name"] for u in unquoted[:5])
            findings.append(Finding(
                kind="service_unused",
                severity="info",
                title=f"{len(unquoted)} service(s) have never been sold",
                detail=(
                    f"{names} appear in your catalog but have never been quoted or "
                    f"delivered. Either they need promoting, or the catalog needs pruning "
                    f"so quotes are quicker to build."
                ),
                action_label="Review services",
                action_url="/account",
                fingerprint="service_unused",
                data={"count": len(unquoted)},
            ))

        return findings
