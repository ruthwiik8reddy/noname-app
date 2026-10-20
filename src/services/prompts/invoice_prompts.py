"""
services/prompts/invoice_prompts.py — reading a studio's existing invoice.

The model is asked to *describe* the invoice in a closed vocabulary, never to
reproduce it. It picks a layout archetype, reads the exact wording the studio
uses for each label, and copies their footer / terms / payment text verbatim.
Colours are read from pixels elsewhere (services/invoices/scan.py) because
small vision models guess hex values badly.

It is also told which fields to leave empty when unsure. A missing label
falls back to a sensible default; an invented one ends up on a customer's
invoice.
"""

from __future__ import annotations

import json

from ..invoices.style import DATE_FORMATS, DEFAULT_LABELS, LAYOUTS, TABLE_STYLES

_SCHEMA = {
    "layout": "|".join(LAYOUTS),
    "font": "sans|serif|mono",
    "table_style": "|".join(TABLE_STYLES),
    "title_text": "the document-type heading, e.g. INVOICE, Tax Invoice, Statement, Receipt",
    "has_logo": "true if there is a logo graphic or image (not just the name in text), else false",
    "logo_position": "left|center|right",
    "bill_to_side": "left|right",
    "show_quantity": True,
    "currency_symbol": "$",
    "date_format": "|".join(DATE_FORMATS),
    "invoice_number_example": "the invoice number exactly as printed, e.g. Q-0007",
    "business_name": "",
    "business_lines": ["address line", "phone", "email", "website"],
    "labels": {k: f"their wording for '{v}' or empty" for k, v in DEFAULT_LABELS.items()},
    "footer_text": "",
    "terms_text": "",
    "payment_instructions": "",
}


class InvoicePrompts:

    @staticmethod
    def describe_invoice() -> str:
        return f"""You are looking at an invoice from an auto detailing business.
Describe how it is designed so a new invoice can be produced in the same style.
Return ONLY a JSON object with these keys:

{json.dumps(_SCHEMA, indent=2)}

Layout meanings:
- classic: logo/business name top-left, the title (e.g. INVOICE) top-right
- banner: a solid coloured band across the top containing the title/business
- centered: logo, business name and title centred at the top
- minimal: no coloured fills, just thin lines and plenty of white space

Table style meanings:
- filled: the item table header row has a solid coloured background
- lined: only horizontal lines between rows
- striped: alternating shaded rows
- boxed: every cell has a border

How to read the trickier fields:
- title_text: the word(s) naming the document type. It is NEVER the business
  name, even if the business name is the biggest text on the page.
- has_logo: true only for a picture, emblem or graphic mark. The business name
  written in large letters is NOT a logo.
- show_quantity: true if the item table has any quantity-like column (Qty,
  Quantity, Hours, Hrs, Units). Put that column's heading in labels.quantity.
- layout "banner" ONLY if there is a solid coloured rectangle behind the top
  text. Coloured text on a white page is not a banner.
- font: "serif" if the letters have small feet/strokes at their ends (like Times
  or Georgia), "sans" if the strokes end plainly (like Arial or Helvetica).
- footer_text: the line of text at the very bottom of the page, e.g. a thank-you.
- terms_text: text under a heading like Terms, Terms & Conditions or Policy.
- payment_instructions: text under a heading like Payment, Payment Details,
  How to Pay, Payment Terms, Bank Details or Remit To. Copy it word for word.

Rules:
- Copy text EXACTLY as printed (same words, same capitalisation). Do not
  improve or translate it.
- Copy the business's own name, address, phone, email and website into
  business_name and business_lines.
- Do NOT copy any customer's name, the line items, prices or dates — only the
  design and the business's own details. (invoice_number_example is the one
  exception: it's used only to learn the numbering format.)
- If you cannot read something clearly, use an empty string. Empty is a
  valid answer; a guess is not.
"""

    @staticmethod
    def header_roles(numbered_lines) -> str:
        rows = "\n".join(f"{i}: {desc}" for i, desc in numbered_lines)
        return f"""These are the text lines at the top of an auto detailing business's invoice,
in reading order, with their position on the page.

{rows}

Give each line one role:
- business: the business's own name, address, phone, email, website, tax number
- title: the document type (INVOICE, Receipt, Statement…)
- meta: invoice number or date lines
- bill_to_label: the heading above the customer (Bill To, Customer, Client…)
- customer: the CUSTOMER's name, address, phone or email
- vehicle: the customer's car
- other: anything else
Return ONLY JSON: {{"roles": {{"0": "role", "1": "role", ...}}}}"""

    @staticmethod
    def repair(bad_output: str, schema_hint: str = "") -> str:
        return (
            "Your previous answer was not valid JSON. Return ONLY the JSON object, "
            "no prose, no code fences. Keys: " + ", ".join(_SCHEMA.keys())
            + "\n\nPrevious answer:\n" + (bad_output or "")[:1500]
        )
