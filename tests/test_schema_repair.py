"""Older databases are brought up to the current schema at startup."""

import glob
import os
import sqlite3
import tempfile
import unittest

from src.schema_repair import repair

LEGACY = """
CREATE TABLE studios (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    studio_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT DEFAULT '',
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (studio_id) REFERENCES studios(id)
);
CREATE INDEX idx_customers_studio ON customers(studio_id);
CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, studio_id INTEGER NOT NULL, customer_name TEXT);
INSERT INTO studios (name) VALUES ('Old Studio');
INSERT INTO customers (studio_id, name, email, username, password) VALUES (1, 'Asha', 'a@x.in', 'asha', 'h');
"""


class SchemaRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(LEGACY)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_missing_columns_and_relaxes_legacy_required_ones(self):
        changed = repair(self.db)
        self.assertIn(("bookings", "estimate_id"), changed)
        conn = sqlite3.connect(self.db)
        # A customer created from just a name and phone (what New booking does) now fits.
        conn.execute("INSERT INTO customers (studio_id, name, phone) VALUES (1, 'Ravi', '9876543210')")
        self.assertEqual(conn.execute("SELECT email FROM customers WHERE name='Ravi'").fetchone()[0], "")
        # Existing rows, unique usernames and indexes survive the rebuild.
        self.assertEqual(conn.execute("SELECT email, username FROM customers WHERE name='Asha'").fetchone(), ("a@x.in", "asha"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO customers (studio_id, name, username) VALUES (1, 'Dup', 'asha')")
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='customers'")}
        self.assertIn("idx_customers_studio", indexes)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        self.assertEqual(len(glob.glob(os.path.join(self.tmp.name, "legacy-pre-repair-*.db.bak"))), 1)

    def test_is_idempotent(self):
        repair(self.db)
        self.assertEqual(repair(self.db), [])


if __name__ == "__main__":
    unittest.main()
