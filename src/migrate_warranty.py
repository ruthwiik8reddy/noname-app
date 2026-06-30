"""
migrate_warranty.py
--------------------
Run ONCE to add warranty and completion tracking fields to the jobs table.

New columns:
  warranty_duration  TEXT  — e.g. "5 Years", "2 Years", "30 Days", ""
  completed_at       TEXT  — ISO date when job was marked Completed

Run from project root:
    python src/migrate_warranty.py
"""

import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]

if "warranty_duration" not in cols:
    conn.execute("ALTER TABLE jobs ADD COLUMN warranty_duration TEXT DEFAULT ''")
    print("✓ jobs.warranty_duration added")
else:
    print("✓ jobs.warranty_duration already exists")

if "completed_at" not in cols:
    conn.execute("ALTER TABLE jobs ADD COLUMN completed_at TEXT DEFAULT ''")
    print("✓ jobs.completed_at added")
else:
    print("✓ jobs.completed_at already exists")

# Backfill warranty_duration from service name using common detailing keywords
# Studios can always override this per job — this is just a sensible default.
SERVICE_WARRANTY_MAP = [
    ("ceramic coating", "5 Years"),
    ("graphene",        "5 Years"),
    ("ppf",             "10 Years"),
    ("paint protection","10 Years"),
    ("paint correction","30 Days"),
    ("full detail",     "30 Days"),
    ("interior detail", "30 Days"),
]

jobs = conn.execute(
    "SELECT id, service FROM jobs WHERE warranty_duration = '' OR warranty_duration IS NULL"
).fetchall()

updated = 0
for job in jobs:
    svc = (job[1] or "").lower()
    for keyword, duration in SERVICE_WARRANTY_MAP:
        if keyword in svc:
            conn.execute(
                "UPDATE jobs SET warranty_duration=? WHERE id=?",
                (duration, job[0])
            )
            updated += 1
            break

conn.commit()
conn.close()
print(f"✓ Auto-set warranty_duration for {updated} existing job(s)")
print("Migration complete.")
