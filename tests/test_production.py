"""Production behaviour: shared lockout, sessions, error pages, headers, admin commands."""

from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from src.config import Config
from src.security import LoginThrottle
from tests.test_phase5 import AppTestCase


class SharedLoginThrottleTests(unittest.TestCase):
    """Two workers = two LoginThrottle objects over one database."""

    def test_failures_are_shared_between_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            worker_a, worker_b = LoginThrottle(3, 60, 120, db), LoginThrottle(3, 60, 120, db)
            key = worker_a.key("9.9.9.9", "Owner")
            worker_a.record_failure(key)
            worker_b.record_failure(key)
            self.assertEqual(worker_a.locked_for(key), 0)
            worker_a.record_failure(key)
            self.assertGreater(worker_b.locked_for(key), 0)        # the other worker sees the lock
            worker_b.record_success(key)
            self.assertEqual(worker_a.locked_for(key), 0)

    def test_different_ip_is_not_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = LoginThrottle(1, 60, 120, os.path.join(tmp, "t.db"))
            t.record_failure(t.key("1.1.1.1", "owner"))
            self.assertEqual(t.locked_for(t.key("2.2.2.2", "owner")), 0)


class ProductionAppTests(AppTestCase):

    def tearDown(self):
        with self.db() as conn:
            conn.execute("DELETE FROM login_throttle")
            conn.execute("UPDATE studios SET password=? WHERE username='shinepro'",
                         (__import__("src.auth", fromlist=["hash_password"]).hash_password("shine123"),))

    def test_lockout_after_repeated_failures(self):
        c = self.app.test_client()
        for _ in range(Config.AUTH_MAX_LOGIN_ATTEMPTS):
            c.post("/login", data={"username": "shinepro", "password": "wrong"})
        page = c.post("/login", data={"username": "shinepro", "password": "shine123"}).get_data(as_text=True)
        self.assertIn("Too many failed attempts", page)

    def test_password_reset_ends_existing_sessions(self):
        from src import manage
        c = self.login()
        self.assertEqual(c.get("/dashboard").status_code, 200)
        with mock.patch("sys.stdin", io.StringIO("Brand#New2026\n")), redirect_stdout(io.StringIO()):
            manage.main(["reset-password", "--username", "shinepro", "--password-stdin"])
        r = c.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.app.test_client().post(
            "/login", data={"username": "shinepro", "password": "Brand#New2026"}).status_code, 302)

    def test_removed_staff_is_signed_out(self):
        from src.auth import hash_password
        with self.db() as conn:
            conn.execute("DELETE FROM staff WHERE username='t_gone'")
            conn.execute("INSERT INTO staff (studio_id, name, role, username, password) "
                         "VALUES (1,'Gone','technician','t_gone',?)", (hash_password("Gone#2026x"),))
        c = self.login("t_gone", "Gone#2026x")
        with self.db() as conn:
            conn.execute("DELETE FROM staff WHERE username='t_gone'")
        self.assertEqual(c.get("/jobs").status_code, 302)

    def test_error_pages_are_friendly_and_json_for_api(self):
        c = self.login()
        page = c.get("/no-such-page")
        self.assertEqual(page.status_code, 404)
        self.assertIn("Page not found", page.get_data(as_text=True))
        self.assertNotIn("Traceback", page.get_data(as_text=True))
        api = c.get("/no-such-page", headers={"Accept": "application/json"})
        self.assertEqual(api.get_json(), {"error": "Page not found", "status": 404})

    def test_plain_text_errors_get_the_error_page(self):
        r = self.login().get("/estimates/999999")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 404)
        self.assertIn("ERROR 404", body)
        self.assertIn("Estimate not found", body)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertTrue(r.headers["X-Request-ID"])

    def test_favicon(self):
        r = self.app.test_client().get("/favicon.ico")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))

    def test_unhandled_exception_shows_reference_not_traceback(self):
        c = self.login()
        with mock.patch("src.controller.render_template", side_effect=RuntimeError("boom secret detail")):
            r = c.get("/dashboard")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 500)
        self.assertIn("Something went wrong", body)
        self.assertNotIn("boom secret detail", body)
        self.assertRegex(body, r"Reference: [0-9a-f]{32}")
        self.assertEqual(r.headers["X-Request-ID"], re.search(r"Reference: ([0-9a-f]{32})", body).group(1))

    def test_security_headers(self):
        r = self.login().get("/dashboard")
        csp = r.headers["Content-Security-Policy"]
        for directive in ("default-src 'self'", "object-src 'none'", "base-uri 'self'", "form-action 'self'"):
            self.assertIn(directive, csp)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_staff_username_cannot_shadow_an_owner(self):
        c = self.login()
        page = c.get("/staff/new").get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)
        data = {"name": "Imposter", "username": "velvetauto", "password": "Strong#Pass1", "role": "technician"}
        if csrf:
            data["csrf"] = csrf.group(1)
        body = c.post("/staff/new", data=data).get_data(as_text=True)
        self.assertIn("already taken", body)

    def test_unknown_username_still_costs_a_hash(self):
        from src import auth
        with mock.patch.object(auth, "verify_password", wraps=auth.verify_password) as spy:
            self.app.test_client().post("/login", data={"username": "nobody-here", "password": "x"})
        self.assertEqual(spy.call_count, 1)

    def test_inventory_names_are_not_executable(self):
        with self.db() as conn:
            conn.execute("INSERT INTO inventory_items (studio_id, name, sku, quantity, unit, cost_per_unit) "
                         "VALUES (1, ?, 'X1', 1, 'pcs', 100)", ("x');alert(1);//",))
        page = self.login().get("/inventory").get_data(as_text=True)
        self.assertNotIn("openAdjustModal('", page)
        self.assertIn('data-name="x&#39;);alert(1);//"', page)


class ManageCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = Config.DB_PATH
        Config.DB_PATH = os.path.join(self.tmp.name, "m.db")

    def tearDown(self):
        Config.DB_PATH = self.saved
        self.tmp.cleanup()

    def run_cmd(self, argv, stdin=""):
        from src import manage
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out):
            manage.main(argv)
        return out.getvalue()

    def test_create_studio_then_backup(self):
        out = self.run_cmd(["create-studio", "--name", "Autofiera", "--city", "Hyderabad", "--owner", "Owner",
                            "--username", "Autofiera", "--currency", "inr", "--password-stdin"], "Strong#Pass1\n")
        self.assertIn("Currency: INR", out)
        conn = sqlite3.connect(Config.DB_PATH)
        row = conn.execute("SELECT username, password FROM studios").fetchone()
        self.assertEqual(row[0], "autofiera")
        self.assertTrue(row[1].startswith("pbkdf2:"))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

        dest = os.path.join(self.tmp.name, "backups")
        for _ in range(3):
            self.run_cmd(["backup", "--dest", dest, "--keep", "2"])
        files = os.listdir(dest)
        self.assertLessEqual(len(files), 2)
        copy = sqlite3.connect(os.path.join(dest, sorted(files)[-1]))
        self.assertEqual(copy.execute("SELECT name FROM studios").fetchone()[0], "Autofiera")
        copy.close()

    def test_weak_password_and_duplicate_rejected(self):
        args = ["create-studio", "--name", "A", "--city", "B", "--owner", "C", "--username", "dup", "--password-stdin"]
        with self.assertRaises(SystemExit):
            self.run_cmd(args, "weak\n")
        self.run_cmd(args, "Strong#Pass1\n")
        with self.assertRaises(SystemExit):
            self.run_cmd(args, "Strong#Pass1\n")


if __name__ == "__main__":
    unittest.main()
