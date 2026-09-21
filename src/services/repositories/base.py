"""
services/repositories/base.py — data access boundary.

Repositories own SQL. Orchestrators own decisions. Controllers own HTTP.
No layer reaches past its neighbour.

The connection strategy matters: `get_db()` binds to Flask's `g`, which does
not exist in a background thread, a CLI script or a unit test. So repositories
accept an optional connection. Inside a request they reuse the request-scoped
one (single transaction, no double-open on the same SQLite file); outside, they
open and close their own.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from ...config import Config


def row_to_dict(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


class BaseRepository:
    """Shared connection plumbing for every repository."""

    def __init__(self, connection: Optional[sqlite3.Connection] = None, db_path: Optional[str] = None):
        self._connection = connection
        self._db_path = db_path or Config.DB_PATH

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return

        from flask import has_app_context
        if has_app_context():
            from ...db_manager import get_db
            yield get_db()
            return

        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ── query helpers ─────────────────────────────────────────────────────

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            return rows_to_dicts(conn.execute(sql, tuple(params)).fetchall())

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Dict[str, Any]:
        with self._conn() as conn:
            return row_to_dict(conn.execute(sql, tuple(params)).fetchone())

    def fetch_scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row and row[0] is not None else default

    def execute(self, sql: str, params: Iterable[Any] = (), commit: bool = True) -> int:
        """Returns lastrowid for inserts, rowcount for updates/deletes."""
        with self._conn() as conn:
            cur = conn.execute(sql, tuple(params))
            if commit:
                conn.commit()
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def execute_many(self, sql: str, seq: Iterable[Iterable[Any]], commit: bool = True) -> None:
        with self._conn() as conn:
            conn.executemany(sql, [tuple(p) for p in seq])
            if commit:
                conn.commit()

    def table_exists(self, name: str) -> bool:
        return bool(
            self.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        )

    def column_exists(self, table: str, column: str) -> bool:
        with self._conn() as conn:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(c["name"] == column for c in cols)
