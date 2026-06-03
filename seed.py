import sqlite3
import os

DB_PATH = "studios.db"

# Create DB and table
conn = sqlite3.connect(DB_PATH)
conn.execute("""
    CREATE TABLE IF NOT EXISTS studios (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL,
        city     TEXT NOT NULL,
        owner    TEXT NOT NULL,
        logo     TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
""")

# Clear existing data (fresh seed)
conn.execute("DELETE FROM studios")

# 3 test studios
studios = [
    {
        "name":     "Shine Pro Detailing",
        "city":     "Los Angeles, CA",
        "owner":    "James Carter",
        "logo":     "shinepro.svg",
        "username": "shinepro",
        "password": "shine123",
    },
    {
        "name":     "Apex Detail Studio",
        "city":     "Miami, FL",
        "owner":    "Tyler Brooks",
        "logo":     "apex.svg",
        "username": "apexdetail",
        "password": "apex123",
    },
    {
        "name":     "Velvet Auto Spa",
        "city":     "New York, NY",
        "owner":    "Nathan Gold",
        "logo":     "velvet.svg",
        "username": "velvetauto",
        "password": "velvet123",
    },
]

for s in studios:
    conn.execute(
        "INSERT INTO studios (name, city, owner, logo, username, password) VALUES (?, ?, ?, ?, ?, ?)",
        (s["name"], s["city"], s["owner"], s["logo"], s["username"], s["password"])
    )

conn.commit()
conn.close()

print("Database created: studios.db")
print("3 studios seeded:")
print("  shinepro  / shine123  → Shine Pro Detailing, LA")
print("  apexdetail / apex123  → Apex Detail Studio, Miami")
print("  velvetauto / velvet123 → Velvet Auto Spa, New York")
