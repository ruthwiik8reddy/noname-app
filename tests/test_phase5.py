"""
tests/test_phase5.py — security hardening, invoice styles, Studio Assistant.

No Ollama required. Model-dependent paths run twice: once with a
RecordingProvider returning canned JSON, once with a dead provider, because
the degraded branch is what runs when the model is down.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.security import LoginThrottle, safe_redirect_target
from src.services.assistant import fallback
from src.services.invoices import scan
from src.services.invoices.style import normalize_style, readable_on
from src.services.llm.base import NullProvider, RecordingProvider
from src.services.repositories.invoice_repository import compute_totals


# ── pure units ────────────────────────────────────────────────────────────────

class SafeRedirectTests(unittest.TestCase):

    def test_relative_paths_allowed(self):
        self.assertEqual(safe_redirect_target("/jobs?x=1", "/d"), "/jobs?x=1")

    def test_external_and_tricky_targets_rejected(self):
        for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)", "evil.com", ""):
            self.assertEqual(safe_redirect_target(bad, "/d"), "/d", bad)


class LoginThrottleTests(unittest.TestCase):

    def test_locks_after_max_failures_and_success_resets(self):
        t = LoginThrottle(3, 60, 120)
        k = t.key("1.2.3.4", "Owner")
        for _ in range(2):
            t.record_failure(k)
        self.assertEqual(t.locked_for(k), 0)
        t.record_failure(k)
        self.assertGreater(t.locked_for(k), 0)
        t.record_success(k)
        self.assertEqual(t.locked_for(k), 0)

    def test_usernames_are_case_insensitive(self):
        t = LoginThrottle(1, 60, 120)
        t.record_failure(t.key("ip", "Owner"))
        self.assertGreater(t.locked_for(t.key("ip", "owner")), 0)


class InvoiceStyleTests(unittest.TestCase):

    def test_bad_values_fall_back(self):
        s = normalize_style({"layout": "<script>", "accent_color": "red", "title_size": "huge",
                             "labels": {"total": "Amount Due", "evil_key": "x"}, "font": "Comic Sans"})
        self.assertEqual(s["layout"], "classic")
        self.assertEqual(s["accent_color"], "#1F2937")
        self.assertEqual(s["font"], "sans")
        self.assertEqual(s["labels"]["total"], "Amount Due")
        self.assertNotIn("evil_key", s["labels"])

    def test_short_hex_expanded_and_unreadable_text_fixed(self):
        s = normalize_style({"header_bg": "#fff", "header_text_color": "#FEFEFE"})
        self.assertEqual(s["header_bg"], "#FFFFFF")
        self.assertEqual(s["header_text_color"], readable_on("#FFFFFF"))

    def test_totals_are_computed_not_trusted(self):
        t = compute_totals([{"quantity": 2, "unit_price": 1050}, {"quantity": 1, "unit_price": 900}], 10, 1000)
        self.assertEqual(t, {"subtotal": 3000, "discount": 1000, "tax_amount": 200, "total": 2200})

    def test_discount_cannot_exceed_subtotal(self):
        self.assertEqual(compute_totals([{"quantity": 1, "unit_price": 500}], 0, 9999)["total"], 0)


def _banner_invoice_png(path: str, band=(20, 60, 140), accent=(200, 30, 40)) -> None:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 1000), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 800, 130], fill=band)               # coloured top band
    d.rectangle([40, 300, 760, 340], fill=accent)          # table header row
    for y in range(380, 700, 40):
        d.line([40, y, 760, y], fill=(30, 30, 30), width=2)
    img.save(path)


class ScanTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_palette_reads_band_and_accent_from_pixels(self):
        src = os.path.join(self.dir, "inv.png")
        _banner_invoice_png(src)
        p = scan.palette(src)
        self.assertIsNotNone(p["top_band"])
        r, g, b = (int(p["top_band"][i:i + 2], 16) for i in (1, 3, 5))
        self.assertTrue(b > r and b > g, p["top_band"])
        self.assertIsNotNone(p["accent"])

    def test_header_alignment_from_ink(self):
        from PIL import Image, ImageDraw
        centered = os.path.join(self.dir, "c.png"); split = os.path.join(self.dir, "s.png")
        for path, boxes in ((centered, [(300, 60, 940, 110), (420, 130, 820, 150)]),
                            (split, [(70, 60, 420, 110), (70, 120, 360, 140), (900, 60, 1170, 110)])):
            img = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(img)
            for b in boxes:
                d.rectangle(b, fill=(40, 40, 40))
            img.save(path)
        self.assertEqual(scan.palette(centered)["header_align"], "centered")
        self.assertEqual(scan.palette(split)["header_align"], "split")

    def test_model_banner_without_band_is_overruled(self):
        from src.services.orchestrators.invoice_style_orchestrator import InvoiceStyleOrchestrator
        style, notes = InvoiceStyleOrchestrator._merge(
            {}, {"layout": "banner", "title_text": "Luxe", "business_name": "LUXE"},
            {"top_band": None, "accent": "#6D28D9", "header_align": "centered"})
        self.assertEqual(style["layout"], "centered")
        self.assertEqual(style["title_text"], "INVOICE")   # business name rejected as title
        self.assertTrue(notes)

    def test_customer_values_never_become_labels(self):
        from src.services.orchestrators.invoice_style_orchestrator import InvoiceStyleOrchestrator
        style, notes = InvoiceStyleOrchestrator._merge({}, {"labels": {
            "vehicle": "2022 BMW X5", "total": "Balance Due", "bill_to": "sam@x.com",
            "issue_date": "12/10/2026", "invoice_number": "Statement #"}}, {})
        self.assertEqual(style["labels"]["vehicle"], "Vehicle")
        self.assertEqual(style["labels"]["bill_to"], "Bill To")
        self.assertEqual(style["labels"]["issue_date"], "Date")
        self.assertEqual(style["labels"]["total"], "Balance Due")
        self.assertEqual(style["labels"]["invoice_number"], "Statement #")
        self.assertTrue(any("customer details" in n for n in notes))

    def test_non_image_rejected(self):
        bad = os.path.join(self.dir, "x.png")
        with open(bad, "w") as fh:
            fh.write("<html>not an image</html>")
        with self.assertRaises(scan.ScanError):
            scan.to_image(bad, os.path.join(self.dir, "out.png"))

    def test_disallowed_extension_rejected(self):
        with self.assertRaises(scan.ScanError):
            scan.to_image(os.path.join(self.dir, "x.html"), os.path.join(self.dir, "out.png"))


class DVIEchoFilterTests(unittest.TestCase):
    """Measured on qwen2.5vl:7b: most false positives were our own definition echoed back."""

    def test_echoed_definitions_are_flagged(self):
        from src.services.prompts.dvi_prompts import DVIPrompts as P
        self.assertTrue(P.echoes_instructions(
            "Fine circular hairlines visible as a 'spider-web' halo around a reflected light source."))
        self.assertTrue(P.echoes_instructions("Many fine circular hairlines, visible around a reflected light source."))

    def test_real_observations_pass(self):
        from src.services.prompts.dvi_prompts import DVIPrompts as P
        for text in ("Several dark jagged lines run across the centre of the hood toward the windshield.",
                     "Small chip exposing grey primer on the leading edge of the bonnet.",
                     "A distinct scratch on the rear bumper, lower left near the wheel arch.",
                     "Paint looks clean."):
            self.assertFalse(P.echoes_instructions(text), text)

    def test_normalizer_drops_echoed_finding(self):
        from src.services.orchestrators import DVIOrchestrator
        kept = DVIOrchestrator()._normalize_findings([
            {"defect_type": "swirl_marks", "severity": 2, "confidence": 0.8,
             "description": "Fine circular hairlines visible as a 'spider-web' halo around a reflected light source."},
            {"defect_type": "rock_chip", "severity": 2, "confidence": 0.6,
             "description": "Small chip exposing grey primer on the leading edge of the bonnet."},
        ], {"panel": "hood"})
        self.assertEqual([k["defect_type"] for k in kept], ["rock_chip"])


class FallbackParserTests(unittest.TestCase):

    def test_common_requests(self):
        acts = fallback.parse("add service Ceramic Pro for $1,199 and 8 hours; raise all prices by 5%")
        self.assertEqual(acts[0]["action"], "add_service")
        self.assertEqual(acts[0]["args"]["name"], "Ceramic Pro")
        self.assertEqual(acts[0]["args"]["price"], "1,199")
        self.assertEqual(acts[0]["args"]["duration_hours"], "8")
        self.assertEqual(acts[1], {"action": "adjust_all_prices", "args": {"percent": "5"}})

    def test_lowering_is_negative(self):
        self.assertEqual(fallback.parse("lower prices by 10%")[0]["args"]["percent"], "-10")

    def test_unknown_request_gives_nothing(self):
        self.assertEqual(fallback.parse("make me a coffee"), [])


# ── integration: real app, temp database ──────────────────────────────────────

class AppTestCase(unittest.TestCase):
    """Boots the real app against a fresh seeded database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls._saved = (Config.DB_PATH, Config.PRIVATE_STORAGE)
        Config.DB_PATH = os.path.join(cls.tmp, "test.db")
        Config.PRIVATE_STORAGE = os.path.join(cls.tmp, "private")
        os.environ["AGENT_SCHEDULER"] = "0"
        from src import create_app
        cls.app = create_app()
        cls.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        Config.DB_PATH, Config.PRIVATE_STORAGE = cls._saved

    def db(self):
        conn = sqlite3.connect(Config.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def login(self, username="shinepro", password="shine123"):
        c = self.app.test_client()
        r = c.post("/login", data={"username": username, "password": password})
        self.assertEqual(r.status_code, 302, r.data[:300])
        return c


class SecurityIntegrationTests(AppTestCase):

    def test_seed_passwords_are_hashed_and_plaintext_rejected(self):
        with self.db() as conn:
            pw = conn.execute("SELECT password FROM studios WHERE username='shinepro'").fetchone()[0]
            self.assertTrue(pw.startswith(("pbkdf2:", "scrypt:")))
            conn.execute("UPDATE studios SET password='plain' WHERE username='velvetauto'")
        r = self.app.test_client().post("/login", data={"username": "velvetauto", "password": "plain"})
        self.assertIn(b"Invalid", r.data)

    def test_cross_site_post_blocked_same_site_allowed(self):
        c = self.login()
        r = c.post("/assistant/plan", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/invoices/1/status", data={"status": "Paid"}, headers={"Origin": "http://localhost"})
        self.assertNotEqual(r.status_code, 403)

    def test_estimate_approval_needs_token(self):
        with self.db() as conn:
            cur = conn.execute("INSERT INTO estimates (studio_id, customer_name, customer_email, customer_phone, "
                               "vehicle, status, approval_token) VALUES (2,'C','c@x','1','V','Sent','tok_abc123456789')")
            est_id = cur.lastrowid
        anon = self.app.test_client()
        self.assertEqual(anon.post(f"/estimates/{est_id}/approve", data={"action": "approve", "signature": "x"}).status_code, 404)
        self.assertEqual(anon.post("/estimates/approve/tok_abc123456789", data={"action": "approve", "signature": "Me"}).status_code, 200)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM estimates WHERE id=?", (est_id,)).fetchone()[0], "Approved")

    def test_sms_settings_are_per_studio_and_never_touch_env(self):
        env = os.path.join(os.path.dirname(Config.TEMPLATE_FOLDER), ".env")
        before = os.path.getmtime(env) if os.path.exists(env) else None
        c = self.login()
        c.post("/account/sms-config", data={"twilio_sid": "AC" + "a" * 32, "twilio_from": "+15551234567",
                                            "twilio_token": "b" * 32})
        with self.db() as conn:
            rows = dict(conn.execute("SELECT key, value FROM studio_settings WHERE studio_id=1 "
                                     "AND key LIKE 'twilio%'").fetchall())
            other = conn.execute("SELECT COUNT(*) FROM studio_settings WHERE studio_id=2 AND key LIKE 'twilio%'").fetchone()[0]
        self.assertEqual(rows.get("twilio_from_number"), "+15551234567")
        self.assertEqual(other, 0)
        self.assertEqual(before, os.path.getmtime(env) if os.path.exists(env) else None)

    def test_removed_staff_lose_access_immediately(self):
        from src.auth import hash_password
        with self.db() as conn:
            conn.execute("INSERT INTO staff (studio_id,name,role,username,password) VALUES (1,'Temp','service_advisor','temp_sa',?)",
                         (hash_password("Temp!Pass123"),))
        c = self.login("temp_sa", "Temp!Pass123")
        self.assertEqual(c.get("/invoices/").status_code, 200)
        with self.db() as conn:
            conn.execute("UPDATE staff SET role='technician' WHERE username='temp_sa'")
        self.assertEqual(c.get("/invoices/").status_code, 302)          # role changed → logged out
        c = self.login("temp_sa", "Temp!Pass123")
        with self.db() as conn:
            conn.execute("DELETE FROM staff WHERE username='temp_sa'")
        r = c.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_sms_settings_reject_newline_injection(self):
        c = self.login()
        r = c.post("/account/sms-config", data={"twilio_sid": "AC" + "a" * 32 + "\nSECRET_KEY=x",
                                                "twilio_from": "+15551234567"})
        self.assertIn("error=", r.headers["Location"])


class InvoiceIntegrationTests(AppTestCase):

    def test_learn_style_from_upload_with_model(self):
        from src.routes import invoice_routes
        invoice_routes._style_ai._provider = RecordingProvider([json.dumps({
            "layout": "banner", "font": "serif", "title_text": "TAX INVOICE", "table_style": "striped",
            "business_name": "Shine Pro Detailing", "business_lines": ["1 Sunset Blvd", "(310) 555-0100"],
            "labels": {"bill_to": "Invoice To", "total": "Amount Due", "rate": ""},
            "footer_text": "Drive clean.", "number_prefix": "SP-",
        })])
        path = os.path.join(self.tmp, "mine.png")
        _banner_invoice_png(path)
        c = self.login()
        with open(path, "rb") as fh:
            r = c.post("/invoices/template/upload", data={"invoice": (fh, "mine.png")},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=learned", r.headers["Location"])

        preview = c.get("/invoices/template/preview").get_data(as_text=True)
        self.assertIn("TAX INVOICE", preview)
        self.assertIn("Invoice To", preview)
        self.assertIn("Amount Due", preview)
        self.assertIn(">Rate<", preview)            # empty label from the model kept the default
        self.assertIn("Drive clean.", preview)
        self.assertIn("Georgia", preview)            # serif font
        # The uploaded file is private: not in /static, only served to its studio.
        mine = c.get("/invoices/template/source")
        self.assertEqual(mine.status_code, 200)
        other = self.login("apexdetail", "apex123")
        theirs = other.get("/invoices/template/source")
        self.assertNotEqual(mine.data, theirs.data)   # 404, or their own (different) upload
        self.assertEqual(self.app.test_client().get("/invoices/template/source").status_code, 302)

    def test_learn_style_degrades_without_model(self):
        from src.routes import invoice_routes
        invoice_routes._style_ai._provider = NullProvider("offline")
        path = os.path.join(self.tmp, "mine2.png")
        _banner_invoice_png(path, band=(10, 120, 30))
        c = self.login("apexdetail", "apex123")
        with open(path, "rb") as fh:
            r = c.post("/invoices/template/upload", data={"invoice": (fh, "inv.png")},
                       content_type="multipart/form-data")
        self.assertIn("degraded=1", r.headers["Location"])
        html = c.get("/invoices/template/preview").get_data(as_text=True)
        self.assertIn("Apex Detail Studio", html)    # studio's own name filled in

    def test_create_invoice_pdf_and_public_link(self):
        c = self.login()
        r = c.post("/invoices/new", data={
            "customer_name": "Alex <b>Bold</b>", "number": "",
            "item_name": ["Full Detail", "Wax"], "item_desc": ["", ""],
            "item_qty": ["1", "2"], "item_price": ["250", "19.99"],
            "tax_percent": "10", "discount": "",
        })
        self.assertEqual(r.status_code, 302, r.data[:400])
        inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        with self.db() as conn:
            inv = conn.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
        self.assertEqual(inv["subtotal"], 25000 + 3998)
        self.assertEqual(inv["total"], 28998 + 2900)

        doc = c.get(f"/invoices/{inv_id}/document").get_data(as_text=True)
        self.assertIn("Alex &lt;b&gt;Bold&lt;/b&gt;", doc)  # escaped
        pdf = c.get(f"/invoices/{inv_id}/pdf")
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))

        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/invoices/{inv_id}").status_code, 302)    # login required
        self.assertEqual(anon.get(f"/i/{inv['public_token']}").status_code, 200)
        self.assertEqual(anon.get("/i/not-a-token").status_code, 404)
        other = self.login("apexdetail", "apex123")
        self.assertEqual(other.get(f"/invoices/{inv_id}").status_code, 404)  # other studio

    def test_invoice_rejects_bad_numbers(self):
        c = self.login()
        r = c.post("/invoices/new", data={"customer_name": "X", "item_name": ["A"], "item_desc": [""],
                                          "item_qty": ["-3"], "item_price": ["10"], "tax_percent": "0"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Quantity", r.data)


def _realistic_invoice_png(path: str) -> None:
    """A text invoice drawn with the PDF library's bundled fonts, so this runs anywhere."""
    import reportlab
    from PIL import Image, ImageDraw, ImageFont
    fdir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    f = lambda size, bold=False: ImageFont.truetype(os.path.join(fdir, "_eb_____.pfb" if bold else "_er_____.pfb"), size)
    img = Image.new("RGB", (1240, 1754), "white"); d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1240, 220], fill=(20, 83, 45))
    d.text((70, 60), "PINE RIDGE DETAIL", font=f(50, True), fill="white")
    d.text((70, 140), "5 Forest Lane, Boise ID", font=f(26), fill="white")
    d.text((900, 80), "INVOICE", font=f(52, True), fill="white")
    d.text((70, 280), "Bill To", font=f(28, True), fill=(20, 83, 45))
    d.text((70, 325), "Morgan Ellis", font=f(28), fill="black")
    d.text((70, 365), "2021 Subaru Outback", font=f(26), fill=(90, 90, 90))
    d.text((760, 280), "Invoice # PR-5521", font=f(26), fill="black")
    d.text((760, 320), "Date 09/30/2026", font=f(26), fill="black")
    d.rectangle([70, 470, 1170, 530], fill=(20, 83, 45))
    for x, t in ((90, "Description"), (720, "Qty"), (860, "Rate"), (1020, "Amount")):
        d.text((x, 484), t, font=f(26, True), fill="white")
    y = 560
    for name, q, r in (("Exterior Hand Wash", "1", "$60.00"), ("Engine Bay Clean", "1", "$95.00"), ("Leather Treatment", "2", "$45.00")):
        for x, t in ((90, name), (725, q), (860, r), (1020, r)):
            d.text((x, y), t, font=f(26), fill="black")
        y += 56
    d.text((820, y + 40), "Total Due   $245.00", font=f(30, True), fill=(20, 83, 45))
    d.text((70, y + 160), "Payment", font=f(28, True), fill=(20, 83, 45))
    d.text((70, y + 200), "Cash or card at pickup", font=f(24), fill=(60, 60, 60))
    d.text((440, 1640), "Thank you from Pine Ridge!", font=f(28), fill=(90, 90, 90))
    img.save(path)


@unittest.skipUnless(__import__("shutil").which("tesseract"), "Tesseract OCR not installed")
class ExactInvoiceTests(AppTestCase):
    """The studio's own invoice as the page: only job-specific parts are replaced."""

    def _upload(self, client):
        from src.routes import invoice_routes
        invoice_routes._style_ai._provider = NullProvider("offline")   # exact mode needs no model
        path = os.path.join(self.tmp, "pine.png")
        _realistic_invoice_png(path)
        with open(path, "rb") as fh:
            return client.post("/invoices/template/upload", data={"invoice": (fh, "pine.png")},
                               content_type="multipart/form-data")

    def _ocr(self, png_bytes):
        from src.services.invoices import ocr
        p = os.path.join(self.tmp, "read.png"); open(p, "wb").write(png_bytes)
        return " ".join(l.text for l in ocr.read_lines(p, os.path.join(self.tmp, "read_bin.png")))

    def test_upload_builds_exact_template_and_invoices_use_it(self):
        c = self.login()
        self._upload(c)
        from src.services.repositories import InvoiceRepository
        with self.app.app_context():
            tpl = InvoiceRepository().get_template(1)
        self.assertEqual(tpl["mode"], "exact", tpl.get("exact_note"))
        self.assertEqual(tpl["exact"]["number_prefix"], "PR-")
        self.assertEqual(tpl["exact"]["table"]["text"]["font"], "Times")
        self.assertEqual(tpl["style"]["number_prefix"], "PR-")      # synced into numbering

        r = c.post("/invoices/new", data={
            "customer_name": "Riley Chen", "vehicle": "2025 Rivian R1S", "number": "",
            "item_name": ["Ceramic Coating", "Window Tint"], "item_desc": ["", ""],
            "item_qty": ["1", "1"], "item_price": ["1200", "350"], "tax_percent": "6", "discount": ""})
        inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        pdf = c.get(f"/invoices/{inv_id}/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        text = self._ocr(c.get(f"/invoices/{inv_id}/preview.png").data)
        # Their design survives…
        for kept in ("PINE RIDGE DETAIL", "Bill To", "Payment", "Thank you from Pine Ridge"):
            self.assertIn(kept.lower(), text.lower(), kept)
        # …the sample customer and items don't…
        for gone in ("Morgan Ellis", "Subaru", "Exterior Hand Wash", "$245.00", "09/30/2026"):
            self.assertNotIn(gone.lower(), text.lower(), gone)
        # …and the new invoice's data is there, numbered in their format.
        for new in ("Riley Chen", "Ceramic Coating", "$1,643.00", "PR-"):
            self.assertIn(new.lower(), text.lower(), new)

    def test_kept_page_is_private_and_mode_can_switch(self):
        c = self.login("apexdetail", "apex123")
        self._upload(c)
        own = c.get("/invoices/template/blank.png")
        self.assertEqual(own.status_code, 200)
        theirs = self.login().get("/invoices/template/blank.png")   # studio 1: never studio 2's page
        self.assertTrue(theirs.status_code == 404 or theirs.data != own.data)
        other = self.app.test_client()
        self.assertEqual(other.get("/invoices/template/blank.png").status_code, 302)
        c.post("/invoices/template/mode", data={"mode": "rebuilt"})
        from src.services.repositories import InvoiceRepository
        with self.app.app_context():
            self.assertEqual(InvoiceRepository().get_template(2)["mode"], "rebuilt")
        c.post("/invoices/template/mode", data={"mode": "exact"})
        with self.app.app_context():
            self.assertEqual(InvoiceRepository().get_template(2)["mode"], "exact")

    def test_leak_guard_blocks_unremovable_customer_text(self):
        from src.services.invoices import exact
        tpl = {"erased_texts": ["Morgan Ellis", "1"]}
        path = os.path.join(self.tmp, "pine2.png"); _realistic_invoice_png(path)
        # The untouched original still shows the customer: the guard must say so.
        self.assertIn("Morgan Ellis", exact.find_leaks(path, tpl, os.path.join(self.tmp, "w.png")))


def _gst_invoice_pdf(path: str) -> None:
    """A GST-style invoice PDF like the ones Indian invoicing tools produce (standard fonts, text layer)."""
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen.canvas import Canvas
    W, H = 595.28, 841.89
    c = Canvas(path, pagesize=(W, H))
    y = lambda top: H - top
    tan, dark, grey = HexColor("#D4A373"), HexColor("#1A1A1A"), HexColor("#666666")
    c.setFillColor(HexColor("#F7F7F2")); c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(dark); c.setFont("Helvetica-Bold", 24); c.drawRightString(555, y(70), "TAX INVOICE")
    c.setFillColor(tan); c.setFont("Helvetica", 12)
    c.drawRightString(555, y(90), "Invoice #: 0041"); c.drawRightString(555, y(105), "Date: 02-Oct-2026")
    c.setFont("Times-Bold", 28); c.drawString(40, y(150), "Glossworks - Shine Assured")
    c.setFont("Helvetica-Bold", 10); c.drawString(40, y(180), "SUPPLIER"); c.drawString(300, y(180), "BILL TO")
    c.setFillColor(dark); c.setFont("Helvetica", 11)
    c.drawString(40, y(200), "Glossworks Detailing"); c.drawString(300, y(200), "Kavya Reddy")
    c.setFillColor(grey); c.setFont("Helvetica", 9)
    c.drawString(40, y(215), "Place Of Supply: Telangana (36)")
    c.drawString(300, y(215), "GSTIN: 36AAACK1234M1Z9")
    c.drawString(300, y(230), "Address: 14 Road No. 3, Banjara Hills,")
    c.drawString(300, y(242), "Hyderabad, Telangana - 500034")
    c.setFillColor(tan); c.setFont("Helvetica-Bold", 10); c.drawString(40, y(310), "VEHICLE DETAILS")
    c.setFillColor(dark); c.setFont("Helvetica", 10); c.drawString(40, y(325), "TS08AB1234")
    c.setStrokeColor(HexColor("#1A991A")); c.setLineWidth(1.5); c.rect(300, y(332), 70, 22, stroke=1, fill=0)
    c.setFillColor(HexColor("#1A991A")); c.setFont("Helvetica-Bold", 14); c.drawString(316, y(327), "PAID")
    c.setFillColor(HexColor("#EBEBE6")); c.rect(40, y(358), 515, 26, stroke=0, fill=1)
    c.setFillColor(tan); c.setFont("Helvetica-Bold", 10)
    c.drawString(51, y(346), "DESCRIPTION"); c.drawString(280, y(346), "SAC"); c.drawRightString(545, y(346), "AMOUNT")
    c.setFillColor(dark); c.setFont("Helvetica", 10)
    c.drawString(51, y(389), "Foam Wash"); c.drawString(280, y(389), "998714"); c.drawRightString(545, y(389), "2,500.00")
    c.setStrokeColor(HexColor("#CCCCCC")); c.setLineWidth(1); c.line(40, y(408), 555, y(408))
    c.setFillColor(grey); c.drawString(382, y(443), "Taxable Value")
    c.setFillColor(dark); c.drawRightString(545, y(443), "2,500.00")
    c.setFillColor(tan); c.rect(355, y(488), 210, 30, stroke=0, fill=1)
    c.setFillColor(HexColor("#FFFFFF")); c.setFont("Helvetica-Bold", 14)
    c.drawString(366, y(473), "TOTAL"); c.drawRightString(545, y(473), "Rs. 2,500.00")
    c.setFillColor(tan); c.drawString(40, y(453), "Total In Words")
    c.setFillColor(dark); c.setFont("Helvetica-Oblique", 10); c.drawString(40, y(473), "Two Thousand Five Hundred Only")
    c.setFillColor(tan); c.setFont("Helvetica-Bold", 10); c.drawString(40, y(702), "BANK DETAILS")
    c.setFillColor(dark); c.setFont("Helvetica", 9); c.drawString(40, y(717), "BANK NAME: EXAMPLE BANK")
    c.setFont("Helvetica-Bold", 10); c.drawRightString(555, y(780), "Authorized Signatory")
    c.showPage(); c.save()


class ExactPdfInvoiceTests(AppTestCase):
    """PDF uploads are edited directly — exact fonts, vector artwork, no OCR needed."""

    def test_gst_pdf_becomes_exact_vector_template(self):
        import pypdfium2 as pdfium
        from src.routes import invoice_routes
        invoice_routes._style_ai._provider = NullProvider("offline")
        path = os.path.join(self.tmp, "gst.pdf"); _gst_invoice_pdf(path)
        c = self.login("velvetauto", "velvet123")
        with open(path, "rb") as fh:
            c.post("/invoices/template/upload", data={"invoice": (fh, "gst.pdf")}, content_type="multipart/form-data")
        from src.services.repositories import InvoiceRepository
        with self.app.app_context():
            tpl = InvoiceRepository().get_template(3)
        self.assertEqual(tpl["mode"], "exact", tpl.get("exact_note"))
        ex = tpl["exact"]
        self.assertEqual(ex["source"], "pdf")
        self.assertTrue(tpl["blank_file"].endswith(".pdf"))
        self.assertEqual(sorted(k for k in ex["slots"] if k in ("customer", "vehicle", "stamp", "amount_words")),
                         ["amount_words", "customer", "stamp", "vehicle"])
        self.assertEqual(ex["slots"]["customer"]["labels"], {"tax_id": "GSTIN: ", "address": "Address: "})
        self.assertEqual((ex["number_digits"], ex["number_last"], ex["date_format"]), (4, 41, "DD-Mon-YYYY"))
        self.assertTrue(ex["indian_numbering"])

        form = c.get("/invoices/new").get_data(as_text=True)
        self.assertIn('value="0042"', form)                      # their numbering continues
        r = c.post("/invoices/new", data={
            "customer_name": "Arjun Motors LLP", "customer_tax_id": "36abcde9999f1z2", "vehicle": "TS10XY9876",
            "customer_address": "Plot 7, Gachibowli\nHyderabad", "number": "", "issue_date": "2026-10-20",
            "item_name": ["Ceramic Coating", "PPF Full Front", "Interior Detail"], "item_desc": ["", "", ""],
            "item_qty": ["1", "1", "1"], "item_price": ["45000", "65000", "3500"], "tax_percent": "18"})
        inv_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
        pdf = c.get(f"/invoices/{inv_id}/pdf").data
        text = pdfium.PdfDocument(pdf)[0].get_textpage().get_text_range()
        for kept in ("TAX INVOICE", "Glossworks - Shine Assured", "SUPPLIER", "BILL TO", "VEHICLE DETAILS",
                     "Total In Words", "BANK NAME: EXAMPLE BANK", "Authorized Signatory"):
            self.assertIn(kept, text, kept)
        for gone in ("Kavya Reddy", "36AAACK1234M1Z9", "Banjara", "TS08AB1234", "Foam Wash", "2,500.00",
                     "Two Thousand Five Hundred", "0041", "02-Oct-2026"):
            self.assertNotIn(gone, text, gone)
        for new in ("Invoice #: 0042", "Date: 20-Oct-2026", "Arjun Motors LLP", "GSTIN: 36ABCDE9999F1Z2",
                    "Address: Plot 7, Gachibowli", "TS10XY9876", "Ceramic Coating", "998714", "45,000.00",
                    "Taxable Value", "1,13,500.00", "GST (18%)", "TOTAL", "Rs. 1,33,930.00",
                    "One Lakh Thirty Three Thousand Nine Hundred Thirty Only"):
            self.assertIn(new, text, new)
        # Their artwork stays vector: the page has no full-page picture behind it.
        page = pdfium.PdfDocument(pdf)[0]
        images = [o for o in page.get_objects() if o.type == 3]       # FPDF_PAGEOBJ_IMAGE
        self.assertEqual(images, [])


class AssistantIntegrationTests(AppTestCase):

    def _provider(self, *responses):
        from src.routes import assistant_routes
        assistant_routes._assistant._provider = RecordingProvider([json.dumps(r) for r in responses])

    def test_plan_previews_then_applies_dependent_steps(self):
        self._provider({"reply": "On it.", "actions": [
            {"action": "add_service", "args": {"name": "Ceramic Pro", "price": 1199, "duration_hours": 8}},
            {"action": "update_service", "args": {"name": "Ceramic Pro", "price": 1299}},
            {"action": "update_setting", "args": {"key": "default_tax_percent", "value": "9.5%"}},
        ]})
        c = self.login()
        plan = c.post("/assistant/plan", json={"message": "add ceramic pro then bump its price, and set tax to 9.5%"}).get_json()
        self.assertTrue(all(s["ok"] for s in plan["steps"]), plan)
        with self.db() as conn:   # dry run changed nothing
            self.assertIsNone(conn.execute("SELECT 1 FROM services WHERE name='Ceramic Pro'").fetchone())

        res = c.post(f"/assistant/requests/{plan['id']}/apply", json={}).get_json()
        self.assertEqual(res["status"], "applied", res)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT price FROM services WHERE studio_id=1 AND name='Ceramic Pro'").fetchone()[0], 1299)
            self.assertEqual(conn.execute("SELECT value FROM studio_settings WHERE studio_id=1 AND key='default_tax_percent'").fetchone()[0], "9.5")
        # Can't apply twice.
        self.assertEqual(c.post(f"/assistant/requests/{plan['id']}/apply", json={}).status_code, 409)

    def test_invalid_step_blocks_whole_plan(self):
        self._provider({"reply": "", "actions": [
            {"action": "add_bay", "args": {"name": "Bay 99"}},
            {"action": "update_setting", "args": {"key": "twilio_auth_token", "value": "x"}},
            {"action": "drop_database", "args": {}},
        ]})
        c = self.login()
        plan = c.post("/assistant/plan", json={"message": "do stuff"}).get_json()
        self.assertEqual([s["ok"] for s in plan["steps"]], [True, False, False])
        res = c.post(f"/assistant/requests/{plan['id']}/apply", json={}).get_json()
        self.assertEqual(res["status"], "failed")
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM bays WHERE name='Bay 99'").fetchone())

    def test_unrelated_step_is_flagged_and_skipped_by_default(self):
        """A valid change the owner never asked for must not ride along."""
        self._provider({"reply": "Sure.", "actions": [
            {"action": "add_bay", "args": {"name": "Review Bay"}},
            {"action": "update_setting", "args": {"key": "invoice_payment_terms_days", "value": "0"}},
        ]})
        c = self.login()
        plan = c.post("/assistant/plan", json={"message": "add a bay called Review Bay for google reviews"}).get_json()
        self.assertEqual([s["suspect"] for s in plan["steps"]], [False, True])
        self.assertTrue(plan["steps"][1]["warning"])
        res = c.post(f"/assistant/requests/{plan['id']}/apply", json={}).get_json()
        self.assertEqual(res["status"], "applied")
        self.assertEqual(len(res["steps"]), 1)
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM studio_settings WHERE studio_id=1 "
                                           "AND key='invoice_payment_terms_days' AND value='0'").fetchone())

    def test_missing_numbers_become_a_question(self):
        self._provider({"reply": "Adding it.", "actions": [
            {"action": "add_service", "args": {"name": "Window Tint"}}]})
        c = self.login()
        plan = c.post("/assistant/plan", json={"message": "add a window tint service"}).get_json()
        self.assertEqual(plan["steps"], [])
        self.assertIn("price", plan["reply"])
        self.assertEqual(plan["status"], "answered")

    def test_studio_isolation(self):
        self._provider({"reply": "", "actions": [{"action": "adjust_all_prices", "args": {"percent": 10}}]})
        with self.db() as conn:
            before = dict(conn.execute("SELECT id, price FROM services WHERE studio_id=2").fetchall())
        c = self.login()
        plan = c.post("/assistant/plan", json={"message": "raise prices 10%"}).get_json()
        c.post(f"/assistant/requests/{plan['id']}/apply", json={})
        with self.db() as conn:
            after = dict(conn.execute("SELECT id, price FROM services WHERE studio_id=2").fetchall())
        self.assertEqual(before, after)
        other = self.login("apexdetail", "apex123")
        self.assertEqual(other.post(f"/assistant/requests/{plan['id']}/apply", json={}).status_code, 409)

    def test_offline_fallback(self):
        from src.routes import assistant_routes
        assistant_routes._assistant._provider = NullProvider("offline")
        c = self.login()
        d = c.post("/assistant/plan", json={"message": "add bay Detail Bay 7"}).get_json()
        self.assertTrue(d["_status"]["degraded"])
        self.assertEqual(d["steps"][0]["action"], "add_bay")

    def test_technicians_cannot_use_assistant(self):
        c = self.login("maria", "tech123")
        self.assertEqual(c.post("/assistant/plan", json={"message": "add bay X"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
