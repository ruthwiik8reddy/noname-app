"""
services/invoices/render_exact.py — fill a studio's own invoice with a new job.

Two kinds of blank page come out of exact.py:

* a **PDF** (their own file with the job-specific text objects removed) — the
  new text is drawn on a transparent overlay and merged onto it, so their
  logo, seals, bands and rules stay vector and byte-identical;
* a **PNG** (scans and photos) — drawn as the page background.

Slot coordinates are pixels of the analysis image; here they become PDF
points (y flipped). Text is drawn in the same PDF font the original used when
we know it (PDF uploads), else the matched Helvetica/Times/Courier.

Totals stay where theirs were: the grand total on its original line (often
inside a coloured band), other lines stacked above it. Items fill the space
above; if they don't fit, rows tighten, then continue on a second page with
the totals after them.
"""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional

MIN_SQUEEZE = 0.72

_DATE_FORMATS = {
    "MM/DD/YYYY": "%m/%d/%Y", "DD/MM/YYYY": "%d/%m/%Y", "YYYY-MM-DD": "%Y-%m-%d",
    "Mon D, YYYY": "%b %d, %Y", "Month D, YYYY": "%B %d, %Y",
    "DD-Mon-YYYY": "%d-%b-%Y", "D Mon YYYY": "%d %b %Y",
}
_STANDARD = {
    ("Helvetica", False, False): "Helvetica", ("Helvetica", True, False): "Helvetica-Bold",
    ("Helvetica", False, True): "Helvetica-Oblique", ("Helvetica", True, True): "Helvetica-BoldOblique",
    ("Times", False, False): "Times-Roman", ("Times", True, False): "Times-Bold",
    ("Times", False, True): "Times-Italic", ("Times", True, True): "Times-BoldItalic",
    ("Courier", False, False): "Courier", ("Courier", True, False): "Courier-Bold",
    ("Courier", False, True): "Courier-Oblique", ("Courier", True, True): "Courier-BoldOblique",
}


def font_name(slot: Dict[str, Any]) -> str:
    """The exact PDF font when the original used a standard one, else the closest standard face."""
    name = slot.get("name")
    if name in _STANDARD.values():
        return name
    return _STANDARD.get((slot.get("font", "Helvetica"), bool(slot.get("bold")), bool(slot.get("italic"))), "Helvetica")


def _hex(c: Optional[str], fallback: str = "#111111"):
    from reportlab.lib.colors import HexColor
    try:
        return HexColor(c or fallback)
    except Exception:  # noqa: BLE001
        return HexColor(fallback)


