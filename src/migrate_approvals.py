import sqlite3


def migrate(path):
    with sqlite3.connect(path,timeout=15) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS estimate_approval_versions (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, estimate_id INTEGER NOT NULL,
          token_hash TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,
          created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, created_by TEXT NOT NULL,
          revoked_at INTEGER, decided_at INTEGER, decision TEXT, signer TEXT);
        CREATE INDEX IF NOT EXISTS ix_approval_estimate ON estimate_approval_versions(studio_id,estimate_id);
        ''')
