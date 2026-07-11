"""
migrate_products.py
---------------------
Creates the products catalog used by the AI estimator.

Each product stores:
  • cost_per_sqft   — material cost per square foot
  • labour_per_sqft — labour cost per square foot
  • markup_percent  — margin added on top of (material + labour)

Run once from project root:  python src/migrate_products.py
Safe to re-run.
"""
import sqlite3, os

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(BASE, "studios.db")
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

conn.execute("""
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    studio_id       INTEGER NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT DEFAULT 'PPF',      -- PPF | Ceramic | Correction | Other
    brand           TEXT DEFAULT '',
    cost_per_sqft   REAL DEFAULT 0,          -- material $/sqft
    labour_per_sqft REAL DEFAULT 0,          -- labour  $/sqft
    markup_percent  REAL DEFAULT 0,          -- margin %
    warranty_years  INTEGER DEFAULT 0,
    notes           TEXT DEFAULT '',
    active          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (studio_id) REFERENCES studios(id)
)
""")
print("  ✓ products table ready")

# Cache AI-estimated vehicle surface areas so we don't re-ask the model every time
conn.execute("""
CREATE TABLE IF NOT EXISTS vehicle_sqft_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle     TEXT NOT NULL UNIQUE,   -- e.g. "2023 BMW X7"
    total_sqft  REAL NOT NULL,
    breakdown   TEXT DEFAULT '',        -- JSON: per-panel sqft if available
    source      TEXT DEFAULT 'ai',      -- ai | manual
    created_at  TEXT DEFAULT (datetime('now'))
)
""")
print("  ✓ vehicle_sqft_cache table ready")

conn.commit(); conn.close()
print("✓ migrate_products complete")
