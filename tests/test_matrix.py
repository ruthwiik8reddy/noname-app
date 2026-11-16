"""
test_matrix.py — the app tested from every angle, generated from its own route map.

  A. every page × every kind of visitor      never crashes; strangers are kept out
  B. every page with a record id, as another studio      never shows the other studio's data
  C. every action × hostile request           no change without a session, a same-site origin and a token
  D. every action, signed in to studio 2, aimed at studio 1's records      studio 1 is untouched
  E. every form × hostile and odd input       no crash; nothing executable is ever rendered
  F–I. money, durations, passwords, redirects, totals, tracking stages: edge cases by the hundred

Runs against a fresh demo database with AI switched to its fallbacks, so it is
deterministic and needs no model.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import random
import re
import sqlite3
import tempfile
from decimal import Decimal

import pytest

from src.config import Config

# ── one app for the whole module (route map is needed at collection time) ────

_saved = {k: getattr(Config, k) for k in ("DB_PATH", "PRIVATE_UPLOAD_ROOT", "PRIVATE_STORAGE", "SEED_DEMO", "AI_ENABLED")}
_tmp = tempfile.mkdtemp(prefix="bayq-matrix-")
os.environ["AGENT_SCHEDULER"] = "0"
Config.DB_PATH = os.path.join(_tmp, "boot.db")
Config.SEED_DEMO = True
from src.app import create_app  # noqa: E402

APP = create_app()
APP.config["TESTING"] = True
for _k, _v in _saved.items():
    setattr(Config, _k, _v)

RULES = sorted((r for r in APP.url_map.iter_rules() if r.endpoint != "static"), key=lambda r: (r.rule, r.endpoint))
GET_RULES = [r for r in RULES if "GET" in r.methods]
POST_RULES = [r for r in RULES if r.methods & {"POST", "PUT", "PATCH", "DELETE"}]

# Pages anyone may open (they still refuse bad tokens).
PUBLIC = {"main.login", "main.logout", "health", "favicon", "approvals.public", "portal_admin.public_tracking",
          "portal_admin.public_status", "invoices.public_invoice", "invoices.public_logo",
          "invoices.public_invoice_pdf", "invoices.public_png", "portal.retired_portal", "main.index",
          "main.customer_tracking", "main.customer_dvi", "main.warranty_card", "main.cust_chat",
          "main.customer_inspection", "main.tracking_status"}

MARK = "Zq1marker"           # appears only in studio 1's records
HOST = {"Origin": "http://localhost"}

# Tables holding studio data (logs and audit trails excluded: they may legitimately grow).
CORE = ["bays", "bookings", "contact_permissions", "customers", "dvi_findings", "dvi_inspections", "dvi_photos",
        "estimate_approval_versions", "estimates", "followups", "inventory_items", "inventory_logs", "invoice_templates",
        "invoices", "job_assignments", "job_cost_plans", "job_labor", "job_material_entries", "job_tracking_links",
        "jobs", "leads", "media", "notes", "owner_requests", "products", "reminders", "services", "staff",
        "staff_cost_rates", "studio_settings", "vehicle_warranties", "vehicles", "warranty_claims"]


def _db():
    conn = sqlite3.connect(Config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def state(studio_id=None) -> str:
    """Fingerprint of studio data — all studios, or one."""
    h = hashlib.sha256()
    with _db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in CORE:
            if t not in tables:
                continue
            where = "" if studio_id is None else f" WHERE studio_id={int(studio_id)}"
            for row in conn.execute(f"SELECT * FROM {t}{where} ORDER BY rowid"):
                h.update(repr(tuple(row)).encode())
        for child, parent, key in (("estimate_items", "estimates", "estimate_id"), ("invoice_items", "invoices", "invoice_id")):
            q = f"SELECT c.* FROM {child} c JOIN {parent} p ON p.id=c.{key}"
            q += "" if studio_id is None else f" WHERE p.studio_id={int(studio_id)}"
            for row in conn.execute(q + " ORDER BY c.rowid"):
                h.update(repr(tuple(row)).encode())
        studio_q = "SELECT * FROM studios" + ("" if studio_id is None else f" WHERE id={int(studio_id)}")
        for row in conn.execute(studio_q + " ORDER BY id"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _insert(conn, table, **values):
    cols = ", ".join(values)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values())).lastrowid


def _seed_records(conn, sid, tag):
    """One of everything for a studio. `tag` makes studio 1's records recognisable."""
    ids = {"studio": sid}
    ids["customer"] = _insert(conn, "customers", studio_id=sid, name=f"Cust {tag}", phone=f"90000{sid}0001",
                              email=f"c{sid}@example.com")
    ids["vehicle"] = _insert(conn, "vehicles", studio_id=sid, customer_id=ids["customer"], make_model=f"Car {tag}",
                             license_plate=f"TS0{sid}AB{sid}234")
    service = conn.execute("SELECT id FROM services WHERE studio_id=? LIMIT 1", (sid,)).fetchone()[0]
    ids["service"] = service
    ids["bay"] = conn.execute("SELECT id FROM bays WHERE studio_id=? LIMIT 1", (sid,)).fetchone()[0]
    ids["estimate"] = _insert(conn, "estimates", studio_id=sid, customer_id=ids["customer"], vehicle_id=ids["vehicle"],
                              customer_name=f"Cust {tag}", customer_email="", customer_phone=f"90000{sid}0001",
                              vehicle=f"Car {tag}", subtotal=100000, tax_percent=18, tax_amount=18000,
                              total=118000, status="Draft")
    _insert(conn, "estimate_items", estimate_id=ids["estimate"], name=f"Item {tag}", quantity=1,
            unit_price=100000, total=100000)
    ids["booking"] = _insert(conn, "bookings", studio_id=sid, customer_id=ids["customer"], vehicle_id=ids["vehicle"],
                             customer_name=f"Cust {tag}", customer_phone=f"90000{sid}0001", vehicle=f"Car {tag}",
                             service_id=service, bay_id=ids["bay"], date="2031-01-15", time_slot="10:00 AM",
                             status="Pending")
    tech = conn.execute("SELECT id FROM staff WHERE studio_id=? AND role='technician' LIMIT 1", (sid,)).fetchone()[0]
    ids["tech"] = tech
    ids["job"] = _insert(conn, "jobs", studio_id=sid, car=f"Car {tag}", service=f"Svc {tag}", status="In Progress",
                         technician="tech", price=5000, customer_id=ids["customer"], vehicle_id=ids["vehicle"],
                         assigned_staff_id=tech)
    ids["invoice"] = _insert(conn, "invoices", studio_id=sid, number=f"T-{sid}-001", customer_name=f"Cust {tag}",
                             subtotal=100000, tax_percent=0, tax_amount=0, total=100000, status="Draft",
                             public_token=f"tok{sid}" + "x" * 29)
    _insert(conn, "invoice_items", invoice_id=ids["invoice"], name=f"Line {tag}", quantity=1, unit_price=100000,
            total=100000)
    ids["lead"] = _insert(conn, "leads", studio_id=sid, name=f"Lead {tag}", phone=f"80000{sid}0001")
    ids["item"] = _insert(conn, "inventory_items", studio_id=sid, sku=f"SKU{sid}", name=f"Stock {tag}", quantity=10,
                          unit="pcs", cost_per_unit=500)
    ids["product"] = _insert(conn, "products", studio_id=sid, name=f"Prod {tag}", cost_per_sqft=5,
                             labour_per_sqft=3)
    ids["reminder"] = _insert(conn, "reminders", studio_id=sid, customer_id=ids["customer"], message=f"Rem {tag}",
                              due_date="2031-02-01")
    ids["note"] = _insert(conn, "notes", studio_id=sid, job_id=ids["job"], content=f"Note {tag}")
    ids["media"] = _insert(conn, "media", studio_id=sid, job_id=ids["job"], filename=f"m{sid}.png",
                           original_name=f"Media {tag}.png")
    ids["inspection"] = _insert(conn, "dvi_inspections", studio_id=sid, job_id=ids["job"])
    return ids


