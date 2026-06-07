import sqlite3
from .db_manager import init_db
from .config import Config


def seed_db():
    conn = sqlite3.connect(Config.DB_PATH)
    for t in ["bookings", "jobs", "services", "bays", "staff", "studios"]:
        conn.execute(f"DELETE FROM {t}")

    studios = [
        (1, "Shine Pro Detailing", "Los Angeles, CA", "James Carter", "shinepro.svg", "shinepro", "shine123"),
        (2, "Apex Detail Studio", "Miami, FL", "Tyler Brooks", "apex.svg", "apexdetail", "apex123"),
        (3, "Velvet Auto Spa", "New York, NY", "Nathan Gold", "velvetauto.svg", "velvetauto", "velvet123"),
    ]
    conn.executemany("INSERT INTO studios (id,name,city,owner,logo,username,password) VALUES (?,?,?,?,?,?,?)", studios)

    staff = [
        (1, 1, "James Carter", "admin", "shinepro", "shine123"),
        (2, 1, "Maria Lopez", "technician", "maria", "tech123"),
        (3, 2, "Tyler Brooks", "admin", "apexdetail", "apex123"),
        (4, 2, "Carlos Diaz", "technician", "carlos", "tech123"),
        (5, 3, "Nathan Gold", "admin", "velvetauto", "velvet123"),
        (6, 3, "Aisha Patel", "technician", "aisha", "tech123"),
    ]
    conn.executemany("INSERT INTO staff (id,studio_id,name,role,username,password) VALUES (?,?,?,?,?,?)", staff)

    bays = [
        (1,1,"Bay 1"),(2,1,"Bay 2"),(3,1,"Bay 3"),
        (4,2,"Bay 1"),(5,2,"Bay 2"),(6,2,"Bay 3"),
        (7,3,"Bay 1"),(8,3,"Bay 2"),
    ]
    conn.executemany("INSERT INTO bays (id,studio_id,name) VALUES (?,?,?)", bays)

    services = [
        (1,1,"Full Detail", 4.0, 250),
        (2,1,"Ceramic Coating", 6.0, 850),
        (3,1,"Paint Correction", 5.0, 620),
        (4,1,"Full PPF", 8.0, 1400),
        (5,1,"Interior Detail", 3.0, 320),
        (6,2,"Full Detail", 4.0, 280),
        (7,2,"Ceramic Coating", 6.0, 950),
        (8,2,"Full PPF", 8.0, 2200),
        (9,3,"Full Detail", 4.0, 260),
        (10,3,"Ceramic Coating", 6.0, 890),
        (11,3,"Interior Detail", 3.0, 380),
    ]
    conn.executemany("INSERT INTO services (id,studio_id,name,duration_hr,price) VALUES (?,?,?,?,?)", services)

    jobs = [
        (1,"2023 Mercedes GLE", "Ceramic Coating", "In Progress","Maria Lopez", 850),
        (1,"2022 BMW M4", "Paint Correction", "Completed", "Maria Lopez", 620),
        (1,"2024 Porsche Cayenne", "Full PPF", "Pending", "Unassigned", 1400),
        (2,"2022 Ferrari 488", "Full PPF", "In Progress","Carlos Diaz", 2200),
        (2,"2023 Lamborghini Urus", "Ceramic Coating", "Completed", "Sofia Reyes", 950),
        (3,"2023 Cadillac Escalade", "Ceramic Coating", "Completed", "Aisha Patel", 890),
        (3,"2022 Lincoln Navigator", "Full PPF", "In Progress","Aisha Patel", 1350),
    ]
    conn.executemany("INSERT INTO jobs (studio_id,car,service,status,technician,price) VALUES (?,?,?,?,?,?)", jobs)
    conn.commit()
    conn.close()


def initialize_db():
    init_db()
    conn = sqlite3.connect(Config.DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM studios")
    studio_count = cursor.fetchone()[0]
    conn.close()
    if studio_count == 0:
        seed_db()


if __name__ == "__main__":
    initialize_db()
    print("✓ Database initialized and seeded")
