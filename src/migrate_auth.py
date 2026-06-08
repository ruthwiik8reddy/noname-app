"""
migrate_auth.py
---------------
Run ONCE to:
1. Add the customers table
2. Hash all plain-text passwords in studios and staff tables

Safe to run multiple times — already-hashed passwords are skipped.
"""
import sqlite3
import os
import sys

# Works both from root and from src/
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH  = os.path.join(BASE_DIR, "studios.db")

# Import hash_password from auth.py
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
from auth import hash_password

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# ── 1. Add customers table ────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id  INTEGER NOT NULL,
        name       TEXT NOT NULL,
        email      TEXT NOT NULL,
        phone      TEXT DEFAULT '',
        username   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")
print("✓ customers table ready")

# ── 2. Hash plain-text passwords in studios ───────────────────────────────────
studios = conn.execute("SELECT id, password FROM studios").fetchall()
rehashed = 0
for s in studios:
    pwd = s["password"]
    if not pwd.startswith("pbkdf2:") and not pwd.startswith("scrypt:"):
        conn.execute(
            "UPDATE studios SET password=? WHERE id=?",
            (hash_password(pwd), s["id"])
        )
        rehashed += 1
print(f"✓ Studios: {rehashed} passwords hashed ({len(studios) - rehashed} already hashed)")

# ── 3. Hash plain-text passwords in staff ────────────────────────────────────
staff = conn.execute("SELECT id, password FROM staff").fetchall()
rehashed = 0
for s in staff:
    pwd = s["password"]
    if not pwd.startswith("pbkdf2:") and not pwd.startswith("scrypt:"):
        conn.execute(
            "UPDATE staff SET password=? WHERE id=?",
            (hash_password(pwd), s["id"])
        )
        rehashed += 1
print(f"✓ Staff: {rehashed} passwords hashed ({len(staff) - rehashed} already hashed)")

conn.commit()
conn.close()

print()
print("Migration complete.")
print("Existing logins still work — plain passwords are auto-migrated on next login too.")
