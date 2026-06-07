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


def init_db():
    conn = sqlite3.connect(Config.DB_PATH)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bays (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL,
            name      TEXT NOT NULL,
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    """)
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
            FOREIGN KEY (studio_id)  REFERENCES studios(id),
            FOREIGN KEY (service_id) REFERENCES services(id),
            FOREIGN KEY (bay_id)     REFERENCES bays(id),
            FOREIGN KEY (staff_id)   REFERENCES staff(id)
        )
    """)
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
            FOREIGN KEY (studio_id) REFERENCES studios(id)
        )
    """)
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
    conn.commit()
    conn.close()
