"""
services/repositories/job_repository.py

Backs the dispatch board. `jobs.technician` stays a TEXT name for backward
compatibility with every existing template and query; `jobs.assigned_staff_id`
is the new foreign key. Both are written together so old code keeps working
while new code gets referential integrity.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseRepository

UNASSIGNED = "Unassigned"


class JobRepository(BaseRepository):

    # ── reads ─────────────────────────────────────────────────────────────

    def get(self, studio_id: int, job_id: int) -> Dict[str, Any]:
        return self.fetch_one("SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id))

    def list_by_status(self, studio_id: int, status: str) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT j.*, s.name AS staff_name, s.id AS staff_id
            FROM jobs j
            LEFT JOIN staff s ON s.id = j.assigned_staff_id
            WHERE j.studio_id=? AND j.status=?
            ORDER BY j.id DESC
            """,
            (studio_id, status),
        )

    def board(self, studio_id: int, statuses: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """One column per status, in the order the workflow defines."""
        return {status: self.list_by_status(studio_id, status) for status in statuses}

    def list_for_technician(self, studio_id: int, staff_id: int, name: str) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT * FROM jobs
            WHERE studio_id=? AND (assigned_staff_id=? OR technician=?)
            ORDER BY CASE status WHEN 'In Progress' THEN 0 WHEN 'Pending' THEN 1 ELSE 2 END, id DESC
            """,
            (studio_id, staff_id, name),
        )

    def active_load(self, studio_id: int) -> Dict[int, Dict[str, Any]]:
        """Open-job counts and committed hours per technician — drives suggestions."""
        rows = self.fetch_all(
            """
            SELECT j.assigned_staff_id AS staff_id,
                   COUNT(*) AS open_jobs,
                   SUM(CASE WHEN j.status='In Progress' THEN 1 ELSE 0 END) AS in_progress,
                   COALESCE(SUM(sv.duration_hr), 0) AS committed_hours
            FROM jobs j
            LEFT JOIN services sv ON sv.name = j.service AND sv.studio_id = j.studio_id
            WHERE j.studio_id=? AND j.status IN ('Pending','In Progress')
              AND j.assigned_staff_id IS NOT NULL
            GROUP BY j.assigned_staff_id
            """,
            (studio_id,),
        )
        return {int(r["staff_id"]): r for r in rows if r.get("staff_id") is not None}

    def completion_stats(self, studio_id: int, days: int = 90) -> Dict[int, Dict[str, Any]]:
        """Per-technician throughput by service — used to match skill to job."""
        rows = self.fetch_all(
            """
            SELECT assigned_staff_id AS staff_id, service, COUNT(*) AS completed
            FROM jobs
            WHERE studio_id=? AND status='Completed' AND assigned_staff_id IS NOT NULL
              AND (completed_at = '' OR completed_at >= date('now', ?))
            GROUP BY assigned_staff_id, service
            """,
            (studio_id, f"-{int(days)} days"),
        )
        out: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            sid = int(row["staff_id"])
            entry = out.setdefault(sid, {"total": 0, "by_service": {}})
            entry["total"] += int(row["completed"])
            entry["by_service"][row["service"]] = int(row["completed"])
        return out

    def counts_by_status(self, studio_id: int) -> Dict[str, int]:
        rows = self.fetch_all(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE studio_id=? GROUP BY status", (studio_id,)
        )
        return {r["status"]: int(r["n"]) for r in rows}

    # ── writes ────────────────────────────────────────────────────────────

    def create(
        self,
        studio_id: int,
        car: str,
        service: str,
        price: int = 0,
        customer_id: Optional[int] = None,
        status: str = "Pending",
    ) -> int:
        return self.execute(
            "INSERT INTO jobs (studio_id, car, service, status, technician, price, customer_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (studio_id, car, service, status, UNASSIGNED, int(price), customer_id),
        )

    def assign(self, studio_id: int, job_id: int, staff_id: Optional[int], staff_name: str) -> bool:
        rows = self.execute(
            "UPDATE jobs SET assigned_staff_id=?, technician=?, assigned_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (staff_id, staff_name or UNASSIGNED, job_id, studio_id),
        )
        return bool(rows)

    def set_status(self, studio_id: int, job_id: int, status: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=? WHERE id=? AND studio_id=?", (status, job_id, studio_id)
            )
            if status == "In Progress":
                conn.execute(
                    "UPDATE jobs SET started_at=COALESCE(NULLIF(started_at,''), datetime('now')) "
                    "WHERE id=? AND studio_id=?",
                    (job_id, studio_id),
                )
            elif status == "Completed":
                conn.execute(
                    "UPDATE jobs SET completed_at=date('now') WHERE id=? AND studio_id=?",
                    (job_id, studio_id),
                )
            conn.commit()
            return bool(cur.rowcount)

    def record_history(
        self,
        studio_id: int,
        job_id: int,
        from_status: Optional[str],
        to_status: str,
        actor: str,
        note: str = "",
    ) -> int:
        return self.execute(
            "INSERT INTO job_status_history (studio_id, job_id, from_status, to_status, actor, note) "
            "VALUES (?,?,?,?,?,?)",
            (studio_id, job_id, from_status or "", to_status, actor, note),
        )

    def record_assignment(
        self, studio_id: int, job_id: int, staff_id: Optional[int], staff_name: str, actor: str
    ) -> int:
        return self.execute(
            "INSERT INTO job_assignments (studio_id, job_id, staff_id, staff_name, assigned_by) "
            "VALUES (?,?,?,?,?)",
            (studio_id, job_id, staff_id, staff_name, actor),
        )

    def history(self, studio_id: int, job_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM job_status_history WHERE studio_id=? AND job_id=? ORDER BY id DESC",
            (studio_id, job_id),
        )

    def assignment_history(self, studio_id: int, job_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM job_assignments WHERE studio_id=? AND job_id=? ORDER BY id DESC",
            (studio_id, job_id),
        )
