"""
migrate_notes.py
------------------
Run ONCE to add the notes table.
Run from project root: python src/migrate_notes.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        job_id      INTEGER,
        estimate_id INTEGER,
        booking_id  INTEGER,
        note_type   TEXT NOT NULL DEFAULT 'internal',  -- internal | client
        content     TEXT NOT NULL,
        author_name TEXT DEFAULT '',
        author_role TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (job_id)      REFERENCES jobs(id),
        FOREIGN KEY (estimate_id) REFERENCES estimates(id),
        FOREIGN KEY (booking_id)  REFERENCES bookings(id)
    )
""")
conn.commit()
conn.close()
print("✓ notes table ready")
