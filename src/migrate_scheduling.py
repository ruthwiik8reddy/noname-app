import sqlite3


def migrate(path):
    with sqlite3.connect(path,timeout=15) as conn:
        if 'duration_minutes' not in {r[1] for r in conn.execute('PRAGMA table_info(bookings)')}:
            conn.execute('ALTER TABLE bookings ADD COLUMN duration_minutes INTEGER')
        conn.execute('CREATE INDEX IF NOT EXISTS ix_bay_reservation ON bookings(studio_id,bay_id,date)')
