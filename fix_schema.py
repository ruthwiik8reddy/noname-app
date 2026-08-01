# fix_schema_standalone.py
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'studios.db')

def run():
    print(f"📂 Using database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ── Create all core tables if they don't exist ──

    # Studios
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS studios (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            city     TEXT NOT NULL,
            owner    TEXT NOT NULL,
            logo     TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')

    # Staff
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS staff (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id  INTEGER NOT NULL,
            name       TEXT NOT NULL,
            role       TEXT NOT NULL,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    # Bays
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bays (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL,
            name      TEXT NOT NULL,
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    # Services
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS services (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id   INTEGER NOT NULL,
            name        TEXT NOT NULL,
            duration_hr REAL NOT NULL,
            price       INTEGER NOT NULL,
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    # Bookings (with estimate_id and customer_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id     INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            vehicle       TEXT NOT NULL,
            service_id    INTEGER NOT NULL,
            bay_id        INTEGER,
            staff_id      INTEGER,
            date          TEXT NOT NULL,
            time_slot     TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'Pending',
            notes         TEXT DEFAULT '',
            created_at    TEXT DEFAULT (datetime('now')),
            customer_id   INTEGER,
            estimate_id   INTEGER,
            FOREIGN KEY (studio_id)  REFERENCES studios(id),
            FOREIGN KEY (service_id) REFERENCES services(id),
            FOREIGN KEY (bay_id)     REFERENCES bays(id),
            FOREIGN KEY (staff_id)   REFERENCES staff(id)
        )
    ''')

    # Jobs (with all needed columns)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id        INTEGER NOT NULL,
            car              TEXT NOT NULL,
            service          TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'Pending',
            technician       TEXT NOT NULL DEFAULT 'Unassigned',
            price            INTEGER NOT NULL DEFAULT 0,
            customer_id      INTEGER,
            tracking_token   TEXT UNIQUE,
            warranty_duration TEXT DEFAULT '',
            completed_at     TEXT DEFAULT '',
            payment_received INTEGER DEFAULT 0,
            payment_status   TEXT DEFAULT 'unpaid',
            FOREIGN KEY (studio_id) REFERENCES studios(id),
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    ''')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uix_jobs_tracking_token ON jobs(tracking_token)')

    # Estimates
    cursor.execute('''
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
            customer_id     INTEGER,
            services_summary TEXT DEFAULT '',
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    # Estimate items
    cursor.execute('''
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
    ''')

    # Customers
    cursor.execute('''
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
    ''')

    # Vehicles
    cursor.execute('''
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
    ''')

    # Media
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id    INTEGER NOT NULL,
            job_id       INTEGER,
            booking_id   INTEGER,
            stage        TEXT NOT NULL DEFAULT 'before',
            filename     TEXT NOT NULL,
            original_name TEXT NOT NULL,
            media_type   TEXT NOT NULL DEFAULT 'image',
            caption      TEXT DEFAULT '',
            uploaded_by  TEXT DEFAULT '',
            uploaded_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id)  REFERENCES studios(id),
            FOREIGN KEY (job_id)     REFERENCES jobs(id),
            FOREIGN KEY (booking_id) REFERENCES bookings(id)
        )
    ''')

    # Notes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id   INTEGER NOT NULL,
            job_id      INTEGER,
            estimate_id INTEGER,
            booking_id  INTEGER,
            note_type   TEXT NOT NULL DEFAULT 'internal',
            content     TEXT NOT NULL,
            author_name TEXT DEFAULT '',
            author_role TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id)   REFERENCES studios(id),
            FOREIGN KEY (job_id)      REFERENCES jobs(id),
            FOREIGN KEY (estimate_id) REFERENCES estimates(id),
            FOREIGN KEY (booking_id)  REFERENCES bookings(id)
        )
    ''')

    # Reminders
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id     INTEGER NOT NULL,
            customer_id   INTEGER,
            job_id        INTEGER,
            booking_id    INTEGER,
            reminder_type TEXT NOT NULL DEFAULT 'rebooking',
            message       TEXT NOT NULL,
            due_date      TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT DEFAULT (datetime('now')),
            sent_at       TEXT,
            FOREIGN KEY (studio_id)   REFERENCES studios(id),
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (job_id)      REFERENCES jobs(id),
            FOREIGN KEY (booking_id)  REFERENCES bookings(id)
        )
    ''')

    # Panel inspections
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS panel_inspections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id     INTEGER NOT NULL,
            job_id        INTEGER NOT NULL,
            panel_key     TEXT NOT NULL,
            health_rating TEXT DEFAULT 'good',
            treatment     TEXT DEFAULT '',
            notes         TEXT DEFAULT '',
            before_photo  TEXT DEFAULT '',
            after_photo   TEXT DEFAULT '',
            thickness     TEXT DEFAULT '',
            solution      TEXT DEFAULT '',
            images        TEXT DEFAULT '',
            after_images  TEXT DEFAULT '',
            updated_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id) REFERENCES studios(id),
            FOREIGN KEY (job_id)    REFERENCES jobs(id),
            UNIQUE(job_id, panel_key)
        )
    ''')

    # Products (for AI estimator)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id       INTEGER NOT NULL,
            name            TEXT NOT NULL,
            category        TEXT DEFAULT 'PPF',
            brand           TEXT DEFAULT '',
            cost_per_sqft   REAL DEFAULT 0,
            labour_per_sqft REAL DEFAULT 0,
            markup_percent  REAL DEFAULT 0,
            warranty_years  INTEGER DEFAULT 0,
            notes           TEXT DEFAULT '',
            active          INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    # Vehicle sqft cache
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_sqft_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle     TEXT NOT NULL UNIQUE,
            total_sqft  REAL NOT NULL,
            breakdown   TEXT DEFAULT '',
            source      TEXT DEFAULT 'ai',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    ''')

    # ── Inventory tables ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id     INTEGER NOT NULL,
            sku           TEXT UNIQUE NOT NULL,
            name          TEXT NOT NULL,
            category      TEXT NOT NULL DEFAULT 'General',
            quantity      REAL NOT NULL DEFAULT 0,
            unit          TEXT NOT NULL DEFAULT 'units',
            reorder_level REAL NOT NULL DEFAULT 5,
            cost_per_unit INTEGER NOT NULL DEFAULT 0,
            supplier      TEXT DEFAULT '',
            updated_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id  INTEGER NOT NULL,
            item_id    INTEGER NOT NULL,
            change_qty REAL NOT NULL,
            reason     TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id) REFERENCES studios(id),
            FOREIGN KEY (item_id) REFERENCES inventory_items(id)
        )
    ''')
    # Job Materials (Point of Use Tracking)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS job_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            inventory_id INTEGER NOT NULL,
            technician TEXT NOT NULL,
            quantity_used REAL NOT NULL,
            logged_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (studio_id) REFERENCES studios(id),
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (inventory_id) REFERENCES inventory_items(id)
        )
    ''')

    # ── Seed default studio if no studios exist ──
    if not cursor.execute("SELECT id FROM studios LIMIT 1").fetchone():
        print("  🌱 Seeding default studio (shinepro)...")
        cursor.execute("""
            INSERT INTO studios (id, name, city, owner, logo, username, password)
            VALUES (1, 'Shine Pro Detailing', 'Los Angeles, CA', 'James Carter', 'shinepro.svg', 'shinepro', 'shine123')
        """)
        # Insert default services for studio 1
        cursor.executemany("""
            INSERT INTO services (studio_id, name, duration_hr, price)
            VALUES (1, ?, ?, ?)
        """, [
            ('Full Detail', 4.0, 250),
            ('Ceramic Coating', 6.0, 850),
            ('Paint Correction', 5.0, 620),
            ('Full PPF', 8.0, 1400),
            ('Interior Detail', 3.0, 320),
        ])
        # Insert some bays
        cursor.executemany("INSERT INTO bays (studio_id, name) VALUES (1, ?)", [('Bay 1',), ('Bay 2',), ('Bay 3',)])
        # Insert staff (for login)
        cursor.execute("""
            INSERT INTO staff (studio_id, name, role, username, password)
            VALUES (1, 'James Carter', 'general_manager', 'shinepro', 'shine123')
        """)
        # Insert a couple of customers (for bookings)
        cursor.execute("""
            INSERT INTO customers (studio_id, name, phone, username, password)
            VALUES (1, 'John Smith', '+1-555-0001', 'john', '')
        """)
        cursor.execute("""
            INSERT INTO customers (studio_id, name, phone, username, password)
            VALUES (1, 'Emma Wilson', '+1-555-0002', 'emma', '')
        """)
        conn.commit()

    conn.commit()
    conn.close()
    print("✅ Schema and seed data ready!")

if __name__ == "__main__":
    run()