"""
services/invoices/exact.py — turn a studio's real invoice into a fill-in template.

The most exact copy of their invoice is their invoice. So instead of
rebuilding the design, we keep their page and replace only what changes from
job to job:

    their invoice ──▶ lines with exact boxes
                       (PDF text layer when there is one — exact fonts, sizes,
                        colours; OCR for scans and photos)
                  ──▶ rules (+ one small model call) decide which lines are variable
                  ──▶ blank page: variable content removed
                       (PDF: text objects deleted from their own file, artwork
                        stays vector; image: text inpainted into its background)
                  ──▶ slots: where new text goes, in which font, size, colour, alignment

What counts as variable on a detailing invoice: the customer block (incl.
labelled lines like "GSTIN:" / "Address:"), the vehicle, invoice number and
dates, item rows, totals values, the amount in words, and status stamps
("PAID"). Everything else — letterhead, logo, seals, signatures, bank details,
terms, bands, rules — stays exactly as it was.

Safety: an old customer's details surviving onto every new invoice would be a
privacy failure. Customer and vehicle areas are erased as whole regions, not
line by line, and find_leaks re-reads the blank page before it is ever used.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import ocr
from .ocr import Line

logger = logging.getLogger(__name__)

MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
AMOUNT = re.compile(r"[$€£₹]?\s?-?\d[\d,]*\.\d{2}\b|[$€£₹]\s?\d[\d,]*")
DATE = re.compile(
    r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b"
    rf"|\b{MONTHS}\s+\d{{1,2}},?\s+\d{{2,4}}\b"
    rf"|\b\d{{1,2}}[-/ .]{MONTHS}[-/ .,]+\d{{2,4}}\b",
    re.I,
)
MONTH = re.compile(r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)", re.I)
TOTAL_WORDS = re.compile(r"\b(sub\s*-?\s*total|total|taxable|tax|gst|cgst|sgst|igst|vat|hst|pst|discount|due|balance|"
                         r"paid|payable|amount)\b", re.I)
BILL_TO = re.compile(r"^\s*(bill(ed)?\s*to|invoice\s*to|sold\s*to|ship\s*to|customer|client|prepared\s*for|for|to|"
                     r"attention|attn|buyer|recipient)\b", re.I)
VEHICLE_LABEL = re.compile(r"^\s*(vehicle(\s*(details?|info|no\.?|number|reg(istration)?))?|car|registration(\s*no\.?)?|"
                           r"reg\.?\s*no\.?|licen[cs]e\s*plate|plate(\s*no\.?)?|vin)\s*:?\s*$", re.I)
META_LABEL = re.compile(r"\b(invoice|inv|statement|receipt|bill|ref|reference|no|number|date|issued|due|order)\b|#", re.I)
DUE_LABEL = re.compile(r"\bdue\b", re.I)
STAMP = re.compile(r"^\s*(paid|unpaid|paid\s+in\s+full|overdue|void|cancel+ed|due)\s*$", re.I)
NUMBER_WORD = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                         r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
                         r"hundred|thousand|lakh|lac|crore|million|billion|zero)\b", re.I)
CUSTOMER_FIELD_LABELS = [
    ("tax_id", re.compile(r"^\s*(gstin|gst\s*(no\.?|number)?|vat(\s*no\.?)?|tax\s*id|tin|abn|ein|pan)\s*[:#-]", re.I)),
    ("address", re.compile(r"^\s*(address|addr\.?)\s*[:#-]", re.I)),
    ("phone", re.compile(r"^\s*(phone|ph\.?|mobile|mob\.?|tel\.?|contact)\s*[:#-]", re.I)),
    ("email", re.compile(r"^\s*(e-?mail)\s*[:#-]", re.I)),
]
# Checked in this order: "Unit Cost" is a rate, "Units" a quantity.
COLUMN_ROLES = {
    "code": re.compile(r"^(sac|hsn|hsn\s*/\s*sac|sac\s*/\s*hsn|code|sku|part\s*no\.?)\b", re.I),
    "rate": re.compile(r"^(rate|price|unit|each|cost|unit\s*price|unit\s*cost|mrp)\b", re.I),
    "quantity": re.compile(r"^(qty|quantity|hrs|hours|hr|units?|pcs|count|nos?\.?)\b", re.I),
    "amount": re.compile(r"^(amount|total|line\s*total|sum|subtotal|charge|ext|value)\b", re.I),
}
INR = re.compile(r"\b(rs\.?|inr|rupees?)\b|₹|gstin|lakh|crore", re.I)

# PDF font families we can draw, with the bundled files used to match them.
FAMILIES = {
    "Helvetica": ("_a______.pfb", "_ab_____.pfb", 0.718),
    "Times": ("_er_____.pfb", "_eb_____.pfb", 0.662),
    "Courier": ("com_____.pfb", "cob_____.pfb", 0.571),
}


@dataclass
class Row:
    """Lines whose vertical extents overlap — one visual row of the page."""
    lines: List[Line]

    @property
    def y(self) -> int:
        return min(l.y for l in self.lines)

    @property
    def y2(self) -> int:
        return max(l.y2 for l in self.lines)

    @property
    def text(self) -> str:
        return "  ".join(l.text for l in sorted(self.lines, key=lambda l: l.x))


class ExactTemplateError(ValueError):
    """The invoice can't be turned into a fill-in template; fall back to the rebuilt style."""


# ── text geometry ─────────────────────────────────────────────────────────────

_DESCENDERS = set("gjpqy,;()[]{}")


