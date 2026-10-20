"""
services/invoices/style.py — the invoice style contract.

A studio's invoice "look" is reduced to a closed set of choices: one of a few
layout archetypes, colours, a font family, and the exact wording they use
("Tax Invoice", "Bill To", "Rate", "Amount Due"…). Every value is validated
here before it is stored or rendered.

Why a closed set instead of letting a model write HTML: generated markup is
an injection vector straight into a customer-facing document, it breaks the
PDF renderer, and it can't be edited by the owner afterwards. A style dict
can be clamped, previewed, and fixed by hand in a form.

The layout archetypes cover how almost every small-business invoice is
arranged:
  * classic  — logo/business top-left, big title top-right
  * banner   — solid colour band across the top with title and business in it
  * centered — logo, business and title centred above everything
  * minimal  — no fills, hairline rules, lots of whitespace
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

LAYOUTS = ("classic", "banner", "centered", "minimal")
FONTS = {
    "sans": "Helvetica, Arial, sans-serif",
    "serif": "Georgia, 'Times New Roman', serif",
    "mono": "'Courier New', Courier, monospace",
}
DATE_FORMATS = {
    "MM/DD/YYYY": "%m/%d/%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "Mon D, YYYY": "%b %d, %Y",
    "Month D, YYYY": "%B %d, %Y",
    "DD-Mon-YYYY": "%d-%b-%Y",
    "D Mon YYYY": "%d %b %Y",
}
TABLE_STYLES = ("filled", "lined", "striped", "boxed")
SIDES = ("left", "right")

DEFAULT_LABELS: Dict[str, str] = {
    "bill_to": "Bill To",
    "invoice_number": "Invoice #",
    "issue_date": "Date",
    "due_date": "Due Date",
    "vehicle": "Vehicle",
    "description": "Description",
    "quantity": "Qty",
    "rate": "Rate",
    "amount": "Amount",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "tax": "Tax",
    "total": "Total",
    "notes": "Notes",
    "terms": "Terms & Conditions",
    "payment": "Payment Information",
}

DEFAULT_STYLE: Dict[str, Any] = {
    "layout": "classic",
    "font": "sans",
    "accent_color": "#1F2937",
    "text_color": "#111827",
    "muted_color": "#6B7280",
    "header_bg": "#1F2937",
    "header_text_color": "#FFFFFF",
    "table_style": "filled",
    "table_header_bg": "#1F2937",
    "table_header_text": "#FFFFFF",
    "title_text": "INVOICE",
    "title_size": 28,
    "show_logo": True,
    "logo_position": "left",
    "bill_to_side": "left",
    "show_quantity": True,
    "show_vehicle": True,
    "currency_symbol": "$",
    "date_format": "MM/DD/YYYY",
    "number_prefix": "INV-",
    "business_name": "",
    "business_lines": [],
    "labels": dict(DEFAULT_LABELS),
    "footer_text": "Thank you for your business!",
    "terms_text": "",
    "payment_instructions": "",
}

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_MAX_TEXT = 600


def clean_color(value: Any, fallback: str) -> str:
    """Accept '#abc', 'abc', '#aabbcc'; anything else falls back."""
    if not isinstance(value, str):
        return fallback
    m = _HEX.match(value.strip())
    if not m:
        return fallback
    hexpart = m.group(1)
    if len(hexpart) == 3:
        hexpart = "".join(c * 2 for c in hexpart)
    return "#" + hexpart.upper()


def clean_text(value: Any, fallback: str = "", limit: int = _MAX_TEXT) -> str:
    if value is None:
        return fallback
    text = str(value).replace("\r", "").strip()
    # Control characters have no business on an invoice.
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    return text[:limit] if text else fallback


def _choice(value: Any, options, fallback: str) -> str:
    v = str(value or "").strip().lower()
    for opt in options:
        if v == opt.lower():
            return opt
    return fallback


def _bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def relative_luminance(hex_color: str) -> float:
    h = clean_color(hex_color, "#000000")[1:]
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def readable_on(bg: str) -> str:
    """Black or white, whichever reads better on `bg`."""
    return "#111111" if relative_luminance(bg) > 0.45 else "#FFFFFF"


def normalize_style(raw: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Merge `raw` over `base` (or the defaults) and clamp every field.

    Never raises: unknown keys are dropped, bad values fall back. The output is
    always safe to render.
    """
    raw = raw or {}
    b = dict(DEFAULT_STYLE)
    if base:
        b.update({k: v for k, v in base.items() if k in DEFAULT_STYLE})
    out: Dict[str, Any] = {}

    out["layout"] = _choice(raw.get("layout", b["layout"]), LAYOUTS, b["layout"])
    out["font"] = _choice(raw.get("font", b["font"]), FONTS.keys(), b["font"])
    for key in ("accent_color", "text_color", "muted_color", "header_bg", "table_header_bg"):
        out[key] = clean_color(raw.get(key, b[key]), clean_color(b[key], DEFAULT_STYLE[key]))
    # Text colours on filled areas: honour what was read, but never let it be
    # unreadable — a white-on-white header is worse than a slightly wrong one.
    for text_key, bg_key in (("header_text_color", "header_bg"), ("table_header_text", "table_header_bg")):
        color = clean_color(raw.get(text_key, b[text_key]), readable_on(out[bg_key]))
        if abs(relative_luminance(color) - relative_luminance(out[bg_key])) < 0.25:
            color = readable_on(out[bg_key])
        out[text_key] = color

    out["table_style"] = _choice(raw.get("table_style", b["table_style"]), TABLE_STYLES, b["table_style"])
    out["title_text"] = clean_text(raw.get("title_text", b["title_text"]), "INVOICE", 40)
    try:
        out["title_size"] = max(16, min(48, int(float(raw.get("title_size", b["title_size"])))))
    except (TypeError, ValueError):
        out["title_size"] = b["title_size"]
    out["show_logo"] = _bool(raw.get("show_logo"), b["show_logo"])
    out["logo_position"] = _choice(raw.get("logo_position", b["logo_position"]),
                                   ("left", "center", "right"), b["logo_position"])
    out["bill_to_side"] = _choice(raw.get("bill_to_side", b["bill_to_side"]), SIDES, b["bill_to_side"])
    out["show_quantity"] = _bool(raw.get("show_quantity"), b["show_quantity"])
    out["show_vehicle"] = _bool(raw.get("show_vehicle"), b["show_vehicle"])
    out["currency_symbol"] = clean_text(raw.get("currency_symbol", b["currency_symbol"]), "$", 4)
    out["date_format"] = _choice(raw.get("date_format", b["date_format"]), DATE_FORMATS.keys(), b["date_format"])
    out["number_prefix"] = re.sub(r"[^A-Za-z0-9#\-_/ ]", "",
                                  clean_text(raw.get("number_prefix", b["number_prefix"]), "", 12))
    out["business_name"] = clean_text(raw.get("business_name", b["business_name"]), "", 120)

    lines = raw.get("business_lines", b["business_lines"])
    if isinstance(lines, str):
        lines = lines.split("\n")
    out["business_lines"] = [clean_text(l, "", 120) for l in (lines or []) if clean_text(l, "", 120)][:6]

    labels = dict(DEFAULT_LABELS)
    labels.update({k: v for k, v in (b.get("labels") or {}).items() if k in DEFAULT_LABELS})
    for k, v in (raw.get("labels") or {}).items():
        if k in DEFAULT_LABELS:
            labels[k] = clean_text(v, labels[k], 40)
    out["labels"] = labels

    for key in ("footer_text", "terms_text", "payment_instructions"):
        out[key] = clean_text(raw.get(key, b[key]), "")
    return out


def style_from_form(form) -> Dict[str, Any]:
    """Build a raw style dict from the template editor form."""
    raw: Dict[str, Any] = {k: form.get(k) for k in DEFAULT_STYLE if k not in ("labels", "business_lines")}
    for key in ("show_logo", "show_quantity", "show_vehicle"):
        raw[key] = form.get(key) == "on"
    raw["business_lines"] = form.get("business_lines", "")
    raw["labels"] = {k: form.get(f"label_{k}") for k in DEFAULT_LABELS if form.get(f"label_{k}") is not None}
    return raw


def format_money(cents: int, style: Dict[str, Any]) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{style.get('currency_symbol', '$')}{abs(cents) / 100:,.2f}"


def format_date(iso: str, style: Dict[str, Any]) -> str:
    from datetime import datetime

    if not iso:
        return ""
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return iso
    return d.strftime(DATE_FORMATS.get(style.get("date_format", ""), "%m/%d/%Y"))


def swatches(style: Dict[str, Any]) -> List[str]:
    return [style["accent_color"], style["header_bg"], style["table_header_bg"], style["text_color"]]
