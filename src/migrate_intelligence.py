"""Idempotent storage for reproducible intelligence reports and execution leases."""
import sqlite3


def migrate(path):
    with sqlite3.connect(path, timeout=15) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS intelligence_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL,
            actor TEXT NOT NULL,
            question TEXT NOT NULL,
            source TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ix_intelligence_reports_studio
            ON intelligence_reports(studio_id, id DESC);
        CREATE TABLE IF NOT EXISTS agent_leases (
            studio_id INTEGER NOT NULL, agent TEXT NOT NULL,
            token TEXT NOT NULL, expires_at REAL NOT NULL,
            PRIMARY KEY(studio_id, agent)
        );
        CREATE TABLE IF NOT EXISTS intelligence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            studio_id INTEGER NOT NULL, event TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            processed_revision INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,
            token TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(studio_id,event)
        );
        """)
        # Events commit atomically with domain changes, including legacy routes.
        # Coalesce repeated writes by studio and event to avoid a thread per change.
        for table, event in (
            ('jobs','jobs_changed'), ('estimates','estimates_changed'),
            ('bookings','booking_created'), ('inventory_items','stock_changed'),
            ('inventory_logs','stock_changed'), ('leads','leads_changed'),
            ('dvi_inspections','dvi_analyzed'), ('customers','customers_changed'),
        ):
            for operation in ('INSERT','UPDATE','DELETE'):
                row = 'OLD' if operation == 'DELETE' else 'NEW'
                # Lead agents update scores themselves. Only substantive lead edits enqueue work.
                update_columns = ' OF status, last_contacted, service_interest, budget_hint, notes' if table=='leads' and operation=='UPDATE' else ''
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS intel_{table}_{operation.lower()}
                    AFTER {operation}{update_columns} ON {table}
                    BEGIN
                        INSERT INTO intelligence_events(studio_id,event) VALUES({row}.studio_id,'{event}')
                        ON CONFLICT(studio_id,event) DO UPDATE SET revision=revision+1,
                            attempts=0, available_at=0, updated_at=datetime('now');
                    END""")
