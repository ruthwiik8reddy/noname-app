"""Vehicle identity and audited job cost records; no guessed historical links."""
import sqlite3


def migrate(path):
    with sqlite3.connect(path, timeout=15) as conn:
        if 'vehicle_id' not in {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}:
            conn.execute('ALTER TABLE jobs ADD COLUMN vehicle_id INTEGER REFERENCES vehicles(id)')
        conn.executescript('''
        CREATE INDEX IF NOT EXISTS ix_jobs_vehicle ON jobs(studio_id,vehicle_id);
        CREATE TABLE IF NOT EXISTS staff_cost_rates (
          studio_id INTEGER NOT NULL, staff_id INTEGER NOT NULL, hourly_cents INTEGER NOT NULL CHECK(hourly_cents>=0),
          updated_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY(studio_id,staff_id));
        CREATE TABLE IF NOT EXISTS job_labor (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, job_id INTEGER NOT NULL, staff_id INTEGER NOT NULL,
          started_at INTEGER NOT NULL, ended_at INTEGER, hourly_cents INTEGER,
          actor TEXT NOT NULL, request_key TEXT NOT NULL, UNIQUE(studio_id,request_key),
          CHECK(ended_at IS NULL OR ended_at>=started_at));
        CREATE UNIQUE INDEX IF NOT EXISTS ix_one_timer_per_staff ON job_labor(studio_id,staff_id) WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS ix_labor_job ON job_labor(studio_id,job_id);
        CREATE TABLE IF NOT EXISTS job_material_entries (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, job_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
          item_name TEXT NOT NULL, unit TEXT NOT NULL, quantity_milli INTEGER NOT NULL CHECK(quantity_milli!=0),
          unit_cost_cents INTEGER, cost_cents INTEGER, return_of INTEGER REFERENCES job_material_entries(id),
          actor TEXT NOT NULL, reason TEXT NOT NULL, request_key TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(studio_id,request_key));
        CREATE INDEX IF NOT EXISTS ix_material_job ON job_material_entries(studio_id,job_id);
        CREATE TABLE IF NOT EXISTS job_cost_plans (
          studio_id INTEGER NOT NULL, job_id INTEGER NOT NULL, planned_minutes INTEGER,
          planned_material_cents INTEGER, reviewed_at TEXT, reviewed_by TEXT,
          PRIMARY KEY(studio_id,job_id));
        CREATE TABLE IF NOT EXISTS job_record_audit (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, job_id INTEGER,
          action TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TRIGGER IF NOT EXISTS stop_job_timers AFTER UPDATE OF status ON jobs
          WHEN lower(NEW.status) IN ('completed','cancelled','canceled')
          BEGIN UPDATE job_labor SET ended_at=max(started_at,unixepoch('now'))
            WHERE job_id=NEW.id AND studio_id=NEW.studio_id AND ended_at IS NULL; END;
        CREATE TRIGGER IF NOT EXISTS invalidate_cost_review AFTER UPDATE OF price,status ON jobs
          BEGIN UPDATE job_cost_plans SET reviewed_at=NULL,reviewed_by=NULL
            WHERE job_id=NEW.id AND studio_id=NEW.studio_id; END;
        ''')
        for table in ('job_labor','job_material_entries'):
            for operation in ('INSERT','UPDATE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_invalidate_{operation.lower()}
                  AFTER {operation} ON {table} BEGIN
                  UPDATE job_cost_plans SET reviewed_at=NULL,reviewed_by=NULL
                    WHERE job_id=NEW.job_id AND studio_id=NEW.studio_id;
                  INSERT INTO intelligence_events(studio_id,event) VALUES(NEW.studio_id,'jobs_changed')
                    ON CONFLICT(studio_id,event) DO UPDATE SET revision=revision+1,attempts=0,available_at=0;
                  END''')

        for operation in ('INSERT','UPDATE'):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS cost_plan_event_{operation.lower()}
              AFTER {operation} ON job_cost_plans BEGIN
                INSERT INTO intelligence_events(studio_id,event) VALUES(NEW.studio_id,'jobs_changed')
                  ON CONFLICT(studio_id,event) DO UPDATE SET revision=revision+1,attempts=0,available_at=0;
              END""")
