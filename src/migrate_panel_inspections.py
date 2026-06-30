"""
migrate_panel_inspections.py
------------------------------
Run ONCE to add the panel_inspections table — stores per-panel paint
health and treatment data for the 3D Digital Vehicle Inspection report.

Run from project root:
    python src/migrate_panel_inspections.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

# The canonical set of body panels we let staff inspect.
# panel_key must match the mesh userData.panelKey in the Three.js viewer.
CANONICAL_PANELS = [
    "hood", "roof", "front_bumper", "rear_bumper",
    "front_left_door", "front_right_door",
    "rear_left_door", "rear_right_door",
    "left_fender", "right_fender",
    "left_quarter", "right_quarter",
    "trunk",
]

conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS panel_inspections (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id     INTEGER NOT NULL,
        job_id        INTEGER NOT NULL,
        panel_key     TEXT NOT NULL,           -- e.g. 'hood', matches 3D mesh
        health_rating TEXT DEFAULT 'good',     -- excellent | good | fair | poor
        treatment     TEXT DEFAULT '',         -- e.g. 'PPF applied', 'Ceramic coat'
        notes         TEXT DEFAULT '',
        before_photo  TEXT DEFAULT '',          -- media filename (before)
        after_photo   TEXT DEFAULT '',          -- media filename (after)
        updated_at    TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id),
        FOREIGN KEY (job_id)    REFERENCES jobs(id),
        UNIQUE(job_id, panel_key)
    )
""")
conn.commit()
conn.close()
print("✓ panel_inspections table ready")
print(f"  Canonical panels ({len(CANONICAL_PANELS)}): {', '.join(CANONICAL_PANELS)}")
