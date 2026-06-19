"""
migrate_crm.py
----------------
Run ONCE to:
1. Extend customers table with CRM fields (if not present)
2. Add vehicles table (one customer -> many vehicles)
3. Link existing bookings/estimates to customer records retroactively

Run from project root: python src/migrate_crm.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# ── 1. Ensure customers table exists with CRM fields ──────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id  INTEGER NOT NULL,
        name       TEXT NOT NULL,
        email      TEXT DEFAULT '',
        phone      TEXT DEFAULT '',
        username   TEXT UNIQUE,
        password   TEXT DEFAULT '',
        notes      TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(customers)").fetchall()]

if "phone" not in existing_cols:
    conn.execute("ALTER TABLE customers ADD COLUMN phone TEXT DEFAULT ''")

if "notes" not in existing_cols:
    conn.execute("ALTER TABLE customers ADD COLUMN notes TEXT DEFAULT ''")

print("✓ customers table ready with CRM fields")

# ── 2. Vehicles table ──────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        make_model  TEXT NOT NULL,
        year        TEXT DEFAULT '',
        color       TEXT DEFAULT '',
        license_plate TEXT DEFAULT '',
        vin         TEXT DEFAULT '',
        notes       TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)   REFERENCES studios(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    )
""")
print("✓ vehicles table ready")

# ── 3. Add customer_id link to bookings/estimates for fast lookup ─────────────
def table_exists(name: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None

for table in ["bookings", "estimates"]:
    if not table_exists(table):
        print(f"⚠ {table} table doesn't exist yet — skipping")
        continue
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if "customer_id" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN customer_id INTEGER")
        print(f"✓ {table}.customer_id column added")
    else:
        print(f"✓ {table}.customer_id already exists")

# ── 4. Backfill: create customer records from existing bookings ───────────────
if not table_exists("bookings"):
    print("⚠ bookings table doesn't exist — skipping backfill")
    conn.commit()
    conn.close()
    print("Migration complete (partial — run again once bookings table exists).")
    exit()

sample_bookings = conn.execute("""
    SELECT DISTINCT studio_id, customer_name, customer_phone, vehicle
    FROM bookings WHERE customer_id IS NULL
""").fetchall()

created = 0
for b in sample_bookings:
    existing = conn.execute(
        "SELECT id FROM customers WHERE studio_id=? AND phone=?",
        (b["studio_id"], b["customer_phone"])
    ).fetchone()

    if existing:
        customer_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO customers (studio_id, name, phone, email, username, password) "
            "VALUES (?,?,?,?,?,?)",
            (b["studio_id"], b["customer_name"], b["customer_phone"], "",
             f"cust_{b['studio_id']}_{b['customer_phone']}", "")
        )
        customer_id = cur.lastrowid
        created += 1

    # Link the vehicle if not already present for this customer
    veh_exists = conn.execute(
        "SELECT id FROM vehicles WHERE customer_id=? AND make_model=?",
        (customer_id, b["vehicle"])
    ).fetchone()
    if not veh_exists:
        conn.execute(
            "INSERT INTO vehicles (studio_id, customer_id, make_model) VALUES (?,?,?)",
            (b["studio_id"], customer_id, b["vehicle"])
        )

    conn.execute(
        "UPDATE bookings SET customer_id=? WHERE studio_id=? AND customer_phone=? AND vehicle=?",
        (customer_id, b["studio_id"], b["customer_phone"], b["vehicle"])
    )

conn.commit()
conn.close()
print(f"✓ Backfilled {created} customer record(s) from existing bookings")
print("Migration complete.")
