"""
migrate_reminders.py
----------------------
Run ONCE to add the reminders table.
Run from project root: python src/migrate_reminders.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id     INTEGER NOT NULL,
        customer_id   INTEGER,
        job_id        INTEGER,
        booking_id    INTEGER,
        reminder_type TEXT NOT NULL DEFAULT 'rebooking',  -- rebooking | review | custom
        message       TEXT NOT NULL,
        due_date      TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',     -- pending | sent | dismissed
        created_at    TEXT DEFAULT (datetime('now')),
        sent_at       TEXT,
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (job_id)      REFERENCES jobs(id),
        FOREIGN KEY (booking_id)  REFERENCES bookings(id)
    )
""")
conn.commit()
conn.close()
print("✓ reminders table ready")