@pytest.fixture(scope="module")
def world():
    saved = {k: getattr(Config, k) for k in _saved}
    Config.DB_PATH = os.path.join(_tmp, "matrix.db")
    Config.PRIVATE_UPLOAD_ROOT = os.path.join(_tmp, "private")
    Config.PRIVATE_STORAGE = os.path.join(_tmp, "private", "documents")
    Config.SEED_DEMO = True
    Config.AI_ENABLED = "0"
    from src.seed import initialize_db
    initialize_db()
    from src.auth import hash_password
    with _db() as conn:
        s1 = _seed_records(conn, 1, MARK)
        s2 = _seed_records(conn, 2, "other")
        _insert(conn, "staff", studio_id=1, name="Gina Manager", role="general_manager", username="gm1",
                password=hash_password("tech123"))
    yield {1: s1, 2: s2}
    for k, v in saved.items():
        setattr(Config, k, v)


_SESSIONS = {}


def client(who):
    """A fresh test client signed in as `who` (None = not signed in).

    Each role logs in once (password hashing is deliberately slow); later
    clients start from a copy of that session cookie.
    """
    c = APP.test_client()
    if not who:
        return c
    if who not in _SESSIONS:
        user, pw = {"owner1": ("shinepro", "shine123"), "tech1": ("maria", "tech123"), "gm1": ("gm1", "tech123"),
                    "owner2": ("apexdetail", "apex123"), "tech2": ("carlos", "tech123")}[who]
        r = c.post("/login", data={"username": user, "password": pw}, headers=HOST)
        assert r.status_code == 302 and "/login" not in r.headers["Location"], (who, r.status_code)
        _SESSIONS[who] = c.get_cookie("session").value
    c.set_cookie("session", _SESSIONS[who])
    return c


