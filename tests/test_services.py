"""
tests/test_services.py — service-layer regression suite.

Run from the project root:

    python -m unittest discover tests -v

No Ollama required. `RecordingProvider` returns canned model output, which is
the whole reason the LLM sits behind an interface: the DVI pipeline can be
tested end-to-end — vision output → vocabulary validation → pricing →
consolidation → estimate conversion — without a GPU or a network.

Every deterministic path is asserted on exact numbers. Every AI path is
asserted twice: once with a working model, once with a dead one, because the
degraded branch is the one that runs on a customer's laptop at 9am.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.analytics import DepletionForecaster
from src.services.llm.base import LLMUnavailableError, NullProvider, RecordingProvider
from src.services.orchestrators import (
    AssignmentOrchestrator,
    DVIOrchestrator,
    InventoryIntelligenceOrchestrator,
    OrchestratorError,
)
from src.services.orchestrators.base import BaseOrchestrator
from src.services.pricing import UpchargeCalculator
from src.services.repositories import (
    DVIRepository,
    InventoryRepository,
    JobRepository,
    StaffRepository,
)

SCHEMA_MODULES = ("studios", "staff", "jobs", "services", "inventory_items", "inventory_logs")


def build_test_db(path: str) -> None:
    """Minimal schema + the Phase 2/3 migration, so tests exercise the real DDL."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE studios (id INTEGER PRIMARY KEY, name TEXT, city TEXT, owner TEXT,
            logo TEXT, username TEXT, password TEXT);
        CREATE TABLE staff (id INTEGER PRIMARY KEY, studio_id INTEGER, name TEXT, role TEXT,
            username TEXT, password TEXT);
        CREATE TABLE services (id INTEGER PRIMARY KEY, studio_id INTEGER, name TEXT,
            duration_hr REAL, price INTEGER);
        CREATE TABLE customers (id INTEGER PRIMARY KEY, studio_id INTEGER, name TEXT,
            email TEXT, phone TEXT);
        CREATE TABLE bookings (id INTEGER PRIMARY KEY, studio_id INTEGER, customer_name TEXT,
            vehicle TEXT, service_id INTEGER, date TEXT, time_slot TEXT, status TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, studio_id INTEGER, car TEXT, service TEXT,
            status TEXT DEFAULT 'Pending', technician TEXT DEFAULT 'Unassigned',
            price INTEGER DEFAULT 0, customer_id INTEGER, completed_at TEXT DEFAULT '');
        CREATE TABLE estimates (id INTEGER PRIMARY KEY, studio_id INTEGER, customer_name TEXT,
            customer_email TEXT, customer_phone TEXT, vehicle TEXT, status TEXT,
            subtotal INTEGER, tax_percent REAL, tax_amount INTEGER, total INTEGER,
            notes TEXT, internal_notes TEXT, customer_id INTEGER);
        CREATE TABLE estimate_items (id INTEGER PRIMARY KEY, estimate_id INTEGER, name TEXT,
            description TEXT, quantity INTEGER, unit_price INTEGER, total INTEGER);
        CREATE TABLE inventory_items (id INTEGER PRIMARY KEY, studio_id INTEGER, sku TEXT,
            name TEXT, category TEXT, quantity REAL, unit TEXT, reorder_level REAL,
            cost_per_unit INTEGER, supplier TEXT, updated_at TEXT);
        -- created_at default MUST mirror production: without it, rows written by
        -- adjust_stock() land with a NULL timestamp and vanish from every
        -- date-filtered query. A test schema that drifts from production hides
        -- exactly the bugs it exists to catch.
        CREATE TABLE inventory_logs (id INTEGER PRIMARY KEY, studio_id INTEGER, item_id INTEGER,
            change_qty REAL, reason TEXT, created_at TEXT DEFAULT (datetime('now')));

        INSERT INTO studios VALUES (1,'Test Studio','LA','Owner','l.svg','t','p');
        INSERT INTO staff VALUES (1,1,'Maria Lopez','technician','maria','p'),
                                 (2,1,'Carlos Diaz','technician','carlos','p'),
                                 (3,1,'Gina Moss','general_manager','gina','p');
        INSERT INTO services VALUES (1,1,'Ceramic Coating',6.0,850),
                                    (2,1,'Paint Correction',5.0,620),
                                    (3,1,'Interior Detail',3.0,320);
        INSERT INTO customers VALUES (1,1,'Dana Reyes','dana@example.com','555-0100');
        """
    )
    conn.commit()
    conn.close()

    from src.migrate_phase2 import migrate

    migrate(path)


class ForecasterTests(unittest.TestCase):
    """Pure arithmetic — asserted on exact values, no model involved."""

    def setUp(self):
        self.today = date(2026, 6, 30)
        self.forecaster = DepletionForecaster(today=self.today)

    def _daily(self, item_id: int, per_day: float, days: int):
        return [
            {"item_id": item_id, "day": (self.today - timedelta(days=d)).isoformat(), "qty": per_day}
            for d in range(1, days + 1)
        ]

    def test_burn_rate_and_stockout_date(self):
        item = {"id": 1, "sku": "CER", "name": "Ceramic", "category": "Coatings",
                "quantity": 10.0, "unit": "L", "reorder_level": 2.0, "cost_per_unit": 5000}
        result = self.forecaster.forecast_all(
            [item], self._daily(1, 1.0, 30),
            first_seen={1: (self.today - timedelta(days=30)).isoformat()},
        )[0]

        self.assertAlmostEqual(result.daily_burn, 1.0, places=2)
        self.assertAlmostEqual(result.days_to_empty, 10.0, places=1)
        self.assertEqual(result.stockout_date, (self.today + timedelta(days=10)).isoformat())
        # 10 on hand - 2 reorder buffer = 8 days of headroom
        self.assertAlmostEqual(result.days_to_reorder, 8.0, places=1)
        self.assertEqual(result.confidence, "high")

    def test_recent_usage_is_weighted_above_older_usage(self):
        """A shop that just got busy should see the forecast react, not average away."""
        item = {"id": 1, "sku": "X", "name": "X", "category": "c", "quantity": 100.0,
                "unit": "u", "reorder_level": 5.0, "cost_per_unit": 100}
        rows = [
            {"item_id": 1, "day": (self.today - timedelta(days=d)).isoformat(),
             "qty": 10.0 if d <= 7 else 1.0}
            for d in range(1, 31)
        ]
        result = self.forecaster.forecast_all(
            [item], rows, first_seen={1: (self.today - timedelta(days=30)).isoformat()}
        )[0]
        flat_average = (10 * 7 + 1 * 23) / 30  # ≈ 3.1
        self.assertGreater(result.daily_burn, flat_average)

    def test_no_usage_yields_no_false_prediction(self):
        item = {"id": 1, "sku": "X", "name": "X", "category": "c", "quantity": 5.0,
                "unit": "u", "reorder_level": 10.0, "cost_per_unit": 100}
        result = self.forecaster.forecast_all([item], [])[0]

        self.assertEqual(result.daily_burn, 0.0)
        self.assertIsNone(result.days_to_empty)      # never invent a date
        self.assertIsNone(result.stockout_date)
        self.assertEqual(result.confidence, "none")
        self.assertEqual(result.urgency, "warning")  # but below reorder still matters

    def test_thin_history_reports_low_confidence(self):
        item = {"id": 1, "sku": "X", "name": "X", "category": "c", "quantity": 20.0,
                "unit": "u", "reorder_level": 2.0, "cost_per_unit": 100}
        result = self.forecaster.forecast_all(
            [item], self._daily(1, 2.0, 2),
            first_seen={1: (self.today - timedelta(days=2)).isoformat()},
        )[0]
        self.assertEqual(result.confidence, "low")

    def test_waste_detection_needs_a_real_outlier(self):
        items = [{"id": 1, "sku": "P", "name": "Polish", "unit": "L", "cost_per_unit": 2000}]

        steady = {1: {10: 1.0, 11: 1.1, 12: 0.9, 13: 1.0, 14: 1.05}}
        self.assertEqual(self.forecaster.detect_waste(items, steady), [])

        spiked = {1: {10: 1.0, 11: 1.1, 12: 0.9, 13: 1.0, 14: 5.0}}
        signals = self.forecaster.detect_waste(items, spiked)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].outlier_jobs[0]["job_id"], 14)

    def test_waste_detection_ignores_small_samples(self):
        """Three jobs is not a pattern — refuse to accuse."""
        items = [{"id": 1, "sku": "P", "name": "Polish", "unit": "L", "cost_per_unit": 2000}]
        self.assertEqual(self.forecaster.detect_waste(items, {1: {1: 1.0, 2: 1.0, 3: 9.0}}), [])


class UpchargeTests(unittest.TestCase):

    def setUp(self):
        self.calc = UpchargeCalculator([
            {"id": 2, "name": "Paint Correction", "price": 620},
            {"id": 1, "name": "Ceramic Coating", "price": 850},
        ])

    def test_prices_from_studio_catalog_not_rate_card(self):
        priced = self.calc.price_finding(
            {"defect_type": "swirl_marks", "panel": "hood", "severity": 3, "confidence": 0.9}
        )
        self.assertEqual(priced["pricing_basis"], "studio_catalog")
        self.assertEqual(priced["suggested_service"], "Paint Correction")
        self.assertEqual(priced["suggested_upcharge_cents"], 62000)  # severity 3 = ×1.0

    def test_severity_scales_price(self):
        low = self.calc.price_finding(
            {"defect_type": "swirl_marks", "severity": 1, "confidence": 0.9})
        high = self.calc.price_finding(
            {"defect_type": "swirl_marks", "severity": 5, "confidence": 0.9})
        self.assertEqual(low["suggested_upcharge_cents"], 31000)   # ×0.5
        self.assertEqual(high["suggested_upcharge_cents"], 105400)  # ×1.7

    def test_low_confidence_is_logged_but_never_billed(self):
        priced = self.calc.price_finding(
            {"defect_type": "deep_scratch", "severity": 4, "confidence": 0.2})
        self.assertEqual(priced["suggested_upcharge_cents"], 0)
        self.assertEqual(priced["pricing_basis"], "not_priced")

    def test_falls_back_to_rate_card_when_catalog_has_no_match(self):
        priced = UpchargeCalculator([]).price_finding(
            {"defect_type": "dent", "severity": 3, "confidence": 0.9})
        self.assertEqual(priced["pricing_basis"], "default_rate_card")
        self.assertEqual(priced["suggested_service"], "Paintless Dent Repair")

    def test_same_defect_on_many_panels_is_one_line(self):
        findings = [
            self.calc.price_finding({"defect_type": "swirl_marks", "panel": p,
                                     "severity": 3, "confidence": 0.9})
            for p in ("hood", "roof", "trunk")
        ]
        lines = self.calc.consolidate(findings)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].panels), 3)
        # Base once + incremental per extra panel, not 3 × base.
        self.assertLess(lines[0].amount_cents, 62000 * 3)
        self.assertGreater(lines[0].amount_cents, 62000)

    def test_condition_score(self):
        self.assertEqual(UpchargeCalculator.condition_score([]), 10.0)
        wrecked = UpchargeCalculator.condition_score(
            [{"severity": 5, "confidence": 1.0}] * 4)
        self.assertLess(wrecked, 3.0)


class JsonExtractionTests(unittest.TestCase):
    """Small local models are messy. The parser must cope."""

    def test_bare_json(self):
        self.assertEqual(BaseOrchestrator.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(BaseOrchestrator.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_buried_in_prose(self):
        messy = 'Sure! Here is the analysis:\n{"a": 1, "b": [2,3]}\nHope that helps!'
        self.assertEqual(BaseOrchestrator.extract_json(messy), {"a": 1, "b": [2, 3]})

    def test_braces_inside_strings_do_not_break_balancing(self):
        self.assertEqual(
            BaseOrchestrator.extract_json('{"note": "use {this} syntax"}'),
            {"note": "use {this} syntax"},
        )

    def test_unparseable_returns_none(self):
        self.assertIsNone(BaseOrchestrator.extract_json("I cannot help with that."))
        self.assertIsNone(BaseOrchestrator.extract_json(""))


class DatabaseBackedTest(unittest.TestCase):
    """Base for tests that need a real SQLite file."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        build_test_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)


