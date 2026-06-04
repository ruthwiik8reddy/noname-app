import sqlite3

DB_PATH = "studios.db"
conn = sqlite3.connect(DB_PATH)

conn.execute("""
    CREATE TABLE IF NOT EXISTS estimates (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id       INTEGER NOT NULL,
        customer_name   TEXT NOT NULL,
        customer_email  TEXT NOT NULL,
        customer_phone  TEXT NOT NULL,
        vehicle         TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'Draft',
        subtotal        INTEGER NOT NULL DEFAULT 0,
        tax_percent     REAL NOT NULL DEFAULT 8.5,
        tax_amount      INTEGER NOT NULL DEFAULT 0,
        total           INTEGER NOT NULL DEFAULT 0,
        notes           TEXT DEFAULT '',
        internal_notes  TEXT DEFAULT '',
        signature       TEXT DEFAULT '',
        approved_at     TEXT DEFAULT '',
        created_at      TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

conn.execute("""
    CREATE TABLE IF NOT EXISTS estimate_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        estimate_id INTEGER NOT NULL,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        quantity    INTEGER NOT NULL DEFAULT 1,
        unit_price  INTEGER NOT NULL DEFAULT 0,
        total       INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (estimate_id) REFERENCES estimates(id)
    )
""")

conn.commit()
conn.close()
print("✓ Estimates tables added to studios.db")
print("  Run this once — it won't affect existing data")