def csrf_of(c):
    page = c.get("/account").get_data(as_text=True)
    m = re.search(r'name="csrf" value="([^"]+)"', page)
    if not m:
        with c.session_transaction() as s:
            return s.get("work_records_csrf", "")
    return m.group(1)


ARG_SOURCE = {"jid": "job", "job_id": "job", "bid": "booking", "booking_id": "booking", "eid": "estimate",
              "estimate_id": "estimate", "customer_id": "customer", "vid": "vehicle", "invoice_id": "invoice",
              "lead_id": "lead", "item_id": "item", "pid": "product", "reminder_id": "reminder", "note_id": "note",
              "media_id": "media", "inspection_id": "inspection", "staff_id": "tech", "record_id": "booking",
              "studio_id": "studio"}
ARG_FIXED = {"kind": "bookings", "operation": "start", "action": "approve", "agent_name": "inventory",
             "panel": "hood", "filename": "nope.png", "unused": "x", "token": "t" * 43, "fid": "999999",
             "finding_id": "999999", "photo_id": "999999", "report_id": "999999", "request_id": "999999"}


def url_for_rule(rule, ids):
    path = rule.rule
    for arg in rule.arguments:
        value = ids.get(ARG_SOURCE[arg]) if arg in ARG_SOURCE else ARG_FIXED.get(arg)
        if value is None:
            return None
        path = re.sub(r"<(?:[^:<>]+:)?" + arg + r">", str(value), path)
    return path


def rid(rule):
    return f"{sorted(rule.methods - {'HEAD', 'OPTIONS'})[0]} {rule.rule}"


# ── A. every page × every visitor ─────────────────────────────────────────────

