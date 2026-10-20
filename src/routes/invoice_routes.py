"""
routes/invoice_routes.py — invoices in the studio's own style.

  /invoices/                      list
  /invoices/template              upload an existing invoice, tune the learned style
  /invoices/template/preview      the style applied to a sample invoice (iframe)
  /invoices/new                   create (optionally ?job_id= or ?estimate_id=)
  /invoices/<id>                  view, mark sent/paid/void
  /invoices/<id>/document         rendered invoice (iframe / print)
  /invoices/<id>/pdf              PDF download
  /i/<token>                      customer view, no login (unguessable token)

Uploaded invoices and logos are kept in private storage, never /static, and
are only served to the studio that owns them.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Dict, List, Optional

from flask import (Blueprint, Response, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup, escape

from ..auth import admin_required, auth_context, permission_required
from ..config import Config
from ..services.invoices import render_exact, scan
from ..services.invoices.style import (DATE_FORMATS, DEFAULT_LABELS, FONTS, LAYOUTS, TABLE_STYLES,
                                       format_date, format_money, normalize_style, style_from_form)
from ..services.orchestrators import InvoiceStyleOrchestrator, OrchestratorError
from ..services.repositories import InvoiceRepository, SettingsRepository
from ..services.repositories.invoice_repository import STATUSES

logger = logging.getLogger(__name__)

bp = Blueprint("invoices", __name__)

_style_ai = InvoiceStyleOrchestrator()

LOGO_EXTENSIONS = {"png", "jpg", "jpeg"}
MAX_ITEMS = 60


# ── helpers ───────────────────────────────────────────────────────────────────

def _sidebar() -> Dict[str, Any]:
    return {"studio": session.get("studio"), "city": session.get("city"),
            "logo": session.get("logo"), "owner": session.get("owner")}


def _actor() -> str:
    return session.get("name") or session.get("studio") or "staff"


def _private_dir(studio_id: int) -> str:
    path = os.path.join(Config.PRIVATE_STORAGE, f"studio_{int(studio_id)}", "invoice_template")
    os.makedirs(path, exist_ok=True)
    return path


def _style_for(studio_id: int) -> Dict[str, Any]:
    tpl = InvoiceRepository().get_template(studio_id)
    if tpl and tpl.get("style"):
        return normalize_style(tpl["style"])
    return _style_ai.studio_defaults(studio_id)


def _logo_path(studio_id: int) -> Optional[str]:
    """Absolute path of the logo to print: uploaded one first, then the studio's own."""
    tpl = InvoiceRepository().get_template(studio_id)
    if tpl and tpl.get("logo_file"):
        path = os.path.join(_private_dir(studio_id), tpl["logo_file"])
        if os.path.exists(path):
            return path
    studio_logo = session.get("logo") if session.get("studio_id") == studio_id else None
    if not studio_logo:
        from ..services.repositories import BaseRepository
        studio_logo = BaseRepository().fetch_scalar("SELECT logo FROM studios WHERE id=?", (studio_id,))
    if studio_logo:
        path = os.path.join(Config.STATIC_FOLDER, "logos", os.path.basename(studio_logo))
        if os.path.exists(path):
            return path
    return None


def _nl2br(text: str) -> Markup:
    return Markup("<br>".join(escape(line) for line in (text or "").split("\n")))


def _render_document(inv: Dict[str, Any], style: Dict[str, Any], logo_src: Optional[str],
                     for_pdf: bool = False) -> str:
    return render_template(
        "invoice_document.html", inv=inv, style=style, logo_src=logo_src, for_pdf=for_pdf,
        fonts=FONTS, money=lambda c: format_money(int(c or 0), style),
        date=lambda d: format_date(d, style), nl2br=_nl2br,
    )


def _html_to_pdf(html: str, logo_path: Optional[str]) -> bytes:
    from xhtml2pdf import pisa

    def link_callback(uri: str, rel: str) -> str:
        # The only local resource an invoice references is its logo. Anything
        # else (a URL someone typed into a note, say) is not fetched.
        if uri == "logo" and logo_path:
            return logo_path
        return ""

    buf = BytesIO()
    result = pisa.CreatePDF(html, dest=buf, link_callback=link_callback)
    if result.err:
        logger.error("Invoice PDF generation failed")
        abort(500)
    return buf.getvalue()


