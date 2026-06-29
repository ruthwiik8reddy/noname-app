"""
migrate_jobs_tracking.py
--------------------------
Run ONCE to:
1. Add customer_id column to jobs table  (makes jobs customer-centric)
2. Add tracking_token column to jobs     (unique token for public customer link)
3. Backfill tracking tokens for existing jobs
4. Backfill customer_id for existing jobs by matching vehicles.make_model → jobs.car

Run from project root:
    python src/migrate_jobs_tracking.py
"""

import sqlite3
import secrets
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]

# ── 1. Add customer_id column to jobs ────────────────────────────────────────
if "customer_id" not in cols:
    conn.execute("ALTER TABLE jobs ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
    print("✓ jobs.customer_id column added")
else:
    print("✓ jobs.customer_id already exists")

# ── 2. Add tracking_token column to jobs ─────────────────────────────────────
if "tracking_token" not in cols:
    # SQLite does not support ADD COLUMN with UNIQUE — add the column first,
    # then create a unique index separately.
    conn.execute("ALTER TABLE jobs ADD COLUMN tracking_token TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uix_jobs_tracking_token ON jobs(tracking_token)")
    print("✓ jobs.tracking_token column added with unique index")
else:
    # Ensure the unique index exists even on older installs that had the column
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uix_jobs_tracking_token ON jobs(tracking_token)")
    print("✓ jobs.tracking_token already exists")

# ── 3. Backfill tracking tokens for any jobs that don't have one ──────────────
jobs_without_token = conn.execute(
    "SELECT id FROM jobs WHERE tracking_token IS NULL"
).fetchall()

for job in jobs_without_token:
    token = secrets.token_urlsafe(20)
    conn.execute("UPDATE jobs SET tracking_token=? WHERE id=?", (token, job["id"]))

conn.commit()
print(f"✓ Generated tracking tokens for {len(jobs_without_token)} existing job(s)")

# ── 4. Backfill customer_id on jobs from vehicles table (best-effort) ─────────
# Jobs store car as plain text. We match against vehicles.make_model.
jobs_without_customer = conn.execute(
    "SELECT id, studio_id, car FROM jobs WHERE customer_id IS NULL"
).fetchall()

linked = 0
for job in jobs_without_customer:
    row = conn.execute(
        """
        SELECT v.customer_id
        FROM vehicles v
        WHERE v.studio_id = ? AND v.make_model = ?
        LIMIT 1
        """,
        (job["studio_id"], job["car"])
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE jobs SET customer_id=? WHERE id=?",
            (row["customer_id"], job["id"])
        )
        linked += 1

conn.commit()
conn.close()

print(f"✓ Backfilled customer_id for {linked} job(s) via vehicle match")
print("Migration complete. Jobs are now customer-centric with secure tracking tokens.")