class AssignmentTests(DatabaseBackedTest):

    def setUp(self):
        super().setUp()
        self.jobs = JobRepository(db_path=self.db)
        self.staff = StaffRepository(db_path=self.db)
        self.orch = AssignmentOrchestrator(self.jobs, self.staff)
        self.job_id = self.jobs.create(1, "2023 BMW M4", "Ceramic Coating", 850)

    def test_cannot_start_an_unassigned_job(self):
        result = self.orch.transition(1, self.job_id, "In Progress", "tester")
        self.assertFalse(result.ok)
        self.assertEqual(result["error"], "unassigned")

    def test_cannot_skip_straight_to_completed(self):
        result = self.orch.transition(1, self.job_id, "Completed", "tester")
        self.assertFalse(result.ok)
        self.assertEqual(result["error"], "invalid_transition")

    def test_happy_path(self):
        self.assertTrue(self.orch.assign(1, self.job_id, 1, "tester").ok)
        self.assertTrue(self.orch.transition(1, self.job_id, "In Progress", "tester").ok)
        self.assertTrue(self.orch.transition(1, self.job_id, "Completed", "tester").ok)
        self.assertEqual(self.jobs.get(1, self.job_id)["status"], "Completed")

    def test_completed_is_terminal(self):
        self.orch.assign(1, self.job_id, 1, "tester")
        self.orch.transition(1, self.job_id, "In Progress", "tester")
        self.orch.transition(1, self.job_id, "Completed", "tester")
        self.assertFalse(self.orch.transition(1, self.job_id, "In Progress", "tester").ok)

    def test_force_overrides_the_state_machine(self):
        """Deliberate admin override must still be possible — and still audited."""
        self.orch.assign(1, self.job_id, 1, "tester")
        result = self.orch.transition(1, self.job_id, "Completed", "boss", force=True)
        self.assertTrue(result.ok)
        self.assertEqual(self.jobs.history(1, self.job_id)[0]["to_status"], "Completed")

    def test_assignment_writes_both_columns(self):
        """Legacy `technician` text stays in sync with the new FK."""
        self.orch.assign(1, self.job_id, 2, "tester")
        job = self.jobs.get(1, self.job_id)
        self.assertEqual(job["assigned_staff_id"], 2)
        self.assertEqual(job["technician"], "Carlos Diaz")

    def test_unknown_staff_is_rejected(self):
        self.assertFalse(self.orch.assign(1, self.job_id, 999, "tester").ok)

    def test_transitions_are_audited(self):
        self.orch.assign(1, self.job_id, 1, "alice")
        self.orch.transition(1, self.job_id, "In Progress", "bob")
        history = self.jobs.history(1, self.job_id)
        self.assertEqual(history[0]["actor"], "bob")
        self.assertEqual(history[0]["from_status"], "Pending")

    def test_suggestion_prefers_the_idle_technician(self):
        busy = self.jobs.create(1, "Other Car", "Ceramic Coating", 800)
        self.orch.assign(1, busy, 1, "tester")
        self.orch.transition(1, busy, "In Progress", "tester")

        top = self.orch.suggest_technician(1, self.job_id)["top_pick"]
        self.assertEqual(top["name"], "Carlos Diaz")  # Maria is occupied

    def test_tenant_isolation(self):
        self.assertEqual(self.jobs.get(2, self.job_id), {})


