"""
migrate_phase2.py — schema for DVI (Phase 2) and Job Dispatch (Phase 3).

Idempotent and safe to re-run, matching the convention of every other migration
in this project. Run it once after pulling:

    python -m src.migrate_phase2

Optional demo data (inventory items plus ~8 weeks of realistic usage logs, so
the AI Estimator has something to forecast on a fresh database):

    python -m src.migrate_phase2 --with-demo-data
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta

from .config import Config

# ── new tables ────────────────────────────────────────────────────────────────

DDL = [
    # Digital Vehicle Inspections
    """
    CREATE TABLE IF NOT EXISTS dvi_inspections (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id            INTEGER NOT NULL,
        job_id               INTEGER NOT NULL,
        status               TEXT NOT NULL DEFAULT 'draft',
        summary              TEXT DEFAULT '',
        condition_score      REAL,
        total_upcharge_cents INTEGER NOT NULL DEFAULT 0,
        model_used           TEXT DEFAULT '',
        degraded             INTEGER NOT NULL DEFAULT 0,
        estimate_id          INTEGER,
        created_by           TEXT DEFAULT '',
        created_at           TEXT DEFAULT (datetime('now')),
        updated_at           TEXT DEFAULT (datetime('now')),
        analyzed_at          TEXT,
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (job_id)      REFERENCES jobs(id),
        FOREIGN KEY (estimate_id) REFERENCES estimates(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dvi_photos (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id      INTEGER NOT NULL,
        inspection_id  INTEGER NOT NULL,
        filename       TEXT NOT NULL,
        filepath       TEXT NOT NULL,
        original_name  TEXT DEFAULT '',
        panel          TEXT NOT NULL DEFAULT 'unspecified',
        uploaded_by    TEXT DEFAULT '',
        uploaded_at    TEXT DEFAULT (datetime('now')),
        analyzed       INTEGER NOT NULL DEFAULT 0,
        analyzed_at    TEXT,
        analysis_error TEXT DEFAULT '',
        FOREIGN KEY (studio_id)     REFERENCES studios(id),
        FOREIGN KEY (inspection_id) REFERENCES dvi_inspections(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dvi_findings (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id                INTEGER NOT NULL,
        inspection_id            INTEGER NOT NULL,
        photo_id                 INTEGER,
        defect_type              TEXT NOT NULL,
        panel                    TEXT NOT NULL DEFAULT 'unspecified',
        severity                 INTEGER NOT NULL DEFAULT 1,
        confidence               REAL NOT NULL DEFAULT 0,
        description              TEXT DEFAULT '',
        suggested_service        TEXT DEFAULT '',
        suggested_upcharge_cents INTEGER NOT NULL DEFAULT 0,
        pricing_basis            TEXT DEFAULT '',
        decision                 TEXT NOT NULL DEFAULT 'pending',
        decided_by               TEXT DEFAULT '',
        decided_at               TEXT,
        raw_json                 TEXT DEFAULT '',
        created_at               TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)     REFERENCES studios(id),
        FOREIGN KEY (inspection_id) REFERENCES dvi_inspections(id),
        FOREIGN KEY (photo_id)      REFERENCES dvi_photos(id)
    )
    """,
    # Dispatch audit trail
    """
    CREATE TABLE IF NOT EXISTS job_assignments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        job_id      INTEGER NOT NULL,
        staff_id    INTEGER,
        staff_name  TEXT DEFAULT '',
        assigned_by TEXT DEFAULT '',
        assigned_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id),
        FOREIGN KEY (job_id)    REFERENCES jobs(id),
        FOREIGN KEY (staff_id)  REFERENCES staff(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_status_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        job_id      INTEGER NOT NULL,
        from_status TEXT DEFAULT '',
        to_status   TEXT NOT NULL,
        actor       TEXT DEFAULT '',
        note        TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id),
        FOREIGN KEY (job_id)    REFERENCES jobs(id)
    )
    """,
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dvi_insp_job     ON dvi_inspections(studio_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_dvi_photos_insp  ON dvi_photos(inspection_id)",
    "CREATE INDEX IF NOT EXISTS idx_dvi_find_insp    ON dvi_findings(inspection_id)",
    "CREATE INDEX IF NOT EXISTS idx_job_assign_job   ON job_assignments(studio_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_job_hist_job     ON job_status_history(studio_id, job_id)",
    # The forecaster scans logs by studio and date on every dashboard load.
    "CREATE INDEX IF NOT EXISTS idx_inv_logs_studio  ON inventory_logs(studio_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_inv_logs_item    ON inventory_logs(item_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_studio_stat ON jobs(studio_id, status)",
]

# Columns added to the existing jobs table.
NEW_JOB_COLUMNS = [
    ("assigned_staff_id", "INTEGER"),
    ("assigned_at", "TEXT DEFAULT ''"),
    ("started_at", "TEXT DEFAULT ''"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def migrate(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or Config.DB_PATH)
    conn.row_factory = sqlite3.Row

    for statement in DDL:
        conn.execute(statement)
    for statement in INDEXES:
        conn.execute(statement)

    for column, spec in NEW_JOB_COLUMNS:
        if not _column_exists(conn, "jobs", column):
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {spec}")
            print(f"  + jobs.{column}")

    # Backfill assigned_staff_id from the legacy technician-name text column so
    # existing jobs appear correctly on the new dispatch board.
    linked = conn.execute(
        """
        UPDATE jobs
        SET assigned_staff_id = (
            SELECT s.id FROM staff s
            WHERE s.studio_id = jobs.studio_id AND s.name = jobs.technician
        )
        WHERE assigned_staff_id IS NULL
          AND technician IS NOT NULL
          AND technician != 'Unassigned'
          AND EXISTS (
            SELECT 1 FROM staff s
            WHERE s.studio_id = jobs.studio_id AND s.name = jobs.technician
          )
        """
    ).rowcount
    if linked:
        print(f"  ↳ linked {linked} existing job(s) to a staff record")

    # Seed history for jobs that predate the audit trail.
    seeded = conn.execute(
        """
        INSERT INTO job_status_history (studio_id, job_id, from_status, to_status, actor, note)
        SELECT studio_id, id, '', status, 'migration', 'Backfilled from existing job state'
        FROM jobs
        WHERE id NOT IN (SELECT job_id FROM job_status_history)
        """
    ).rowcount
    if seeded:
        print(f"  ↳ backfilled status history for {seeded} job(s)")

    conn.commit()
    conn.close()
    print("✓ Phase 2/3 schema is up to date")


# ── optional demo data ────────────────────────────────────────────────────────

DEMO_ITEMS = [
    # (sku, name, category, qty, unit, reorder, cost_cents, supplier, daily_base_usage)
    ("CER-9H-50",  "Ceramic Coating 9H",        "Coatings",    4.5,  "bottles", 3,  6500,  "Gtechniq",     0.34),
    ("PPF-ULT-60", "PPF Film Roll 60in",        "Film",        38.0, "ft",      25, 1450,  "XPEL",         2.10),
    ("POL-CUT-1L", "Cutting Compound 1L",       "Polish",      6.0,  "bottles", 4,  2800,  "Menzerna",     0.28),
    ("POL-FIN-1L", "Finishing Polish 1L",       "Polish",      9.0,  "bottles", 4,  2600,  "Menzerna",     0.22),
    ("PAD-FOAM-5", "Foam Cutting Pads 5in",     "Consumables", 22.0, "pads",    15, 850,   "Lake Country", 1.60),
    ("MF-TOWEL",   "Microfiber Towels",         "Consumables", 64.0, "units",   40, 320,   "The Rag Co",   3.20),
    ("IPA-WIPE",   "IPA Panel Wipe 500ml",      "Chemicals",   3.0,  "bottles", 3,  1200,  "CarPro",       0.30),
    ("TIRE-DRS",   "Tire Dressing 1L",          "Chemicals",   7.0,  "bottles", 3,  1900,  "Chemical Guys", 0.18),
    ("GLS-CLN",    "Glass Cleaner 1L",          "Chemicals",   11.0, "bottles", 4,  900,   "Stoner",       0.20),
    ("CLAY-BAR",   "Clay Bar Medium",           "Consumables", 5.0,  "units",   6,  1100,  "Bilt Hamber",  0.25),
]


def seed_demo_data(db_path: str | None = None, studio_id: int = 1, weeks: int = 8) -> None:
    """
    Generates inventory plus a realistic usage history: weekday-heavy, quieter
    weekends, occasional restocks, and a few deliberate over-consumption jobs so
    the waste detector has genuine outliers to find.
    """
    random.seed(42)  # reproducible demos
    conn = sqlite3.connect(db_path or Config.DB_PATH)
    conn.row_factory = sqlite3.Row

    studio = conn.execute("SELECT id, name FROM studios WHERE id=?", (studio_id,)).fetchone()
    if not studio:
        print(f"✗ Studio {studio_id} not found — run `python -m src.seed` first")
        conn.close()
        return

    job_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM jobs WHERE studio_id=? ORDER BY id", (studio_id,)
        ).fetchall()
    ] or [1, 2, 3]

    today = date.today()
    created = 0

    for sku, name, category, qty, unit, reorder, cost, supplier, base_usage in DEMO_ITEMS:
        scoped_sku = f"{sku}-S{studio_id}"
        existing = conn.execute(
            "SELECT id FROM inventory_items WHERE studio_id=? AND sku=?", (studio_id, scoped_sku)
        ).fetchone()
        if existing:
            item_id = existing["id"]
            conn.execute(
                "DELETE FROM inventory_logs WHERE studio_id=? AND item_id=?", (studio_id, item_id)
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO inventory_items
                  (studio_id, sku, name, category, quantity, unit, reorder_level, cost_per_unit, supplier)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (studio_id, scoped_sku, name, category, qty, unit, reorder, cost, supplier),
            )
            item_id = cur.lastrowid
            created += 1

        conn.execute("UPDATE inventory_items SET quantity=? WHERE id=?", (qty, item_id))

        for day_offset in range(weeks * 7, 0, -1):
            when = today - timedelta(days=day_offset)
            if when.weekday() >= 5 and random.random() < 0.65:
                continue  # quiet weekends
            if random.random() < 0.25:
                continue  # not every product moves every day

            amount = round(base_usage * random.uniform(0.6, 1.4), 2)
            job_id = random.choice(job_ids)

            # Sprinkle a few genuine outliers for the waste detector to surface.
            if random.random() < 0.05:
                amount = round(amount * random.uniform(2.6, 3.4), 2)

            if amount <= 0:
                continue
            conn.execute(
                "INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason, created_at) "
                "VALUES (?,?,?,?,?)",
                (studio_id, item_id, -amount, f"Job #{job_id} Usage", f"{when.isoformat()} 14:00:00"),
            )

            if random.random() < 0.04:
                restock = round(base_usage * random.uniform(20, 40), 1)
                conn.execute(
                    "INSERT INTO inventory_logs (studio_id, item_id, change_qty, reason, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (studio_id, item_id, restock, "Restock — supplier delivery",
                     f"{when.isoformat()} 09:00:00"),
                )

    conn.commit()
    total_logs = conn.execute(
        "SELECT COUNT(*) AS n FROM inventory_logs WHERE studio_id=?", (studio_id,)
    ).fetchone()["n"]
    conn.close()
    print(f"✓ Demo data ready for '{studio['name']}': {created} new item(s), {total_logs} movement logs")


if __name__ == "__main__":
    print("Running Phase 2/3 migration…")
    migrate()
    if "--with-demo-data" in sys.argv:
        studio = 1
        if "--studio" in sys.argv:
            studio = int(sys.argv[sys.argv.index("--studio") + 1])
        seed_demo_data(studio_id=studio)
