# import sqlite3
# from flask import g as request_context
# from .config import Config


# def get_db():
#     if "db" not in request_context:
#         request_context.db = sqlite3.connect(Config.DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
#         request_context.db.row_factory = sqlite3.Row
#     return request_context.db


# def close_db(e=None):
#     db = request_context.pop("db", None)
#     if db is not None:
#         db.close()

# def get_catalog_for_ai_context(studio_id: int):
#     """
#     Retrieves a lightweight, token-optimized list of services/products for the AI.
#     Follows Single Responsibility Principle (SRP).
#     """
#     conn = get_db()
#     # Fetching services. If you also have a separate 'products' table from product_routes, 
#     # you can UNION or fetch those here as well.
#     services = conn.execute(
#         "SELECT id, name, price, duration_hr FROM services WHERE studio_id=?", 
#         (studio_id,)
#     ).fetchall()
    
#     return [dict(s) for s in services]

# def init_db():
#     conn = sqlite3.connect(Config.DB_PATH)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS studios (
#             id       INTEGER PRIMARY KEY AUTOINCREMENT,
#             name     TEXT NOT NULL,
#             city     TEXT NOT NULL,
#             owner    TEXT NOT NULL,
#             logo     TEXT NOT NULL,
#             username TEXT UNIQUE NOT NULL,
#             password TEXT NOT NULL
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS staff (
#             id         INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id  INTEGER NOT NULL,
#             name       TEXT NOT NULL,
#             role       TEXT NOT NULL,
#             username   TEXT UNIQUE NOT NULL,
#             password   TEXT NOT NULL,
#             FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS bays (
#             id        INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id INTEGER NOT NULL,
#             name      TEXT NOT NULL,
#             FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS services (
#             id          INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id   INTEGER NOT NULL,
#             name        TEXT NOT NULL,
#             duration_hr REAL NOT NULL,
#             price       INTEGER NOT NULL,
#             FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS bookings (
#             id            INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id     INTEGER NOT NULL,
#             customer_name TEXT NOT NULL,
#             customer_phone TEXT NOT NULL,
#             vehicle       TEXT NOT NULL,
#             service_id    INTEGER NOT NULL,
#             bay_id        INTEGER,
#             staff_id      INTEGER,
#             date          TEXT NOT NULL,
#             time_slot     TEXT NOT NULL,
#             status        TEXT NOT NULL DEFAULT 'Pending',
#             notes         TEXT DEFAULT '',
#             created_at    TEXT DEFAULT (datetime('now')),
#             FOREIGN KEY (studio_id)  REFERENCES studios(id),
#             FOREIGN KEY (service_id) REFERENCES services(id),
#             FOREIGN KEY (bay_id)     REFERENCES bays(id),
#             FOREIGN KEY (staff_id)   REFERENCES staff(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS jobs (
#             id         INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id  INTEGER NOT NULL,
#             car        TEXT NOT NULL,
#             service    TEXT NOT NULL,
#             status     TEXT NOT NULL DEFAULT 'Pending',
#             technician TEXT NOT NULL DEFAULT 'Unassigned',
#             price      INTEGER NOT NULL DEFAULT 0,
#             FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS estimates (
#             id              INTEGER PRIMARY KEY AUTOINCREMENT,
#             studio_id       INTEGER NOT NULL,
#             customer_name   TEXT NOT NULL,
#             customer_email  TEXT NOT NULL,
#             customer_phone  TEXT NOT NULL,
#             vehicle         TEXT NOT NULL,
#             status          TEXT NOT NULL DEFAULT 'Draft',
#             subtotal        INTEGER NOT NULL DEFAULT 0,
#             tax_percent     REAL NOT NULL DEFAULT 8.5,
#             tax_amount      INTEGER NOT NULL DEFAULT 0,
#             total           INTEGER NOT NULL DEFAULT 0,
#             notes           TEXT DEFAULT '',
#             internal_notes  TEXT DEFAULT '',
#             signature       TEXT DEFAULT '',
#             approved_at     TEXT DEFAULT '',
#             created_at      TEXT DEFAULT (datetime('now')),
#             FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS estimate_items (
#             id          INTEGER PRIMARY KEY AUTOINCREMENT,
#             estimate_id INTEGER NOT NULL,
#             name        TEXT NOT NULL,
#             description TEXT DEFAULT '',
#             quantity    INTEGER NOT NULL DEFAULT 1,
#             unit_price  INTEGER NOT NULL DEFAULT 0,
#             total       INTEGER NOT NULL DEFAULT 0,
#             FOREIGN KEY (estimate_id) REFERENCES estimates(id)
#         )
#     """)
#     # Add this inside init_db() in db_manager.py:
#     conn.execute("""
#     CREATE TABLE IF NOT EXISTS inventory_items (
#         id            INTEGER PRIMARY KEY AUTOINCREMENT,
#         studio_id     INTEGER NOT NULL,
#         sku           TEXT UNIQUE NOT NULL,
#         name          TEXT NOT NULL,
#         category      TEXT NOT NULL DEFAULT 'General',
#         quantity      REAL NOT NULL DEFAULT 0,
#         unit          TEXT NOT NULL DEFAULT 'units', -- e.g. ml, oz, bottles, pads
#         reorder_level REAL NOT NULL DEFAULT 5,
#         cost_per_unit INTEGER NOT NULL DEFAULT 0, -- in cents
#         supplier      TEXT DEFAULT '',
#         updated_at    TEXT DEFAULT (datetime('now')),
#         FOREIGN KEY (studio_id) REFERENCES studios(id)
#         )
#     """)

#     conn.execute("""
#     CREATE TABLE IF NOT EXISTS inventory_logs (
#         id         INTEGER PRIMARY KEY AUTOINCREMENT,
#         studio_id  INTEGER NOT NULL,
#         item_id    INTEGER NOT NULL,
#         change_qty REAL NOT NULL,
#         reason     TEXT NOT NULL, -- e.g. 'Restock', 'Job #12 Usage', 'Adjustment'
#         created_at TEXT DEFAULT (datetime('now')),
#         FOREIGN KEY (studio_id) REFERENCES studios(id),
#         FOREIGN KEY (item_id) REFERENCES inventory_items(id)
#         )
#     """)    
#     conn.commit()
#     conn.close()




# def get_studio_inventory(studio_id: int):
#     conn = get_db()
#     rows = conn.execute(
#         "SELECT * FROM inventory_items WHERE studio_id=? ORDER BY name ASC", 
#         (studio_id,)
#     ).fetchall()
#     return [dict(r) for r in rows]

# def get_inventory_consumption(studio_id: int):
#     """Returns dict {item_id: total_consumed_quantity} for the last 30 days."""
#     conn = get_db()
#     rows = conn.execute("""
#         SELECT item_id, SUM(ABS(change_qty)) as consumed
#         FROM inventory_logs
#         WHERE studio_id = ? AND change_qty < 0 AND created_at >= date('now', '-30 days')
#         GROUP BY item_id
#     """, (studio_id,)).fetchall()
#     return {r["item_id"]: r["consumed"] for r in rows}












import sqlite3
from flask import g as request_context
from .config import Config


def get_db():
    if "db" not in request_context:
        request_context.db = sqlite3.connect(Config.DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        request_context.db.row_factory = sqlite3.Row
    return request_context.db


def close_db(e=None):
    db = request_context.pop("db", None)
    if db is not None:
        db.close()

def get_catalog_for_ai_context(studio_id: int):
    """
    Retrieves a lightweight, token-optimized list of services/products for the AI.
    Follows Single Responsibility Principle (SRP).
    """
    conn = get_db()
    services = conn.execute(
        "SELECT id, name, price, duration_hr FROM services WHERE studio_id=?", 
        (studio_id,)
    ).fetchall()
    return [dict(s) for s in services]

def init_db():
    conn = sqlite3.connect(Config.DB_PATH)
    
    # Studios
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
    
    # Staff
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
    
    # Bays
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bays (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL,
            name      TEXT NOT NULL,
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    """)
    
    # Services
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
    
    # Bookings (with customer_id and estimate_id)
    conn.execute("""
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
    """)
    
    # ── UPDATED JOBS TABLE ── (with all new columns)
    conn.execute("""
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
    """)
    # Create unique index for tracking_token (for speed)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uix_jobs_tracking_token ON jobs(tracking_token)")
    
    # Estimates
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
            customer_id     INTEGER,
            services_summary TEXT DEFAULT '',
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    """)
    
    # Estimate Items
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
    
    # Customers
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
    
    # Vehicles
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
    
    # Media
    conn.execute("""
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
    """)
    
    # Notes
    conn.execute("""
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
    """)
    
    # Reminders
    conn.execute("""
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
    """)
    
    # Panel Inspections
    conn.execute("""
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
    """)
    
    # Products (for AI estimator)
    conn.execute("""
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
    """)
    
    # Vehicle sqft cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_sqft_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle     TEXT NOT NULL UNIQUE,
            total_sqft  REAL NOT NULL,
            breakdown   TEXT DEFAULT '',
            source      TEXT DEFAULT 'ai',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    
    # ── Inventory Tables ──
    conn.execute("""
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
    """)

    conn.execute("""
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
    """)
    
    conn.commit()
    conn.close()

    # ── Phase 2 (DVI) + Phase 3 (Dispatch) schema ──
    # Delegated to the migration module so there is exactly one definition of
    # these tables. Safe and idempotent on every startup.
    from .migrate_phase2 import migrate as _migrate_phase2
    from .migrate_phase4 import migrate as _migrate_phase4

    _migrate_phase2(Config.DB_PATH)
    _migrate_phase4(Config.DB_PATH)
    from .migrate_intelligence import migrate as migrate_intelligence
    migrate_intelligence(Config.DB_PATH)


def get_studio_inventory(studio_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM inventory_items WHERE studio_id=? ORDER BY name ASC", 
        (studio_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_inventory_consumption(studio_id: int):
    """Returns dict {item_id: total_consumed_quantity} for the last 30 days."""
    conn = get_db()
    rows = conn.execute("""
        SELECT item_id, SUM(ABS(change_qty)) as consumed
        FROM inventory_logs
        WHERE studio_id = ? AND change_qty < 0 AND created_at >= date('now', '-30 days')
        GROUP BY item_id
    """, (studio_id,)).fetchall()
    return {r["item_id"]: r["consumed"] for r in rows}