class InventoryOrchestratorTests(DatabaseBackedTest):

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO inventory_items (id,studio_id,sku,name,category,quantity,unit,"
            "reorder_level,cost_per_unit,supplier) VALUES (1,1,'CER','Ceramic','Coatings',"
            "5.0,'L',2.0,5000,'Acme')"
        )
        today = date.today()
        for d in range(1, 31):
            conn.execute(
                "INSERT INTO inventory_logs (studio_id,item_id,change_qty,reason,created_at) "
                "VALUES (1,1,-1.0,?,?)",
                (f"Job #{d} Usage", f"{(today - timedelta(days=d)).isoformat()} 12:00:00"),
            )
        conn.commit()
        conn.close()
        self.repo = InventoryRepository(db_path=self.db)

    def test_forecast_survives_a_dead_model(self):
        """The whole point: numbers render when Ollama is down."""
        orch = InventoryIntelligenceOrchestrator(
            repository=self.repo, provider=NullProvider("offline for test")
        )
        result = orch.stock_forecast(1, "Test Studio")

        self.assertTrue(result["_status"]["degraded"])
        self.assertEqual(result["_status"]["source"], "deterministic")
        self.assertAlmostEqual(result["forecasts"][0]["daily_burn"], 1.0, places=1)
        self.assertAlmostEqual(result["forecasts"][0]["days_to_empty"], 5.0, places=1)
        self.assertIn("headline", result["briefing"])
        self.assertTrue(result["briefing"]["priority_actions"])

    def test_forecast_uses_the_model_when_it_is_up(self):
        canned = json.dumps({
            "headline": "Ceramic runs out Friday.",
            "narrative": "Order now.",
            "priority_actions": [{"sku": "CER", "action": "Order 30 L", "why": "5 days left"}],
            "watch_list": [], "cost_notes": [],
        })
        provider = RecordingProvider([canned])
        orch = InventoryIntelligenceOrchestrator(repository=self.repo, provider=provider)
        result = orch.stock_forecast(1, "Test Studio")

        self.assertFalse(result["_status"]["degraded"])
        self.assertEqual(result["briefing"]["headline"], "Ceramic runs out Friday.")
        # The model must have been handed finished arithmetic, not raw logs.
        self.assertIn("PRE-CALCULATED", provider.calls[0]["prompt"])
        self.assertIn("days_until_empty", provider.calls[0]["prompt"])

    def test_numbers_are_identical_with_and_without_ai(self):
        """The model must not be able to change a figure."""
        canned = json.dumps({"headline": "h", "narrative": "n", "priority_actions": [],
                             "watch_list": [], "cost_notes": []})
        with_ai = InventoryIntelligenceOrchestrator(
            repository=self.repo, provider=RecordingProvider([canned])
        ).stock_forecast(1, "S")
        without = InventoryIntelligenceOrchestrator(
            repository=self.repo, provider=NullProvider()
        ).stock_forecast(1, "S")

        self.assertEqual(with_ai["forecasts"], without["forecasts"])
        self.assertEqual(with_ai["health"], without["health"])

    def test_cache_returns_without_a_second_model_call(self):
        provider = RecordingProvider([json.dumps(
            {"headline": "h", "narrative": "n", "priority_actions": [],
             "watch_list": [], "cost_notes": []})])
        orch = InventoryIntelligenceOrchestrator(repository=self.repo, provider=provider)
        orch.stock_forecast(1, "S")
        second = orch.stock_forecast(1, "S")
        self.assertEqual(second["_status"]["source"], "cache")
        self.assertEqual(len(provider.calls), 1)

    def test_invalidation_forces_recompute(self):
        provider = RecordingProvider([json.dumps(
            {"headline": "h", "narrative": "n", "priority_actions": [],
             "watch_list": [], "cost_notes": []})] * 2)
        orch = InventoryIntelligenceOrchestrator(repository=self.repo, provider=provider)
        orch.stock_forecast(1, "S")
        orch.invalidate(1)
        orch.stock_forecast(1, "S")
        self.assertEqual(len(provider.calls), 2)

    def test_stock_adjustment_writes_an_audit_log(self):
        new_qty = self.repo.adjust_stock(1, 1, -2.0, "Job #99 Usage")
        self.assertEqual(new_qty, 3.0)
        self.assertTrue(
            any("#99" in e["reason"] for e in self.repo.consumption_events(1, 1))
        )

    def test_stock_never_goes_negative(self):
        self.assertEqual(self.repo.adjust_stock(1, 1, -999.0, "oops"), 0.0)


