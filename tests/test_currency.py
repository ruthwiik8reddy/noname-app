"""Each studio shows money in the currency it chose (Account → Currency)."""

from __future__ import annotations

import re
import unittest

from src.services import currency
from tests.test_phase5 import AppTestCase


class FormattingTests(unittest.TestCase):

    def test_rupees_use_indian_grouping(self):
        self.assertEqual(currency.group(180000, "INR"), "1,80,000.00")
        self.assertEqual(currency.group(12345678.5, "INR"), "1,23,45,678.50")
        self.assertEqual(currency.group(999, "INR", 0), "999")

    def test_others_use_thousands(self):
        self.assertEqual(currency.group(180000, "USD"), "180,000.00")
        self.assertEqual(currency.group(-1234.567, "EUR"), "-1,234.57")

    def test_outside_a_request_defaults_to_dollars(self):
        self.assertEqual(currency.money(1199, 0), "$1,199")
        self.assertEqual(currency.money_cents(-250), "-$2.50")


class StudioCurrencyTests(AppTestCase):

    def _csrf(self, client):
        page = client.get("/account").get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def _set(self, client, code):
        return client.post("/account/currency", data={"csrf": self._csrf(client), "currency": code})

    def tearDown(self):
        with self.db() as conn:
            conn.execute("DELETE FROM studio_settings WHERE key='currency'")

    def test_owner_picks_rupees_and_every_page_follows(self):
        c = self.login()
        self.assertIn("US Dollar ($)", c.get("/account").get_data(as_text=True))
        r = self._set(c, "INR")
        self.assertEqual(r.status_code, 302)
        self.assertIn("saved=Currency", r.headers["Location"])

        account = c.get("/account").get_data(as_text=True)
        self.assertIn('<option value="INR" selected>', account)
        self.assertIn("₹1,80,000", account)            # the example in the help text

        for page in ("/dashboard", "/jobs", "/estimates", "/inventory", "/estimates/new", "/invoices/new"):
            html = c.get(page).get_data(as_text=True)
            self.assertNotRegex(html, r"\$\d", page)       # no dollar amounts left on the page
        base = c.get("/dashboard").get_data(as_text=True)
        self.assertIn('"code": "INR"', base)               # page scripts format with it too
        self.assertIn('"locale": "en-IN"', base)

    def test_each_studio_keeps_its_own_currency(self):
        self._set(self.login(), "INR")
        other = self.login("velvetauto", "velvet123")
        self.assertIn('"code": "USD"', other.get("/dashboard").get_data(as_text=True))

    def test_invoices_use_the_studio_symbol(self):
        c = self.login()
        self._set(c, "INR")
        form = c.get("/invoices/new").get_data(as_text=True)
        self.assertIn("Price (₹)", form)
        r = c.post("/invoices/new", data={"customer_name": "Asha", "tax_percent": "18",
                                          "item_name": ["Ceramic"], "item_desc": [""],
                                          "item_qty": ["1"], "item_price": ["150000"]})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        doc = c.get(r.headers["Location"].rstrip("/") + "/document").get_data(as_text=True)
        self.assertIn("₹1,77,000.00", doc)                # 1,50,000 + 18%

    def test_rejects_unknown_currency_and_needs_csrf(self):
        c = self.login()
        r = self._set(c, "XYZ")
        self.assertIn("error=", r.headers["Location"])
        self.assertEqual(c.post("/account/currency", data={"currency": "INR"}).status_code, 403)
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM studio_settings WHERE key='currency'").fetchone())

    def test_public_approval_page_uses_the_studio_currency(self):
        from src.services.repositories.approvals import Approvals
        c = self.login()
        self._set(c, "INR")
        with self.db() as conn:
            cid = conn.execute("INSERT INTO customers (studio_id, name, phone) VALUES (1,'Ravi','9876500000')").lastrowid
            vid = conn.execute("INSERT INTO vehicles (studio_id, customer_id, make_model, license_plate) "
                               "VALUES (1,?,'Hyundai Creta','TS09XY1234')", (cid,)).lastrowid
            eid = conn.execute("INSERT INTO estimates (studio_id, customer_id, vehicle_id, customer_name, customer_email, customer_phone, "
                               "vehicle, subtotal, tax_percent, tax_amount, total, status) "
                               "VALUES (1,?,?,'Ravi','','9876500000','Creta',15000000,0,0,15000000,'Draft')", (cid, vid)).lastrowid
            conn.execute("INSERT INTO estimate_items (estimate_id, name, quantity, unit_price, total) "
                         "VALUES (?, 'Full PPF', 1, 15000000, 15000000)", (eid,))
        token, _version = Approvals().issue(1, eid, "test")
        page = self.app.test_client().get(f"/approvals/{token}").get_data(as_text=True)
        self.assertIn("₹1,50,000.00", page)


if __name__ == "__main__":
    unittest.main()