def _exact_for(studio_id: int, paid: bool = False) -> Optional[tuple]:
    """(slot map, blank page path) when the studio uses an exact copy of its own invoice."""
    tpl = InvoiceRepository().get_template(studio_id)
    if not tpl or tpl.get("mode") != "exact" or not tpl.get("exact") or not tpl.get("blank_file"):
        return None
    name = tpl["blank_file"]
    if paid and tpl["exact"].get("blank_paid"):
        name = tpl["exact"]["blank_paid"]          # their own PAID stamp, untouched
    blank = os.path.join(_private_dir(studio_id), os.path.basename(name))
    return (tpl["exact"], blank) if os.path.exists(blank) else None


def _invoice_pdf(inv: Dict[str, Any], studio_id: int) -> bytes:
    """The PDF for an invoice, in whichever mode the studio uses. Every output goes through here."""
    style = _style_for(studio_id)
    exact_tpl = _exact_for(studio_id, paid=inv.get("status") == "Paid")
    if exact_tpl:
        # Date format starts as whatever their invoice used and the owner can change it.
        slots = dict(exact_tpl[0], date_format=style["date_format"])
        return render_exact.render(slots, exact_tpl[1], inv, lambda c: format_money(int(c or 0), style))
    logo_path = _logo_path(studio_id)
    if logo_path and logo_path.lower().endswith(".svg"):
        logo_path = None  # xhtml2pdf can't draw SVG
    html = _render_document(inv, style, "logo" if logo_path else None, for_pdf=True)
    return _html_to_pdf(html, logo_path)


def _pdf_download(pdf: bytes, filename: str) -> Response:
    return send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=filename)


def _png(pdf: bytes) -> Response:
    resp = Response(render_exact.preview_png(pdf), mimetype="image/png")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _page_with_image(title: str, img_url: str, download_url: Optional[str] = None) -> str:
    """A minimal page showing the rendered invoice — used when it is an exact copy."""
    button = (f'<p style="text-align:center;margin:18px 0 30px"><a href="{escape(download_url)}" '
              f'style="background:#111;color:#fff;padding:10px 18px;border-radius:6px;'
              f'text-decoration:none;font:14px Helvetica,Arial,sans-serif">Download PDF</a></p>') if download_url else ""
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{escape(title)}</title></head>'
            f'<body style="margin:0;background:#F3F4F6">'
            f'<img src="{escape(img_url)}" alt="{escape(title)}" style="display:block;width:100%;max-width:820px;'
            f'margin:24px auto 0;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08),0 8px 24px rgba(0,0,0,.06)">'
            f'{button}</body></html>')


def _sample_invoice(style: Dict[str, Any], tax_percent: float) -> Dict[str, Any]:
    items = [
        {"name": "Ceramic Coating — 5 Year", "description": "Two-stage paint correction included",
         "quantity": 1, "unit_price": 89500, "total": 89500},
        {"name": "Interior Detail", "description": "Leather clean & condition",
         "quantity": 1, "unit_price": 32000, "total": 32000},
        {"name": "Wheel Coating", "description": "", "quantity": 4, "unit_price": 4500, "total": 18000},
    ]
    subtotal = sum(i["total"] for i in items)
    tax = int(round(subtotal * tax_percent / 100))
    return {
        "number": f"{style['number_prefix']}1042", "status": "Sent",
        "customer_name": "Jordan Sample", "customer_email": "jordan@example.com",
        "customer_phone": "+1 555 0100", "customer_address": "12 Example Street\nSpringfield",
        "vehicle": "2023 Porsche 911 Carrera", "issue_date": date.today().isoformat(),
        "due_date": (date.today() + timedelta(days=14)).isoformat(),
        "items": items, "subtotal": subtotal, "discount": 0, "tax_percent": tax_percent,
        "tax_amount": tax, "total": subtotal + tax, "notes": "",
    }


def _next_number(studio_id: int, style: Dict[str, Any]) -> str:
    """Continue the studio's own numbering when we learned it (0008 → 0009), else our default."""
    repo = InvoiceRepository()
    tpl = repo.get_template(studio_id)
    if tpl and tpl.get("exact"):
        existing = [r["number"] for r in repo.fetch_all("SELECT number FROM invoices WHERE studio_id=?", (studio_id,))]
        number = render_exact.next_number(tpl["exact"], existing, style["number_prefix"])
        if number and not repo.number_taken(studio_id, number):
            return number
    return repo.next_number(studio_id, style["number_prefix"])


