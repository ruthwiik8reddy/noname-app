"""
migrate_inspection_full.py
----------------------------
Sets up everything the full cinematic inspection tracker needs:
  • panel_inspections extra columns: thickness, solution, images, after_images
  • warranties table (per job)
  • jobs.payment_received + jobs.payment_status (for the warranty gate)

Run once from project root:  python src/migrate_inspection_full.py
Safe to re-run.
"""
import sqlite3, os

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(BASE, "studios.db")
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

# panel_inspections (create if missing) + extra columns
conn.execute("""
CREATE TABLE IF NOT EXISTS panel_inspections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  studio_id INTEGER NOT NULL, job_id INTEGER NOT NULL, panel_key TEXT NOT NULL,
  health_rating TEXT DEFAULT 'good', treatment TEXT DEFAULT '', notes TEXT DEFAULT '',
  before_photo TEXT DEFAULT '', after_photo TEXT DEFAULT '',
  updated_at TEXT DEFAULT (datetime('now')), UNIQUE(job_id, panel_key))
""")
pcols = [r["name"] for r in conn.execute("PRAGMA table_info(panel_inspections)")]
for col, ddl in [("thickness","thickness TEXT DEFAULT ''"),
                 ("solution","solution TEXT DEFAULT ''"),
                 ("images","images TEXT DEFAULT ''"),
                 ("after_images","after_images TEXT DEFAULT ''")]:
    if col not in pcols:
        conn.execute(f"ALTER TABLE panel_inspections ADD COLUMN {ddl}"); print(f"  + panel_inspections.{col}")

# warranties
conn.execute("""
CREATE TABLE IF NOT EXISTS warranties (
  id INTEGER PRIMARY KEY AUTOINCREMENT, studio_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL UNIQUE, product_name TEXT DEFAULT '', years INTEGER DEFAULT 5,
  created_at TEXT DEFAULT (datetime('now')))
""")
print("  ✓ warranties table")

# jobs payment gate columns
jcols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
if "payment_received" not in jcols:
    conn.execute("ALTER TABLE jobs ADD COLUMN payment_received INTEGER DEFAULT 0"); print("  + jobs.payment_received")
if "payment_status" not in jcols:
    conn.execute("ALTER TABLE jobs ADD COLUMN payment_status TEXT DEFAULT 'unpaid'"); print("  + jobs.payment_status")

conn.commit(); conn.close()
print("✓ migrate_inspection_full complete")
