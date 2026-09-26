import sqlite3


def migrate(path):
    with sqlite3.connect(path,timeout=15) as conn:
        if 'customer_visible' not in {r[1] for r in conn.execute('PRAGMA table_info(media)')}:
            conn.execute('ALTER TABLE media ADD COLUMN customer_visible INTEGER NOT NULL DEFAULT 0')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS portal_invites (
          id INTEGER PRIMARY KEY,studio_id INTEGER NOT NULL,customer_id INTEGER NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,expires_at INTEGER NOT NULL,used_at INTEGER,revoked_at INTEGER,
          actor TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS vehicle_warranties (
          id INTEGER PRIMARY KEY,studio_id INTEGER NOT NULL,customer_id INTEGER NOT NULL,
          vehicle_id INTEGER NOT NULL,job_id INTEGER NOT NULL,title TEXT NOT NULL,
          coverage TEXT NOT NULL,exclusions TEXT NOT NULL,care TEXT NOT NULL,
          starts_on TEXT NOT NULL,ends_on TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Active',
          eligibility_note TEXT NOT NULL,issued_by TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (datetime('now')),
          revoked_reason TEXT,request_key TEXT NOT NULL,request_hash TEXT NOT NULL,UNIQUE(studio_id,request_key));
        CREATE TABLE IF NOT EXISTS warranty_claims (
          id INTEGER PRIMARY KEY,studio_id INTEGER NOT NULL,warranty_id INTEGER NOT NULL,customer_id INTEGER NOT NULL,
          description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Open',resolution TEXT,
          version INTEGER NOT NULL DEFAULT 1,request_key TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(studio_id,customer_id,request_key));
        CREATE TABLE IF NOT EXISTS portal_audit (
          id INTEGER PRIMARY KEY,studio_id INTEGER NOT NULL,customer_id INTEGER NOT NULL,action TEXT NOT NULL,
          actor TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE INDEX IF NOT EXISTS ix_warranty_customer ON vehicle_warranties(studio_id,customer_id);
        ''')