def _parse_money(value: str) -> int:
    """'1,234.50' or '$99' → cents. Raises ValueError on nonsense."""
    cleaned = (value or "").replace(",", "").replace("$", "").strip()
    if not cleaned:
        return 0
    cents = int(round(float(cleaned) * 100))
    if cents < 0 or cents > 100_000_000:
        raise ValueError("amount out of range")
    return cents


def _parse_items(form) -> List[Dict[str, Any]]:
    names, descs = form.getlist("item_name"), form.getlist("item_desc")
    qtys, prices = form.getlist("item_qty"), form.getlist("item_price")
    items = []
    for i, name in enumerate(names[:MAX_ITEMS]):
        name = name.strip()[:160]
        if not name:
            continue
        qty = float((qtys[i] if i < len(qtys) else "1") or 1)
        if qty <= 0 or qty > 10_000:
            raise ValueError(f"Quantity for '{name}' must be between 0 and 10,000.")
        items.append({
            "name": name,
            "description": (descs[i] if i < len(descs) else "").strip()[:300],
            "quantity": qty,
            "unit_price": _parse_money(prices[i] if i < len(prices) else "0"),
        })
    return items


# ── list ──────────────────────────────────────────────────────────────────────

@bp.route("/invoices/")
@permission_required("view_payments")
def invoice_list():
    sid = session["studio_id"]
    repo = InvoiceRepository()
    invoices = repo.list(sid)
    totals = {
        "outstanding": sum(i["total"] for i in invoices if i["status"] == "Sent"),
        "paid": sum(i["total"] for i in invoices if i["status"] == "Paid"),
        "drafts": sum(1 for i in invoices if i["status"] == "Draft"),
    }
    return render_template(
        "invoices.html", **_sidebar(), **auth_context(), active_page="invoices",
        invoices=invoices, totals=totals, has_template=bool(repo.get_template(sid)),
        money=lambda c: format_money(int(c or 0), {"currency_symbol": "$"}),
    )


# ── template: learn + tune ────────────────────────────────────────────────────

@bp.route("/invoices/template", methods=["GET"])
@admin_required
def template_editor():
    sid = session["studio_id"]
    tpl = InvoiceRepository().get_template(sid)
    return render_template(
        "invoice_template.html", **_sidebar(), **auth_context(), active_page="invoices",
        tpl=tpl, style=_style_for(sid), layouts=LAYOUTS, fonts=list(FONTS), table_styles=TABLE_STYLES,
        date_formats=list(DATE_FORMATS), label_keys=DEFAULT_LABELS,
        notes=request.args.getlist("note"), error=request.args.get("error"),
        saved=request.args.get("saved"), ai_offline=request.args.get("degraded") == "1",
    )


