"""
services/repositories/inventory_repository.py

Reads the raw signal the AI Estimator runs on: `inventory_items` (what you
hold) and `inventory_logs` (what moved, when, and why). Every query is scoped
by studio_id — multi-tenancy is enforced at the SQL layer, not in a template.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import BaseRepository

# Usage rows are written by the scanner as e.g. "Job #42 Usage" — we pull the id
# back out so consumption can be attributed per job and waste outliers spotted.
JOB_REF_PATTERN = re.compile(r"#\s*(\d+)")


class InventoryRepository(BaseRepository):

    # ── items ─────────────────────────────────────────────────────────────

    def list_items(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM inventory_items WHERE studio_id=? ORDER BY category, name",
            (studio_id,),
        )

    def get_item(self, studio_id: int, item_id: int) -> Dict[str, Any]:
        return self.fetch_one(
            "SELECT * FROM inventory_items WHERE id=? AND studio_id=?", (item_id, studio_id)
        )

    def find_by_sku(self, studio_id: int, sku: str) -> Dict[str, Any]:
        return self.fetch_one(
            "SELECT * FROM inventory_items WHERE studio_id=? AND sku=?", (studio_id, sku.strip().upper())
        )

    def low_stock(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM inventory_items WHERE studio_id=? AND quantity <= reorder_level "
            "ORDER BY (quantity - reorder_level) ASC",
            (studio_id,),
        )

    def total_valuation_cents(self, studio_id: int) -> int:
        return int(
            self.fetch_scalar(
                "SELECT COALESCE(SUM(quantity * cost_per_unit), 0) FROM inventory_items WHERE studio_id=?",
                (studio_id,),
                default=0,
            )
        )

    # ── movement logs ─────────────────────────────────────────────────────

    def consumption_events(self, studio_id: int, days: int = 60) -> List[Dict[str, Any]]:
        """Every negative movement in the window — the raw material for forecasting."""
        return self.fetch_all(
            """
            SELECT l.id, l.item_id, l.change_qty, l.reason, l.created_at,
                   i.name, i.sku, i.category, i.unit
            FROM inventory_logs l
            JOIN inventory_items i ON i.id = l.item_id
            WHERE l.studio_id = ?
              AND l.change_qty < 0
              AND l.created_at >= datetime('now', ?)
            ORDER BY l.created_at ASC
            """,
            (studio_id, f"-{int(days)} days"),
        )

    def daily_consumption(self, studio_id: int, days: int = 60) -> List[Dict[str, Any]]:
        """Per-item, per-day totals. Feeds the weighted burn-rate calculation."""
        return self.fetch_all(
            """
            SELECT item_id,
                   date(created_at) AS day,
                   SUM(ABS(change_qty)) AS qty
            FROM inventory_logs
            WHERE studio_id = ?
              AND change_qty < 0
              AND created_at >= datetime('now', ?)
            GROUP BY item_id, date(created_at)
            ORDER BY item_id, day
            """,
            (studio_id, f"-{int(days)} days"),
        )

    def per_job_usage(self, studio_id: int, days: int = 90) -> Dict[int, Dict[int, float]]:
        """
        {item_id: {job_id: qty_used}} — reconstructed from the log `reason`.
        Waste detection compares each job against the studio's own median, so
        a studio that legitimately uses more product per car is never flagged.
        """
        rows = self.fetch_all(
            """
            SELECT item_id, reason, SUM(ABS(change_qty)) AS qty
            FROM inventory_logs
            WHERE studio_id = ?
              AND change_qty < 0
              AND created_at >= datetime('now', ?)
              AND reason LIKE '%#%'
            GROUP BY item_id, reason
            """,
            (studio_id, f"-{int(days)} days"),
        )
        out: Dict[int, Dict[int, float]] = {}
        for row in rows:
            match = JOB_REF_PATTERN.search(row.get("reason") or "")
            if not match:
                continue
            job_id = int(match.group(1))
            out.setdefault(int(row["item_id"]), {})[job_id] = float(row["qty"] or 0)
        return out

    def first_movement_dates(self, studio_id: int) -> Dict[int, str]:
        """How far back each item's history goes — drives forecast confidence."""
        rows = self.fetch_all(
            "SELECT item_id, MIN(created_at) AS first_seen FROM inventory_logs "
            "WHERE studio_id=? GROUP BY item_id",
            (studio_id,),
        )
        return {int(r["item_id"]): r["first_seen"] for r in rows if r.get("first_seen")}

    def recent_restocks(self, studio_id: int, days: int = 90) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT l.item_id, l.change_qty, l.created_at, i.name, i.sku
            FROM inventory_logs l JOIN inventory_items i ON i.id = l.item_id
            WHERE l.studio_id=? AND l.change_qty > 0 AND l.created_at >= datetime('now', ?)
            ORDER BY l.created_at DESC
            """,
            (studio_id, f"-{int(days)} days"),
        )

    # ── writes ────────────────────────────────────────────────────────────

    def adjust_stock(self, studio_id: int, item_id: int, change_qty: float, reason: str) -> Optional[float]:
        """Atomic stock move + audit row. Returns the new quantity, or None if not found."""
        with self._conn() as conn:
            item = conn.execute(
                "SELECT quantity FROM inventory_items WHERE id=? AND studio_id=?", (item_id, studio_id)
            ).fetchone()
            if not item:
                return None
            new_qty = max(0.0, float(item["quantity"]) + float(change_qty))
            conn.execute(
                "UPDATE inventory_items SET quantity=?, updated_at=datetime('now') WHERE id=?",
                (new_qty, item_id),
            )
            conn.execute(
                "INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason) VALUES (?,?,?,?)",
                (studio_id, item_id, float(change_qty), reason),
            )
            conn.commit()
            return new_qty

    # ── operational context for the AI narrative ──────────────────────────

    def job_volume(self, studio_id: int, days: int = 30) -> Dict[str, Any]:
        completed = self.fetch_scalar(
            "SELECT COUNT(*) FROM jobs WHERE studio_id=? AND status='Completed' "
            "AND completed_at >= date('now', ?)",
            (studio_id, f"-{int(days)} days"),
            default=0,
        )
        in_progress = self.fetch_scalar(
            "SELECT COUNT(*) FROM jobs WHERE studio_id=? AND status='In Progress'", (studio_id,), default=0
        )
        pending = self.fetch_scalar(
            "SELECT COUNT(*) FROM jobs WHERE studio_id=? AND status='Pending'", (studio_id,), default=0
        )
        upcoming = self.fetch_scalar(
            "SELECT COUNT(*) FROM bookings WHERE studio_id=? AND date >= date('now') "
            "AND date <= date('now', '+14 days')",
            (studio_id,),
            default=0,
        )
        by_service = self.fetch_all(
            "SELECT service, COUNT(*) AS n FROM jobs WHERE studio_id=? AND status='Completed' "
            "AND completed_at >= date('now', ?) GROUP BY service ORDER BY n DESC LIMIT 8",
            (studio_id, f"-{int(days)} days"),
        )
        return {
            "window_days": days,
            "completed_jobs": int(completed),
            "jobs_in_progress": int(in_progress),
            "jobs_pending": int(pending),
            "bookings_next_14d": int(upcoming),
            "completed_by_service": by_service,
        }