ROLES = ["anon", "owner1", "gm1", "tech1", "owner2"]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("rule", GET_RULES, ids=rid)
def test_A_every_page_every_visitor(world, rule, role):
    path = url_for_rule(rule, world[2 if role == "owner2" else 1])
    if path is None:
        pytest.skip("no record for this kind of id")
    c = client(None if role == "anon" else role)
    r = c.get(path, headers={"Accept": "text/html"})
    assert r.status_code < 500, f"{path} crashed for {role}: {r.status_code}"
    if role == "anon" and rule.endpoint not in PUBLIC:
        assert r.status_code in (301, 302, 303, 401, 403, 404, 405, 410), f"{path} open to strangers ({r.status_code})"
        if r.status_code in (302, 303):
            assert "/login" in r.headers["Location"] or rule.endpoint in ("main.index",), r.headers["Location"]


# ── B. another studio never sees studio 1 ─────────────────────────────────────

ID_GET_RULES = [r for r in GET_RULES if r.arguments & set(ARG_SOURCE)]


@pytest.mark.parametrize("who", ["owner2", "tech2"])
@pytest.mark.parametrize("rule", ID_GET_RULES, ids=rid)
def test_B_no_cross_studio_reads(world, rule, who):
    path = url_for_rule(rule, world[1])
    if path is None:
        pytest.skip("no record for this kind of id")
    r = client(who).get(path)
    assert r.status_code < 500
    assert MARK not in r.get_data(as_text=True), f"studio 2 ({who}) saw studio 1 data at {path}"


# ── C. hostile requests change nothing ────────────────────────────────────────

GENERIC = {"name": "Changed", "phone": "123", "status": "Completed", "action": "create-link", "confirmed": "yes",
           "quantity": "5", "amount": "5", "content": "x", "message": "x", "price": "1", "currency": "EUR",
           "mode": "exact", "stage": "Coating", "decision": "accepted", "signer": "X", "request_key": "k" * 24}

ATTACKS = {
    "no-session": lambda c, t: (None, {"Origin": "http://localhost"}),
    "cross-site-origin": lambda c, t: (t, {"Origin": "https://evil.example"}),
    "cross-site-referer": lambda c, t: (t, {"Referer": "https://evil.example/page"}),
    "null-origin-no-token": lambda c, t: (None, {"Origin": "null"}),
}
SKIP_C = {"main.login", "main.logout"}          # signing in/out is meant to work without a session


@pytest.mark.parametrize("attack", list(ATTACKS))
@pytest.mark.parametrize("rule", POST_RULES, ids=rid)
def test_C_hostile_posts_change_nothing(world, rule, attack):
    if rule.endpoint in SKIP_C:
        pytest.skip("login form")
    path = url_for_rule(rule, world[1])
    if path is None:
        pytest.skip("no record for this kind of id")
    c = client(None if attack == "no-session" else "owner1")
    token = csrf_of(c) if attack != "no-session" else None
    token, headers = ATTACKS[attack](c, token)
    data = dict(GENERIC, job_id=world[1]["job"], customer_id=world[1]["customer"], vehicle_id=world[1]["vehicle"],
                staff_id=world[1]["tech"])
    if token:
        data["csrf"] = token
    before = state()
    r = c.post(path, data=data, headers=headers)
    assert r.status_code < 500, f"{path} crashed under {attack}"
    assert state() == before, f"{path} changed data under {attack} ({r.status_code})"


CONTROLS = {
    "new customer": lambda ids: ("/customers/new", {"name": "Control Person", "phone": "7000000001"}),
    "job status": lambda ids: (f"/jobs/{ids['job']}/status", {"status": "Completed"}),
    "currency": lambda ids: ("/account/currency", {"currency": "INR"}),
    "stock adjust": lambda ids: (f"/inventory/{ids['item']}/adjust", {"change_qty": "3", "reason": "count"}),
    "new lead": lambda ids: ("/leads/new", {"name": "Control Lead", "phone": "7000000002"}),
    "tracking link": lambda ids: ("/portal-admin/", {"action": "create-link", "job_id": ids["job"]}),
}


