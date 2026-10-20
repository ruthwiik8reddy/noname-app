"""
services/invoices/pdf_text.py — read and edit a PDF invoice directly.

Most invoices a studio uploads are PDFs their software produced, and those
carry a text layer: every character's exact position, font, size and colour.
That is better than any OCR, so for such PDFs:

* `read_lines` returns the same `Line` objects OCR does, but exact — with the
  real font name, size, colour and baseline attached.
* `redact` removes only the job-specific text (and stamp/rule graphics) from
  the studio's *own* PDF and writes a clean copy. Logo, seals, signatures,
  bands and rules stay as their original vector artwork — not a picture of it.
* `text_of` reads a PDF's text back, for the leak guard.

Coordinates: PDF points have their origin at the bottom-left. Everything
returned here is converted to pixels of the analysis rendering (top-left
origin, `scale` px per pt) so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import ctypes
import logging
import re
from typing import List, Sequence, Tuple

from .ocr import Line, Word

logger = logging.getLogger(__name__)

MIN_CHARS = 40          # fewer than this and it's effectively a scanned image
STANDARD_FONTS = {
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
}


def _doc(path: str):
    import pypdfium2 as pdfium
    return pdfium.PdfDocument(path)


def has_text_layer(path: str) -> bool:
    try:
        doc = _doc(path)
        n = doc[0].get_textpage().count_chars() if len(doc) else 0
        doc.close()
        return n >= MIN_CHARS
    except Exception:  # noqa: BLE001
        return False


def page_size(path: str) -> Tuple[float, float]:
    doc = _doc(path)
    w, h = doc[0].get_size()
    doc.close()
    return float(w), float(h)


def render(path: str, out_png: str, scale: float) -> str:
    doc = _doc(path)
    doc[0].render(scale=scale).to_pil().convert("RGB").save(out_png, "PNG", optimize=True)
    doc.close()
    return out_png


def family_of(font_name: str) -> Tuple[str, bool, bool]:
    """Map a PDF font name to (Helvetica|Times|Courier, bold, italic)."""
    n = (font_name or "").split("+")[-1]
    low = n.lower()
    if re.search(r"times|serif(?!.*sans)|georgia|garamond|book|roman|minion|cambria", low) and "sans" not in low:
        family = "Times"
    elif re.search(r"courier|mono|consol|menlo", low):
        family = "Courier"
    else:
        family = "Helvetica"
    bold = bool(re.search(r"bold|black|heavy|semibold|demi", low))
    italic = bool(re.search(r"italic|oblique", low))
    return family, bold, italic


def read_lines(path: str, scale: float) -> List[Line]:
    """Every printed line of page 1, exact, in pixels at `scale`."""
    import pypdfium2.raw as raw

    doc = _doc(path)
    page = doc[0]
    _, page_h = page.get_size()
    tp = page.get_textpage()
    chars = []
    for i in range(tp.count_chars()):
        ch = tp.get_text_range(i, 1)
        if ch in "\r\n\x00":
            chars.append(None)
            continue
        left, bottom, right, top = tp.get_charbox(i)
        ox, oy = ctypes.c_double(), ctypes.c_double()
        raw.FPDFText_GetCharOrigin(tp.raw, i, ctypes.byref(ox), ctypes.byref(oy))
        buf = ctypes.create_string_buffer(128)
        flags = ctypes.c_int(0)
        raw.FPDFText_GetFontInfo(tp.raw, i, buf, 128, ctypes.byref(flags))
        r, g, b, a = (ctypes.c_uint() for _ in range(4))
        raw.FPDFText_GetFillColor(tp.raw, i, *(ctypes.byref(x) for x in (r, g, b, a)))
        chars.append({
            "ch": ch, "x": left, "x2": right, "top": top, "bottom": bottom,
            "ox": ox.value, "base": oy.value, "size": raw.FPDFText_GetFontSize(tp.raw, i),
            "font": buf.value.decode("latin-1", "replace"),
            "color": "#{:02X}{:02X}{:02X}".format(r.value, g.value, b.value),
        })
    doc.close()

    # Runs: consecutive characters sharing a baseline, font, size and colour,
    # split where the horizontal gap is wider than a word space.
    lines: List[Line] = []
    run: List[dict] = []

    def flush() -> None:
        nonlocal run
        text_chars = [c for c in run if c["ch"].strip()]
        if text_chars:
            lines.append(_to_line(run, page_h, scale))
        run = []

    for c in chars:
        if c is None:
            flush()
            continue
        if run:
            p = run[-1]
            same = (abs(c["base"] - p["base"]) < max(1.0, p["size"] * 0.25) and c["font"] == p["font"]
                    and abs(c["size"] - p["size"]) < 0.1 and c["color"] == p["color"])
            gap = c["x"] - max(q["x2"] for q in run)
            if not same or gap > p["size"] * 1.1 or gap < -p["size"] * 2:
                flush()
        run.append(c)
    flush()
    return _merge_split_runs(lines)


def _to_line(run: List[dict], page_h: float, scale: float) -> Line:
    words: List[Word] = []
    current: List[dict] = []

    def close() -> None:
        if current:
            x = min(c["x"] for c in current)
            x2 = max(c["x2"] for c in current)
            top = max(c["top"] for c in current)
            bottom = min(c["bottom"] for c in current)
            words.append(Word(text="".join(c["ch"] for c in current), conf=100.0,
                              x=int(x * scale), y=int((page_h - top) * scale),
                              w=max(1, int((x2 - x) * scale)), h=max(1, int((top - bottom) * scale)),
                              key=(0, 0, 0)))
            current.clear()

    for c in run:
        if c["ch"].isspace():
            close()
        else:
            current.append(c)
    close()
    first = run[0]
    family, bold, italic = family_of(first["font"])
    x = min(w.x for w in words)
    y = min(w.y for w in words)
    x2 = max(w.x + w.w for w in words)
    y2 = max(w.y + w.h for w in words)
    return Line(
        text=" ".join(w.text for w in words), x=x, y=y, w=x2 - x, h=y2 - y, words=words,
        font={"name": first["font"].split("+")[-1], "family": family, "bold": bold, "italic": italic,
              "size_px": round(first["size"] * scale, 2), "color": first["color"]},
        base=int((page_h - first["base"]) * scale),
    )


def _merge_split_runs(lines: List[Line]) -> List[Line]:
    """
    Rejoin pieces of one word that a PDF generator emitted as separate runs
    ("Detailin" + "g Studio", "Rs. 15" + ",000.00") — same baseline, same font,
    touching horizontally.
    """
    out: List[Line] = []
    for line in sorted(lines, key=lambda l: (l.base or l.y, l.x)):
        prev = out[-1] if out else None
        if (prev and prev.font and line.font and prev.font["name"] == line.font["name"]
                and abs(prev.font["size_px"] - line.font["size_px"]) < 0.5
                and prev.font["color"] == line.font["color"]
                and abs((prev.base or 0) - (line.base or 0)) <= max(2, prev.font["size_px"] * 0.2)
                and -2 <= line.x - (prev.x + prev.w) <= prev.font["size_px"] * 0.35):
            touching = line.x - (prev.x + prev.w) <= prev.font["size_px"] * 0.12
            words = list(prev.words)
            if touching and words:
                last, first = words[-1], line.words[0]
                words[-1] = Word(text=last.text + first.text, conf=100.0, x=last.x, y=min(last.y, first.y),
                                 w=first.x + first.w - last.x, h=max(last.h, first.h), key=(0, 0, 0))
                words += line.words[1:]
            else:
                words += line.words
            x2 = max(prev.x + prev.w, line.x + line.w)
            y = min(prev.y, line.y)
            y2 = max(prev.y + prev.h, line.y + line.h)
            out[-1] = Line(text=" ".join(w.text for w in words), x=prev.x, y=y, w=x2 - prev.x, h=y2 - y,
                           words=words, font=prev.font, base=prev.base)
        else:
            out.append(line)
    return out


def text_of(path: str) -> str:
    doc = _doc(path)
    text = doc[0].get_textpage().get_text_range()
    doc.close()
    return text


def redact(path: str, out_path: str, scale: float, text_boxes: Sequence[Sequence[int]],
           graphic_boxes: Sequence[Sequence[int]] = (), stamp_centres: Sequence[Sequence[float]] = (),
           keep_boxes: Sequence[Sequence[int]] = ()) -> str:
    """
    Write a copy of page 1 without the job-specific content.

    * Text objects overlapping a `text_boxes` area are removed.
    * Thin rules and stamp outlines inside `graphic_boxes` are removed (the
      renderer redraws rules for the new rows). Large fills — bands behind a
      total, coloured table headings — are always kept.

    Boxes are in analysis pixels. The page is then copied into a brand-new
    document, so nothing else from the uploaded file (scripts, attachments,
    metadata, other pages) survives.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    def to_pt(box):
        x, y, x2, y2 = box
        return x / scale, y / scale, x2 / scale, y2 / scale

    src = pdfium.PdfDocument(path)
    page = src[0]
    _, page_h = page.get_size()
    tboxes = [to_pt(b) for b in text_boxes]
    gboxes = [to_pt(b) for b in graphic_boxes]
    centres = [(x / scale, y / scale) for x, y in stamp_centres]
    keeps = [to_pt(b) for b in keep_boxes]

    def inside(bounds, boxes, need: float) -> bool:
        left, bottom, right, top = bounds
        # Object bounds → top-left origin points.
        ox, oy, ox2, oy2 = left, page_h - top, right, page_h - bottom
        area = max(1e-6, (ox2 - ox) * (oy2 - oy))
        for bx, by, bx2, by2 in boxes:
            ix = max(0.0, min(ox2, bx2) - max(ox, bx))
            iy = max(0.0, min(oy2, by2) - max(oy, by))
            if ix * iy / area >= need or (area < 1.0 and ix > 0 and iy > 0):
                return True
        return False

    removed = 0
    count = raw.FPDFPage_CountObjects(page.raw)
    for i in reversed(range(count)):
        obj = raw.FPDFPage_GetObject(page.raw, i)
        kind = raw.FPDFPageObj_GetType(obj)
        l, b, r, t = (ctypes.c_float() for _ in range(4))
        raw.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in (l, b, r, t)))
        bounds = (l.value, b.value, r.value, t.value)
        drop = False
        if keeps and inside(bounds, keeps, 0.5):
            continue                      # explicitly protected (their PAID stamp on the "paid" blank)
        if kind == raw.FPDF_PAGEOBJ_TEXT:
            drop = inside(bounds, tboxes, 0.5)
        elif kind == raw.FPDF_PAGEOBJ_PATH:
            w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
            thin_rule = h <= 2.5 and w > 20
            small_outline = w < 200 and h < 80
            # A stamp's frame surrounds its text even where part of it is
            # hidden behind something else, so match by "contains the text".
            around_stamp = small_outline and any(
                bounds[0] <= cx <= bounds[2] and page_h - bounds[3] <= cy <= page_h - bounds[1]
                for cx, cy in centres)
            drop = around_stamp or ((thin_rule or small_outline) and gboxes and inside(bounds, gboxes, 0.9))
        if drop:
            raw.FPDFPage_RemoveObject(page.raw, obj)
            raw.FPDFPageObj_Destroy(obj)
            removed += 1
    raw.FPDFPage_GenerateContent(page.raw)

    clean = pdfium.PdfDocument.new()
    clean.import_pages(src, [0])
    clean.save(out_path)
    clean.close()
    src.close()
    logger.info("Redacted %d object(s) from uploaded PDF", removed)
    return out_path
