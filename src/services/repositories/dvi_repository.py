"""
services/repositories/dvi_repository.py

Digital Vehicle Inspection storage. Three tables, one hierarchy:

    dvi_inspections  one per job visit
      └─ dvi_photos   one per technician capture (tagged with a panel/angle)
           └─ dvi_findings  zero-to-many defects the vision model reported

Findings keep `raw_json` — the untouched model output. When a customer disputes
an upcharge six months later, you can show exactly what the model saw and said.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import BaseRepository


class DVIRepository(BaseRepository):

    # ── inspections ───────────────────────────────────────────────────────

    def create_inspection(self, studio_id: int, job_id: int, created_by: str) -> int:
        return self.execute(
            "INSERT INTO dvi_inspections (studio_id, job_id, status, created_by) VALUES (?,?,?,?)",
            (studio_id, job_id, "draft", created_by),
        )

    def get_inspection(self, studio_id: int, inspection_id: int) -> Dict[str, Any]:
        return self.fetch_one(
            "SELECT * FROM dvi_inspections WHERE id=? AND studio_id=?", (inspection_id, studio_id)
        )

    def latest_for_job(self, studio_id: int, job_id: int) -> Dict[str, Any]:
        return self.fetch_one(
            "SELECT * FROM dvi_inspections WHERE studio_id=? AND job_id=? ORDER BY id DESC LIMIT 1",
            (studio_id, job_id),
        )

    def get_or_create_for_job(self, studio_id: int, job_id: int, created_by: str) -> Dict[str, Any]:
        existing = self.latest_for_job(studio_id, job_id)
        if existing and existing.get("status") in ("draft", "analyzing", "analyzed"):
            return existing
        new_id = self.create_inspection(studio_id, job_id, created_by)
        return self.get_inspection(studio_id, new_id)

    def list_inspections(self, studio_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT d.*, j.car, j.service, j.status AS job_status,
                   (SELECT COUNT(*) FROM dvi_photos p WHERE p.inspection_id = d.id) AS photo_count,
                   (SELECT COUNT(*) FROM dvi_findings f WHERE f.inspection_id = d.id) AS finding_count
            FROM dvi_inspections d
            LEFT JOIN jobs j ON j.id = d.job_id
            WHERE d.studio_id=?
            ORDER BY d.id DESC LIMIT ?
            """,
            (studio_id, limit),
        )

    def set_inspection_status(self, inspection_id: int, status: str) -> None:
        self.execute(
            "UPDATE dvi_inspections SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, inspection_id),
        )

    def save_summary(
        self,
        inspection_id: int,
        summary: str,
        condition_score: Optional[float],
        total_upcharge_cents: int,
        model_used: str,
        degraded: int = 0,
    ) -> None:
        self.execute(
            """
            UPDATE dvi_inspections
            SET summary=?, condition_score=?, total_upcharge_cents=?, model_used=?,
                degraded=?, status='analyzed', analyzed_at=datetime('now'), updated_at=datetime('now')
            WHERE id=?
            """,
            (summary, condition_score, int(total_upcharge_cents), model_used, int(degraded), inspection_id),
        )

    # ── photos ────────────────────────────────────────────────────────────

    def add_photo(
        self,
        studio_id: int,
        inspection_id: int,
        filename: str,
        filepath: str,
        panel: str,
        uploaded_by: str,
        original_name: str = "",
    ) -> int:
        return self.execute(
            """
            INSERT INTO dvi_photos
              (studio_id, inspection_id, filename, filepath, panel, uploaded_by, original_name)
            VALUES (?,?,?,?,?,?,?)
            """,
            (studio_id, inspection_id, filename, filepath, panel, uploaded_by, original_name),
        )

    def photos(self, inspection_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM dvi_photos WHERE inspection_id=? ORDER BY id ASC", (inspection_id,)
        )

    def unanalyzed_photos(self, inspection_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM dvi_photos WHERE inspection_id=? AND analyzed=0 ORDER BY id ASC",
            (inspection_id,),
        )

    def get_photo(self, studio_id: int, photo_id: int) -> Dict[str, Any]:
        return self.fetch_one(
            "SELECT * FROM dvi_photos WHERE id=? AND studio_id=?", (photo_id, studio_id)
        )

    def mark_photo_analyzed(self, photo_id: int, ok: bool, error: str = "") -> None:
        self.execute(
            "UPDATE dvi_photos SET analyzed=?, analysis_error=?, analyzed_at=datetime('now') WHERE id=?",
            (1 if ok else 0, error[:500], photo_id),
        )

    def delete_photo(self, studio_id: int, photo_id: int) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM dvi_findings WHERE photo_id=?", (photo_id,))
            cur = conn.execute("DELETE FROM dvi_photos WHERE id=? AND studio_id=?", (photo_id, studio_id))
            conn.commit()
            return bool(cur.rowcount)

    # ── findings ──────────────────────────────────────────────────────────

    def clear_findings(self, inspection_id: int) -> None:
        self.execute("DELETE FROM dvi_findings WHERE inspection_id=?", (inspection_id,))

    def add_finding(self, studio_id: int, inspection_id: int, photo_id: Optional[int], finding: Dict[str, Any]) -> int:
        return self.execute(
            """
            INSERT INTO dvi_findings
              (studio_id, inspection_id, photo_id, defect_type, panel, severity, confidence,
               description, suggested_service, suggested_upcharge_cents, pricing_basis, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                studio_id,
                inspection_id,
                photo_id,
                finding.get("defect_type", "unknown"),
                finding.get("panel", "unspecified"),
                int(finding.get("severity", 1)),
                float(finding.get("confidence", 0.0)),
                finding.get("description", ""),
                finding.get("suggested_service", ""),
                int(finding.get("suggested_upcharge_cents", 0)),
                finding.get("pricing_basis", ""),
                json.dumps(finding.get("raw", {}))[:4000],
            ),
        )

    def findings(self, inspection_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            """
            SELECT f.*, p.filename, p.panel AS photo_panel
            FROM dvi_findings f
            LEFT JOIN dvi_photos p ON p.id = f.photo_id
            WHERE f.inspection_id=?
            ORDER BY f.severity DESC, f.suggested_upcharge_cents DESC
            """,
            (inspection_id,),
        )

    def accepted_findings(self, inspection_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM dvi_findings WHERE inspection_id=? AND decision='accepted' "
            "ORDER BY suggested_upcharge_cents DESC",
            (inspection_id,),
        )

    def set_decision(self, studio_id: int, finding_id: int, decision: str, actor: str) -> bool:
        rows = self.execute(
            "UPDATE dvi_findings SET decision=?, decided_by=?, decided_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (decision, actor, finding_id, studio_id),
        )
        return bool(rows)
