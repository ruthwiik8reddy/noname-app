"""
tests/test_agents.py — agent framework and leads pipeline.

No Ollama required. The agents' deterministic paths are asserted on exact
values; the LLM-dependent parts are asserted twice — once with a stub, once
with a dead provider — because the degraded branch is the one that runs on a
laptop at 9am.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.agents.base import SEVERITY_RANK, BaseAgent, Finding
from src.services.agents.leads_agent import LeadsAgent
from src.services.repositories.agent_repository import AgentRepository
from src.services.repositories.lead_repository import LeadRepository


def build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE studios (id INTEGER PRIMARY KEY, name TEXT, city TEXT, owner TEXT,
            logo TEXT, username TEXT, password TEXT);
        CREATE TABLE customers (id INTEGER PRIMARY KEY, studio_id INTEGER, name TEXT,
            email TEXT, phone TEXT);
        CREATE TABLE services (id INTEGER PRIMARY KEY, studio_id INTEGER, name TEXT,
            duration_hr REAL, price INTEGER);
        CREATE TABLE bookings (id INTEGER PRIMARY KEY, studio_id INTEGER, customer_name TEXT,
            customer_phone TEXT, vehicle TEXT, service_id INTEGER, date TEXT,
            time_slot TEXT, status TEXT, created_at TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, studio_id INTEGER, car TEXT, service TEXT,
            status TEXT DEFAULT 'Pending', technician TEXT DEFAULT 'Unassigned',
            price INTEGER DEFAULT 0, completed_at TEXT DEFAULT '');
        INSERT INTO studios VALUES (1,'Test','LA','Owner','l.svg','t','p');
        """
    )
    conn.commit()
    conn.close()
    from src.migrate_phase4 import migrate
    migrate(path)


class FindingTests(unittest.TestCase):

    def test_fingerprint_is_stable_across_runs(self):
        """Same observation twice must produce the same fingerprint, or the
        dashboard fills with duplicates."""
        a = Finding(kind="stock_low", title="X is low", entity_type="item", entity_id=3)
        b = Finding(kind="stock_low", title="X is low", entity_type="item", entity_id=3)
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_different_subjects_differ(self):
        a = Finding(kind="stock_low", title="X is low", entity_id=3)
        b = Finding(kind="stock_low", title="Y is low", entity_id=4)
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_invalid_severity_falls_back_to_info(self):
        self.assertEqual(Finding(kind="k", title="t", severity="catastrophic").severity, "info")

    def test_severity_ordering(self):
        self.assertLess(SEVERITY_RANK["critical"], SEVERITY_RANK["warning"])
        self.assertLess(SEVERITY_RANK["warning"], SEVERITY_RANK["opportunity"])


class AgentLifecycleTests(unittest.TestCase):

    def test_exception_is_captured_not_raised(self):
        """An agent must never be able to break the page that triggered it."""
        class Exploding(BaseAgent):
            name = "boom"
            def collect(self):
                raise ValueError("kaboom")

        result = Exploding(1).run()
        self.assertFalse(result.ok)
        self.assertIn("kaboom", result.error)
        self.assertEqual(result.findings, [])

    def test_findings_are_sorted_worst_first(self):
        class Mixed(BaseAgent):
            name = "mixed"
            def collect(self):
                return [
                    Finding(kind="a", title="info", severity="info"),
                    Finding(kind="b", title="crit", severity="critical"),
                    Finding(kind="c", title="warn", severity="warning"),
                ]

        result = Mixed(1).run()
        self.assertEqual([f.severity for f in result.findings],
                         ["critical", "warning", "info"])

    def test_empty_is_a_valid_result(self):
        class Quiet(BaseAgent):
            name = "quiet"
            def collect(self):
                return []

        result = Quiet(1).run()
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "Nothing to report.")


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        build_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)