def baseline(line: Line) -> int:
    if line.base is not None:
        return int(line.base)
    # Letters without descenders sit exactly on the baseline. Digits don't
    # always: many serif faces (Georgia) use old-style figures that hang below.
    letters = [w for w in line.words if re.fullmatch(r"[A-Za-z&'’.:-]+", w.text) and not (set(w.text) & _DESCENDERS)]
    clean = letters or [w for w in line.words if not (set(w.text) & _DESCENDERS)]
    if clean:
        return int(sorted(w.y + w.h for w in clean)[len(clean) // 2])
    return int(line.y2 - line.h * 0.2)


def cap_height(line: Line) -> float:
    """Height from the top of capitals/digits/ascenders to the baseline."""
    base = baseline(line)
    if line.base is not None and not line.font:
        return max(4.0, base - line.y)          # measured from the line's own ink
    tall = [w for w in line.words if re.search(r"[A-Zbdfhklt]", w.text)] or \
        [w for w in line.words if re.search(r"[0-9]", w.text)]
    top = min((w.y for w in tall), default=line.y)
    return max(4.0, base - top)


def _rows(lines: List[Line]) -> List[Row]:
    rows: List[Row] = []
    for line in sorted(lines, key=lambda l: l.y + l.h / 2):
        mid = line.y + line.h / 2
        for row in rows:
            if row.y - 2 <= mid <= row.y2 + 2:
                row.lines.append(line)
                break
        else:
            rows.append(Row([line]))
    for r in rows:
        r.lines.sort(key=lambda l: l.x)
    return rows


# ── font matching (images only — PDFs tell us their fonts) ────────────────────

def _font_dir() -> str:
    import reportlab
    return os.path.join(os.path.dirname(reportlab.__file__), "fonts")


def match_font(image_path: str, lines: List[Line], families: Optional[List[str]] = None) -> Tuple[str, bool]:
    """
    Which of Helvetica/Times/Courier (regular or bold) these lines were set in.

    Each line is redrawn in every candidate at its measured size, scaled to its
    exact box, and compared with the real pixels. The highest overlap wins.
    """
    exact = [l.font for l in lines if l.font]
    if exact:
        f = exact[0]
        return str(f["family"]), bool(f["bold"])

    from PIL import Image, ImageDraw, ImageFont

    page = Image.open(image_path).convert("L")
    scores: Dict[Tuple[str, bool], float] = {}
    usable = [l for l in lines if len(l.text) >= 4 and l.w > 20]
    usable.sort(key=lambda l: -sum(ch.isalpha() for ch in l.text))
    for line in usable[:8]:
        crop = page.crop((line.x, line.y, line.x2, line.y2))
        thresh = (min(crop.getextrema()) + max(crop.getextrema())) / 2
        hist = crop.histogram()
        dark = sum(hist[: int(thresh)])
        ink_is_dark = dark < (crop.width * crop.height) / 2
        real = crop.point(lambda v: 255 if ((v < thresh) == ink_is_dark) else 0)
        for family, (reg, bold, _) in FAMILIES.items():
            if families and family not in families:
                continue
            for is_bold, fname in ((False, reg), (True, bold)):
                font = ImageFont.truetype(os.path.join(_font_dir(), fname), max(8, int(line.h * 1.3)))
                l, t, r, b = font.getbbox(line.text)
                canvas = Image.new("L", (max(1, r - l), max(1, b - t)), 0)
                ImageDraw.Draw(canvas).text((-l, -t), line.text, font=font, fill=255)
                cand = canvas.resize(real.size)
                a = list(getattr(real, "get_flattened_data", real.getdata)())
                c = list(getattr(cand, "get_flattened_data", cand.getdata)())
                inter = sum(1 for x, y in zip(a, c) if x > 127 and y > 127)
                union = sum(1 for x, y in zip(a, c) if x > 127 or y > 127) or 1
                # Aspect agreement: Courier text is far wider than Helvetica at the same height.
                aspect = (r - l) / max(1, b - t)
                real_aspect = line.w / max(1, line.h)
                aspect_score = min(aspect, real_aspect) / max(aspect, real_aspect)
                # Stroke weight: share of the box that is ink. Bold fills more.
                d_real = sum(1 for x in a if x > 127) / max(1, len(a))
                d_cand = sum(1 for y in c if y > 127) / max(1, len(c))
                weight = min(d_real, d_cand) / max(d_real, d_cand, 1e-6)
                scores[(family, is_bold)] = scores.get((family, is_bold), 0) + \
                    inter / union * 0.45 + aspect_score * 0.3 + weight * 0.25
    if not scores:
        return "Helvetica", False
    family = max({f for f, _ in scores}, key=lambda f: max(scores.get((f, False), 0), scores.get((f, True), 0)))
    # Bold has to win clearly: heavier regular faces (Georgia) would otherwise tip into bold.
    bold = scores.get((family, True), 0) > scores.get((family, False), 0) * 1.06
    return family, bold


# ── slot construction ─────────────────────────────────────────────────────────

def _is_amount_line(line: Line) -> bool:
    return bool(AMOUNT.search(line.text))


def _ink(image_path: str, line: Line, fallback: str = "#111111") -> str:
    if line.font:
        return str(line.font["color"])
    return ocr.ink_color(image_path, line) or fallback


def _px_box(lines: List[Line], pad: int = 3) -> List[int]:
    return [min(l.x for l in lines) - pad, min(l.y for l in lines) - pad,
            max(l.x2 for l in lines) + pad, max(l.y2 for l in lines) + pad]


def _slot(image_path: str, line: Line, x: Optional[int] = None, align: str = "left",
          font: Optional[Tuple[str, bool]] = None, family: Optional[str] = None) -> Dict[str, Any]:
    """Where and how to draw text that replaces `line`."""
    if line.font:
        f = line.font
        return {"x": int(x if x is not None else line.x), "baseline": baseline(line),
                "size": float(f["size_px"]), "color": str(f["color"]), "font": str(f["family"]),
                "bold": bool(f["bold"]), "italic": bool(f["italic"]), "name": str(f["name"]), "align": align}
    if font is None and family:
        font = (family, match_font(image_path, [line], families=[family])[1])
    family, bold = font or match_font(image_path, [line])
    size = cap_height(line) / FAMILIES[family][2]
    return {"x": int(x if x is not None else line.x), "baseline": baseline(line), "size": round(size, 1),
            "color": _ink(image_path, line), "font": family, "bold": bold, "italic": False, "align": align}


def _money_prefix(text: str) -> str:
    """'Rs. 15,000.00' → 'Rs. ', '$502.00' → '$', '15,000.00' → ''."""
    m = re.match(r"^(.*?)-?\d", text.strip())
    return m.group(1) if m else ""


# ── analysis ──────────────────────────────────────────────────────────────────

def analyse(image_path: str, roles: Dict[int, str], lines: List[Line],
            page_pt: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    """
    Build the fill-in template.

    `lines` come from OCR or a PDF text layer (pixels of `image_path`).
    `roles` maps an index into `lines` to one of: business, title, meta,
    bill_to_label, customer, vehicle, other — only used when there is no
    "Bill To"-style label to anchor on. `page_pt` is the real page size for
    PDFs; images get A4 or Letter from their proportions.
    """
    from PIL import Image

    W, H = Image.open(image_path).size
    rows = _rows(lines)
    notes: List[str] = []
    from_pdf = any(l.font for l in lines)
    page_text = " ".join(l.text for l in lines)
    indian = bool(INR.search(page_text))

    # ── table + totals, by rule ───────────────────────────────────────────
    totals_rows = [r for r in rows if any(_is_amount_line(l) for l in r.lines) and TOTAL_WORDS.search(r.text)]
    item_rows = [r for r in rows if any(_is_amount_line(l) for l in r.lines) and r not in totals_rows]
    if totals_rows and item_rows:
        item_rows = [r for r in item_rows if r.y < totals_rows[0].y]
    if not item_rows:
        raise ExactTemplateError("Couldn't find the line items on this invoice.")
    totals_rows = [r for r in totals_rows if r.y > item_rows[-1].y2]
    header_row = None
    for r in reversed([r for r in rows if r.y2 <= item_rows[0].y + 2]):
        words = [l for l in r.lines if re.search(r"[A-Za-z]{2,}", l.text)]
        if not any(_is_amount_line(l) for l in r.lines) and len(words) >= 2:
            header_row = Row(words)
            break
    if header_row is None:
        raise ExactTemplateError("Couldn't find the item table's column headings.")

    columns = []
    for l in header_row.lines:
        role = "description"
        for name, pat in COLUMN_ROLES.items():
            if pat.match(l.text):
                role = name
                break
        columns.append({"role": role, "label": l.text, "x": l.x, "x2": l.x2})
    # Item names show where the description column really starts — OCR can
    # miss the heading itself (white text on a dark band).
    name_lines = [l for r in item_rows for l in r.lines if not _is_amount_line(l) and not re.fullmatch(r"[\d.,x×]+", l.text)]
    if name_lines:
        name_x = min(l.x for l in name_lines)
        if name_x < columns[0]["x"] - 40:
            columns.insert(0, {"role": "description", "label": "", "x": name_x, "x2": name_x + 40})
    columns.sort(key=lambda c: c["x"])
    for i, c in enumerate(columns):
        if i == 0:
            c["role"] = "description"
        elif c["role"] == "description":
            c["role"] = "amount" if i == len(columns) - 1 else "rate"
    for c in columns:
        cells = [l for r in item_rows for l in r.lines
                 if abs(l.cx - (c["x"] + c["x2"]) / 2) < (c["x2"] - c["x"]) / 2 + 60]
        rights = [l.x2 for l in cells]
        lefts = [l.x for l in cells]
        # A column of non-money codes (SAC/HSN 998714) is printed as-is on every row.
        if c["role"] in ("rate", "code") and cells and all(re.fullmatch(r"[A-Z0-9/-]{3,}", l.text) and not _is_amount_line(l)
                                                          for l in cells):
            c["role"], c["sample"] = "code", cells[0].text
        if c["role"] != "description" and rights and (max(rights) - min(rights)) <= (max(lefts) - min(lefts)) \
                and not (len(cells) == 1 and abs(lefts[0] - c["x"]) < 6):
            c["align"], c["anchor"] = "right", int(sorted(rights)[len(rights) // 2])
        elif c["role"] != "description" and len(cells) > 1 and (max(l.cx for l in cells) - min(l.cx for l in cells)) < 12:
            c["align"], c["anchor"] = "center", int(sum(l.cx for l in cells) / len(cells))
        else:
            c["align"], c["anchor"] = "left", int(min(lefts) if lefts else c["x"])
        # Values that end where the heading ends are right-aligned to it.
        if cells and c["role"] in ("amount", "rate", "quantity") and abs(max(rights) - c["x2"]) < 8:
            c["align"], c["anchor"] = "right", int(max(rights))

    item_lines = [l for r in item_rows for l in r.lines]
    desc_lines = [l for l in item_lines if not _is_amount_line(l) and not re.fullmatch(r"[\d.,x×/-]+", l.text)]
    amount_lines = [l for l in item_lines if _is_amount_line(l)]
    item_font = match_font(image_path, desc_lines or item_lines)
    body_family = item_font[0]
    sample = desc_lines[0] if desc_lines else item_lines[0]

    zone_top = _below_fill(image_path, header_row.y2, item_rows[0].y,
                           min(c["x"] for c in columns), max(c["x2"] for c in columns))
    if len(item_rows) > 1:
        pitch = (baseline(item_rows[-1].lines[0]) - baseline(item_rows[0].lines[0])) / (len(item_rows) - 1)
    else:
        pitch = None
    flow_lines = [l for r in item_rows + totals_rows for l in r.lines]
    table_left = min([c["x"] for c in columns] + [l.x for l in flow_lines]) - 12
    table_right = max([c["x2"] for c in columns] + [l.x2 for l in flow_lines]) + 14

    first_base = baseline(item_rows[0].lines[0])
    rule = _row_rule(image_path, table_left + 12, table_right - 12, item_rows,
                     until=(totals_rows[0].y if totals_rows else first_base + 400))
    if pitch is None:
        # One sample row: keep its top padding (first baseline) and bottom
        # padding (rule offset), and space further rows like normal lines.
        size = float(sample.font["size_px"]) if sample.font else cap_height(sample) / FAMILIES[body_family][2]
        pitch = size * 2.6
    if rule:
        rule["mode"] = "each" if len(item_rows) > 1 and rule.get("between") else "after_last"
    items_bottom = item_rows[-1].y2 + (int(rule["offset"] - (item_rows[-1].y2 - baseline(item_rows[-1].lines[0])) + rule["width"] + 4) if rule else 6)
    rule_left, rule_right = _rule_extent(image_path, zone_top, items_bottom, table_left, table_right)
    if rule:
        rule["x1"], rule["x2"] = rule_left, rule_right
    table_left, table_right = min(table_left, rule_left - 3), max(table_right, rule_right + 3)

    erase: List[Dict[str, Any]] = []
    erase.append({"box": [table_left, zone_top, table_right, items_bottom], "mode": "text", "graphics": True})
    erased_texts = [l.text for l in item_lines]

    # ── totals: anchored where they were ──────────────────────────────────
    totals: Dict[str, Any] = {}
    if totals_rows:
        anchored = []
        for r in totals_rows:
            value = max((l for l in r.lines if _is_amount_line(l)), key=lambda l: l.x2)
            left = [l for l in r.lines if l.x2 <= value.x + 4 and TOTAL_WORDS.search(l.text)]
            label = max(left, key=lambda l: l.x2) if left else None
            text = (AMOUNT.sub("", label.text) if label else "").strip(" :")
            kind = ("subtotal" if re.search(r"sub|taxable", text, re.I) else
                    "tax" if re.search(r"tax|gst|vat|hst|pst", text, re.I) else
                    "discount" if re.search(r"discount", text, re.I) else "total")
            anchored.append({
                "kind": kind, "label_text": text,
                "label": _slot(image_path, label, family=body_family) if label else None,
                "value": _slot(image_path, value, family=body_family, align="right"),
                "value_x2": value.x2, "prefix": _money_prefix(value.text),
            })
            for l in ([label] if label else []) + [value]:
                erase.append({"box": _px_box([l], pad=3), "mode": "text"})
            erased_texts.append(value.text)
        anchored[-1]["kind"] = "total"
        bases = [baseline(r.lines[0]) for r in totals_rows]
        totals = {
            "rows": anchored,
            "pitch": round((bases[-1] - bases[0]) / (len(bases) - 1), 1) if len(bases) > 1 else round(pitch * 0.75, 1),
            "baselines": bases,
            "wording": {a["kind"]: a["label_text"] for a in anchored if a["label_text"]},
            "tax_word": "GST" if re.search(r"\bgst", page_text, re.I) else "Tax",
        }
    else:
        notes.append("No totals block found — totals will be printed under the items.")

    # The totals section can move down when a long invoice needs more rows:
    # everything from its first line to the bottom of the grand-total band,
    # with however much empty page lies below it.
    block = None
    if totals_rows:
        grand_value_line = max((l for l in totals_rows[-1].lines if _is_amount_line(l)), key=lambda l: l.x2)
        band_bottom = _fill_bottom(image_path, int(grand_value_line.cx), grand_value_line.y2)
        b_top = min(r.y for r in totals_rows) - 8
        b_bottom = max(band_bottom, max(r.y2 for r in totals_rows)) + 6
        # Lines sharing those rows (e.g. "Total In Words" beside the totals) move with them.
        for l in lines:
            if l.y >= b_top - 4 and l.y < b_bottom + l.h * 2.5 and l.y > items_bottom:
                b_bottom = max(b_bottom, l.y2 + 6)
        next_top = _next_content_below(image_path, b_bottom)
        block = {"top": b_top, "bottom": b_bottom, "slack": max(0, next_top - b_bottom - 24)}

    # Limit of the item area: the first totals row, or the next element below.
    below_items = [r for r in rows if r.y > items_bottom]
    items_limit = (min(r.y for r in totals_rows) - 6) if totals_rows else ((below_items[0].y - 8) if below_items else int(H * 0.9))

    # ── header ────────────────────────────────────────────────────────────
    header_lines = [l for l in lines if l.y2 <= header_row.y]
    idx = {id(l): i for i, l in enumerate(lines)}
    word_role = {id(w): roles.get(i, "other") for i, l in enumerate(lines) for w in l.words}

    def role_of(line: Line) -> str:
        if id(line) in idx:
            return roles.get(idx[id(line)], "other")
        found = [word_role.get(id(w), "other") for w in line.words]
        return next((r for r in found if r != "other"), "other")

    slots: Dict[str, Any] = {}

    # Number / date values. OCR sometimes splits "Date Oct 14, 2026" into
    # pieces, so rejoin close neighbours first. PDF lines are already whole.
    meta_segments = []
    for l in (header_lines if from_pdf else _segments(header_lines)):
        text = l.text
        if not re.search(r"\d", text) or not (META_LABEL.search(text) or role_of(l) == "meta"):
            continue
        if role_of(l) == "business" or any(pat.match(text) for _, pat in CUSTOMER_FIELD_LABELS):
            continue                       # "Address: … SY No.14P" is not an invoice number
        date_m = DATE.search(text)
        if date_m:
            key = "due_date" if DUE_LABEL.search(text[: date_m.start()]) else "issue_date"
            value_words = _words_from(l, date_m.start())
        else:
            key = "invoice_number"
            digits = [w for w in l.words if re.search(r"\d", w.text)]
            value_words = l.words[l.words.index(digits[0]):] if digits else []
        if not value_words or key in slots:
            continue
        vline = ocr._line(value_words)
        slot = _slot(image_path, vline if not l.font else l, family=body_family)
        label_words = [w for w in l.words if w not in value_words]
        label_text = " ".join(w.text for w in label_words)
        if from_pdf:
            # The label and value are one text object in a PDF; both are
            # removed, so both are redrawn — in the identical font.
            slot["label_text"] = label_text + " " if label_text else ""
            slot["x"] = l.x
        else:
            slot["x"] = vline.x
            if label_words:
                label_line = ocr._line(label_words)
                slot["baseline"] = baseline(label_line)
                slot["size"] = round(cap_height(label_line) / FAMILIES[slot["font"]][2], 1)
        slot["sample"] = vline.text
        slot["x2"] = l.x2 if from_pdf else vline.x2
        slots[key] = slot
        meta_segments.append((key, l, vline))
        erase.append({"box": _px_box([l if from_pdf else vline]), "mode": "text"})
        erased_texts.append(vline.text)
    # A right-aligned block of number/date lines stays right-aligned.
    if len(meta_segments) >= 2:
        rights = [(l if from_pdf else v).x2 for _, l, v in meta_segments]
        if max(rights) - min(rights) <= 6:
            for key, _, _ in meta_segments:
                slots[key]["align"] = "right"
                slots[key]["x"] = slots[key]["x2"]

    # Status stamps ("PAID") are per-invoice: remove, and redraw only when it applies.
    totals_line_ids = {id(l) for r in totals_rows for l in r.lines}
    for l in lines:
        # "Paid" as the label of a totals line is wording, not a stamp.
        if STAMP.match(l.text) and id(l) not in totals_line_ids and l.y2 < (totals_rows[-1].y2 if totals_rows else H) \
                and len(l.text) <= 14:
            box = _stamp_box(image_path, l)
            slots["stamp"] = {**_slot(image_path, l), "box": box, "text": l.text.strip().upper(),
                              "centre": [l.cx, (l.y + l.y2) / 2]}
            erase.append({"box": [box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3], "mode": "fill", "graphics": True,
                          "stamp": True})
            erased_texts.append(l.text)
            break

    # Amount in words ("Fifteen Thousand Only", "Rupees Two Lakh Only").
    for l in lines:
        if re.search(r"\bonly\b", l.text, re.I) and len(NUMBER_WORD.findall(l.text)) >= 1 and l.y > header_row.y:
            words = l.text
            first = NUMBER_WORD.search(words)
            last = list(NUMBER_WORD.finditer(words))[-1]
            slots["amount_words"] = {**_slot(image_path, l, family=body_family),
                                     "prefix": words[: first.start()], "suffix": words[last.end():],
                                     "max_px": max(l.w * 1.8, W * 0.42)}
            erase.append({"box": _px_box([l], pad=4), "mode": "text"})
            erased_texts.append(l.text)
            break

    # Customer block: lines under a "Bill To" label (as whole region), plus
    # whatever the model called customer when there is no label.
    customer_lines = [l for l in header_lines if role_of(l) in ("customer",)]
    label = next((l for l in header_lines if BILL_TO.match(l.text) and len(l.text) <= 24
                  and not VEHICLE_LABEL.match(l.text)), None)
    block_limit = _above_fill(image_path, header_row.y, (label.y2 if label else header_row.y - 200),
                              min(c["x"] for c in columns), max(c["x2"] for c in columns)) - 4
    if label:
        customer_lines = [l for l in customer_lines if l is not label]
        for l in _column_under(label, header_lines, block_limit):
            if l not in customer_lines and not STAMP.match(l.text):
                customer_lines.append(l)
    customer_lines.sort(key=lambda l: (l.y, l.x))
    if customer_lines:
        first = customer_lines[0]
        detail_lines = [l for l in customer_lines[1:] if baseline(l) > baseline(first) + first.h * 0.5]
        crow_bases = sorted({baseline(r.lines[0]) for r in _rows(customer_lines)})
        gaps = [b - a for a, b in zip(crow_bases, crow_bases[1:]) if b - a > first.h * 0.5]
        gaps_detail = gaps[1:] or gaps
        cust = _slot(image_path, first, family=body_family)
        cust["pitch"] = int(sorted(gaps_detail)[len(gaps_detail) // 2]) if gaps_detail else int(first.h * 1.5)
        cust["first_gap"] = int(gaps[0]) if gaps else cust["pitch"]
        cust["limit"] = block_limit
        cust["wrap_px"] = max(l.x2 for l in customer_lines) - first.x   # refined below with the area width
        cust["had_vehicle"] = any(re.search(r"\b(19|20)\d{2}\b", l.text) and not re.search(r"\d{5,}", l.text)
                                  for l in detail_lines)
        cust["labels"] = {}
        for l in detail_lines:
            for field, pat in CUSTOMER_FIELD_LABELS:
                m = pat.match(l.text)
                if m and field not in cust["labels"]:
                    cust["labels"][field] = l.text[: m.end()].strip() + " "
        if detail_lines:
            cust["detail"] = _slot(image_path, detail_lines[0], x=first.x, family=body_family)
        slots["customer"] = cust
        area_top = (label.y2 + 2) if label else first.y - 4
        area_left = min(l.x for l in customer_lines) - 6
        right_neighbours = [l.x for l in header_lines if l.x > max(l2.x2 for l2 in customer_lines) + 20
                            and l.y2 > area_top and l.y < block_limit]
        area_right = (min(right_neighbours) - 12) if right_neighbours else int(W * 0.97)
        area_right = max(area_right, max(l.x2 for l in customer_lines) + 4)
        erase.append({"box": [area_left, area_top, area_right, block_limit], "mode": "text"})
        erased_texts += [l.text for l in customer_lines]
        # Several wrapped lines in the original show the width their software
        # wraps at — keep it. Otherwise use the room the block really has.
        if len(detail_lines) < 3:
            cust["wrap_px"] = max(cust["wrap_px"], area_right - first.x - 16)
    else:
        notes.append("Couldn't find where the customer's details go.")

    # Vehicle section ("VEHICLE DETAILS" → "TS07JM0033").
    vlabel = next((l for l in header_lines if VEHICLE_LABEL.match(l.text)), None)
    if vlabel:
        vlines = [l for l in _column_under(vlabel, header_lines, block_limit) if not STAMP.match(l.text)]
        if vlines:
            slots["vehicle"] = _slot(image_path, vlines[0], family=body_family)
            slots["vehicle"]["pitch"] = int(vlines[1].y - vlines[0].y) if len(vlines) > 1 else int(vlines[0].h * 1.4)
            right_neighbours = [l.x for l in header_lines if l.x > max(v.x2 for v in vlines) + 20
                                and l.y2 > vlabel.y2 and l.y < vlines[-1].y2 + 4 and l not in customer_lines]
            v_right = (min(right_neighbours) - 12) if right_neighbours else int(W * 0.48)
            erase.append({"box": [vlabel.x - 6, vlabel.y2 + 2, max(v_right, max(v.x2 for v in vlines) + 4),
                                  max(v.y2 for v in vlines) + 6], "mode": "text"})
            erased_texts += [l.text for l in vlines]

    # ── numbering, formats, page ──────────────────────────────────────────
    sample_no = slots.get("invoice_number", {}).get("sample", "")
    digits = re.search(r"(\d+)\s*$", sample_no)
    page_w_pt, page_h_pt = page_pt or _paper_for(W, H)

    return {
        "version": 2,
        "source": "pdf" if from_pdf else "image",
        "page": {"w": W, "h": H, "w_pt": page_w_pt, "h_pt": page_h_pt},
        "erased_texts": [t for t in erased_texts if len(re.sub(r"\W", "", t)) >= 3],
        "slots": slots,
        "table": {
            "columns": columns, "first_baseline": first_base,
            "zone_top": zone_top, "zone_limit": items_limit, "pitch": round(pitch, 1),
            "left": table_left, "right": table_right,
            "text": _slot(image_path, sample, font=item_font),
            "amount_color": _ink(image_path, amount_lines[0]) if amount_lines else None,
            "money_prefix": _money_prefix(amount_lines[0].text) if amount_lines else "",
            "rule": rule,
            "header_labels": {c["role"]: c["label"] for c in columns},
        },
        "totals": totals,
        "totals_block": block,
        "single_row_sample": len(item_rows) == 1,
        "erase": erase,
        "indian_numbering": indian,
        "number_prefix": _prefix(sample_no),
        "number_digits": len(digits.group(1)) if digits and digits.group(1).startswith("0") else 0,
        "number_last": int(digits.group(1)) if digits else 0,
        "date_format": _date_format(slots.get("issue_date", {}).get("sample", "")),
        "notes": notes,
    }


def _page_bg(img) -> Tuple[int, int, int]:
    w, h = img.size
    samples = [img.getpixel((int(w * fx), int(h * fy))) for fx in (0.3, 0.5, 0.7) for fy in (0.02, 0.5, 0.98)]
    return tuple(sorted(p[i] for p in samples)[len(samples) // 2] for i in range(3))


def _fill_bottom(image_path: str, x: int, y: int) -> int:
    """If (x, y) sits on a coloured band, where that band ends going down; else y."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    bg = _page_bg(img)
    yy = y
    while yy < img.height - 1 and sum(abs(img.getpixel((x, yy))[i] - bg[i]) for i in range(3)) > 30:
        yy += 1
    return yy


def _next_content_below(image_path: str, y: int) -> int:
    """First row under `y` with anything on it, ignoring the page's side margins (edge stripes)."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    bg = _page_bg(img)
    w = img.width
    for yy in range(y, img.height):
        hits = 0
        for x in range(int(w * 0.05), int(w * 0.95), 4):
            if sum(abs(img.getpixel((x, yy))[i] - bg[i]) for i in range(3)) > 40:
                hits += 1
                if hits > 2:
                    return yy
    return img.height - 20


def _paper_for(W: int, H: int) -> Tuple[float, float]:
    """A4 for A4-shaped pages, US Letter otherwise."""
    ratio = H / max(1, W)
    if abs(ratio - 841.89 / 595.28) < 0.04:
        return 595.28, 841.89
    return 612.0, 612.0 * ratio


def _column_under(label: Line, lines: List[Line], limit: int) -> List[Line]:
    """Lines stacked directly under `label`, left-aligned with it, until a gap."""
    under = sorted([l for l in lines if l.y > label.y2 - 2 and abs(l.x - label.x) < 40 and l.y < limit],
                   key=lambda l: l.y)
    out: List[Line] = []
    prev = label
    for l in under:
        if l.y - prev.y2 > max(prev.h, l.h) * 2.2:
            break
        if BILL_TO.match(l.text) and len(l.text) <= 24 or VEHICLE_LABEL.match(l.text):
            break
        out.append(l)
        prev = l
    return out


def _stamp_box(image_path: str, line: Line) -> List[int]:
    """The stamp's frame: walk outward from its text until the coloured border ends."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    ink = ocr.ink_color(image_path, line) if not line.font else str(line.font["color"])
    ink = tuple(int(ink[i:i + 2], 16) for i in (1, 3, 5)) if ink else (0, 128, 0)

    def is_ink(p) -> bool:
        return sum(abs(p[i] - ink[i]) for i in range(3)) < 120

    pad_x, pad_y = int(line.h * 2.5), int(line.h * 1.2)
    x0, y0 = max(0, line.x - pad_x), max(0, line.y - pad_y)
    x1, y1 = min(img.width - 1, line.x2 + pad_x), min(img.height - 1, line.y2 + pad_y)
    left, top, right, bottom = line.x, line.y, line.x2, line.y2
    for x in range(line.x, x0, -1):
        if sum(is_ink(img.getpixel((x, y))) for y in range(line.y, line.y2)) > line.h * 0.6:
            left = x
    for x in range(line.x2, x1):
        if sum(is_ink(img.getpixel((x, y))) for y in range(line.y, line.y2)) > line.h * 0.6:
            right = x
    for y in range(line.y, y0, -1):
        if sum(is_ink(img.getpixel((x, y))) for x in range(line.x, line.x2)) > line.w * 0.6:
            top = y
    for y in range(line.y2, y1):
        if sum(is_ink(img.getpixel((x, y))) for x in range(line.x, line.x2)) > line.w * 0.6:
            bottom = y
    return [left, top, right, bottom]


def _segments(lines: List[Line]) -> List[Line]:
    """Lines on the same row that sit close together, joined back into one."""
    out: List[Line] = []
    for row in _rows(lines):
        group = [row.lines[0]]
        for l in row.lines[1:]:
            prev = group[-1]
            if l.x - prev.x2 < max(prev.h, l.h) * 2.2:
                group.append(l)
            else:
                out.append(ocr._line([w for g in group for w in g.words]))
                group = [l]
        out.append(ocr._line([w for g in group for w in g.words]))
    return out


def _rule_extent(image_path: str, top: int, bottom: int, left: int, right: int) -> Tuple[int, int]:
    """Leftmost start and rightmost end of long horizontal lines between top and bottom."""
    from PIL import Image

    rgb = Image.open(image_path).convert("RGB")
    bg = sum(_page_bg(rgb)) / 3
    img = rgb.convert("L")
    found: List[Tuple[int, int]] = []
    base = max(0, left - 60)
    for y in range(max(0, top), min(img.height, bottom)):
        row = [img.getpixel((x, y)) for x in range(base, min(img.width, right + 60))]
        # The line's own continuous run through the table's middle — not a
        # page-edge stripe that happens to share the pixel row.
        mid = (left + right) // 2 - base
        if not (0 <= mid < len(row)) or row[mid] >= bg - 25:
            continue
        a = b = mid
        while a > 0 and row[a - 1] < bg - 25:
            a -= 1
        while b < len(row) - 1 and row[b + 1] < bg - 25:
            b += 1
        if b - a > (right - left) * 0.5:
            found.append((base + a, base + b))
    if not found:
        return left, right
    return min(a for a, _ in found), max(b for _, b in found)


def _above_fill(image_path: str, start: int, stop: int, left: int, right: int) -> int:
    """Walking up from the table heading text, the first row that is plain background (top of any band)."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    ref = img.getpixel((max(0, left - 20), max(0, stop + 2)))
    for y in range(start, max(stop, 0), -1):
        samples = [img.getpixel((x, y)) for x in range(left, right, max(1, (right - left) // 40))]
        same = sum(1 for p in samples if sum(abs(p[i] - ref[i]) for i in range(3)) < 40)
        if same > len(samples) * 0.9:
            return y
    return start


def _below_fill(image_path: str, top: int, bottom: int, left: int, right: int) -> int:
    """First pixel row under the table heading that is plain background — below any filled band."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    ref = img.getpixel((max(0, left - 20), min(img.height - 1, bottom + 2)))
    for y in range(top, max(top + 1, bottom)):
        samples = [img.getpixel((x, y)) for x in range(left, right, max(1, (right - left) // 40))]
        same = sum(1 for p in samples if sum(abs(p[i] - ref[i]) for i in range(3)) < 40)
        if same > len(samples) * 0.9:
            return y + 1
    return top + max(2, (bottom - top) // 3)


def _words_from(line: Line, char_index: int):
    pos = 0
    for i, w in enumerate(line.words):
        if pos + len(w.text) > char_index:
            return line.words[i:]
        pos += len(w.text) + 1
    return line.words[-1:]


def _prefix(sample: str) -> str:
    m = re.match(r"^\s*([^\d]*?)\s*\d", sample or "")
    return m.group(1) if m else ""


def _date_format(sample: str) -> str:
    s = (sample or "").strip()
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", s):
        return "YYYY-MM-DD"
    if re.match(rf"^\d{{1,2}}-{MONTHS}-\d{{4}}$", s, re.I):
        return "DD-Mon-YYYY"
    if re.match(rf"^\d{{1,2}}\s+{MONTHS}\s+\d{{4}}$", s, re.I):
        return "D Mon YYYY"
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            return "DD/MM/YYYY"
        return "MM/DD/YYYY"
    if MONTH.match(s):
        return "Mon D, YYYY" if re.match(r"^[A-Za-z]{3}\.?\s", s) else "Month D, YYYY"
    return "MM/DD/YYYY"


def _row_rule(image_path: str, left: int, right: int, item_rows: List[Row], until: int) -> Optional[Dict[str, Any]]:
    """
    A horizontal rule under the first item row, if there is one: offset below
    its baseline, colour, weight, and whether it also appears between rows.
    """
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    row = item_rows[0]
    base = baseline(row.lines[0])
    stop = item_rows[1].y if len(item_rows) > 1 else until
    # Compare against the page's own background — on a rule's own pixel row
    # the rule colour would otherwise *be* the "background".
    bg = sum(_page_bg(img))
    best = None
    for y in range(row.y2 + 1, min(stop, img.height)):
        samples = [img.getpixel((x, y)) for x in range(left, right, max(1, (right - left) // 60))]
        dark = [p for p in samples if sum(p) < min(690, bg - 40)]
        if len(dark) > len(samples) * 0.8:
            r, g, b = (sum(p[i] for p in dark) // len(dark) for i in range(3))
            if best is None:
                best = {"offset": y - base, "color": "#{:02X}{:02X}{:02X}".format(r, g, b), "width": 1}
            else:
                best["width"] += 1
        elif best:
            break
    if best:
        best["between"] = len(item_rows) > 1
    return best


# ── amount in words ───────────────────────────────────────────────────────────

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(cents: int, indian: bool) -> str:
    """15000.00 → 'Fifteen Thousand'; Indian numbering uses Lakh and Crore."""
    whole, paise = divmod(abs(int(cents)), 100)
    if whole == 0:
        words = "Zero"
    elif indian:
        parts = []
        for size, name in ((10 ** 7, "Crore"), (10 ** 5, "Lakh"), (10 ** 3, "Thousand")):
            if whole >= size:
                parts.append(f"{amount_in_words(whole // size * 100, True)} {name}")
                whole %= size
        if whole:
            parts.append(_below_thousand(whole))
        words = " ".join(parts)
    else:
        parts = []
        for size, name in ((10 ** 9, "Billion"), (10 ** 6, "Million"), (10 ** 3, "Thousand")):
            if whole >= size:
                parts.append(f"{_below_thousand(whole // size)} {name}")
                whole %= size
        if whole:
            parts.append(_below_thousand(whole))
        words = " ".join(parts)
    if paise:
        words += f" and {_below_thousand(paise)} {'Paise' if indian else 'Cents'}"
    return words


# ── blank page + leak guard ───────────────────────────────────────────────────

def find_leaks(blank_path: str, template: Dict[str, Any], work_path: str) -> List[str]:
    """
    Read the blank page back and list any erased text that is still legible.

    For PDFs this reads the blank file's own text layer (exact); for images,
    OCR. A template whose blank page still shows a previous customer's details
    is never used.
    """
    words = lambda t: re.findall(r"[a-z0-9]+", t.lower())
    if blank_path.lower().endswith(".pdf"):
        from . import pdf_text
        seen_text = pdf_text.text_of(blank_path)
    else:
        seen_text = " ".join(l.text for l in ocr.read_lines(blank_path, work_path))
    seen = " " + " ".join(words(seen_text)) + " "
    leaks = []
    for text in template.get("erased_texts", []):
        key = " ".join(words(text))
        # Whole words only — "Detail" (an item) must not match "DETAILING" (their name).
        if len(key.replace(" ", "")) >= 4 and f" {key} " in seen:
            leaks.append(text)
    return leaks


def make_blank(image_path: str, erase: List[Any], out_path: str) -> str:
    """
    Image invoices: remove the variable content.

    "text" areas lose only their ink — each pixel that differs from its local
    background is replaced by that background, so coloured bands and tints
    under the text survive. "fill" areas (stamps) are painted out entirely.
    """
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    for entry in erase:
        box = entry["box"] if isinstance(entry, dict) else entry
        mode = entry.get("mode", "fill") if isinstance(entry, dict) else "fill"
        x, y, x2, y2 = (int(v) for v in box)
        x, y = max(0, x), max(0, y)
        x2, y2 = min(img.width - 1, x2), min(img.height - 1, y2)
        if x2 <= x or y2 <= y:
            continue
        if mode == "text":
            m = 24
            cx, cy, cx2, cy2 = max(0, x - m), max(0, y - m), min(img.width, x2 + m), min(img.height, y2 + m)
            crop = img.crop((cx, cy, cx2, cy2))
            small = crop.resize((max(1, crop.width // 4), max(1, crop.height // 4)))
            bg = small.filter(ImageFilter.MedianFilter(7)).resize(crop.size)
            # Judge the background by the ring just outside the box — text-dense
            # boxes (a big bold total) are mostly ink inside.
            ring = []
            for xx in range(x, x2, max(1, (x2 - x) // 40)):
                for yy in (max(0, y - 3), min(img.height - 1, y2 + 3)):
                    ring.append(img.getpixel((xx, yy)))
            for yy in range(y, y2, max(1, (y2 - y) // 12)):
                for xx in (max(0, x - 3), min(img.width - 1, x2 + 3)):
                    ring.append(img.getpixel((xx, yy)))
            med = tuple(sorted(p[i] for p in ring)[len(ring) // 2] for i in range(3))
            # Tolerant enough for a photo's paper tint and sensor noise.
            uniform = sum(1 for p in ring if sum(abs(p[i] - med[i]) for i in range(3)) < 42) >= len(ring) * 0.8
            if uniform:
                # One background colour around the text: a solid fill is exact and leaves no halo.
                draw.rectangle([x, y, x2, y2], fill=med)
            else:
                # A band or tint under the text: replace only the ink, keep the fill.
                diff = ImageChops.difference(crop, bg).convert("L")
                mask = diff.point(lambda v: 255 if v > 18 else 0).filter(ImageFilter.MaxFilter(7))
                healed = Image.composite(bg, crop, mask)
                img.paste(healed.crop((x - cx, y - cy, x2 - cx, y2 - cy)), (x, y))
        else:
            ring = []
            for xx in range(x, x2, max(1, (x2 - x) // 30)):
                for yy in (max(0, y - 3), min(img.height - 1, y2 + 3)):
                    ring.append(img.getpixel((xx, yy)))
            for yy in range(y, y2, max(1, (y2 - y) // 10)):
                for xx in (max(0, x - 3), min(img.width - 1, x2 + 3)):
                    ring.append(img.getpixel((xx, yy)))
            fill = tuple(sorted(p[i] for p in ring)[len(ring) // 2] for i in range(3))
            draw.rectangle([x, y, x2, y2], fill=fill)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def header_lines_for_model(lines: List[Line], W: int, H: int, stop_y: int) -> List[Tuple[int, str]]:
    """The numbered header lines, in reading order, for the role prompt."""
    out = []
    for row in _rows([l for l in lines if l.y2 <= stop_y]):
        for l in row.lines:
            out.append((lines.index(l), f"x={l.x * 100 // W}% y={l.y * 100 // H}% size={l.h} \"{l.text}\""))
    return out


def find_table_top(lines: List[Line]) -> int:
    """y of the item table's heading row, for limiting the model to header lines."""
    rows = _rows(lines)
    amount_rows = [r for r in rows if any(_is_amount_line(l) for l in r.lines) and not TOTAL_WORDS.search(r.text)]
    if not amount_rows:
        return max((l.y2 for l in lines), default=0)
    for r in reversed([r for r in rows if r.y2 <= amount_rows[0].y + 2]):
        if not any(_is_amount_line(l) for l in r.lines) and len(r.lines) >= 2:
            return r.y
    return amount_rows[0].y
