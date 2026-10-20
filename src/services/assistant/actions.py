"""
services/assistant/actions.py — everything the Studio Assistant is allowed to do.

The model never touches the database. It proposes a list of
`{"action": name, "args": {...}}`; each one is validated here against the
studio's current data, shown to the owner as a plain-English preview, and only
applied when they press Apply — re-validated at that moment, in one
transaction, scoped to their studio.

To teach the assistant a new capability, add an Action subclass and register
it in ACTIONS. Nothing else changes: the prompt, the preview and the apply
path are all generated from this registry.

Deliberately absent: anything touching logins, staff accounts, SMS
credentials, payments or deleting customer records. Those stay behind their
own screens.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


class ActionError(ValueError):
    """The proposed action can't be applied as written. Message is shown to the owner."""


@dataclass
class Param:
    name: str
    kind: str              # "text" | "money" | "number" | "int" | "percent"
    required: bool = True
    help: str = ""
    max_len: int = 120
    lo: Optional[float] = None
    hi: Optional[float] = None


def _coerce(p: Param, value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        if p.required:
            raise ActionError(f"'{p.name}' is required.")
        return None
    if p.kind == "text":
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) > p.max_len:
            raise ActionError(f"'{p.name}' is too long (max {p.max_len} characters).")
        return text
    raw = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        num = float(raw)
    except ValueError as exc:
        raise ActionError(f"'{p.name}' must be a number, got '{value}'.") from exc
    if p.kind == "int":
        num = int(round(num))
    if p.lo is not None and num < p.lo:
        raise ActionError(f"'{p.name}' must be at least {p.lo:g}.")
    if p.hi is not None and num > p.hi:
        raise ActionError(f"'{p.name}' must be at most {p.hi:g}.")
    return num