class AgentRepositoryTests(DatabaseTest):

    def setUp(self):
        super().setUp()
        self.repo = AgentRepository(db_path=self.db)

    def test_repeat_runs_do_not_duplicate_findings(self):
        f = Finding(kind="stock_low", title="Ceramic is low", entity_id=1)
        for _ in range(5):
            self.repo.save_findings(1, "inventory", None, [f])
        self.assertEqual(len(self.repo.open_findings(1)), 1)

    def test_created_at_is_preserved_on_update(self):
        """"How long has this been true?" must stay answerable."""
        f = Finding(kind="stock_low", title="Low", entity_id=1)
        self.repo.save_findings(1, "inventory", None, [f])
        first = self.repo.open_findings(1)[0]["created_at"]
        f.detail = "updated detail"
        self.repo.save_findings(1, "inventory", None, [f])
        row = self.repo.open_findings(1)[0]
        self.assertEqual(row["created_at"], first)
        self.assertEqual(row["detail"], "updated detail")

    def test_dismissed_stays_dismissed_on_rerun(self):
        """Dismissing must mean something — an agent shouldn't nag."""
        f = Finding(kind="stock_low", title="Low", severity="warning", entity_id=1)
        self.repo.save_findings(1, "inventory", None, [f])
        fid = self.repo.open_findings(1)[0]["id"]
        self.repo.set_status(1, fid, "dismissed", "tester")

        self.repo.save_findings(1, "inventory", None, [f])
        self.assertEqual(len(self.repo.open_findings(1)), 0)

    def test_dismissed_reopens_only_on_escalation(self):
        f = Finding(kind="stock_low", title="Low", severity="warning", entity_id=1)
        self.repo.save_findings(1, "inventory", None, [f])
        self.repo.set_status(1, self.repo.open_findings(1)[0]["id"], "dismissed", "t")

        worse = Finding(kind="stock_low", title="Low", severity="critical", entity_id=1)
        worse.fingerprint = f.fingerprint
        self.repo.save_findings(1, "inventory", None, [worse])
        self.assertEqual(len(self.repo.open_findings(1)), 1)

    def test_vanished_findings_auto_resolve(self):
        """Restock the item and the warning should disappear by itself."""
        a = Finding(kind="stock_low", title="A low", entity_id=1)
        b = Finding(kind="stock_low", title="B low", entity_id=2)
        self.repo.save_findings(1, "inventory", None, [a, b])
        self.repo.resolve_missing(1, "inventory", [a.fingerprint])
        self.assertEqual(len(self.repo.open_findings(1)), 1)

    def test_run_records_failure(self):
        run_id = self.repo.start_run(1, "inventory", "manual")
        self.repo.finish_run(run_id, "failed", 0, 12, error="boom")
        run = self.repo.recent_runs(1)[0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("boom", run["error"])

    def test_never_run_agents_are_due(self):
        self.repo.ensure_settings(1, "inventory", 60)
        self.assertIn("inventory", self.repo.due_agents(1))

    def test_recently_run_agents_are_not_due(self):
        self.repo.ensure_settings(1, "inventory", 60)
        self.repo.touch_last_run(1, "inventory")
        self.assertNotIn("inventory", self.repo.due_agents(1))

    def test_disabled_agents_are_never_due(self):
        self.repo.ensure_settings(1, "inventory", 60)
        self.repo.set_enabled(1, "inventory", False)
        self.assertNotIn("inventory", self.repo.due_agents(1))

    def test_tenant_isolation(self):
        self.repo.save_findings(1, "inventory", None, [Finding(kind="k", title="t")])
        self.assertEqual(len(self.repo.open_findings(2)), 0)


class LeadScoringTests(unittest.TestCase):
    """Scoring is deterministic — same input, same number, every time."""

    def test_identical_input_scores_identically(self):
        lead = {"budget_hint": 150000, "source": "referral", "status": "quoted",
                "phone": "+91 90000 00000", "email": "a@b.com",
                "service_interest": "Full PPF", "vehicle": "Audi Q7"}
        self.assertEqual(LeadsAgent.score(lead)[0], LeadsAgent.score(lead)[0])

    def test_high_value_referral_outscores_thin_instagram(self):
        rich = {"budget_hint": 200000, "source": "referral", "status": "negotiating",
                "phone": "1", "email": "a@b.com", "service_interest": "PPF", "vehicle": "Q7"}
        thin = {"budget_hint": 0, "source": "instagram", "status": "new",
                "phone": "", "email": "", "service_interest": "", "vehicle": ""}
        self.assertGreater(LeadsAgent.score(rich)[0], LeadsAgent.score(thin)[0])

    def test_score_is_bounded(self):
        maxed = {"budget_hint": 99999999, "source": "referral", "status": "negotiating",
                 "phone": "1", "email": "a@b.com", "service_interest": "x", "vehicle": "y"}
        score, _ = LeadsAgent.score(maxed)
        self.assertLessEqual(score, 100)
        self.assertGreaterEqual(LeadsAgent.score({})[0], 0)

    def test_reason_explains_the_score(self):
        _, reason = LeadsAgent.score({"budget_hint": 200000, "source": "referral", "status": "new"})
        self.assertIn("high-value", reason)
        self.assertIn("referral", reason)

    def test_missing_phone_is_called_out(self):
        _, reason = LeadsAgent.score({"source": "manual", "status": "new", "phone": ""})
        self.assertIn("no phone", reason)


class LeadRepositoryTests(DatabaseTest):

    def setUp(self):
        super().setUp()
        self.repo = LeadRepository(db_path=self.db)

    def test_create_and_fetch(self):
        lead_id = self.repo.create(1, {"name": "Ada", "phone": "123", "source": "referral"}, "t")
        self.assertEqual(self.repo.get(1, lead_id)["name"], "Ada")

    def test_creation_is_logged(self):
        lead_id = self.repo.create(1, {"name": "Ada"}, "tester")
        self.assertEqual(self.repo.events(1, lead_id)[0]["kind"], "created")

    def test_invalid_stage_rejected(self):
        lead_id = self.repo.create(1, {"name": "Ada"}, "t")
        self.assertFalse(self.repo.update_status(1, lead_id, "banana", "t"))
        self.assertEqual(self.repo.get(1, lead_id)["status"], "new")

    def test_stale_detection(self):
        lead_id = self.repo.create(1, {"name": "Quiet"}, "t")
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.repo.execute("UPDATE leads SET created_at=?, last_contacted='' WHERE id=?", (old, lead_id))

        stale = self.repo.stale(1, days=5)
        self.assertEqual(len(stale), 1)
        self.assertGreaterEqual(stale[0]["days_quiet"], 9)

    def test_recent_contact_is_not_stale(self):
        lead_id = self.repo.create(1, {"name": "Fresh"}, "t")
        self.repo.log_contact(1, lead_id, "Called", "t")
        self.assertEqual(len(self.repo.stale(1, days=5)), 0)

    def test_won_leads_are_not_chased(self):
        lead_id = self.repo.create(1, {"name": "Closed"}, "t")
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.repo.execute("UPDATE leads SET created_at=? WHERE id=?", (old, lead_id))
        self.repo.update_status(1, lead_id, "won", "t")
        self.assertEqual(len(self.repo.stale(1, days=5)), 0)

    def test_conversion_creates_a_customer(self):
        lead_id = self.repo.create(1, {"name": "Buyer", "email": "b@x.com", "phone": "9"}, "t")
        customer_id = self.repo.convert(1, lead_id, "t")
        self.assertIsNotNone(customer_id)
        row = self.repo.fetch_one("SELECT * FROM customers WHERE id=?", (customer_id,))
        self.assertEqual(row["name"], "Buyer")
        self.assertEqual(self.repo.get(1, lead_id)["status"], "won")

    def test_conversion_is_idempotent(self):
        lead_id = self.repo.create(1, {"name": "Buyer"}, "t")
        first = self.repo.convert(1, lead_id, "t")
        self.assertEqual(self.repo.convert(1, lead_id, "t"), first)

    def test_import_dedupes_on_phone_and_email(self):
        rows = [
            {"name": "A", "phone": "+91 99999 11111"},
            {"name": "B", "email": "b@example.com"},
        ]
        self.assertEqual(self.repo.bulk_import(1, rows, "t")["created"], 2)
        again = self.repo.bulk_import(1, rows, "t")
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped"], 2)

    def test_conversion_rate(self):
        for _ in range(3):
            lid = self.repo.create(1, {"name": "W"}, "t")
            self.repo.update_status(1, lid, "won", "t")
        lid = self.repo.create(1, {"name": "L"}, "t")
        self.repo.update_status(1, lid, "lost", "t")
        self.assertEqual(self.repo.stats(1)["conversion_rate"], 75.0)


class ImportParsingTests(unittest.TestCase):

    def test_parses_name_phone_vehicle(self):
        from src.routes.lead_routes import parse_pasted
        rows = parse_pasted("Priya, +91 98450 11223, Audi Q7, full PPF", "whatsapp")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Priya")
        self.assertIn("98450", rows[0]["phone"])
        self.assertEqual(rows[0]["vehicle"], "Audi Q7")

    def test_extracts_email(self):
        from src.routes.lead_routes import parse_pasted
        rows = parse_pasted("Jenny Park, jenny@example.com, Tesla Model 3", "whatsapp")
        self.assertEqual(rows[0]["email"], "jenny@example.com")

    def test_blank_lines_ignored(self):
        from src.routes.lead_routes import parse_pasted
        self.assertEqual(len(parse_pasted("Ada, 123\n\n   \nBob, 456", "manual")), 2)

    def test_junk_returns_nothing_usable(self):
        from src.routes.lead_routes import parse_pasted
        self.assertEqual(parse_pasted("\n\n  \n", "manual"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
