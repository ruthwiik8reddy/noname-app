 """
migrate_estimate_booking_link.py
---------------------------------
Run ONCE to:
1. Add services_summary column to estimates  — human-readable list of items
2. Add estimate_id column to bookings        — links a booking to its approved estimate

Run from project root:
    python src/migrate_estimate_booking_link.py
"""
import sqlite3, os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
cols_est  = [r[1] for r in conn.execute("PRAGMA table_info(estimates)").fetchall()]
cols_book = [r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()]

if "services_summary" not in cols_est:
    conn.execute("ALTER TABLE estimates ADD COLUMN services_summary TEXT DEFAULT ''")
    print("✓ estimates.services_summary added")
else:
    print("✓ estimates.services_summary already exists")

if "estimate_id" not in cols_book:
    conn.execute("ALTER TABLE bookings ADD COLUMN estimate_id INTEGER REFERENCES estimates(id)")
    print("✓ bookings.estimate_id added")
else:
    print("✓ bookings.estimate_id already exists")

# Backfill services_summary for existing estimates
estimates = conn.execute("SELECT id FROM estimates WHERE services_summary='' OR services_summary IS NULL").fetchall()
updated = 0
for (eid,) in estimates:
    items = conn.execute(
        "SELECT name, quantity FROM estimate_items WHERE estimate_id=?", (eid,)
    ).fetchall()
    if items:
        summary = ", ".join(
            f"{r[0]}" + (f" ×{r[1]}" if r[1] > 1 else "") for r in items
        )
        conn.execute("UPDATE estimates SET services_summary=? WHERE id=?", (summary, eid))
        updated += 1

conn.commit()
conn.close()
print(f"✓ Backfilled services_summary for {updated} existing estimate(s)")
print("Migration complete.")
