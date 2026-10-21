"""
services/currency.py — each studio's own currency.

A studio picks its currency once (Account → Currency). Every price, total
and report is then shown in it: pages through the `money` / `money_cents`
template filters, page scripts through `window.money`, and messages built in
Python through `money()` / `money_cents()` below.

Only the display changes. Amounts are stored as plain numbers (job prices in
whole units, estimates and invoices in hundredths), so switching currency
never converts or rewrites any figure.

Which studio's currency? `use_studio(studio_id)` pins it for the current
request — public pages (approval links, tracking) call it with the record's
studio. Otherwise a logged-in request uses the session's studio. Code that runs
outside a request (agents, schedulers) runs inside `with for_studio(studio_id):`.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional

# code → (symbol, label). Symbol is what is printed before the amount.
CURRENCIES: Dict[str, tuple] = {
    "INR": ("₹", "Indian Rupee (₹)"),
    "USD": ("$", "US Dollar ($)"),
    "EUR": ("€", "Euro (€)"),
    "GBP": ("£", "British Pound (£)"),
    "AED": ("AED ", "UAE Dirham (AED)"),
    "SAR": ("SAR ", "Saudi Riyal (SAR)"),
    "AUD": ("A$", "Australian Dollar (A$)"),
    "CAD": ("C$", "Canadian Dollar (C$)"),
    "SGD": ("S$", "Singapore Dollar (S$)"),
    "NZD": ("NZ$", "New Zealand Dollar (NZ$)"),
    "ZAR": ("R ", "South African Rand (R)"),
    "LKR": ("Rs ", "Sri Lankan Rupee (Rs)"),
    "NPR": ("Rs ", "Nepalese Rupee (Rs)"),
    "PKR": ("Rs ", "Pakistani Rupee (Rs)"),
    "BDT": ("৳", "Bangladeshi Taka (৳)"),
    "MYR": ("RM ", "Malaysian Ringgit (RM)"),
}
DEFAULT = "USD"            # what every studio showed before currencies existed
SETTING = "currency"
_scoped: ContextVar[Optional[str]] = ContextVar("studio_currency", default=None)


def valid(code: str) -> bool:
    return (code or "").upper() in CURRENCIES


def choices():
    return [(code, label) for code, (_sym, label) in CURRENCIES.items()]


def symbol(code: str) -> str:
    return CURRENCIES.get((code or "").upper(), CURRENCIES[DEFAULT])[0]


def js_locale(code: str) -> str:
    """Locale for the browser's number grouping (en-IN gives 1,80,000)."""
    return "en-IN" if code in ("INR", "NPR", "LKR", "PKR", "BDT") else "en-US"


# ── which studio ──────────────────────────────────────────────────────────────

def _lookup(studio_id: int) -> str:
    from ..config import Config
    try:
        conn = sqlite3.connect(Config.DB_PATH, timeout=5)
        try:
            row = conn.execute("SELECT value FROM studio_settings WHERE studio_id=? AND key=?",
                               (int(studio_id), SETTING)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError):
        return DEFAULT
    return row[0].upper() if row and valid(row[0]) else DEFAULT


def use_studio(studio_id: Optional[int]) -> None:
    """Show money for this studio for the rest of the request (public pages)."""
    from flask import g, has_request_context
    if has_request_context() and studio_id:
        g.currency_studio_id = int(studio_id)
        g.pop("currency_code", None)


@contextmanager
def for_studio(studio_id: Optional[int]):
    """Format money for this studio inside the block — also outside a request (agents)."""
    token = _scoped.set(_lookup(studio_id) if studio_id else None)
    try:
        yield
    finally:
        _scoped.reset(token)


def code_for(studio_id: Optional[int] = None) -> str:
    if studio_id:
        return _lookup(studio_id)
    if _scoped.get():
        return _scoped.get()
    from flask import g, has_request_context, session
    if not has_request_context():
        return DEFAULT
    if "currency_code" not in g:
        sid = g.get("currency_studio_id") or session.get("studio_id")
        g.currency_code = _lookup(sid) if sid else DEFAULT
    return g.currency_code


def forget() -> None:
    """Call after the setting changes so this request shows the new currency."""
    from flask import g, has_request_context
    if has_request_context():
        g.pop("currency_code", None)


# ── formatting ────────────────────────────────────────────────────────────────

def group(amount: Any, code: str, decimals: int = 2) -> str:
    """1234567.5 → '1,234,567.50', or '12,34,567.50' for rupee currencies."""
    try:
        value = Decimal(str(amount if amount not in (None, "") else 0))
    except Exception:  # noqa: BLE001 — anything unparseable shows as zero
        value = Decimal(0)
    value = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    if js_locale(code) == "en-IN" and len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    else:
        whole = f"{int(whole):,}"
    return sign + whole + (f".{frac}" if decimals else "")


def money(amount: Any, decimals: int = 2, studio_id: Optional[int] = None) -> str:
    """Whole-unit amount (e.g. jobs.price) with the studio's symbol."""
    code = code_for(studio_id)
    text = group(amount, code, decimals)
    return ("-" + symbol(code) + text[1:]) if text.startswith("-") else symbol(code) + text


def money_cents(cents: Any, decimals: int = 2, studio_id: Optional[int] = None) -> str:
    """Amount stored in hundredths (estimates, invoices, inventory costs)."""
    try:
        amount = Decimal(int(cents or 0)) / 100
    except (TypeError, ValueError):
        amount = Decimal(0)
    return money(amount, decimals, studio_id)


def install(app) -> None:
    """Template filters and the `studio_currency` template global. Safe to call twice."""
    if "money_cents" in app.jinja_env.filters:
        return
    from jinja2 import pass_context

    # pass_context stops Jinja from pre-computing `{{ 500|money }}` once at
    # compile time — the result depends on which studio is viewing.
    app.jinja_env.filters["money"] = pass_context(lambda _ctx, v, d=2: money(v, d))
    app.jinja_env.filters["money_cents"] = pass_context(lambda _ctx, v, d=2: money_cents(v, d))

    @app.context_processor
    def _currency_context():
        code = code_for()
        return {"studio_currency": {"code": code, "symbol": symbol(code), "locale": js_locale(code)}}


def attach(blueprint) -> None:
    """Install on whichever app registers this blueprint (tests build small apps)."""
    blueprint.record_once(lambda state: install(state.app))
