"""
migrate_roles.py
-----------------
Run ONCE to normalize existing staff roles to the new 6-role system.

Old seed data used generic roles like "admin" and "technician" for staff.
This maps them to the new spec:
  admin (in staff table) → general_manager
  technician             → technician (unchanged)

Run from project root: python src/migrate_roles.py
"""
import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Map old role values to new role system
ROLE_MAP = {
    "admin": "general_manager",   # staff-table "admin" becomes GM (studio owner stays in studios table)
    "technician": "technician",   # unchanged
}

staff = conn.execute("SELECT id, role FROM staff").fetchall()
updated = 0
for s in staff:
    new_role = ROLE_MAP.get(s["role"], s["role"])
    if new_role != s["role"]:
        conn.execute("UPDATE staff SET role=? WHERE id=?", (new_role, s["id"]))
        updated += 1

conn.commit()
conn.close()

print(f"✓ Updated {updated} staff role(s)")
print("Roles now follow: general_manager, service_advisor, technician, photographer")