@pytest.mark.parametrize("name", list(CONTROLS))
def test_C_control_legitimate_requests_do_change_data(world, name):
    """Proves the two checks above can see a change: the same requests, done properly, succeed."""
    c = client("owner1")
    path, data = CONTROLS[name](world[1])
    before = state(1)
    r = c.post(path, data=dict(data, csrf=csrf_of(c)), headers=HOST)
    assert r.status_code < 400, (path, r.status_code)
    assert state(1) != before, f"{name}: a legitimate request didn't change anything — the harness is blind"


# ── D. studio 2 can't change studio 1 ─────────────────────────────────────────

@pytest.mark.parametrize("rule", POST_RULES, ids=rid)
def test_D_no_cross_studio_writes(world, rule):
    if rule.endpoint in SKIP_C:
        pytest.skip("login form")
    path = url_for_rule(rule, world[1])
    if path is None:
        pytest.skip("no record for this kind of id")
    c = client("owner2")
    data = dict(GENERIC, csrf=csrf_of(c), job_id=world[1]["job"], customer_id=world[1]["customer"],
                vehicle_id=world[1]["vehicle"], staff_id=world[1]["tech"], estimate_id=world[1]["estimate"],
                booking_id=world[1]["booking"], invoice_id=world[1]["invoice"], timer_id=1, entry_id=1)
    before = state(1)
    r = c.post(path, data=data, headers=HOST)
    assert r.status_code < 500, f"{path} crashed"
    assert state(1) == before, f"studio 2 changed studio 1 via {path} ({r.status_code})"


# ── E. every form × hostile and odd input ─────────────────────────────────────

PAYLOADS = {
    "script": "<script>alert('xss')</script>",
    "img-onerror": '"><img src=x onerror=alert(1)>',
    "js-url": "javascript:alert(1)",
    "sql-quote": "' OR '1'='1' --",
    "sql-drop": "Robert'); DROP TABLE customers;--",
    "template": "{{7*7}}{% print 1 %}",
    "format": "%s%s%n%x{0}",
    "unicode": "आयुष 🚗 Ünïcødé ‮reversed",
    "long": "A" * 20000,
    "empty": "",
    "spaces": "    ",
    "null-byte": "abc\x00def",
    "negative": "-1",
    "huge-number": "1e308",
    "nan": "NaN",
    "path": "../../../../etc/passwd",
    "crlf": "x\r\nSet-Cookie: pwned=1",
}

FORMS = {
    "/customers/new": ["name", "phone", "email", "notes", "vehicle_make_model"],
    "/leads/new": ["name", "phone", "email", "source", "notes", "service_interest", "budget_hint"],
    "/inventory/new": ["name", "sku", "category", "unit", "quantity", "cost_per_unit", "reorder_level"],
    "/products/new": ["name", "category", "cost_per_sqft", "labour_per_sqft", "markup_pct", "notes"],
    "/reminders/new": ["message", "due_date", "customer_id"],
    "/notes/add": ["content", "job_id", "note_type"],
    "/bookings/new": ["customer_name", "customer_phone", "vehicle", "service_id", "date", "time_slot", "notes"],
    "/estimates/new": ["customer_name", "customer_email", "customer_phone", "vehicle", "item_name", "item_desc",
                       "item_qty", "item_price", "tax_percent", "notes"],
    "/invoices/new": ["customer_name", "customer_phone", "vehicle", "item_name", "item_desc", "item_qty",
                      "item_price", "tax_percent", "discount", "notes", "number", "customer_tax_id"],
    "/staff/new": ["name", "username", "password", "role"],
    "/account/currency": ["currency"],
    "/account/sms-config": ["twilio_sid", "twilio_token", "twilio_from", "whatsapp_country_code"],
    "/dispatch/job/new": ["car", "service", "price", "staff_id"],
    "CUSTOMER/vehicles/new": ["make_model", "year", "color", "license_plate", "vin"],
    "JOB/status": ["status"],
    "BOOKING/status": ["status"],
    "ITEM/adjust": ["change", "reason"],
}


