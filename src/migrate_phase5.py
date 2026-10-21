"""
migrate_phase5.py — Security hardening + Invoices + Studio Assistant.

Idempotent; runs on every startup from db_manager.init_db(). Run by hand with:

    python -m src.migrate_phase5

What it adds:

**studio_settings** — per-studio key/value settings. Twilio credentials used to
be written into the shared .env, which meant any studio admin could overwrite
every other studio's SMS sender. Each studio now owns its own row set.

**estimates.approval_token** — the public approval page used to be addressed by
the sequential estimate id, so anyone could approve anyone's estimate by
counting. Links are now addressed by an unguessable token.

**invoice_templates / invoices / invoice_items** — invoices, rendered in a
style learned from the studio's own existing invoice.

**owner_requests** — every plain-English request made to the Studio Assistant,
its proposed plan, and what was actually applied. An audit trail for an agent
that changes data.

It also hashes any password still stored in plain text, so the plain-text
login fallback could be removed from auth.py.
"""

from __future__ import annotations

import secrets
import sqlite3

from .config import Config

DDL = [
    """
    CREATE TABLE IF NOT EXISTS studio_settings (
        studio_id  INTEGER NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (studio_id, key),
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invoice_templates (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id       INTEGER NOT NULL UNIQUE,
        source_file     TEXT DEFAULT '',
        source_name     TEXT DEFAULT '',
        logo_file       TEXT DEFAULT '',
        style_json      TEXT NOT NULL DEFAULT '{}',
        extracted_by    TEXT DEFAULT '',
        degraded        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invoices (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id       INTEGER NOT NULL,
        number          TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'Draft',
        customer_id     INTEGER,
        customer_name   TEXT NOT NULL DEFAULT '',
        customer_email  TEXT DEFAULT '',
        customer_phone  TEXT DEFAULT '',
        customer_address TEXT DEFAULT '',
        vehicle         TEXT DEFAULT '',
        job_id          INTEGER,
        estimate_id     INTEGER,
        issue_date      TEXT NOT NULL DEFAULT (date('now')),
        due_date        TEXT DEFAULT '',
        subtotal        INTEGER NOT NULL DEFAULT 0,
        discount        INTEGER NOT NULL DEFAULT 0,
        tax_percent     REAL NOT NULL DEFAULT 0,
        tax_amount      INTEGER NOT NULL DEFAULT 0,
        total           INTEGER NOT NULL DEFAULT 0,
        notes           TEXT DEFAULT '',
        public_token    TEXT UNIQUE,
        paid_at         TEXT DEFAULT '',
        created_by      TEXT DEFAULT '',
        created_at      TEXT DEFAULT (datetime('now')),
        UNIQUE (studio_id, number),
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (job_id)      REFERENCES jobs(id),
        FOREIGN KEY (estimate_id) REFERENCES estimates(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invoice_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id  INTEGER NOT NULL,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        quantity    REAL NOT NULL DEFAULT 1,
        unit_price  INTEGER NOT NULL DEFAULT 0,
        total       INTEGER NOT NULL DEFAULT 0,
        position    INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS owner_requests (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        request     TEXT NOT NULL,
        reply       TEXT DEFAULT '',
        plan_json   TEXT NOT NULL DEFAULT '[]',
        status      TEXT NOT NULL DEFAULT 'proposed',
        result_json TEXT DEFAULT '',
        source      TEXT DEFAULT 'ai',
        created_by  TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now')),
        applied_at  TEXT DEFAULT '',
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invoices_studio ON invoices(studio_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_items_inv ON invoice_items(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_owner_requests_studio ON owner_requests(studio_id, created_at)",
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _hash_plaintext_passwords(conn: sqlite3.Connection) -> int:
    from .auth import hash_password, is_hashed

    changed = 0
    for table in ("studios", "staff", "customers"):
        rows = conn.execute(f"SELECT id, password FROM {table}").fetchall()
        for row_id, pw in rows:
            if pw and not is_hashed(pw):
                conn.execute(f"UPDATE {table} SET password=? WHERE id=?", (hash_password(pw), row_id))
                changed += 1
    return changed


def migrate(db_path: str = Config.DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for stmt in DDL:
            conn.execute(stmt)

        if not _has_column(conn, "estimates", "approval_token"):
            conn.execute("ALTER TABLE estimates ADD COLUMN approval_token TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_estimates_approval_token "
            "ON estimates(approval_token)"
        )
        for (est_id,) in conn.execute(
            "SELECT id FROM estimates WHERE approval_token IS NULL OR approval_token=''"
        ).fetchall():
            conn.execute("UPDATE estimates SET approval_token=? WHERE id=?",
                         (secrets.token_urlsafe(24), est_id))

        # Exact mode: the studio's own invoice as a fill-in template.
        for column, ddl in (("mode", "TEXT NOT NULL DEFAULT 'rebuilt'"),
                            ("exact_json", "TEXT NOT NULL DEFAULT ''"),
                            ("blank_file", "TEXT NOT NULL DEFAULT ''"),
                            ("exact_note", "TEXT NOT NULL DEFAULT ''")):
            if not _has_column(conn, "invoice_templates", column):
                conn.execute(f"ALTER TABLE invoice_templates ADD COLUMN {column} {ddl}")

        # Business customers (B2B) need their tax registration on the invoice — e.g. GSTIN in India.
        if not _has_column(conn, "invoices", "customer_tax_id"):
            conn.execute("ALTER TABLE invoices ADD COLUMN customer_tax_id TEXT DEFAULT ''")

        # Where an in-progress car is (Washing / Correction / Coating / Curing),
        # shown on the customer's tracking timeline.
        if not _has_column(conn, "jobs", "tracking_stage"):
            conn.execute("ALTER TABLE jobs ADD COLUMN tracking_stage TEXT DEFAULT ''")

        hashed = _hash_plaintext_passwords(conn)
        conn.commit()
        if hashed:
            print(f"  ✓ Hashed {hashed} plain-text password(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    print("Running Phase 5 migration…")
    migrate()
    print("✓ Phase 5 schema ready")