def format_date(iso: str, fmt: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime(_DATE_FORMATS.get(fmt, "%m/%d/%Y"))
    except ValueError:
        return iso


def format_amount(cents: int, prefix: str = "", indian: bool = False) -> str:
    """12345678 → '1,23,456.78' (Indian grouping) or '123,456.78', with their prefix ('Rs. ', '$')."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    if indian and whole >= 1000:
        digits = str(whole)
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        text = ",".join(groups + [tail])
    else:
        text = f"{whole:,}"
    return f"{sign}{prefix}{text}.{frac:02d}"


def next_number(template: Dict[str, Any], existing: List[str], prefix: str) -> Optional[str]:
    """Continue their own numbering: after '0008' comes '0009', zero-padded the same way."""
    width = int(template.get("number_digits") or 0)
    last = int(template.get("number_last") or 0)
    for num in existing:
        if prefix and not num.startswith(prefix):
            continue
        m = re.search(r"(\d+)\s*$", num)
        if m:
            last = max(last, int(m.group(1)))
    if not width and not last:
        return None
    return f"{prefix}{str(last + 1).zfill(width)}"


class _Painter:
    def __init__(self, canvas, scale: float, page_h: float):
        self.c, self.s, self.h = canvas, scale, page_h

    def y(self, px: float) -> float:
        return self.h - px * self.s

    def width_px(self, text: str, slot: Dict[str, Any], size_factor: float = 1.0) -> float:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        return stringWidth(text, font_name(slot), float(slot.get("size", 12)) * self.s * size_factor) / self.s

    def text(self, slot: Dict[str, Any], text: str, *, x_px: Optional[float] = None,
             baseline_px: Optional[float] = None, size_factor: float = 1.0, color: Optional[str] = None,
             align: Optional[str] = None, max_width_px: Optional[float] = None) -> None:
        if not text:
            return
        font = font_name(slot)
        size = max(5.0, float(slot.get("size", 12)) * self.s * size_factor)
        if max_width_px:
            from reportlab.pdfbase.pdfmetrics import stringWidth
            limit = max_width_px * self.s
            while stringWidth(text, font, size) > limit and size > 5.5:
                size -= 0.25
        self.c.setFont(font, size)
        self.c.setFillColor(_hex(color or slot.get("color")))
        x = (x_px if x_px is not None else slot["x"]) * self.s
        y = self.y(baseline_px if baseline_px is not None else slot["baseline"])
        how = align or slot.get("align", "left")
        if how == "right":
            self.c.drawRightString(x, y, text)
        elif how == "center":
            self.c.drawCentredString(x, y, text)
        else:
            self.c.drawString(x, y, text)

    def wrap(self, text: str, slot: Dict[str, Any], width_px: float) -> List[str]:
        lines, cur = [], ""
        for w in text.split():
            trial = f"{cur} {w}".strip()
            if cur and self.width_px(trial, slot) > width_px:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def rule(self, x1_px: float, x2_px: float, y_px: float, color: str, width_px: float) -> None:
        self.c.setStrokeColor(_hex(color))
        self.c.setLineWidth(max(0.4, width_px * self.s))
        self.c.line(x1_px * self.s, self.y(y_px), x2_px * self.s, self.y(y_px))


def _totals_lines(inv: Dict[str, Any], totals: Dict[str, Any]) -> List[tuple]:
    """(kind, label, cents) in display order, worded the way their invoice words them."""
    rows = totals.get("rows") or []
    wording = totals.get("wording", {})
    out = []
    if inv.get("discount") or inv.get("tax_percent") or any(r["kind"] == "subtotal" for r in rows):
        out.append(("subtotal", wording.get("subtotal", "Subtotal"), int(inv["subtotal"])))
    if inv.get("discount"):
        out.append(("discount", wording.get("discount", "Discount"), -int(inv["discount"])))
    if inv.get("tax_percent"):
        word = wording.get("tax", totals.get("tax_word", "Tax"))
        out.append(("tax", f"{word} ({inv['tax_percent']:g}%)", int(inv["tax_amount"])))
    out.append(("total", wording.get("total", "Total"), int(inv["total"])))
    return out


def _place_totals(lines: List[tuple], totals: Dict[str, Any], table: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Grand total on its original line; the others stacked above, on their original lines where they exist."""
    rows = totals["rows"]
    grand, normals = rows[-1], rows[:-1]
    bases = totals.get("baselines") or []
    pitch = totals.get("pitch", table["pitch"])
    grand_base = bases[-1]
    free = list(bases[:-1])
    positions = []
    for i in range(len(lines) - 1):
        positions.append(free[-1 - i] if i < len(free) else (positions[-1] if positions else grand_base) - pitch)
    positions.reverse()
    placed = []
    fallback = normals[-1] if normals else None
    grand_label_x = (grand.get("label") or {}).get("x", table["left"])
    for (kind, label, cents), base in zip(lines[:-1], positions):
        src = next((r for r in normals if r["kind"] == kind), fallback)
        # Extra lines their invoice never had use the item text style, lined up under the total's label.
        label_slot = (src or {}).get("label") or table["text"]
        value_slot = (src or {}).get("value") or table["text"]
        label_x = (src["label"]["x"] if src and src.get("label") else grand_label_x)
        placed.append({"label_slot": label_slot, "value_slot": value_slot, "label": label, "cents": cents,
                       "base": base, "label_x": label_x,
                       "value_x2": (src or grand)["value_x2"], "prefix": (src or grand).get("prefix", "")})
    g_label = grand.get("label") or table["text"]
    placed.append({"label_slot": g_label, "value_slot": grand["value"], "label": lines[-1][1], "cents": lines[-1][2],
                   "base": grand_base, "label_x": g_label.get("x", table["left"]),
                   "value_x2": grand["value_x2"], "prefix": grand.get("prefix", "")})
    return placed


def render(template: Dict[str, Any], blank_path: str, inv: Dict[str, Any],
           money: Optional[Callable[[int], str]] = None) -> bytes:
    """Fill the template with `inv` and return PDF bytes. Amounts use the format read from their invoice."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    W, H = template["page"]["w"], template["page"]["h"]
    page_w = float(template["page"].get("w_pt") or 612.0)
    page_h = float(template["page"].get("h_pt") or page_w * H / W)
    scale = page_w / W
    vector = blank_path.lower().endswith(".pdf")
    indian = bool(template.get("indian_numbering"))
    table = template["table"]
    slots = template.get("slots", {})
    fmt = template.get("date_format", "MM/DD/YYYY")

    def amount(cents: int) -> str:
        return format_amount(cents, table.get("money_prefix", ""), indian)

    # ── layout first: where the totals go decides how much room the items
    # have, and may move their totals section — before anything is painted.
    totals = template.get("totals") or {}
    lines_out = _totals_lines(inv, totals)
    placed = _place_totals(lines_out, totals, table) if totals.get("rows") else []
    t_pitch = totals.get("pitch", table["pitch"])
    rule = table.get("rule")
    # The last row's baseline must leave room for the rule under it and a gap before the totals.
    tail = (rule["offset"] + rule["width"]) if rule else float(table["text"].get("size", 12)) * 0.6
    limit = (min(pl["base"] for pl in placed) - t_pitch * 0.9 - tail) if placed else table["zone_limit"]

    cols = {col["role"]: col for col in table["columns"]}
    text_slot = dict(table["text"])
    items: List[Dict[str, Any]] = inv.get("items", [])
    rows_needed = sum(1.8 if it.get("description") else 1 for it in items)
    available = limit - table["first_baseline"]
    needed = (rows_needed - 1) * table["pitch"]

    # Long invoice: move their totals section down into the empty space
    # below it (never past the next thing on the page) before tightening rows.
    block = template.get("totals_block")
    shift = 0.0
    if block and needed > available and placed:
        shift = min(float(block.get("slack", 0)), needed - available)
        if shift > 0:
            for pl in placed:
                pl["base"] += shift
            limit += shift
            available += shift
            if words_slot_in_block(slots.get("amount_words"), block):
                slots = dict(slots, amount_words=dict(slots["amount_words"],
                                                      baseline=slots["amount_words"]["baseline"] + shift))
            blank_path = (_shift_pdf_block(blank_path, block, shift, scale) if vector
                          else _shift_image_block(blank_path, block, shift))

    buf = BytesIO()
    c = Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle(f"Invoice {inv.get('number', '')}")
    if not vector:
        c.drawImage(ImageReader(blank_path), 0, 0, width=page_w, height=page_h)
    p = _Painter(c, scale, page_h)

    # ── header values ─────────────────────────────────────────────────────
    def meta(key: str, value: str) -> None:
        slot = slots.get(key)
        if slot:
            p.text(slot, slot.get("label_text", "") + value)

    meta("invoice_number", inv.get("number", ""))
    meta("issue_date", format_date(inv.get("issue_date", ""), fmt))
    if "due_date" in slots:
        meta("due_date", format_date(inv["due_date"], fmt) if inv.get("due_date") else "Upon receipt")

    cust = slots.get("customer")
    if cust:
        detail = cust.get("detail", cust)
        labels = cust.get("labels", {})
        lines: List[tuple] = [(cust, inv.get("customer_name", ""))]
        vehicle_here = inv.get("vehicle") and "vehicle" not in slots
        if vehicle_here and cust.get("had_vehicle"):
            lines.append((detail, inv["vehicle"]))
        if inv.get("customer_tax_id"):
            lines.append((detail, labels.get("tax_id", "") + inv["customer_tax_id"]))
        address = ", ".join(l.strip().rstrip(",") for l in (inv.get("customer_address") or "").split("\n") if l.strip())
        if address:
            for part in p.wrap(labels.get("address", "") + address, detail, cust.get("wrap_px", 400)):
                lines.append((detail, part))
        for field in ("phone", "email"):
            if inv.get(f"customer_{field}"):
                lines.append((detail, labels.get(field, "") + inv[f"customer_{field}"]))
        if vehicle_here and not cust.get("had_vehicle"):
            lines.append((detail, inv["vehicle"]))
        y = cust["baseline"]
        for i, (slot, text) in enumerate(lines):
            if i:
                y += cust.get("first_gap", cust["pitch"]) if i == 1 else cust["pitch"]
            if y > cust.get("limit", 1e9):
                break
            p.text(slot, text, x_px=cust["x"], baseline_px=y, align="left")

    if "vehicle" in slots and inv.get("vehicle"):
        p.text(slots["vehicle"], inv["vehicle"], align="left")

    # ── status stamp: theirs, in their style, only when it's true ─────────
    # Their own stamp is already on the "paid" blank page; only draw one if they had none.
    if inv.get("status") == "Paid" and not (slots.get("stamp") and template.get("blank_paid")):
        stamp = slots.get("stamp")
        c.saveState()
        if stamp:
            x0, y0, x1, y1 = stamp["box"]
            c.setStrokeColor(_hex(stamp["color"]))
            c.setLineWidth(max(1.0, 2 * scale))
            c.rect(x0 * scale, p.y(y1), (x1 - x0) * scale, (y1 - y0) * scale, stroke=1, fill=0)
            p.text(stamp, stamp.get("text", "PAID") if stamp.get("text") in ("PAID", "PAID IN FULL") else "PAID",
                   x_px=(x0 + x1) / 2, align="center")
        else:
            c.setFillColor(_hex("#16A34A"))
            c.setStrokeColor(_hex("#16A34A"))
            c.setFont("Helvetica-Bold", 30)
            c.translate((table["right"] - 160) * scale, p.y(table["first_baseline"]) - 10)
            c.rotate(14)
            c.drawCentredString(0, 0, "PAID")
            c.setLineWidth(2.5)
            c.roundRect(-58, -10, 116, 40, 6)
        c.restoreState()

    # A single generously padded sample row can tighten further than a real table.
    min_pitch = float(text_slot.get("size", 12)) * 1.9
    floor = max(min_pitch / table["pitch"], 0.45) if template.get("single_row_sample") else MIN_SQUEEZE
    squeeze = 1.0 if needed <= available or needed <= 0 else max(floor, available / needed)
    pitch = table["pitch"] * squeeze
    size_factor = max(0.92, squeeze)
    desc = cols.get("description") or table["columns"][0]
    desc_right = min((col["x"] for col in table["columns"] if col["x"] > desc["x"]), default=table["right"]) - 16
    y = table["first_baseline"]
    overflowed = False

    for n, it in enumerate(items):
        if y > limit + 1 and not overflowed:
            c.showPage()
            c.setPageSize((page_w, page_h))
            overflowed = True
            y = 72 / scale + float(text_slot.get("size", 12))
        p.text(text_slot, it["name"], x_px=desc["anchor"], baseline_px=y, size_factor=size_factor,
               align="left", max_width_px=desc_right - desc["anchor"])
        values = {"quantity": "%g" % float(it["quantity"]), "rate": amount(int(it["unit_price"])),
                  "amount": amount(int(it["total"]))}
        for role, col in cols.items():
            if role == "description":
                continue
            value = col.get("sample", "") if role == "code" else values.get(role, "")
            p.text(text_slot, value, x_px=col["anchor"], baseline_px=y, size_factor=size_factor,
                   align=col["align"], color=table.get("amount_color") if role == "amount" else None)
        if it.get("description"):
            y += pitch * 0.8
            p.text(text_slot, it["description"], x_px=desc["anchor"], baseline_px=y,
                   size_factor=size_factor * 0.8, align="left", color="#6B7280",
                   max_width_px=desc_right - desc["anchor"])
        if rule and (rule.get("mode") == "each" or n == len(items) - 1):
            p.rule(rule.get("x1", table["left"] + 12), rule.get("x2", table["right"] - 12),
                   y + rule["offset"], rule["color"], rule["width"])
        y += pitch

    # ── totals ────────────────────────────────────────────────────────────
    if placed and not overflowed:
        # Keep every label clear of its amount, even when ours are longer than theirs.
        gap = float(table["text"].get("size", 12)) * 0.8
        clash = 0.0
        for pl in placed:
            value_w = p.width_px(format_amount(pl["cents"], pl["prefix"], indian), pl["value_slot"])
            label_w = p.width_px(pl["label"], pl["label_slot"])
            clash = max(clash, pl["label_x"] + label_w + gap - (pl["value_x2"] - value_w))
        for pl in placed:
            pl["label_x"] -= max(0.0, clash)
        for pl in placed:
            p.text(pl["label_slot"], pl["label"], x_px=pl["label_x"], baseline_px=pl["base"], align="left")
            p.text(pl["value_slot"], format_amount(pl["cents"], pl["prefix"], indian), x_px=pl["value_x2"],
                   baseline_px=pl["base"], align="right")
    else:
        # No totals in their design, or the items ran onto another page:
        # totals follow the items, in their styles where we have them.
        y += pitch * 0.3
        value_x2 = placed[-1]["value_x2"] if placed else table["right"] - 20
        label_x = placed[-1]["label_x"] if placed else desc["anchor"]
        for i, (kind, label, cents) in enumerate(lines_out):
            grand = i == len(lines_out) - 1
            slot_l = placed[-1]["label_slot"] if (placed and grand) else text_slot
            slot_v = placed[-1]["value_slot"] if (placed and grand) else text_slot
            color = "#111111" if overflowed else None   # their white-on-band colour has no band here
            p.text(slot_l, label, x_px=label_x, baseline_px=y, align="left", color=color)
            p.text(slot_v, format_amount(cents, placed[-1]["prefix"] if placed else table.get("money_prefix", ""), indian),
                   x_px=value_x2, baseline_px=y, align="right", color=color)
            y += t_pitch

    words = slots.get("amount_words")
    if words and not overflowed:
        from .exact import amount_in_words
        text = f"{words.get('prefix', '')}{amount_in_words(inv['total'], indian)}{words.get('suffix', ' Only')}"
        p.text(words, text, align="left", max_width_px=words.get("max_px"))

    if inv.get("notes") and not overflowed:
        p.text(text_slot, inv["notes"].replace("\n", "  ")[:300], x_px=desc["anchor"],
               baseline_px=min(y - pitch * 0.4, limit), size_factor=0.85, align="left", color="#4B5563",
               max_width_px=table["right"] - desc["anchor"])

    c.showPage()
    c.save()
    overlay = buf.getvalue()
    return _merge(blank_path, overlay) if vector else overlay


def words_slot_in_block(slot: Optional[Dict[str, Any]], block: Dict[str, Any]) -> bool:
    return bool(slot) and block["top"] - 4 <= slot["baseline"] <= block["bottom"] + 4


def _shift_pdf_block(blank_pdf: str, block: Dict[str, Any], shift_px: float, scale: float) -> str:
    """Move every vector object inside the totals section down by `shift_px` in a temp copy of their page."""
    import ctypes
    import tempfile

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    doc = pdfium.PdfDocument(blank_pdf)
    page = doc[0]
    _, page_h = page.get_size()
    top_pt, bottom_pt, dy = block["top"] * scale, block["bottom"] * scale, shift_px * scale
    for i in range(raw.FPDFPage_CountObjects(page.raw)):
        obj = raw.FPDFPage_GetObject(page.raw, i)
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        raw.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in (l, b, r, t)))
        o_top, o_bottom = page_h - t.value, page_h - b.value
        if o_top >= top_pt - 1 and o_bottom <= bottom_pt + 1:
            raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 0, -dy)
    raw.FPDFPage_GenerateContent(page.raw)
    out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    out.close()
    doc.save(out.name)
    doc.close()
    return out.name


def _shift_image_block(blank_png: str, block: Dict[str, Any], shift_px: float) -> str:
    """Image templates: move the totals strip down and fill the gap with the page colour."""
    import tempfile

    from PIL import Image

    from .exact import _page_bg

    img = Image.open(blank_png).convert("RGB")
    w = img.width
    x0, x1 = int(w * 0.04), int(w * 0.96)
    top, bottom, dy = int(block["top"]), int(block["bottom"]), int(round(shift_px))
    strip = img.crop((x0, top, x1, bottom))
    img.paste(_page_bg(img), (x0, top, x1, min(img.height, bottom + dy)))
    img.paste(strip, (x0, top + dy))
    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    out.close()
    img.save(out.name, "PNG")
    return out.name


def _merge(blank_pdf: str, overlay_pdf: bytes) -> bytes:
    """Their redacted page underneath, our text on top; continuation pages are added as-is."""
    from pypdf import PdfReader, PdfWriter

    over = PdfReader(BytesIO(overlay_pdf))
    out = PdfWriter(clone_from=blank_pdf)
    out.pages[0].merge_page(over.pages[0])
    for extra in over.pages[1:]:
        out.add_page(extra)
    data = BytesIO()
    out.write(data)
    return data.getvalue()


def preview_png(pdf_bytes: bytes, scale: float = 1.6) -> bytes:
    """First page of the PDF as a PNG — the browser preview is the real PDF, not an approximation."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    image = doc[0].render(scale=scale).to_pil()
    doc.close()
    out = BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()
