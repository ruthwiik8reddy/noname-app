"""
reminder_service.py — Follow-up reminder scheduling for Autofiera
--------------------------------------------------------------------
Two automatic triggers:
  1. Review request   — fires 1 day after a job is marked Completed
  2. Rebooking nudge   — fires N days after completion, N depends on
                          the service type (ceramic coating needs a
                          top-up sooner than a one-off detail, etc.)

Delivery is abstracted behind `_dispatch()` — right now it just marks
the reminder "sent" and prints to console. Swapping in real WhatsApp/
email sending later means only touching that one function.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional


# Days after completion to nudge for a rebooking, by service keyword.
# Falls back to DEFAULT_REBOOK_DAYS if no keyword matches.
REBOOK_INTERVALS = {
    "ceramic coating":   180,   # top-up around 6 months
    "graphene":          240,
    "ppf":               365,  # annual inspection reminder
    "paint correction":  120,
    "interior detail":   60,
    "full detail":       45,
}
DEFAULT_REBOOK_DAYS = 90
REVIEW_REQUEST_DAYS = 1


class ReminderService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Auto-scheduling on job completion ──────────────────────────────────

    def schedule_for_completed_job(self, studio_id: int, job_id: int) -> None:
        """Call this when a job's status changes to 'Completed'."""
        conn = self._connect()
        job = conn.execute("SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id)).fetchone()
        if not job:
            conn.close()
            return

        customer_id = self._find_customer_for_job(conn, studio_id, job)
        today = datetime.now().date()

        # Review request — always scheduled, 1 day out
        review_due = today + timedelta(days=REVIEW_REQUEST_DAYS)
        self._insert_reminder(
            conn, studio_id, customer_id, job_id, None,
            "review",
            f"Ask {job['car']} owner for a review of their {job['service']} experience.",
            review_due.strftime("%Y-%m-%d"),
        )

        # Rebooking nudge — interval depends on service type
        days = self._rebook_days_for(job["service"])
        rebook_due = today + timedelta(days=days)
        self._insert_reminder(
            conn, studio_id, customer_id, job_id, None,
            "rebooking",
            f"Time to remind {job['car']} owner about a {job['service']} top-up / re-service.",
            rebook_due.strftime("%Y-%m-%d"),
        )

        conn.close()

    def _rebook_days_for(self, service_name: str) -> int:
        name = (service_name or "").lower()
        for keyword, days in REBOOK_INTERVALS.items():
            if keyword in name:
                return days
        return DEFAULT_REBOOK_DAYS

    def _find_customer_for_job(self, conn: sqlite3.Connection, studio_id: int, job) -> Optional[int]:
        """Best-effort match: jobs table doesn't store customer_id directly,
        so we try to find a customer with a vehicle matching this car."""
        row = conn.execute(
            "SELECT customer_id FROM vehicles WHERE studio_id=? AND make_model=? LIMIT 1",
            (studio_id, job["car"])
        ).fetchone()
        return row["customer_id"] if row else None

    def _insert_reminder(
        self, conn, studio_id, customer_id, job_id, booking_id,
        reminder_type, message, due_date,
    ) -> None:
        # Avoid duplicate auto-reminders for the same job + type
        existing = conn.execute(
            "SELECT id FROM reminders WHERE studio_id=? AND job_id=? AND reminder_type=? AND status='pending'",
            (studio_id, job_id, reminder_type)
        ).fetchone()
        if existing:
            return
        conn.execute("""
            INSERT INTO reminders
            (studio_id, customer_id, job_id, booking_id, reminder_type, message, due_date)
            VALUES (?,?,?,?,?,?,?)
        """, (studio_id, customer_id, job_id, booking_id, reminder_type, message, due_date))
        conn.commit()

    # ── Manual reminder creation ────────────────────────────────────────────

    def create_manual_reminder(
        self, studio_id: int, customer_id: int, message: str, due_date: str
    ) -> None:
        conn = self._connect()
        conn.execute("""
            INSERT INTO reminders (studio_id, customer_id, reminder_type, message, due_date)
            VALUES (?,?,?,?,?)
        """, (studio_id, customer_id, "custom", message, due_date))
        conn.commit()
        conn.close()

    # ── Querying ─────────────────────────────────────────────────────────────

    def get_due_reminders(self, studio_id: int):
        """Reminders due today or overdue, still pending."""
        conn = self._connect()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT r.*, c.name as customer_name, c.phone as customer_phone
            FROM reminders r
            LEFT JOIN customers c ON r.customer_id = c.id
            WHERE r.studio_id=? AND r.status='pending' AND r.due_date <= ?
            ORDER BY r.due_date ASC
        """, (studio_id, today)).fetchall()
        conn.close()
        return rows

    def get_upcoming_reminders(self, studio_id: int):
        """Reminders scheduled for the future, still pending."""
        conn = self._connect()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT r.*, c.name as customer_name, c.phone as customer_phone
            FROM reminders r
            LEFT JOIN customers c ON r.customer_id = c.id
            WHERE r.studio_id=? AND r.status='pending' AND r.due_date > ?
            ORDER BY r.due_date ASC
        """, (studio_id, today)).fetchall()
        conn.close()
        return rows

    # ── Actions ─────────────────────────────────────────────────────────────

    def mark_sent(self, studio_id: int, reminder_id: int) -> None:
        conn = self._connect()
        reminder = conn.execute("""
            SELECT r.*, c.name as customer_name, c.phone as customer_phone
            FROM reminders r
            LEFT JOIN customers c ON r.customer_id = c.id
            WHERE r.id=? AND r.studio_id=?
        """, (reminder_id, studio_id)).fetchone()
        if reminder:
            self._dispatch(reminder)
            conn.execute(
                "UPDATE reminders SET status='sent', sent_at=datetime('now') WHERE id=?",
                (reminder_id,)
            )
            conn.commit()
        conn.close()

    def dismiss(self, studio_id: int, reminder_id: int) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE reminders SET status='dismissed' WHERE id=? AND studio_id=?",
            (reminder_id, studio_id)
        )
        conn.commit()
        conn.close()

    def _dispatch(self, reminder) -> None:
        """
        Delivery layer. Swap this out for real WhatsApp/email/SMS later —
        everything above this stays the same.
        """
        name  = reminder["customer_name"] if "customer_name" in reminder.keys() else "Unknown"
        phone = reminder["customer_phone"] if "customer_phone" in reminder.keys() else "—"
        print(f"[Reminder] Would send to {name} ({phone}): {reminder['message']}")
