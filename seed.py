import sqlite3

DB_PATH = "studios.db"
conn = sqlite3.connect(DB_PATH)

# ── Studios ──────────────────────────────────────────────────────────────────
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

# ── Staff ────────────────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS staff (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id  INTEGER NOT NULL,
        name       TEXT NOT NULL,
        role       TEXT NOT NULL,
        username   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

# ── Bays ─────────────────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS bays (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id INTEGER NOT NULL,
        name      TEXT NOT NULL,
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

# ── Services ─────────────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        name        TEXT NOT NULL,
        duration_hr REAL NOT NULL,
        price       INTEGER NOT NULL,
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

# ── Bookings ─────────────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id    INTEGER NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        vehicle      TEXT NOT NULL,
        service_id   INTEGER NOT NULL,
        bay_id       INTEGER,
        staff_id     INTEGER,
        date         TEXT NOT NULL,
        time_slot    TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'Pending',
        notes        TEXT DEFAULT '',
        created_at   TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (studio_id)  REFERENCES studios(id),
        FOREIGN KEY (service_id) REFERENCES services(id),
        FOREIGN KEY (bay_id)     REFERENCES bays(id),
        FOREIGN KEY (staff_id)   REFERENCES staff(id)
    )
""")

# ── Jobs ─────────────────────────────────────────────────────────────────────
conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id  INTEGER NOT NULL,
        car        TEXT NOT NULL,
        service    TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'Pending',
        technician TEXT NOT NULL DEFAULT 'Unassigned',
        price      INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (studio_id) REFERENCES studios(id)
    )
""")

# ── Clear and reseed ──────────────────────────────────────────────────────────
for t in ["bookings","jobs","services","bays","staff","studios"]:
    conn.execute(f"DELETE FROM {t}")

studios = [
    (1, "Shine Pro Detailing", "Los Angeles, CA", "James Carter", "shinepro.svg", "shinepro",   "shine123"),
    (2, "Apex Detail Studio",  "Miami, FL",       "Tyler Brooks", "apex.svg",     "apexdetail", "apex123"),
    (3, "Velvet Auto Spa",     "New York, NY",    "Nathan Gold",  "velvet.svg",   "velvetauto", "velvet123"),
]
conn.executemany("INSERT INTO studios (id,name,city,owner,logo,username,password) VALUES (?,?,?,?,?,?,?)", studios)

staff = [
    (1, 1, "James Carter", "admin",      "shinepro",    "shine123"),
    (2, 1, "Maria Lopez",  "technician", "maria",       "tech123"),
    (3, 2, "Tyler Brooks", "admin",      "apexdetail",  "apex123"),
    (4, 2, "Carlos Diaz",  "technician", "carlos",      "tech123"),
    (5, 3, "Nathan Gold",  "admin",      "velvetauto",  "velvet123"),
    (6, 3, "Aisha Patel",  "technician", "aisha",       "tech123"),
]
conn.executemany("INSERT INTO staff (id,studio_id,name,role,username,password) VALUES (?,?,?,?,?,?)", staff)

bays = [
    (1,1,"Bay 1"),(2,1,"Bay 2"),(3,1,"Bay 3"),
    (4,2,"Bay 1"),(5,2,"Bay 2"),(6,2,"Bay 3"),
    (7,3,"Bay 1"),(8,3,"Bay 2"),
]
conn.executemany("INSERT INTO bays (id,studio_id,name) VALUES (?,?,?)", bays)

services = [
    (1,1,"Full Detail",        4.0,  250),
    (2,1,"Ceramic Coating",    6.0,  850),
    (3,1,"Paint Correction",   5.0,  620),
    (4,1,"Full PPF",           8.0, 1400),
    (5,1,"Interior Detail",    3.0,  320),
    (6,2,"Full Detail",        4.0,  280),
    (7,2,"Ceramic Coating",    6.0,  950),
    (8,2,"Full PPF",           8.0, 2200),
    (9,3,"Full Detail",        4.0,  260),
   (10,3,"Ceramic Coating",    6.0,  890),
   (11,3,"Interior Detail",    3.0,  380),
]
conn.executemany("INSERT INTO services (id,studio_id,name,duration_hr,price) VALUES (?,?,?,?,?)", services)

jobs = [
    (1,"2023 Mercedes GLE",   "Ceramic Coating",  "In Progress","Maria Lopez",  850),
    (1,"2022 BMW M4",          "Paint Correction", "Completed",  "Maria Lopez",  620),
    (1,"2024 Porsche Cayenne", "Full PPF",         "Pending",    "Unassigned",  1400),
    (2,"2022 Ferrari 488",     "Full PPF",         "In Progress","Carlos Diaz", 2200),
    (2,"2023 Lamborghini Urus","Ceramic Coating",  "Completed",  "Sofia Reyes",  950),
    (3,"2023 Cadillac Escalade","Ceramic Coating", "Completed",  "Aisha Patel",  890),
    (3,"2022 Lincoln Navigator","Full PPF",        "In Progress","Aisha Patel", 1350),
]
conn.executemany("INSERT INTO jobs (studio_id,car,service,status,technician,price) VALUES (?,?,?,?,?,?)", jobs)

conn.commit()
conn.close()
print("✓ Database seeded — studios, staff, bays, services, jobs, bookings tables ready")