def _form_path(path, ids):
    return (path.replace("CUSTOMER", f"/customers/{ids['customer']}").replace("JOB", f"/jobs/{ids['job']}")
                .replace("BOOKING", f"/bookings/{ids['booking']}").replace("ITEM", f"/inventory/{ids['item']}"))


@pytest.mark.parametrize("payload", list(PAYLOADS))
@pytest.mark.parametrize("form", list(FORMS))
def test_E_forms_survive_hostile_input(world, form, payload):
    c = client("owner1")
    data = {field: PAYLOADS[payload] for field in FORMS[form]}
    data["csrf"] = csrf_of(c)
    r = c.post(_form_path(form, world[1]), data=data, headers=HOST)
    assert r.status_code < 500, f"{form} crashed on {payload}"
    if payload == "crlf":
        assert "pwned" not in r.headers.get("Set-Cookie", "")


LISTINGS = ["/customers", "/leads", "/inventory", "/products", "/dashboard", "/bookings", "/estimates", "/invoices/",
            "/staff", "/jobs", "/reminders", "/dispatch/", "/account", f"/customers/{{customer}}"]


@pytest.mark.parametrize("page", LISTINGS)
def test_E_hostile_input_is_never_rendered_live(world, page):
    """Runs after the form attacks above (same module): whatever was stored must come back escaped."""
    body = client("owner1").get(page.replace("{customer}", str(world[1]["customer"]))).get_data(as_text=True)
    assert "<script>alert('xss')</script>" not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert 'href="javascript:alert(1)"' not in body


# ── F. money formatting ───────────────────────────────────────────────────────

from src.services import currency  # noqa: E402

AMOUNTS = [0, 0.004, 0.005, 1, 9.99, 999, 1000, 99999.5, 100000, 180000, 1234567.891, 99999999.99, -1, -180000.5]