class DVIPipelineTests(DatabaseBackedTest):
    """End-to-end Phase 2 with a stubbed vision model."""

    def setUp(self):
        super().setUp()
        self.jobs = JobRepository(db_path=self.db)
        self.dvi = DVIRepository(db_path=self.db)
        self.job_id = self.jobs.create(1, "2023 BMW M4", "Ceramic Coating", 850, customer_id=1)
        self.tmpdir = tempfile.mkdtemp()

        self.inspection_id = self.dvi.create_inspection(1, self.job_id, "maria")
        self.photo_path = os.path.join(self.tmpdir, "hood.jpg")
        with open(self.photo_path, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"0" * 512)  # minimal JPEG-ish bytes
        self.photo_id = self.dvi.add_photo(
            1, self.inspection_id, "hood.jpg", self.photo_path, "hood", "maria"
        )

    def _orchestrator(self, responses):
        return DVIOrchestrator(
            dvi_repository=self.dvi,
            job_repository=self.jobs,
            provider=RecordingProvider(responses),
            upload_base=self.tmpdir,
        )

    VISION_OK = json.dumps({
        "image_quality": "good",
        "panel_observed": "hood",
        "overall_condition": "fair",
        "findings": [
            {"defect_type": "swirl_marks", "panel": "hood", "severity": 3,
             "confidence": 0.85, "description": "Circular swirls across the centre."},
            {"defect_type": "deep_scratch", "panel": "hood", "severity": 4,
             "confidence": 0.7, "description": "A scratch near the badge."},
        ],
    })
    SUMMARY_OK = json.dumps({
        "customer_summary": "We found some paint defects.",
        "condition_headline": "Fair condition",
        "recommendations": [{"service": "Paint Correction", "reason": "Restores gloss",
                             "priority": "high"}],
        "technician_notes": ["Two-stage correction on the hood."],
    })

    def test_full_pipeline(self):
        result = self._orchestrator([self.VISION_OK, self.SUMMARY_OK]).analyze(1, self.inspection_id)

        self.assertEqual(len(result["findings"]), 2)
        self.assertGreater(result["total_upcharge_cents"], 0)
        self.assertFalse(result["_status"]["degraded"])
        self.assertLess(result["condition_score"], 10.0)
        self.assertEqual(result["summary"]["condition_headline"], "Fair condition")

    def test_out_of_vocabulary_defects_are_dropped_not_guessed(self):
        noisy = json.dumps({
            "image_quality": "good",
            "findings": [
                {"defect_type": "micro_marring_haze", "panel": "hood", "severity": 2,
                 "confidence": 0.9, "description": "invented defect"},
                {"defect_type": "swirl_marks", "panel": "hood", "severity": 2,
                 "confidence": 0.9, "description": "real defect"},
            ],
        })
        result = self._orchestrator([noisy, self.SUMMARY_OK]).analyze(1, self.inspection_id)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["defect_type"], "swirl_marks")

    def test_clean_panel_produces_no_findings(self):
        clean = json.dumps({"image_quality": "good", "overall_condition": "excellent",
                            "findings": []})
        summary = json.dumps({"customer_summary": "Vehicle is in great shape.",
                              "condition_headline": "Excellent", "recommendations": [],
                              "technician_notes": []})
        result = self._orchestrator([clean, summary]).analyze(1, self.inspection_id)

        self.assertEqual(result["findings"], [])
        self.assertEqual(result["total_upcharge_cents"], 0)
        self.assertEqual(result["condition_score"], 10.0)

    def test_severity_is_clamped_to_the_scale(self):
        wild = json.dumps({"image_quality": "good", "findings": [
            {"defect_type": "dent", "panel": "hood", "severity": 99,
             "confidence": 5.0, "description": "x"}]})
        result = self._orchestrator([wild, self.SUMMARY_OK]).analyze(1, self.inspection_id)
        self.assertEqual(result["findings"][0]["severity"], 5)
        self.assertEqual(result["findings"][0]["confidence"], 1.0)

    def test_poor_image_quality_is_surfaced(self):
        blurry = json.dumps({"image_quality": "poor", "findings": []})
        summary = json.dumps({"customer_summary": "s", "condition_headline": "h",
                              "recommendations": [], "technician_notes": []})
        result = self._orchestrator([blurry, summary]).analyze(1, self.inspection_id)
        self.assertEqual(len(result["quality_flags"]), 1)

    def test_summary_degrades_but_findings_survive(self):
        """Vision worked, the text model died — findings must still be priced."""
        result = self._orchestrator([self.VISION_OK]).analyze(1, self.inspection_id)
        self.assertTrue(result["_status"]["degraded"])
        self.assertEqual(len(result["findings"]), 2)
        self.assertGreater(result["total_upcharge_cents"], 0)
        self.assertEqual(result["summary"]["_generated_by"], "deterministic_fallback")

    def test_total_vision_failure_raises(self):
        orch = DVIOrchestrator(
            dvi_repository=self.dvi, job_repository=self.jobs,
            provider=RecordingProvider([], available=True), upload_base=self.tmpdir,
        )
        with self.assertRaises(LLMUnavailableError):
            orch.analyze(1, self.inspection_id)

    def test_analysis_requires_a_photo(self):
        empty = self.dvi.create_inspection(1, self.job_id, "maria")
        orch = self._orchestrator([self.VISION_OK])
        with self.assertRaises(OrchestratorError):
            orch.analyze(1, empty)

    def test_accepted_findings_become_an_estimate(self):
        orch = self._orchestrator([self.VISION_OK, self.SUMMARY_OK])
        orch.analyze(1, self.inspection_id)

        for finding in self.dvi.findings(self.inspection_id):
            orch.decide(1, finding["id"], "accepted", "gina")

        result = orch.to_estimate(1, self.inspection_id, "gina")
        self.assertGreater(result["estimate_id"], 0)

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        estimate = conn.execute(
            "SELECT * FROM estimates WHERE id=?", (result["estimate_id"],)
        ).fetchone()
        items = conn.execute(
            "SELECT * FROM estimate_items WHERE estimate_id=?", (result["estimate_id"],)
        ).fetchall()
        conn.close()

        self.assertEqual(estimate["customer_name"], "Dana Reyes")  # pulled from the job
        self.assertEqual(estimate["vehicle"], "2023 BMW M4")
        self.assertTrue(len(items) >= 1)
        self.assertEqual(estimate["subtotal"] + estimate["tax_amount"], estimate["total"])

    def test_conversion_requires_an_accepted_finding(self):
        orch = self._orchestrator([self.VISION_OK, self.SUMMARY_OK])
        orch.analyze(1, self.inspection_id)
        with self.assertRaises(OrchestratorError):
            orch.to_estimate(1, self.inspection_id, "gina")

    def test_rejects_unsupported_upload_types(self):
        self.assertFalse(DVIOrchestrator.is_allowed_image("notes.pdf"))
        self.assertFalse(DVIOrchestrator.is_allowed_image("script.exe"))
        self.assertTrue(DVIOrchestrator.is_allowed_image("photo.HEIC"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
