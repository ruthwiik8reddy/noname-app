"""
reminder_service.py — Follow-up reminder scheduling
-----------------------------------------------------
Automatic triggers on job completion:
  1. Review request   — 1 day after Completed
  2. Rebooking nudge  — N days after, based on service type
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

REBOOK_INTERVALS = {
    "ceramic coating": 180,
    "graphene": 240,
    "ppf": 365,
    "paint correction": 120,
    "interior detail": 60,
    "full detail": 45,
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
        logger.info(f"Entering schedule_for_completed_job(studio_id={studio_id}, job_id={job_id})")
        try:
            conn = self._connect()
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id)
            ).fetchone()
            if not job:
                logger.warning(f"Job {job_id} not found for studio {studio_id}")
                conn.close()
                return

            customer_id = self._find_customer_for_job(conn, studio_id, job)
            today = datetime.now().date()

            # Review request — 1 day out
            review_due = today + timedelta(days=REVIEW_REQUEST_DAYS)
            self._insert_reminder(
                conn, studio_id, customer_id, job_id, None, "review",
                f"Ask {job['car']} owner for a review of their {job['service']} experience.",
                review_due.strftime("%Y-%m-%d"),
            )

            # Rebooking nudge
            days = self._rebook_days_for(job["service"])
            rebook_due = today + timedelta(days=days)
            self._insert_reminder(
                conn, studio_id, customer_id, job_id, None, "rebooking",
                f"Time to remind {job['car']} owner about a {job['service']} top-up / re-service.",
                rebook_due.strftime("%Y-%m-%d"),
            )

            conn.close()
            logger.info(f"Exiting schedule_for_completed_job — reminders scheduled for job {job_id}")
        except Exception as e:
            logger.error(f"Error in schedule_for_completed_job(job_id={job_id}): {e}", exc_info=True)

    def _rebook_days_for(self, service_name: str) -> int:
        name = (service_name or "").lower()
        for keyword, days in REBOOK_INTERVALS.items():
            if keyword in name:
                return days
        return DEFAULT_REBOOK_DAYS

    def _find_customer_for_job(self, conn: sqlite3.Connection, studio_id: int, job) -> Optional[int]:
        """Best-effort match via vehicle name."""
        row = conn.execute(
            "SELECT customer_id FROM vehicles WHERE studio_id=? AND make_model=? LIMIT 1",
            (studio_id, job["car"])
        ).fetchone()
        return row["customer_id"] if row else None

    def _insert_reminder(self, conn, studio_id, customer_id, job_id, booking_id,
                         reminder_type, message, due_date) -> None:
        existing = conn.execute(
            "SELECT id FROM reminders WHERE studio_id=? AND job_id=? AND reminder_type=? AND status='pending'",
            (studio_id, job_id, reminder_type)
        ).fetchone()
        if existing:
            logger.debug(f"Duplicate reminder skipped: job={job_id}, type={reminder_type}")
            return
        conn.execute("""
            INSERT INTO reminders
            (studio_id, customer_id, job_id, booking_id, reminder_type, message, due_date)
            VALUES (?,?,?,?,?,?,?)
        """, (studio_id, customer_id, job_id, booking_id, reminder_type, message, due_date))
        conn.commit()
        logger.debug(f"Reminder inserted: job={job_id}, type={reminder_type}, due={due_date}")

    # ── Manual reminder creation ────────────────────────────────────────────

    def create_manual_reminder(self, studio_id: int, customer_id: int, message: str, due_date: str) -> None:
        logger.info(f"Entering create_manual_reminder(studio={studio_id}, customer={customer_id})")
        try:
            conn = self._connect()
            conn.execute("""
                INSERT INTO reminders (studio_id, customer_id, reminder_type, message, due_date)
                VALUES (?,?,?,?,?)
            """, (studio_id, customer_id, "custom", message, due_date))
            conn.commit()
            conn.close()
            logger.info("Exiting create_manual_reminder — success")
        except Exception as e:
            logger.error(f"Error in create_manual_reminder: {e}", exc_info=True)

    # ── Querying ─────────────────────────────────────────────────────────────

    def get_due_reminders(self, studio_id: int):
        logger.debug(f"Entering get_due_reminders(studio_id={studio_id})")
        conn = self._connect()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT r.*, c.name as customer_name, c.phone as customer_phone
            FROM reminders r LEFT JOIN customers c ON r.customer_id = c.id
            WHERE r.studio_id=? AND r.status='pending' AND r.due_date <= ?
            ORDER BY r.due_date ASC
        """, (studio_id, today)).fetchall()
        conn.close()
        logger.debug(f"Exiting get_due_reminders — {len(rows)} due")
        return rows

    def get_upcoming_reminders(self, studio_id: int):
        logger.debug(f"Entering get_upcoming_reminders(studio_id={studio_id})")
        conn = self._connect()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT r.*, c.name as customer_name, c.phone as customer_phone
            FROM reminders r LEFT JOIN customers c ON r.customer_id = c.id
            WHERE r.studio_id=? AND r.status='pending' AND r.due_date > ?
            ORDER BY r.due_date ASC
        """, (studio_id, today)).fetchall()
        conn.close()
        logger.debug(f"Exiting get_upcoming_reminders — {len(rows)} upcoming")
        return rows

    # ── Actions ─────────────────────────────────────────────────────────────

    def mark_sent(self, studio_id: int, reminder_id: int) -> None:
        logger.info(f"Entering mark_sent(studio={studio_id}, reminder={reminder_id})")
        try:
            conn = self._connect()
            reminder = conn.execute("""
                SELECT r.*, c.name as customer_name, c.phone as customer_phone
                FROM reminders r LEFT JOIN customers c ON r.customer_id = c.id
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
            logger.info(f"Exiting mark_sent — reminder {reminder_id} dispatched")
        except Exception as e:
            logger.error(f"Error in mark_sent(reminder={reminder_id}): {e}", exc_info=True)

    def dismiss(self, studio_id: int, reminder_id: int) -> None:
        logger.info(f"Entering dismiss(studio={studio_id}, reminder={reminder_id})")
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE reminders SET status='dismissed' WHERE id=? AND studio_id=?",
                (reminder_id, studio_id)
            )
            conn.commit()
            conn.close()
            logger.info(f"Exiting dismiss — reminder {reminder_id} dismissed")
        except Exception as e:
            logger.error(f"Error in dismiss(reminder={reminder_id}): {e}", exc_info=True)

    def _dispatch(self, reminder) -> None:
        """Delivery stub — swap for real SMS/WhatsApp/email later."""
        name = reminder["customer_name"] if "customer_name" in reminder.keys() else "Unknown"
        phone = reminder["customer_phone"] if "customer_phone" in reminder.keys() else "—"
        logger.info(f"[Dispatch] Would send to {name} ({phone}): {reminder['message']}")