@pytest.mark.parametrize("amount", AMOUNTS)
@pytest.mark.parametrize("code", list(currency.CURRENCIES))
def test_F_grouping_round_trips(code, amount):
    text = currency.group(amount, code)
    expected = Decimal(str(amount)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert Decimal(text.replace(",", "")) == expected
    whole = text.lstrip("-").split(".")[0]
    groups = whole.split(",")
    if currency.js_locale(code) == "en-IN":
        assert len(groups[-1]) <= 3 and all(len(g) == 2 for g in groups[1:-1]) and len(groups[0]) <= 2 or len(groups) == 1
    else:
        assert all(len(g) == 3 for g in groups[1:]) and 1 <= len(groups[0]) <= 3


@pytest.mark.parametrize("code", list(currency.CURRENCIES))
def test_F_symbol_and_sign_placement(code):
    with APP.test_request_context():
        from flask import g
        g.currency_code = code
        assert currency.money(-5) == "-" + currency.symbol(code) + "5.00"
        assert currency.money_cents(12345) == currency.symbol(code) + currency.group(123.45, code)
        assert currency.money(1500, 0).endswith("1,500")


@pytest.mark.parametrize("raw", [None, "", "abc", "12x", float("nan"), "1e3", object()])
def test_F_garbage_amounts_show_as_zero_or_value(raw):
    out = currency.group(raw, "USD")
    assert out in ("0.00", "1,000.00", "NaN") or re.fullmatch(r"-?[\d,]+\.\d\d", out), out


# ── G. assistant: durations, currency words, money parsing ────────────────────

from src.services.assistant.actions import ActionError, _currency_rule, _duration  # noqa: E402
from src.services.orchestrators.studio_assistant_orchestrator import _fix_duration_units  # noqa: E402

DURATIONS = [
    ("takes 2 days", 2, 48), ("2 day job", 2, 48), ("about 3 days", 3, 72), ("1 day", 1, 24),
    ("45 minutes", 45, 0.75), ("30 mins", 30, 0.5), ("90 min", 90, 1.5), ("1 week", 1, 168),
    ("half a day is 4 hours", 4, 4), ("4 hours", 4, 4), ("2 days", 48, 48), ("45 minutes", 0.75, 0.75),
    ("no units here", 6, 6), ("1.5 days", 1.5, 36), ("2 Days", 2, 48), ("10 MINUTES", 10, 0.17),
    ("2 days for the coating and price 2", 2, 48), ("2 weeks", 2, 336),
]


@pytest.mark.parametrize("message,given,expected", DURATIONS)
def test_G_spoken_durations_become_hours(message, given, expected):
    steps = [{"action": "add_service", "args": {"name": "X", "price": 1, "duration_hours": given}}]
    _fix_duration_units(message, steps)
    assert steps[0]["args"]["duration_hours"] == expected


@pytest.mark.parametrize("args", [{}, {"duration_hours": None}, {"duration_hours": "abc"}, "not-a-dict", None])
def test_G_duration_fix_ignores_garbage(args):
    steps = [{"action": "add_service", "args": args}]
    _fix_duration_units("takes 2 days", steps)          # must not raise


CURRENCY_WORDS = [("rupees", "INR"), ("Rupee", "INR"), ("rs", "INR"), ("Rs.", "INR"), ("₹", "INR"), ("inr", "INR"),
                  ("dollars", "USD"), ("$", "USD"), ("usd", "USD"), ("euro", "EUR"), ("€", "EUR"), ("pounds", "GBP"),
                  ("£", "GBP"), ("dirham", "AED"), ("aed", "AED"), ("sgd", "SGD"), ("  INR  ", "INR")]


@pytest.mark.parametrize("word,code", CURRENCY_WORDS)
def test_G_currency_words(word, code):
    assert _currency_rule(word) == code


@pytest.mark.parametrize("word", ["bitcoin", "", "XYZ", "rupiah", "<script>", "INR; DROP TABLE"])
def test_G_unknown_currency_rejected(word):
    with pytest.raises(ActionError):
        _currency_rule(word)


@pytest.mark.parametrize("hours,text", [(24, "1 day (24 h)"), (48, "2 days (48 h)"), (0.5, "0.5 h"), (8, "8 h"),
                                        (36, "36 h"), (168, "7 days (168 h)")])
def test_G_duration_labels(hours, text):
    assert _duration(hours) == text


# ── H. passwords, redirects, lockout ──────────────────────────────────────────

from src.auth import validate_password_strength  # noqa: E402
from src.security import LoginThrottle, safe_next  # noqa: E402

STRONG = ["Abcdef1!", "Autofiera#2026", "Zz9_zzzz", "P@ssw0rdLong", "Ünï1code!A"]
WEAK = ["", "short1!", "alllowercase1!", "ALLUPPER1!", "NoDigits!!", "NoSpecial123", "12345678", "        ", "Aa1!"]


@pytest.mark.parametrize("pw", STRONG)
def test_H_strong_passwords_accepted(pw):
    assert validate_password_strength(pw) == []


@pytest.mark.parametrize("pw", WEAK)
def test_H_weak_passwords_rejected(pw):
    assert validate_password_strength(pw)


SAFE = ["/dashboard", "/jobs?x=1", "/portal/", "/a/b/c", "/invoices/3#top"]
UNSAFE = ["//evil.com", "https://evil.com", "http:/evil", "javascript:alert(1)", "\\\\evil", "/\\evil", "evil.com",
          "", None, 42, "/foo\nbar", "/\tx", "///evil.com", "\x00/x", " /x"]


@pytest.mark.parametrize("target", SAFE)
def test_H_safe_redirects_kept(target):
    assert safe_next(target) == target


@pytest.mark.parametrize("target", UNSAFE)
def test_H_unsafe_redirects_replaced(target):
    assert safe_next(target, "/home") == "/home"


@pytest.mark.parametrize("max_attempts", [1, 2, 3, 5, 10])
@pytest.mark.parametrize("shared", [False, True])
def test_H_lockout_exactly_at_limit(max_attempts, shared, tmp_path):
    t = LoginThrottle(max_attempts, 60, 120, str(tmp_path / "t.db") if shared else None)
    k = t.key("10.0.0.1", "User")
    for _ in range(max_attempts - 1):
        t.record_failure(k)
    assert t.locked_for(k) == 0
    t.record_failure(k)
    assert t.locked_for(k) > 0
    assert t.locked_for(t.key("10.0.0.1", "other")) == 0
    assert t.locked_for(t.key("10.0.0.2", "user")) == 0


# ── I. invoice totals, money parsing, tracking stages ─────────────────────────

from src.routes.invoice_routes import _parse_money  # noqa: E402
from src.services.repositories.invoice_repository import compute_totals  # noqa: E402
from src.services.tracking_stages import NAMES, WORK_STAGES, stage_index, timeline  # noqa: E402

_rng = random.Random(20261114)
TOTAL_CASES = [([{"quantity": _rng.choice([1, 2, 3, 0.5, 1.25, 10]), "unit_price": _rng.randint(0, 5_000_000)}
                 for _ in range(_rng.randint(1, 6))], _rng.choice([0, 5, 12, 18, 28, 8.25]), _rng.randint(0, 9_000_000))
               for _ in range(60)]


@pytest.mark.parametrize("items,tax,discount", TOTAL_CASES)
def test_I_invoice_totals_are_consistent(items, tax, discount):
    t = compute_totals(items, tax, discount)
    assert t["subtotal"] == sum(int(round(i["quantity"] * i["unit_price"])) for i in items)
    assert 0 <= t["discount"] <= t["subtotal"]
    assert t["total"] == t["subtotal"] - t["discount"] + t["tax_amount"]
    assert abs(t["tax_amount"] - (t["subtotal"] - t["discount"]) * tax / 100) <= 0.5
    assert all(isinstance(v, int) for v in t.values())


MONEY_IN = [("0", 0), ("1", 100), ("1.5", 150), ("1,234.50", 123450), ("$99", 9900), ("  12.34 ", 1234),
            ("1,80,000", 18000000), ("0.01", 1), ("1.005", 101), ("", 0)]


@pytest.mark.parametrize("raw,cents", MONEY_IN)
def test_I_money_parsing(raw, cents):
    assert _parse_money(raw) == cents


@pytest.mark.parametrize("raw", ["abc", "1.2.3", "--5", "1e400", "nan", "inf", "-5"])
def test_I_money_parsing_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        _parse_money(raw)


STAGE_CASES = ([({"status": "Pending"}, 0), ({"status": "Completed"}, 5), ({"status": "completed "}, 5),
                ({"status": "In Progress"}, 1), ({"status": None}, 0), ({}, 0),
                ({"status": "In Progress", "tracking_stage": "Bogus"}, 1),
                ({"status": "Pending", "tracking_stage": "Curing"}, 0),
                ({"status": "Completed", "tracking_stage": "Washing"}, 5)]
               + [({"status": "In Progress", "tracking_stage": s}, NAMES.index(s)) for s in WORK_STAGES])


@pytest.mark.parametrize("job,index", STAGE_CASES)
def test_I_tracking_stage(job, index):
    assert stage_index(job) == index
    states = [s["state"] for s in timeline(job)]
    assert states.count("active") == 1 and states.index("active") == index
    assert states[:index] == ["completed"] * index and set(states[index + 1:]) <= {"upcoming"}