@bp.route("/invoices/template/upload", methods=["POST"])
@admin_required
def template_upload():
    """Scan an existing invoice and learn its style."""
    sid = session["studio_id"]
    upload = request.files.get("invoice")
    if not upload or not upload.filename:
        return redirect(url_for("invoices.template_editor", error="Choose your invoice file first."))
    ext = scan.extension(upload.filename)
    if ext not in scan.ALLOWED_EXTENSIONS:
        return redirect(url_for("invoices.template_editor",
                                error="Upload a PDF, PNG, JPG or WEBP of your invoice."))

    folder = _private_dir(sid)
    raw_path = os.path.join(folder, f"upload_{uuid.uuid4().hex}.{ext}")
    image_name = f"source_{uuid.uuid4().hex}.png"
    image_path = os.path.join(folder, image_name)
    upload.save(raw_path)
    try:
        scan.to_image(raw_path, image_path)
        result = _style_ai.learn(sid, image_path)
    except scan.ScanError as exc:
        scan.safe_remove(image_path)
        return redirect(url_for("invoices.template_editor", error=str(exc)))
    except OrchestratorError as exc:
        scan.safe_remove(image_path)
        scan.safe_remove(raw_path)
        return redirect(url_for("invoices.template_editor", error=str(exc)))

    try:
        # A PDF from their invoicing software is edited directly. The uploaded
        # file itself is never kept: only a sanitised copy of page 1 is.
        exact_result = _style_ai.learn_exact(image_path, folder, pdf_path=raw_path if ext == "pdf" else None)
    finally:
        scan.safe_remove(raw_path)
    style = result["style"]
    if exact_result["exact"]:
        # Read straight off their invoice — more reliable than the model's guess.
        ex = exact_result["exact"]
        grand_prefix = ((ex.get("totals") or {}).get("rows") or [{}])[-1].get("prefix", "").strip()
        style = normalize_style({"number_prefix": ex.get("number_prefix", style["number_prefix"]),
                                 "date_format": ex.get("date_format", style["date_format"]),
                                 "currency_symbol": grand_prefix or style["currency_symbol"]},
                                base=style)

    repo = InvoiceRepository()
    previous = repo.get_template(sid)
    repo.save_template(
        sid, style, source_file=image_name, source_name=os.path.basename(upload.filename)[:120],
        extracted_by="ai" if result["read_by_ai"] else "colours-only",
        degraded=result["_status"]["degraded"],
    )
    repo.save_exact(sid, exact_result["exact"], exact_result["blank_file"], exact_result["note"])
    if previous:
        old_ex = previous.get("exact") or {}
        new_ex = exact_result["exact"] or {}
        keep = {image_name, exact_result["blank_file"], new_ex.get("blank_view"), new_ex.get("blank_paid")}
        for old in (previous.get("source_file"), previous.get("blank_file"), old_ex.get("blank_view"),
                    old_ex.get("blank_paid")):
            if old and old not in keep:
                scan.safe_remove(os.path.join(folder, os.path.basename(old)))

    notes = list(result["notes"])
    if not exact_result["exact"] and exact_result["note"]:
        notes.append("Exact copy not available: " + exact_result["note"])
    return redirect(url_for("invoices.template_editor", saved="learned", note=notes,
                            degraded="1" if result["_status"]["degraded"] else "0"))


@bp.route("/invoices/template/style", methods=["POST"])
@admin_required
def template_save_style():
    sid = session["studio_id"]
    style = normalize_style(style_from_form(request.form), base=_style_for(sid))
    logo_name = None
    logo = request.files.get("logo")
    if logo and logo.filename:
        ext = scan.extension(logo.filename)
        if ext not in LOGO_EXTENSIONS:
            return redirect(url_for("invoices.template_editor", error="Logo must be a PNG or JPG."))
        logo_name = f"logo_{uuid.uuid4().hex}.png"
        tmp = os.path.join(_private_dir(sid), f"tmp_{uuid.uuid4().hex}.{ext}")
        logo.save(tmp)
        try:
            from PIL import Image
            with Image.open(tmp) as im:
                im = im.convert("RGBA")
                im.thumbnail((600, 240))
                im.save(os.path.join(_private_dir(sid), logo_name), "PNG")
        except Exception:  # noqa: BLE001
            return redirect(url_for("invoices.template_editor", error="That logo file couldn't be read."))
        finally:
            scan.safe_remove(tmp)
    InvoiceRepository().save_template(sid, style, logo_file=logo_name)
    return redirect(url_for("invoices.template_editor", saved="style"))


@bp.route("/invoices/template/source")
@admin_required
def template_source():
    tpl = InvoiceRepository().get_template(session["studio_id"])
    if not tpl or not tpl.get("source_file"):
        abort(404)
    path = os.path.join(_private_dir(session["studio_id"]), os.path.basename(tpl["source_file"]))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@bp.route("/invoices/logo")
@permission_required("view_payments")
def studio_logo():
    path = _logo_path(session["studio_id"])
    if not path:
        abort(404)
    return send_file(path)


@bp.route("/invoices/template/preview", methods=["GET", "POST"])
@admin_required
def template_preview():
    """Sample invoice in the saved style, or in unsaved form values (live preview)."""
    sid = session["studio_id"]
    style = _style_for(sid)
    if request.method == "POST":
        style = normalize_style(style_from_form(request.form), base=style)
    tax = float(SettingsRepository().get(sid, "default_tax_percent") or 0)
    if request.method == "GET" and _exact_for(sid):
        return _page_with_image("Preview", url_for("invoices.template_preview_png"))
    logo = url_for("invoices.studio_logo") if _logo_path(sid) else None
    return _render_document(_sample_invoice(style, tax), style, logo)


@bp.route("/invoices/template/preview.png")
@admin_required
def template_preview_png():
    sid = session["studio_id"]
    style = _style_for(sid)
    tax = float(SettingsRepository().get(sid, "default_tax_percent") or 0)
    return _png(_invoice_pdf(_sample_invoice(style, tax), sid))


