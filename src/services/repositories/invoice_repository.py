"""
repositories/invoice_repository.py — invoices, their items, and the studio's
learned invoice template.

Money is stored in integer cents, like estimates. Totals are always recomputed
here from the items — never trusted from a form.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Dict, List, Optional

from .base import BaseRepository

STATUSES = ("Draft", "Sent", "Paid", "Void")


def compute_totals(items: List[Dict[str, Any]], tax_percent: float, discount: int = 0) -> Dict[str, int]:
    subtotal = sum(int(round(float(i["quantity"]) * int(i["unit_price"]))) for i in items)
    discount = max(0, min(int(discount or 0), subtotal))
    taxable = subtotal - discount
    tax_amount = int(round(taxable * float(tax_percent) / 100))
    return {"subtotal": subtotal, "discount": discount, "tax_amount": tax_amount,
            "total": taxable + tax_amount}


class InvoiceRepository(BaseRepository):

    # ── template ──────────────────────────────────────────────────────────

    def get_template(self, studio_id: int) -> Dict[str, Any]:
        row = self.fetch_one("SELECT * FROM invoice_templates WHERE studio_id=?", (studio_id,))
        if row:
            try:
                row["style"] = json.loads(row.get("style_json") or "{}")
            except json.JSONDecodeError:
                row["style"] = {}
            try:
                row["exact"] = json.loads(row.get("exact_json") or "null")
            except json.JSONDecodeError:
                row["exact"] = None
        return row

    def save_exact(self, studio_id: int, exact: Optional[Dict[str, Any]], blank_file: str, note: str) -> None:
        """Store the fill-in template; exact mode is switched on only when one was built."""
        self.execute(
            "UPDATE invoice_templates SET exact_json=?, blank_file=?, exact_note=?, "
            "mode=CASE WHEN ? THEN 'exact' ELSE 'rebuilt' END, updated_at=datetime('now') WHERE studio_id=?",
            (json.dumps(exact) if exact else "", blank_file, note[:500], 1 if exact else 0, studio_id),
        )

    def set_mode(self, studio_id: int, mode: str) -> bool:
        if mode not in ("exact", "rebuilt"):
            return False
        if mode == "exact" and not (self.get_template(studio_id) or {}).get("exact"):
            return False
        return bool(self.execute("UPDATE invoice_templates SET mode=? WHERE studio_id=?", (mode, studio_id)))

    def save_template(self, studio_id: int, style: Dict[str, Any], *, source_file: Optional[str] = None,
                      source_name: Optional[str] = None, logo_file: Optional[str] = None,
                      extracted_by: Optional[str] = None, degraded: Optional[bool] = None) -> None:
        existing = self.get_template(studio_id)
        values = {
            "style_json": json.dumps(style),
            "source_file": source_file if source_file is not None else existing.get("source_file", ""),
            "source_name": source_name if source_name is not None else existing.get("source_name", ""),
            "logo_file": logo_file if logo_file is not None else existing.get("logo_file", ""),
            "extracted_by": extracted_by if extracted_by is not None else existing.get("extracted_by", ""),
            "degraded": int(degraded) if degraded is not None else int(existing.get("degraded", 0) or 0),
        }
        self.execute(
            """
            INSERT INTO invoice_templates (studio_id, style_json, source_file, source_name, logo_file,
                                           extracted_by, degraded, updated_at)
            VALUES (?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(studio_id) DO UPDATE SET
              style_json=excluded.style_json, source_file=excluded.source_file,
              source_name=excluded.source_name, logo_file=excluded.logo_file,
              extracted_by=excluded.extracted_by, degraded=excluded.degraded,
              updated_at=datetime('now')
            """,
            (studio_id, values["style_json"], values["source_file"], values["source_name"],
             values["logo_file"], values["extracted_by"], values["degraded"]),
        )

    # ── invoices ──────────────────────────────────────────────────────────

    def list(self, studio_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM invoices WHERE studio_id=? ORDER BY id DESC LIMIT ?", (studio_id, limit)
        )

    def get(self, studio_id: int, invoice_id: int) -> Dict[str, Any]:
        inv = self.fetch_one("SELECT * FROM invoices WHERE id=? AND studio_id=?", (invoice_id, studio_id))
        if inv:
            inv["items"] = self.items(invoice_id)
        return inv

    def get_by_token(self, token: str) -> Dict[str, Any]:
        if not token:
            return {}
        inv = self.fetch_one("SELECT * FROM invoices WHERE public_token=?", (token,))
        if inv:
            inv["items"] = self.items(inv["id"])
        return inv

    def items(self, invoice_id: int) -> List[Dict[str, Any]]:
        return self.fetch_all(
            "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position, id", (invoice_id,)
        )

    def next_number(self, studio_id: int, prefix: str) -> str:
        """Highest numeric suffix used so far with this prefix, plus one."""
        rows = self.fetch_all("SELECT number FROM invoices WHERE studio_id=?", (studio_id,))
        highest = 1000
        for r in rows:
            num = r["number"] or ""
            if prefix and not num.startswith(prefix):
                continue
            m = re.search(r"(\d+)\s*$", num)
            if m:
                highest = max(highest, int(m.group(1)))
        return f"{prefix}{highest + 1}"

    def number_taken(self, studio_id: int, number: str, exclude_id: Optional[int] = None) -> bool:
        row = self.fetch_one(
            "SELECT id FROM invoices WHERE studio_id=? AND number=? AND id != ?",
            (studio_id, number, exclude_id or 0),
        )
        return bool(row)

    def create(self, studio_id: int, header: Dict[str, Any], items: List[Dict[str, Any]],
               actor: str) -> int:
        totals = compute_totals(items, header.get("tax_percent", 0), header.get("discount", 0))
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO invoices (studio_id, number, status, customer_id, customer_name,
                    customer_email, customer_phone, customer_address, customer_tax_id, vehicle, job_id,
                    estimate_id, issue_date, due_date, subtotal, discount, tax_percent, tax_amount, total,
                    notes, public_token, created_by)
                VALUES (?,?, 'Draft', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (studio_id, header["number"], header.get("customer_id"), header.get("customer_name", ""),
                 header.get("customer_email", ""), header.get("customer_phone", ""),
                 header.get("customer_address", ""), header.get("customer_tax_id", ""),
                 header.get("vehicle", ""), header.get("job_id"),
                 header.get("estimate_id"), header["issue_date"], header.get("due_date", ""),
                 totals["subtotal"], totals["discount"], float(header.get("tax_percent", 0)),
                 totals["tax_amount"], totals["total"], header.get("notes", ""),
                 secrets.token_urlsafe(24), actor),
            )
            invoice_id = cur.lastrowid
            for pos, it in enumerate(items):
                conn.execute(
                    "INSERT INTO invoice_items (invoice_id, name, description, quantity, unit_price, "
                    "total, position) VALUES (?,?,?,?,?,?,?)",
                    (invoice_id, it["name"], it.get("description", ""), float(it["quantity"]),
                     int(it["unit_price"]), int(round(float(it["quantity"]) * int(it["unit_price"]))), pos),
                )
            conn.commit()
        return invoice_id

    def set_status(self, studio_id: int, invoice_id: int, status: str) -> bool:
        if status not in STATUSES:
            return False
        paid_at = "datetime('now')" if status == "Paid" else "''"
        return bool(self.execute(
            f"UPDATE invoices SET status=?, paid_at={paid_at} WHERE id=? AND studio_id=?",
            (status, invoice_id, studio_id),
        ))

    # ── prefill sources ───────────────────────────────────────────────────

    def from_estimate(self, studio_id: int, estimate_id: int) -> Dict[str, Any]:
        est = self.fetch_one("SELECT * FROM estimates WHERE id=? AND studio_id=?", (estimate_id, studio_id))
        if not est:
            return {}
        items = self.fetch_all(
            "SELECT name, description, quantity, unit_price FROM estimate_items WHERE estimate_id=?",
            (estimate_id,),
        )
        return {
            "customer_id": est.get("customer_id"), "customer_name": est["customer_name"],
            "customer_email": est.get("customer_email", ""), "customer_phone": est.get("customer_phone", ""),
            "vehicle": est.get("vehicle", ""), "estimate_id": estimate_id,
            "tax_percent": est.get("tax_percent"), "items": items,
        }

    def from_job(self, studio_id: int, job_id: int) -> Dict[str, Any]:
        job = self.fetch_one("SELECT * FROM jobs WHERE id=? AND studio_id=?", (job_id, studio_id))
        if not job:
            return {}
        customer: Dict[str, Any] = {}
        if job.get("customer_id"):
            customer = self.fetch_one(
                "SELECT id, name, email, phone FROM customers WHERE id=? AND studio_id=?",
                (job["customer_id"], studio_id),
            )
        # jobs.price is whole dollars; invoices are cents.
        return {
            "customer_id": customer.get("id"), "customer_name": customer.get("name", ""),
            "customer_email": customer.get("email", ""), "customer_phone": customer.get("phone", ""),
            "vehicle": job.get("car", ""), "job_id": job_id,
            "items": [{"name": job.get("service", "Service"), "description": "",
                       "quantity": 1, "unit_price": int(job.get("price") or 0) * 100}],
        }
