"""
migrate_media.py
------------------
Run ONCE to add the media table.
Run from project root: python src/migrate_media.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS media (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id    INTEGER NOT NULL,
        job_id       INTEGER,
        booking_id   INTEGER,
        stage        TEXT NOT NULL DEFAULT 'before',  -- before | during | after
        filename     TEXT NOT NULL,
        original_name TEXT NOT NULL,
        media_type   TEXT NOT NULL DEFAULT 'image',    -- image | video
        caption      TEXT DEFAULT '',
        uploaded_by  TEXT DEFAULT '',
        uploaded_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)  REFERENCES studios(id),
        FOREIGN KEY (job_id)     REFERENCES jobs(id),
        FOREIGN KEY (booking_id) REFERENCES bookings(id)
    )
""")
conn.commit()
conn.close()
print("✓ media table ready")
