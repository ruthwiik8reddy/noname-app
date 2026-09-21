"""
services/repositories/agent_repository.py — persistence for runs and findings.

The important behaviour here is the upsert in `save_findings`. An agent on a
15-minute schedule that INSERTs would produce ~96 copies of "Ceramic Coating is
low" per day. Instead each finding carries a fingerprint, and a re-observation
updates the existing row — refreshing the detail and bumping `updated_at` while
preserving `created_at`, so "how long has this been true?" stays answerable.

The one subtlety: a dismissed finding that recurs is deliberately NOT reopened
by a plain re-run. If a person said "I know, don't tell me again", an agent
noticing the same thing an hour later should respect that. It reopens only when
the underlying facts change (a different severity), which is a real escalation
rather than nagging.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import BaseRepository


class AgentRepository(BaseRepository):

    # ── runs ──────────────────────────────────────────────────────────────

    def start_run(self, studio_id: int, agent: str, trigger: str, detail: str = "") -> int:
        return self.execute(
            "INSERT INTO agent_runs (studio_id, agent, trigger, trigger_detail, status) "
            "VALUES (?,?,?,?,'running')",
            (studio_id, agent, trigger, detail[:200]),
        )

    def finish_run(
        self, run_id: int, status: str, findings_count: int, duration_ms: int,
        degraded: bool = False, error: str = "",
    ) -> None:
        self.execute(
            "UPDATE agent_runs SET status=?, findings_count=?, duration_ms=?, degraded=?, "
            "error=?, finished_at=datetime('now') WHERE id=?",
            (status, findings_count, duration_ms, 1 if degraded else 0, error[:400], run_id),
        )

    def recent_runs(self, studio_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM agent_runs WHERE studio_id=? ORDER BY id DESC LIMIT ?",
            (studio_id, limit),
        )

    def last_run_per_agent(self, studio_id: int) -> Dict[str, Dict[str, Any]]:
        rows = self.fetch_all(
            """
            SELECT r.* FROM agent_runs r
            JOIN (SELECT agent, MAX(id) AS mx FROM agent_runs WHERE studio_id=? GROUP BY agent) m
              ON m.mx = r.id
            """,
            (studio_id,),
        )
        return {r["agent"]: r for r in rows}

    # ── findings ──────────────────────────────────────────────────────────

    def save_findings(self, studio_id: int, agent: str, run_id: Optional[int], findings: List[Any]) -> int:
        """Upsert by fingerprint. Returns the number written."""
        from ..agents.base import SEVERITY_RANK
        if not findings:
            return 0
        with self._conn() as conn:
            for f in findings:
                row = f.as_row(studio_id, agent, run_id)
                existing = conn.execute(
                    "SELECT id, status, severity FROM agent_findings WHERE studio_id=? AND fingerprint=?",
                    (studio_id, f.fingerprint),
                ).fetchone()

                if existing:
                    # Re-open a dismissed finding only if it got worse — otherwise
                    # dismissing something would be meaningless.
                    escalated = SEVERITY_RANK.get(f.severity, 9) < SEVERITY_RANK.get(existing["severity"], 9)
                    new_status = (
                        "open" if (existing["status"] == "resolved" or (existing["status"] == "dismissed" and escalated))
                        else existing["status"]
                    )
                    conn.execute(
                        """
                        UPDATE agent_findings
                        SET run_id=?, severity=?, title=?, detail=?, action_label=?, action_url=?,
                            data_json=?, status=?, updated_at=datetime('now')
                        WHERE id=?
                        """,
                        (run_id, f.severity, f.title, f.detail, f.action_label, f.action_url,
                         json.dumps(f.data, default=str), new_status, existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO agent_findings
                          (studio_id, run_id, agent, kind, severity, title, detail, action_label,
                           action_url, entity_type, entity_id, fingerprint, data_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        row,
                    )
            conn.commit()
        return len(findings)

    def resolve_missing(self, studio_id: int, agent: str, current_fingerprints: List[str]) -> int:
        """
        Auto-close findings this agent no longer reports — you restocked the item,
        so the warning should disappear on its own rather than needing dismissal.
        """
        with self._conn() as conn:
            if current_fingerprints:
                placeholders = ",".join("?" for _ in current_fingerprints)
                cur = conn.execute(
                    f"UPDATE agent_findings SET status='resolved', updated_at=datetime('now') "
                    f"WHERE studio_id=? AND agent=? AND status='open' "
                    f"AND fingerprint NOT IN ({placeholders})",
                    (studio_id, agent, *current_fingerprints),
                )
            else:
                cur = conn.execute(
                    "UPDATE agent_findings SET status='resolved', updated_at=datetime('now') "
                    "WHERE studio_id=? AND agent=? AND status='open'",
                    (studio_id, agent),
                )
            conn.commit()
            return cur.rowcount

    def open_findings(
        self, studio_id: int, agent: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT * FROM agent_findings WHERE studio_id=? AND status='open'"
            + (" AND agent=?" if agent else "")
            + " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 "
              "WHEN 'opportunity' THEN 2 ELSE 3 END, updated_at DESC LIMIT ?"
        )
        params = (studio_id, agent, limit) if agent else (studio_id, limit)
        return self.fetch_all(sql, params)

    def feed(self, studio_id: int, limit: int = 40) -> List[Dict[str, Any]]:
        """Recent activity across all agents, open items first."""
        return self.fetch_all(
            """
            SELECT * FROM agent_findings
            WHERE studio_id=?
            ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                     CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1
                                   WHEN 'opportunity' THEN 2 ELSE 3 END,
                     updated_at DESC
            LIMIT ?
            """,
            (studio_id, limit),
        )

    def counts_by_severity(self, studio_id: int) -> Dict[str, int]:
        rows = self.fetch_all(
            "SELECT severity, COUNT(*) n FROM agent_findings "
            "WHERE studio_id=? AND status='open' GROUP BY severity",
            (studio_id,),
        )
        return {r["severity"]: int(r["n"]) for r in rows}

    def set_status(self, studio_id: int, finding_id: int, status: str, actor: str) -> bool:
        return bool(self.execute(
            "UPDATE agent_findings SET status=?, dismissed_by=?, updated_at=datetime('now') "
            "WHERE id=? AND studio_id=?",
            (status, actor, finding_id, studio_id),
        ))

    # ── settings ──────────────────────────────────────────────────────────

    def settings(self, studio_id: int) -> Dict[str, Dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM agent_settings WHERE studio_id=?", (studio_id,))
        return {r["agent"]: r for r in rows}

    def ensure_settings(self, studio_id: int, agent: str, interval_mins: int) -> Dict[str, Any]:
        existing = self.fetch_one(
            "SELECT * FROM agent_settings WHERE studio_id=? AND agent=?", (studio_id, agent)
        )
        if existing:
            return existing
        self.execute(
            "INSERT OR IGNORE INTO agent_settings (studio_id, agent, enabled, interval_mins) "
            "VALUES (?,?,1,?)",
            (studio_id, agent, interval_mins),
        )
        return self.fetch_one(
            "SELECT * FROM agent_settings WHERE studio_id=? AND agent=?", (studio_id, agent)
        )

    def set_enabled(self, studio_id: int, agent: str, enabled: bool) -> None:
        self.execute(
            "UPDATE agent_settings SET enabled=? WHERE studio_id=? AND agent=?",
            (1 if enabled else 0, studio_id, agent),
        )

    def touch_last_run(self, studio_id: int, agent: str) -> None:
        self.execute(
            "UPDATE agent_settings SET last_run_at=datetime('now') WHERE studio_id=? AND agent=?",
            (studio_id, agent),
        )

    def due_agents(self, studio_id: int) -> List[str]:
        """Agents whose interval has elapsed. Never-run agents are always due."""
        rows = self.fetch_all(
            "SELECT agent, interval_mins, last_run_at FROM agent_settings "
            "WHERE studio_id=? AND enabled=1",
            (studio_id,),
        )
        due = []
        for r in rows:
            if not r["last_run_at"]:
                due.append(r["agent"])
                continue
            elapsed = self.fetch_scalar(
                "SELECT (julianday('now') - julianday(?)) * 24 * 60", (r["last_run_at"],), default=0
            )
            if float(elapsed or 0) >= float(r["interval_mins"]):
                due.append(r["agent"])
        return due