@bp.route("/invoices/template/blank.png")
@admin_required
def template_blank():
    """What we keep from their invoice: their page with the job-specific parts removed."""
    tpl = InvoiceRepository().get_template(session["studio_id"])
    if not tpl or not tpl.get("blank_file"):
        abort(404)
    view = (tpl.get("exact") or {}).get("blank_view") or tpl["blank_file"]
    path = os.path.join(_private_dir(session["studio_id"]), os.path.basename(view))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@bp.route("/invoices/template/mode", methods=["POST"])
@admin_required
def template_mode():
    if not InvoiceRepository().set_mode(session["studio_id"], request.form.get("mode", "")):
        return redirect(url_for("invoices.template_editor", error="An exact copy isn't available for this upload."))
    return redirect(url_for("invoices.template_editor", saved="mode"))


# ── create ────────────────────────────────────────────────────────────────────

@bp.route("/invoices/new", methods=["GET", "POST"])
@permission_required("view_payments")
def invoice_new():
    sid = session["studio_id"]
    repo = InvoiceRepository()
    style = _style_for(sid)
    settings = SettingsRepository().all(sid)
    errors: List[str] = []

    if request.method == "POST":
        f = request.form
        try:
            items = _parse_items(f)
            discount = _parse_money(f.get("discount", ""))
            tax_percent = float(f.get("tax_percent") or 0)
            if not 0 <= tax_percent <= 50:
                raise ValueError("Tax rate must be between 0 and 50%.")
        except ValueError as exc:
            items, discount, tax_percent = [], 0, 0.0
            errors.append(str(exc) if "must" in str(exc) else "Check the prices and quantities.")
        if not items and not errors:
            errors.append("Add at least one line item.")
        name = f.get("customer_name", "").strip()[:120]
        if not name:
            errors.append("Customer name is required.")
        number = (f.get("number", "").strip() or _next_number(sid, style))[:40]
        if repo.number_taken(sid, number):
            errors.append(f"Invoice number {number} is already used.")

        def own(table: str, raw: str) -> Optional[int]:
            if not raw or not raw.isdigit():
                return None
            row = repo.fetch_one(f"SELECT id FROM {table} WHERE id=? AND studio_id=?", (int(raw), sid))
            return row.get("id")

        if not errors:
            invoice_id = repo.create(sid, {
                "number": number,
                "customer_id": own("customers", f.get("customer_id", "")),
                "customer_name": name,
                "customer_email": f.get("customer_email", "").strip()[:160],
                "customer_phone": f.get("customer_phone", "").strip()[:40],
                "customer_address": f.get("customer_address", "").strip()[:300],
                "customer_tax_id": re.sub(r"[^A-Za-z0-9-]", "", f.get("customer_tax_id", ""))[:20].upper(),
                "vehicle": f.get("vehicle", "").strip()[:120],
                "job_id": own("jobs", f.get("job_id", "")),
                "estimate_id": own("estimates", f.get("estimate_id", "")),
                "issue_date": f.get("issue_date") or date.today().isoformat(),
                "due_date": f.get("due_date", ""),
                "tax_percent": tax_percent, "discount": discount,
                "notes": f.get("notes", "").strip()[:1000],
            }, items, _actor())
            return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
        prefill = dict(f)
        prefill["items"] = items or [{"name": "", "description": "", "quantity": 1, "unit_price": 0}]
    else:
        prefill: Dict[str, Any] = {}
        if request.args.get("estimate_id", "").isdigit():
            prefill = repo.from_estimate(sid, int(request.args["estimate_id"]))
        elif request.args.get("job_id", "").isdigit():
            prefill = repo.from_job(sid, int(request.args["job_id"]))
        prefill.setdefault("items", [{"name": "", "description": "", "quantity": 1, "unit_price": 0}])
        if prefill.get("tax_percent") is None:
            prefill["tax_percent"] = settings.get("default_tax_percent", "0")
        prefill["number"] = _next_number(sid, style)
        prefill["issue_date"] = date.today().isoformat()
        try:
            days = int(settings.get("invoice_payment_terms_days") or 0)
        except ValueError:
            days = 0
        prefill["due_date"] = (date.today() + timedelta(days=days)).isoformat() if days else ""

    services = repo.fetch_all("SELECT name, price FROM services WHERE studio_id=? ORDER BY name", (sid,))
    return render_template(
        "invoice_form.html", **_sidebar(), **auth_context(), active_page="invoices",
        prefill=prefill, errors=errors, services=services,
        has_template=bool(repo.get_template(sid)), currency=style["currency_symbol"],
    )


