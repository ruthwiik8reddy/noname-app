import sqlite3


def migrate(path):
    with sqlite3.connect(path,timeout=15) as conn:
        for table,columns in {'estimates':{'vehicle_id':'INTEGER'},'bookings':{'vehicle_id':'INTEGER','request_key':'TEXT','request_hash':'TEXT'},'jobs':{'booking_id':'INTEGER','estimate_id':'INTEGER'}}.items():
            existing={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name,kind in columns.items():
                if name not in existing:conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
        conn.executescript('''CREATE UNIQUE INDEX IF NOT EXISTS ix_booking_request ON bookings(studio_id,request_key) WHERE request_key IS NOT NULL;
          CREATE UNIQUE INDEX IF NOT EXISTS ix_job_booking ON jobs(studio_id,booking_id) WHERE booking_id IS NOT NULL;
          CREATE INDEX IF NOT EXISTS ix_booking_vehicle ON bookings(studio_id,vehicle_id);
          CREATE INDEX IF NOT EXISTS ix_estimate_vehicle ON estimates(studio_id,vehicle_id);''')
