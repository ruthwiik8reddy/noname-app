"""services/repositories/lead_repository.py — the sales pipeline."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseRepository

# Ordered. A lead moves left to right, or falls out to 'lost'.
PIPELINE = ("new", "contacted", "quoted", "negotiating", "won", "lost")
ACTIVE = ("new", "contacted", "quoted", "negotiating")

SOURCES = ("manual", "booking_form", "whatsapp", "phone", "walk_in",
           "instagram", "referral", "website", "other")


class LeadRepository(BaseRepository):

    # ── reads ─────────────────────────────────────────────────────────────

    def get(self, studio_id: int, lead_id: int) -> Dict[str, Any]:
        return self.fetch_one("SELECT * FROM leads WHERE id=? AND studio_id=?", (lead_id, studio_id))

    def list_all(self, studio_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return self.fetch_all(
                "SELECT * FROM leads WHERE studio_id=? AND status=? ORDER BY score DESC, id DESC",
                (studio_id, status),
            )
        return self.fetch_all(
            "SELECT * FROM leads WHERE studio_id=? ORDER BY "
            "CASE status WHEN 'won' THEN 1 WHEN 'lost' THEN 2 ELSE 0 END, score DESC, id DESC",
            (studio_id,),
        )

    def active(self, studio_id: int) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE)
        return self.fetch_all(
            f"SELECT * FROM leads WHERE studio_id=? AND status IN ({placeholders}) "
            f"ORDER BY score DESC, id DESC",
            (studio_id, *ACTIVE),
        )

    def pipeline(self, studio_id: int) -> Dict[str, List[Dict[str, Any]]]:
        return {s: self.list_all(studio_id, s) for s in PIPELINE}

    def stale(self, studio_id: int, days: int = 5) -> List[Dict[str, Any]]:
        """
        Active leads with no contact in `days`. This is the single most valuable
        query in the file — a lead going quiet is invisible until someone asks.
        """
        placeholders = ",".join("?" for _ in ACTIVE)
        return self.fetch_all(
            f"""
            SELECT *,
                   CAST(julianday('now') - julianday(
                        CASE WHEN last_contacted != '' THEN last_contacted ELSE created_at END
                   ) AS INTEGER) AS days_quiet
            FROM leads
            WHERE studio_id=? AND status IN ({placeholders})
              AND julianday('now') - julianday(
                    CASE WHEN last_contacted != '' THEN last_contacted ELSE created_at END
                  ) >= ?
            ORDER BY days_quiet DESC
            """,
            (studio_id, *ACTIVE, days),
        )

    def stats(self, studio_id: int, days: int = 30) -> Dict[str, Any]:
        counts = {
            r["status"]: int(r["n"]) for r in self.fetch_all(
                "SELECT status, COUNT(*) n FROM leads WHERE studio_id=? GROUP BY status", (studio_id,)
            )
        }
        won = counts.get("won", 0)
        closed = won + counts.get("lost", 0)
        pipeline_value = int(self.fetch_scalar(
            f"SELECT COALESCE(SUM(budget_hint),0) FROM leads WHERE studio_id=? "
            f"AND status IN ({','.join('?' for _ in ACTIVE)})",
            (studio_id, *ACTIVE), default=0,
        ))
        new_recent = int(self.fetch_scalar(
            "SELECT COUNT(*) FROM leads WHERE studio_id=? AND created_at >= date('now', ?)",
            (studio_id, f"-{days} days"), default=0,
        ))
        return {
            "by_status": counts,
            "total": sum(counts.values()),
            "active": sum(counts.get(s, 0) for s in ACTIVE),
            "won": won,
            "conversion_rate": round(won / closed * 100, 1) if closed else 0.0,
            "pipeline_value": pipeline_value,
            "new_last_30d": new_recent,
        }

    def by_source(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT source,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS won,
                   SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS lost
            FROM leads WHERE studio_id=? GROUP BY source ORDER BY total DESC
            """,
            (studio_id,),
        )

    # ── writes ────────────────────────────────────────────────────────────

    def create(self, studio_id: int, data: Dict[str, Any], actor: str = "") -> int:
        lead_id = self.execute(
            """
            INSERT INTO leads (studio_id, name, phone, email, vehicle, service_interest,
                               budget_hint, source, status, notes, booking_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                studio_id,
                (data.get("name") or "Unknown").strip(),
                (data.get("phone") or "").strip(),
                (data.get("email") or "").strip(),
                (data.get("vehicle") or "").strip(),
                (data.get("service_interest") or "").strip(),
                int(data.get("budget_hint") or 0),
                data.get("source") if data.get("source") in SOURCES else "manual",
                data.get("status") if data.get("status") in PIPELINE else "new",
                (data.get("notes") or "").strip(),
                data.get("booking_id"),
            ),
        )
        self.add_event(studio_id, lead_id, "created",
                       f"Captured via {data.get('source', 'manual')}", actor)
        return lead_id

    def update_status(self, studio_id: int, lead_id: int, status: str, actor: str) -> bool:
        if status not in PIPELINE:
            return False
        ok = bool(self.execute(
            "UPDATE leads SET status=?, updated_at=datetime('now') WHERE id=? AND studio_id=?",
            (status, lead_id, studio_id),
        ))
        if ok:
            self.add_event(studio_id, lead_id, "status", f"Moved to {status}", actor)
        return ok

    def log_contact(self, studio_id: int, lead_id: int, detail: str, actor: str) -> None:
        self.execute(
            "UPDATE leads SET last_contacted=datetime('now'), updated_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (lead_id, studio_id),
        )
        self.add_event(studio_id, lead_id, "contact", detail, actor)

    def set_score(self, studio_id: int, lead_id: int, score: int, reason: str) -> None:
        self.execute(
            "UPDATE leads SET score=?, score_reason=?, updated_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (max(0, min(int(score), 100)), reason[:400], lead_id, studio_id),
        )

    def set_next_action(self, studio_id: int, lead_id: int, action: str, due: str = "") -> None:
        self.execute(
            "UPDATE leads SET next_action=?, next_action_due=?, updated_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (action[:200], due, lead_id, studio_id),
        )

    def add_event(self, studio_id: int, lead_id: int, kind: str, detail: str, actor: str) -> int:
        return self.execute(
            "INSERT INTO lead_events (studio_id, lead_id, kind, detail, actor) VALUES (?,?,?,?,?)",
            (studio_id, lead_id, kind, detail[:400], actor),
        )

    def events(self, studio_id: int, lead_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM lead_events WHERE studio_id=? AND lead_id=? ORDER BY id DESC",
            (studio_id, lead_id),
        )

    def convert(self, studio_id: int, lead_id: int, actor: str) -> Optional[int]:
        """Promote a won lead into a real customer record."""
        lead = self.get(studio_id, lead_id)
        if not lead:
            return None
        if lead.get("customer_id"):
            return int(lead["customer_id"])

        customer_id = self.execute(
            "INSERT INTO customers (studio_id, name, email, phone) VALUES (?,?,?,?)",
            (studio_id, lead["name"], lead.get("email", ""), lead.get("phone", "")),
        )
        self.execute(
            "UPDATE leads SET customer_id=?, status='won', updated_at=datetime('now') WHERE id=?",
            (customer_id, lead_id),
        )
        self.add_event(studio_id, lead_id, "converted", "Lead converted to customer", actor)
        return customer_id

    # ── bulk import (WhatsApp / call logs) ────────────────────────────────

    def bulk_import(self, studio_id: int, rows: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """
        Import parsed enquiries. De-duplicates on phone, because the same person
        messaging twice is one lead, not two.
        """
        created, skipped = 0, 0
        for row in rows:
            phone = (row.get("phone") or "").strip()
            email = (row.get("email") or "").strip().lower()
            name = (row.get("name") or "").strip()

            # Match on phone OR email OR name — someone who messaged from a
            # number once and emailed the next time is still one person.
            # Matching on phone alone silently duplicated email-only enquiries.
            dupe = None
            if phone:
                dupe = self.fetch_one(
                    "SELECT 1 FROM leads WHERE studio_id=? AND phone=?", (studio_id, phone))
            if not dupe and email:
                dupe = self.fetch_one(
                    "SELECT 1 FROM leads WHERE studio_id=? AND lower(email)=?", (studio_id, email))
            if not dupe and name and not phone and not email:
                dupe = self.fetch_one(
                    "SELECT 1 FROM leads WHERE studio_id=? AND lower(name)=?", (studio_id, name.lower()))
            if dupe:
                skipped += 1
                continue
            self.create(studio_id, row, actor)
            created += 1
        return {"created": created, "skipped": skipped, "total": len(rows)}