# ── view / status / output ────────────────────────────────────────────────────

@bp.route("/invoices/<int:invoice_id>")
@permission_required("view_payments")
def invoice_detail(invoice_id: int):
    sid = session["studio_id"]
    inv = InvoiceRepository().get(sid, invoice_id)
    if not inv:
        abort(404)
    return render_template(
        "invoice_detail.html", **_sidebar(), **auth_context(), active_page="invoices", inv=inv,
        public_url=url_for("invoices.public_invoice", token=inv["public_token"], _external=True),
        money=lambda c: format_money(int(c or 0), _style_for(sid)),
    )


@bp.route("/invoices/<int:invoice_id>/document")
@permission_required("view_payments")
def invoice_document(invoice_id: int):
    sid = session["studio_id"]
    inv = InvoiceRepository().get(sid, invoice_id)
    if not inv:
        abort(404)
    if _exact_for(sid):
        return _page_with_image(inv["number"], url_for("invoices.invoice_png", invoice_id=invoice_id))
    logo = url_for("invoices.studio_logo") if _logo_path(sid) else None
    return _render_document(inv, _style_for(sid), logo)


@bp.route("/invoices/<int:invoice_id>/preview.png")
@permission_required("view_payments")
def invoice_png(invoice_id: int):
    sid = session["studio_id"]
    inv = InvoiceRepository().get(sid, invoice_id)
    if not inv:
        abort(404)
    return _png(_invoice_pdf(inv, sid))


@bp.route("/invoices/<int:invoice_id>/pdf")
@permission_required("view_payments")
def invoice_pdf(invoice_id: int):
    sid = session["studio_id"]
    inv = InvoiceRepository().get(sid, invoice_id)
    if not inv:
        abort(404)
    return _pdf_download(_invoice_pdf(inv, sid), f"{inv['number']}.pdf")


@bp.route("/invoices/<int:invoice_id>/status", methods=["POST"])
@permission_required("view_payments")
def invoice_status(invoice_id: int):
    status = request.form.get("status", "")
    if status not in STATUSES:
        abort(400)
    if not InvoiceRepository().set_status(session["studio_id"], invoice_id, status):
        abort(404)
    return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))


# ── customer view (no login) ──────────────────────────────────────────────────

@bp.route("/i/<token>")
def public_invoice(token: str):
    inv = InvoiceRepository().get_by_token(token)
    if not inv or inv["status"] == "Void":
        abort(404)
    if _exact_for(inv["studio_id"]):
        return _page_with_image(inv["number"], url_for("invoices.public_png", token=token),
                                url_for("invoices.public_invoice_pdf", token=token))
    has_logo = bool(_logo_path(inv["studio_id"]))
    html = _render_document(inv, _style_for(inv["studio_id"]),
                            url_for("invoices.public_logo", token=token) if has_logo else None)
    download = url_for("invoices.public_invoice_pdf", token=token)
    bar = (f'<div style="text-align:center;margin:18px 0 32px;font-family:Helvetica,Arial,sans-serif">'
           f'<a href="{download}" style="background:#111;color:#fff;padding:10px 18px;border-radius:6px;'
           f'text-decoration:none;font-size:14px">Download PDF</a></div></body>')
    return html.replace("</body>", bar, 1)


@bp.route("/i/<token>/logo")
def public_logo(token: str):
    inv = InvoiceRepository().get_by_token(token)
    path = _logo_path(inv["studio_id"]) if inv else None
    if not path:
        abort(404)
    return send_file(path)


@bp.route("/i/<token>/pdf")
def public_invoice_pdf(token: str):
    inv = InvoiceRepository().get_by_token(token)
    if not inv or inv["status"] == "Void":
        abort(404)
    return _pdf_download(_invoice_pdf(inv, inv["studio_id"]), f"{inv['number']}.pdf")


@bp.route("/i/<token>/preview.png")
def public_png(token: str):
    inv = InvoiceRepository().get_by_token(token)
    if not inv or inv["status"] == "Void":
        abort(404)
    return _png(_invoice_pdf(inv, inv["studio_id"]))