class Action:
    name = ""
    summary = ""
    params: Tuple[Param, ...] = ()

    def validate(self, conn: sqlite3.Connection, studio_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
        args = args if isinstance(args, dict) else {}
        clean = {p.name: _coerce(p, args.get(p.name)) for p in self.params}
        return self.check(conn, studio_id, clean)

    def check(self, conn, studio_id, args):
        """Business-rule validation against current data. Return the args to apply."""
        return args

    def preview(self, args: Dict[str, Any]) -> str:
        raise NotImplementedError

    def relevance_terms(self, args: Dict[str, Any]) -> List[str]:
        """
        Words at least one of which should appear in the owner's request for
        this step to make sense. Guards against a model proposing a valid but
        unrelated change (e.g. "set invoice terms to 0 days" in reply to a
        question about Google reviews). Empty list = no check.
        """
        return []

    def apply(self, conn: sqlite3.Connection, studio_id: int, args: Dict[str, Any]) -> str:
        raise NotImplementedError

    def spec(self) -> Dict[str, Any]:
        return {"action": self.name, "does": self.summary,
                "args": {p.name: f"{p.kind}{'' if p.required else ', optional'}"
                         + (f" — {p.help}" if p.help else "") for p in self.params}}


def _words(name: Any) -> List[str]:
    """Distinctive words of a name, for relevance checks ("Ceramic Pro 5-Year" → ceramic, pro, year)."""
    return [w for w in re.findall(r"[a-z0-9]+", str(name or "").lower()) if len(w) > 2]


def _money(v: float) -> str:
    return f"${v:,.2f}".replace(".00", "")


def _find_service(conn, studio_id: int, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM services WHERE studio_id=? AND LOWER(name)=LOWER(?)",
                       (studio_id, name)).fetchone()
    if not row:
        names = [r[0] for r in conn.execute("SELECT name FROM services WHERE studio_id=?", (studio_id,))]
        raise ActionError(f"No service called '{name}'. Your services: {', '.join(names) or 'none yet'}.")
    return row


# ── Services ──────────────────────────────────────────────────────────────────

class AddService(Action):
    name = "add_service"
    summary = "Add a new service to the menu used for bookings, estimates and invoices."
    params = (Param("name", "text", max_len=80),
              Param("price", "money", help="whole dollars", lo=0, hi=100000),
              Param("duration_hours", "number", help="bay time in hours", lo=0.25, hi=72))

    def check(self, conn, studio_id, a):
        if conn.execute("SELECT 1 FROM services WHERE studio_id=? AND LOWER(name)=LOWER(?)",
                        (studio_id, a["name"])).fetchone():
            raise ActionError(f"'{a['name']}' already exists — use update_service to change it.")
        return a

    def preview(self, a):
        return f"Add service “{a['name']}” at {_money(a['price'])}, {a['duration_hours']:g} h"

    def relevance_terms(self, a):
        return ["service", "add", "new", "offer", "package"] + _words(a.get("name"))

    def apply(self, conn, studio_id, a):
        conn.execute("INSERT INTO services (studio_id, name, duration_hr, price) VALUES (?,?,?,?)",
                     (studio_id, a["name"], a["duration_hours"], int(round(a["price"]))))
        return f"Added {a['name']}."


class UpdateService(Action):
    name = "update_service"
    summary = "Change an existing service's name, price or duration."
    params = (Param("name", "text", help="current name, exactly as on the menu", max_len=80),
              Param("new_name", "text", required=False, max_len=80),
              Param("price", "money", required=False, help="whole dollars", lo=0, hi=100000),
              Param("duration_hours", "number", required=False, lo=0.25, hi=72))

    def check(self, conn, studio_id, a):
        row = _find_service(conn, studio_id, a["name"])
        if a["new_name"] is None and a["price"] is None and a["duration_hours"] is None:
            raise ActionError("Nothing to change — give a new name, price or duration.")
        if a["new_name"] and a["new_name"].lower() != row["name"].lower() and conn.execute(
                "SELECT 1 FROM services WHERE studio_id=? AND LOWER(name)=LOWER(?)",
                (studio_id, a["new_name"])).fetchone():
            raise ActionError(f"A service called '{a['new_name']}' already exists.")
        a["id"], a["name"] = row["id"], row["name"]
        a["old_price"], a["old_duration"] = row["price"], row["duration_hr"]
        return a

    def preview(self, a):
        parts = []
        if a.get("new_name"):
            parts.append(f"rename to “{a['new_name']}”")
        if a.get("price") is not None:
            parts.append(f"price {_money(a.get('old_price', 0))} → {_money(a['price'])}")
        if a.get("duration_hours") is not None:
            parts.append(f"duration {a.get('old_duration', 0):g} h → {a['duration_hours']:g} h")
        return f"Update “{a['name']}”: " + ", ".join(parts)

    def relevance_terms(self, a):
        return ["price", "cost", "charge", "rename", "duration", "hour", "minute", "$"] + _words(a.get("name"))

    def apply(self, conn, studio_id, a):
        conn.execute(
            "UPDATE services SET name=COALESCE(?, name), price=COALESCE(?, price), "
            "duration_hr=COALESCE(?, duration_hr) WHERE id=? AND studio_id=?",
            (a["new_name"], int(round(a["price"])) if a["price"] is not None else None,
             a["duration_hours"], a["id"], studio_id))
        return f"Updated {a['name']}."


class RemoveService(Action):
    name = "remove_service"
    summary = "Remove a service from the menu (only if no booking uses it)."
    params = (Param("name", "text", max_len=80),)

    def check(self, conn, studio_id, a):
        row = _find_service(conn, studio_id, a["name"])
        used = conn.execute("SELECT COUNT(*) FROM bookings WHERE studio_id=? AND service_id=?",
                            (studio_id, row["id"])).fetchone()[0]
        if used:
            raise ActionError(f"'{row['name']}' is used by {used} booking(s), so it can't be removed. "
                              f"Rename it or change its price instead.")
        a["id"], a["name"] = row["id"], row["name"]
        return a

    def preview(self, a):
        return f"Remove service “{a['name']}”"

    def relevance_terms(self, a):
        # Destructive: require an explicit verb, not just the name.
        return ["remove", "delete", "drop", "discontinue", "get rid", "stop offering", "take off"]

    def apply(self, conn, studio_id, a):
        conn.execute("DELETE FROM services WHERE id=? AND studio_id=?", (a["id"], studio_id))
        return f"Removed {a['name']}."


class AdjustAllPrices(Action):
    name = "adjust_all_prices"
    summary = "Raise or lower every service price by a percentage (negative to lower)."
    params = (Param("percent", "percent", help="e.g. 10 for +10%, -5 for -5%", lo=-50, hi=100),
              Param("round_to", "int", required=False, help="round new prices to this many dollars, e.g. 5",
                    lo=1, hi=100))

    def check(self, conn, studio_id, a):
        rows = conn.execute("SELECT id, name, price FROM services WHERE studio_id=?", (studio_id,)).fetchall()
        if not rows:
            raise ActionError("You don't have any services yet.")
        if a["percent"] == 0:
            raise ActionError("A 0% change does nothing.")
        step = a["round_to"] or 1
        a["changes"] = [
            {"id": r["id"], "name": r["name"], "old": r["price"],
             "new": max(0, int(round(r["price"] * (1 + a["percent"] / 100) / step) * step))}
            for r in rows
        ]
        return a

    def preview(self, a):
        sign = "+" if a["percent"] > 0 else ""
        lines = ", ".join(f"{c['name']} {_money(c['old'])}→{_money(c['new'])}" for c in a.get("changes", [])[:8])
        more = len(a.get("changes", [])) - 8
        return f"Change all service prices by {sign}{a['percent']:g}%: {lines}" + (f" (+{more} more)" if more > 0 else "")

    def relevance_terms(self, a):
        return ["price", "%", "percent", "rate", "cost"]

    def apply(self, conn, studio_id, a):
        for c in a["changes"]:
            conn.execute("UPDATE services SET price=? WHERE id=? AND studio_id=?", (c["new"], c["id"], studio_id))
        return f"Updated {len(a['changes'])} prices."


# ── Bays ──────────────────────────────────────────────────────────────────────

class AddBay(Action):
    name = "add_bay"
    summary = "Add a service bay (a slot that can take bookings)."
    params = (Param("name", "text", max_len=40),)

    def check(self, conn, studio_id, a):
        if conn.execute("SELECT 1 FROM bays WHERE studio_id=? AND LOWER(name)=LOWER(?)",
                        (studio_id, a["name"])).fetchone():
            raise ActionError(f"A bay called '{a['name']}' already exists.")
        return a

    def preview(self, a):
        return f"Add bay “{a['name']}”"

    def relevance_terms(self, a):
        return ["bay", "booth", "station", "lift"] + _words(a.get("name"))

    def apply(self, conn, studio_id, a):
        conn.execute("INSERT INTO bays (studio_id, name) VALUES (?,?)", (studio_id, a["name"]))
        return f"Added {a['name']}."


class RenameBay(Action):
    name = "rename_bay"
    summary = "Rename a bay."
    params = (Param("name", "text", max_len=40), Param("new_name", "text", max_len=40))

    def check(self, conn, studio_id, a):
        row = conn.execute("SELECT id, name FROM bays WHERE studio_id=? AND LOWER(name)=LOWER(?)",
                           (studio_id, a["name"])).fetchone()
        if not row:
            raise ActionError(f"No bay called '{a['name']}'.")
        a["id"], a["name"] = row["id"], row["name"]
        return a

    def preview(self, a):
        return f"Rename bay “{a['name']}” to “{a['new_name']}”"

    def relevance_terms(self, a):
        return ["bay", "rename"] + _words(a.get("new_name"))

    def apply(self, conn, studio_id, a):
        conn.execute("UPDATE bays SET name=? WHERE id=? AND studio_id=?", (a["new_name"], a["id"], studio_id))
        return f"Renamed {a['name']}."


# ── Products (film/coating catalogue used by Quick Quote) ─────────────────────

class AddProduct(Action):
    name = "add_product"
    summary = "Add a film/coating product to the Quick Quote catalogue (priced per square foot)."
    params = (Param("name", "text", max_len=80),
              Param("category", "text", required=False, help="PPF, Ceramic, Tint, Vinyl…", max_len=30),
              Param("brand", "text", required=False, max_len=40),
              Param("cost_per_sqft", "money", lo=0, hi=1000),
              Param("labour_per_sqft", "money", required=False, lo=0, hi=1000),
              Param("markup_percent", "percent", required=False, lo=0, hi=500),
              Param("warranty_years", "int", required=False, lo=0, hi=25))

    def check(self, conn, studio_id, a):
        if conn.execute("SELECT 1 FROM products WHERE studio_id=? AND LOWER(name)=LOWER(?) AND active=1",
                        (studio_id, a["name"])).fetchone():
            raise ActionError(f"A product called '{a['name']}' already exists.")
        return a

    def preview(self, a):
        bits = [f"{_money(a['cost_per_sqft'])}/sq ft material"]
        if a.get("labour_per_sqft"):
            bits.append(f"{_money(a['labour_per_sqft'])}/sq ft labour")
        if a.get("markup_percent"):
            bits.append(f"{a['markup_percent']:g}% markup")
        if a.get("warranty_years"):
            bits.append(f"{a['warranty_years']}-yr warranty")
        return f"Add product “{a['name']}” ({a.get('category') or 'PPF'}): " + ", ".join(bits)

    def relevance_terms(self, a):
        return ["product", "film", "coating", "ppf", "tint", "vinyl", "sq ft", "sqft", "square"] + _words(a.get("name"))

    def apply(self, conn, studio_id, a):
        conn.execute(
            "INSERT INTO products (studio_id, name, category, brand, cost_per_sqft, labour_per_sqft, "
            "markup_percent, warranty_years) VALUES (?,?,?,?,?,?,?,?)",
            (studio_id, a["name"], a.get("category") or "PPF", a.get("brand") or "", a["cost_per_sqft"],
             a.get("labour_per_sqft") or 0, a.get("markup_percent") or 0, a.get("warranty_years") or 0))
        return f"Added {a['name']}."


# ── Studio settings ───────────────────────────────────────────────────────────

# Settings the assistant may change, with a validator each. SMS credentials
# are intentionally not here.
_SETTING_RULES: Dict[str, Tuple[str, Callable[[str], str]]] = {}


def _num_rule(lo: float, hi: float) -> Callable[[str], str]:
    def check(v: str) -> str:
        try:
            n = float(str(v).replace("%", "").strip())
        except ValueError as exc:
            raise ActionError(f"'{v}' isn't a number.") from exc
        if not lo <= n <= hi:
            raise ActionError(f"Must be between {lo:g} and {hi:g}.")
        return f"{n:g}"
    return check


def _text_rule(limit: int) -> Callable[[str], str]:
    def check(v: str) -> str:
        v = str(v).strip()
        if len(v) > limit:
            raise ActionError(f"Too long (max {limit} characters).")
        return v
    return check


_SETTING_RULES.update({
    "default_tax_percent": ("default sales tax % for estimates and invoices", _num_rule(0, 50)),
    "invoice_payment_terms_days": ("days until an invoice is due", _num_rule(0, 365)),
    "invoice_terms": ("terms & conditions printed on invoices", _text_rule(600)),
    "invoice_payment_instructions": ("how customers should pay (printed on invoices)", _text_rule(400)),
    "invoice_footer": ("footer line on invoices", _text_rule(200)),
    "invoice_number_prefix": ("text before invoice numbers, e.g. INV-", _text_rule(12)),
    "business_address": ("studio address", _text_rule(160)),
    "business_phone": ("studio phone", _text_rule(40)),
    "business_email": ("studio email", _text_rule(120)),
    "business_website": ("studio website", _text_rule(120)),
    "business_tax_id": ("tax / EIN number shown on invoices", _text_rule(40)),
    "business_hours": ("opening hours, e.g. Mon–Fri 8am–6pm", _text_rule(160)),
})


_SETTING_TERMS: Dict[str, List[str]] = {
    "default_tax_percent": ["tax", "gst", "vat", "hst"],
    "invoice_payment_terms_days": ["due", "terms", "net ", "days", "pay within"],
    "invoice_terms": ["terms", "condition", "policy"],
    "invoice_payment_instructions": ["pay", "zelle", "venmo", "bank", "card", "cash", "check", "cheque"],
    "invoice_footer": ["footer", "bottom", "thank"],
    "invoice_number_prefix": ["prefix", "number", "numbering"],
    "business_address": ["address", "located", "location", "moved", "street", "road"],
    "business_phone": ["phone", "number", "call"],
    "business_email": ["email", "e-mail", "@"],
    "business_website": ["website", "site", "url", "www", ".com"],
    "business_tax_id": ["tax id", "ein", "abn", "gstin", "vat number", "tax number"],
    "business_hours": ["hours", "open", "close", "am", "pm"],
}


class UpdateSetting(Action):
    name = "update_setting"
    summary = "Change a studio setting. Keys: " + "; ".join(f"{k} ({d})" for k, (d, _) in _SETTING_RULES.items())
    params = (Param("key", "text", max_len=40), Param("value", "text", max_len=600))

    def check(self, conn, studio_id, a):
        rule = _SETTING_RULES.get(a["key"])
        if not rule:
            raise ActionError(f"'{a['key']}' isn't a setting the assistant can change.")
        a["value"] = rule[1](a["value"])
        a["label"] = rule[0]
        return a

    def preview(self, a):
        return f"Set {a.get('label', a['key'])} to “{a['value']}”"

    def relevance_terms(self, a):
        return _SETTING_TERMS.get(a.get("key", ""), [])

    def apply(self, conn, studio_id, a):
        conn.execute(
            "INSERT INTO studio_settings (studio_id, key, value, updated_at) VALUES (?,?,?, datetime('now')) "
            "ON CONFLICT(studio_id, key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (studio_id, a["key"], a["value"]))
        return f"Saved {a.get('label', a['key'])}."


# ── Anything else ─────────────────────────────────────────────────────────────

class RequestFeature(Action):
    name = "request_feature"
    summary = ("Use when the owner asks for something none of the other actions can do (a new screen, "
               "an integration, a report…). Records it for the product team; changes nothing now.")
    params = (Param("summary", "text", help="one sentence describing what they want", max_len=300),)

    def preview(self, a):
        return f"Log feature request: “{a['summary']}”"

    def apply(self, conn, studio_id, a):
        return "Logged for the product team."


ACTIONS: Dict[str, Action] = {a.name: a for a in (
    AddService(), UpdateService(), RemoveService(), AdjustAllPrices(),
    AddBay(), RenameBay(), AddProduct(), UpdateSetting(), RequestFeature(),
)}


def is_relevant(action: Action, args: Dict[str, Any], message: str) -> bool:
    terms = action.relevance_terms(args)
    if not terms:
        return True
    text = (message or "").lower()
    return any(t in text for t in terms)


def is_missing_info(error: str) -> bool:
    """Errors that mean "the owner didn't give us this yet", not "this is wrong"."""
    return "is required" in error or "must be a number" in error


def catalog() -> List[Dict[str, Any]]:
    return [a.spec() for a in ACTIONS.values()]
