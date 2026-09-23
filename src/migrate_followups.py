"""Reviewed follow-up drafts and explicit, destination-bound permission records."""
import sqlite3


def migrate(path):
    with sqlite3.connect(path, timeout=15) as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS contact_permissions (
          studio_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, channel TEXT NOT NULL,
          destination TEXT NOT NULL, allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
          evidence TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY(studio_id,customer_id,channel), CHECK(channel IN ('email','sms','whatsapp')));
        CREATE TABLE IF NOT EXISTS followups (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
          vehicle_id INTEGER NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('rebooking','estimate')),
          source_id INTEGER NOT NULL, channel TEXT NOT NULL CHECK(channel IN ('email','sms','whatsapp')),
          destination TEXT NOT NULL, body TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft','Approved','Contacted','Closed')),
          version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
          approved_at TEXT, approved_by TEXT, contacted_at TEXT, contact_reference TEXT, contact_booking_floor INTEGER,
          booking_id INTEGER, outcome TEXT, closed_reason TEXT,
          request_key TEXT NOT NULL, request_hash TEXT NOT NULL, UNIQUE(studio_id,request_key));
        CREATE UNIQUE INDEX IF NOT EXISTS ix_followup_active ON followups(studio_id,vehicle_id,purpose)
          WHERE status IN ('Draft','Approved','Contacted');
        CREATE UNIQUE INDEX IF NOT EXISTS ix_followup_booking ON followups(studio_id,booking_id)
          WHERE booking_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_followup_customer ON followups(studio_id,customer_id);
        CREATE TABLE IF NOT EXISTS followup_audit (
          id INTEGER PRIMARY KEY, studio_id INTEGER NOT NULL, followup_id INTEGER,
          action TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now')));
        ''')
