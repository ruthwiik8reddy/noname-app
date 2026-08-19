"""
migrate_phase4.py — Leads pipeline + Agent infrastructure.

Idempotent. Run once after pulling:

    python -m src.migrate_phase4
    python -m src.migrate_phase4 --with-demo-data     # sample leads to look at

Two additions:

**Leads** — you had `customers` (people who have paid) and `bookings` (slots),
but nothing for an enquiry that hasn't converted yet. A lead is a person who
asked about something. It becomes a customer when they book. Without this table
there is no pipeline to score, and no way to notice that someone went cold.

**Agent runs & findings** — agents produce *findings*: a claim, a severity, a
suggested action, and a link back to whatever it's about. Findings persist so
the dashboard has history, so you can dismiss one and not see it again, and so
"what did the system notice last Tuesday" is answerable. `agent_runs` records
every execution — including failures — because an agent that silently stopped
running is worse than one that errors loudly.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

from .config import Config

DDL = [
    # ── Leads ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS leads (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id       INTEGER NOT NULL,
        name            TEXT NOT NULL,
        phone           TEXT DEFAULT '',
        email           TEXT DEFAULT '',
        vehicle         TEXT DEFAULT '',
        service_interest TEXT DEFAULT '',
        budget_hint     INTEGER DEFAULT 0,
        source          TEXT NOT NULL DEFAULT 'manual',
        status          TEXT NOT NULL DEFAULT 'new',
        score           INTEGER DEFAULT 0,
        score_reason    TEXT DEFAULT '',
        notes           TEXT DEFAULT '',
        owner_staff_id  INTEGER,
        customer_id     INTEGER,
        booking_id      INTEGER,
        last_contacted  TEXT DEFAULT '',
        next_action     TEXT DEFAULT '',
        next_action_due TEXT DEFAULT '',
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (booking_id)  REFERENCES bookings(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lead_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id  INTEGER NOT NULL,
        lead_id    INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        detail     TEXT DEFAULT '',
        actor      TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (lead_id) REFERENCES leads(id)
    )
    """,
    # ── Agents ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id      INTEGER NOT NULL,
        agent          TEXT NOT NULL,
        trigger        TEXT NOT NULL DEFAULT 'scheduled',
        trigger_detail TEXT DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'running',
        findings_count INTEGER DEFAULT 0,
        degraded       INTEGER DEFAULT 0,
        error          TEXT DEFAULT '',
        duration_ms    INTEGER DEFAULT 0,
        started_at     TEXT DEFAULT (datetime('now')),
        finished_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_findings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id    INTEGER NOT NULL,
        run_id       INTEGER,
        agent        TEXT NOT NULL,
        kind         TEXT NOT NULL,
        severity     TEXT NOT NULL DEFAULT 'info',
        title        TEXT NOT NULL,
        detail       TEXT DEFAULT '',
        action_label TEXT DEFAULT '',
        action_url   TEXT DEFAULT '',
        entity_type  TEXT DEFAULT '',
        entity_id    INTEGER,
        fingerprint  TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        dismissed_by TEXT DEFAULT '',
        data_json    TEXT DEFAULT '',
        created_at   TEXT DEFAULT (datetime('now')),
        updated_at   TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (run_id) REFERENCES agent_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_settings (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id     INTEGER NOT NULL,
        agent         TEXT NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        interval_mins INTEGER NOT NULL DEFAULT 60,
        last_run_at   TEXT,
        UNIQUE(studio_id, agent)
    )
    """,
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_leads_studio    ON leads(studio_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_leads_score     ON leads(studio_id, score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lead_ev_lead    ON lead_events(lead_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_studio     ON agent_runs(studio_id, agent, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_find_open       ON agent_findings(studio_id, status, severity)",
    # A finding is unique by its fingerprint — re-running an agent updates the
    # existing row instead of stacking duplicates of the same observation.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_find_fp  ON agent_findings(studio_id, fingerprint)",
]


def migrate(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    for stmt in DDL + INDEXES:
        conn.execute(stmt)

    # Backfill: every existing booking was once an enquiry. Creating the lead
    # retroactively means conversion-rate maths has a denominator from day one.
    created = conn.execute(
        """
        INSERT INTO leads (studio_id, name, phone, email, vehicle, service_interest,
                           source, status, booking_id, created_at)
        SELECT b.studio_id,
               COALESCE(NULLIF(b.customer_name, ''), 'Unknown'),
               COALESCE(b.customer_phone, ''),
               '',
               COALESCE(b.vehicle, ''),
               COALESCE(s.name, ''),
               'booking_form',
               CASE WHEN b.status = 'Cancelled' THEN 'lost' ELSE 'won' END,
               b.id,
               COALESCE(NULLIF(b.created_at, ''), datetime('now'))
        FROM bookings b
        LEFT JOIN services s ON s.id = b.service_id
        WHERE b.id NOT IN (SELECT COALESCE(booking_id, -1) FROM leads)
        """
    ).rowcount
    if created:
        print(f"  ↳ backfilled {created} lead(s) from existing bookings")

    conn.commit()
    conn.close()
    print("✓ Phase 4 schema is up to date")


# ── demo data ─────────────────────────────────────────────────────────────────

DEMO_LEADS = [
    ("Priya Raghavan", "+91 98450 11223", "priya.r@example.com", "2023 Audi Q7",
     "Full PPF", 180000, "whatsapp", "new", 4,
     "Asked about full-body PPF, wants to book before Diwali."),
    ("Marcus Bell", "+1-555-0142", "mbell@example.com", "2021 Porsche 911",
     "Ceramic Coating", 95000, "phone", "contacted", 11,
     "Called twice. Comparing us against two other studios."),
    ("Sana Qureshi", "+91 99001 44556", "sana.q@example.com", "2024 BMW X5",
     "Paint Correction", 60000, "walk_in", "quoted", 6,
     "Came in with swirl marks on the bonnet. Quote sent."),
    ("Dev Anand", "+91 90000 77881", "", "2019 Honda City",
     "Interior Detail", 12000, "instagram", "new", 2,
     "DM enquiry, no phone follow-up yet."),
    ("Ellen Frost", "+1-555-0188", "ellen@example.com", "2022 Tesla Model Y",
     "Ceramic Coating", 88000, "referral", "contacted", 25,
     "Referred by an existing customer. Went quiet after the quote."),
    ("Rohit Menon", "+91 97400 22110", "rohit.m@example.com", "2023 Toyota Fortuner",
     "Full PPF", 210000, "website", "negotiating", 3,
     "Wants a discount on the full-body package."),
    ("Aisha Kapoor", "", "aisha.k@example.com", "2020 Mercedes GLC",
     "Paint Correction", 45000, "whatsapp", "new", 9,
     "Sent photos of scratches on the rear door."),
]


def seed_demo_data(db_path: str | None = None, studio_id: int = 1) -> None:
    random.seed(7)
    conn = sqlite3.connect(db_path or Config.DB_PATH)
    conn.row_factory = sqlite3.Row

    if not conn.execute("SELECT 1 FROM studios WHERE id=?", (studio_id,)).fetchone():
        print(f"✗ Studio {studio_id} not found")
        conn.close()
        return

    added = 0
    for name, phone, email, vehicle, service, budget, source, status, days_ago, note in DEMO_LEADS:
        if conn.execute("SELECT 1 FROM leads WHERE studio_id=? AND name=?", (studio_id, name)).fetchone():
            continue
        created = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        contacted = "" if status == "new" else (
            datetime.now() - timedelta(days=max(days_ago - 2, 0))
        ).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            """
            INSERT INTO leads (studio_id, name, phone, email, vehicle, service_interest,
                               budget_hint, source, status, notes, last_contacted, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (studio_id, name, phone, email, vehicle, service, budget, source, status,
             note, contacted, created),
        )
        conn.execute(
            "INSERT INTO lead_events (studio_id, lead_id, kind, detail, actor, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (studio_id, cur.lastrowid, "created", f"Lead captured via {source}", "seed", created),
        )
        added += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) n FROM leads WHERE studio_id=?", (studio_id,)).fetchone()["n"]
    conn.close()
    print(f"✓ Demo leads ready: {added} added, {total} total")


if __name__ == "__main__":
    print("Running Phase 4 migration…")
    migrate()
    if "--with-demo-data" in sys.argv:
        seed_demo_data()
