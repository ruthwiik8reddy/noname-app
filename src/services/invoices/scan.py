"""
services/invoices/scan.py — deterministic reading of an uploaded invoice.

Everything here works with no model running:

* `to_image` turns an uploaded PDF (first page) or photo into a PNG the
  vision model can read, normalised in size and orientation.
* `palette` reads the invoice's real colours straight from the pixels.

Colour is the one thing a small local vision model reliably gets wrong — it
will describe "dark blue" and guess a hex. Pixels don't guess. So the
deterministic palette is authoritative for colour, and the model is trusted
for structure and wording.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | {"pdf"}
MAX_EDGE = 1600


class ScanError(ValueError):
    """The upload can't be turned into something readable."""


def extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def to_image(source_path: str, out_path: str) -> str:
    """
    Write a PNG rendering of `source_path` to `out_path` and return it.

    Raises ScanError for unreadable files — including a file whose content
    doesn't match its extension, which is checked by actually decoding it.
    """
    from PIL import Image, ImageOps

    ext = extension(source_path)
    if ext == "pdf":
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise ScanError("PDF support needs the pypdfium2 package (pip install pypdfium2). "
                            "Upload a PNG or JPG screenshot instead.") from exc
        try:
            pdf = pdfium.PdfDocument(source_path)
            if len(pdf) == 0:
                raise ScanError("That PDF has no pages.")
            image = pdf[0].render(scale=2.0).to_pil()
            pdf.close()
        except ScanError:
            raise
        except Exception as exc:  # noqa: BLE001 - any pdfium failure means unreadable
            raise ScanError("Couldn't read that PDF. Is it password-protected?") from exc
    elif ext in IMAGE_EXTENSIONS:
        try:
            image = Image.open(source_path)
            image = ImageOps.exif_transpose(image)
            image.load()
        except Exception as exc:  # noqa: BLE001
            raise ScanError("That file isn't a readable image.") from exc
    else:
        raise ScanError("Upload a PDF, PNG, JPG or WEBP of your invoice.")

    image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    angle = skew_angle(image)
    if abs(angle) >= 0.3:
        # Fill with the paper colour so the corners don't become dark wedges.
        paper = max(image.convert("RGB").getcolors(image.width * image.height) or [(0, (255, 255, 255))])[1]
        image = image.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=paper)
    image.save(out_path, "PNG", optimize=True)
    return out_path


def skew_angle(image) -> float:
    """
    Degrees to rotate a photographed page so its text runs level.

    Text rows are sharpest — row sums vary the most between ink and gaps —
    when the page is straight, so try small angles and keep the best.
    """
    from PIL import Image

    gray = image.convert("L")
    gray.thumbnail((700, 700))
    ink = gray.point(lambda v: 255 if v < 140 else 0)

    def score(a: float) -> float:
        rows = ink.rotate(a, resample=Image.NEAREST, expand=False)
        w, h = rows.size
        px = rows.load()
        sums = [sum(1 for x in range(0, w, 2) if px[x, y]) for y in range(h)]
        mean = sum(sums) / max(1, len(sums))
        return sum((s - mean) ** 2 for s in sums)

    best, best_score = 0.0, score(0.0)
    for step in (1.0, 0.25):
        centre = best
        for k in range(-6, 7):
            a = centre + k * step
            if a == centre or abs(a) > 8:
                continue
            sc = score(a)
            if sc > best_score:
                best, best_score = a, sc
    return best


def _hex(rgb: Tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _saturation(rgb: Tuple[int, int, int]) -> float:
    mx, mn = max(rgb), min(rgb)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _lightness(rgb: Tuple[int, int, int]) -> float:
    return (max(rgb) + min(rgb)) / 510


def palette(image_path: str) -> Dict[str, Optional[str]]:
    """
    Read the invoice's colours.

    Returns:
      accent      — the most-used colour that is clearly not paper or ink
                    (saturated, or a dark fill covering a real area)
      text        — the darkest common colour (body text)
      top_band    — the dominant colour of the top 15% of the page if it is
                    a solid non-white fill (a banner header), else None
      colors      — the top colours, for the editor's swatch picker
    """
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    small = img.copy()
    small.thumbnail((300, 300))
    quant = small.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()[:30]
    counts = sorted(quant.getcolors(), reverse=True)
    total = sum(c for c, _ in counts) or 1

    colors: List[Tuple[float, Tuple[int, int, int]]] = []
    for count, idx in counts:
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        colors.append((count / total, rgb))  # type: ignore[arg-type]

    def is_paper(rgb) -> bool:
        return _lightness(rgb) > 0.9 and _saturation(rgb) < 0.12

    accent = None
    for share, rgb in colors:
        if is_paper(rgb) or share < 0.01:
            continue
        if _saturation(rgb) > 0.25 and 0.12 < _lightness(rgb) < 0.85:
            accent = _hex(rgb)
            break
    if accent is None:
        # Monochrome invoices: use the most common dark fill as the accent.
        for share, rgb in colors:
            if not is_paper(rgb) and share >= 0.02:
                accent = _hex(rgb)
                break

    # Body text: the darkest *neutral* colour. A dark brand colour used for
    # headings is not the body text colour.
    if accent is None:
        accent = _saturated_ink(img)

    text = None
    for share, rgb in sorted(colors, key=lambda c: _lightness(c[1])):
        if share >= 0.002 and _lightness(rgb) < 0.35 and _saturation(rgb) < 0.3:
            text = _hex(rgb)
            break

    w, h = small.size
    band_h = max(1, int(h * 0.15))
    band = small.crop((0, 0, w, band_h)).quantize(colors=3)
    band_counts = sorted(band.getcolors(), reverse=True)
    band_pal = band.getpalette()
    top_band = None
    if band_counts:
        share = band_counts[0][0] / (w * band_h)
        rgb = tuple(band_pal[band_counts[0][1] * 3: band_counts[0][1] * 3 + 3])
        if share > 0.55 and not is_paper(rgb):
            top_band = _hex(rgb)  # type: ignore[arg-type]

    table_header = _first_filled_row(img, start_frac=0.15 if top_band else 0.05, is_paper=is_paper)
    header_align = _header_alignment(img, is_paper)

    # With a banner, the banner colour is the brand colour; the table keeps its own.
    if top_band:
        accent = top_band

    return {
        "accent": accent,
        "text": text,
        "top_band": top_band,
        "table_header": table_header,
        "header_align": header_align,
        "colors": [_hex(rgb) for share, rgb in colors if share >= 0.01][:8],
    }


def _first_filled_row(img, start_frac: float, is_paper) -> Optional[str]:
    """
    Colour of the first wide solid strip below the header — almost always the
    item table's header row. Rows are sampled across the page; a row counts
    as filled when one non-paper colour covers most of its width.
    """
    small = img.copy()
    small.thumbnail((240, 340))
    w, h = small.size
    px = small.load()
    run: List[Tuple[int, int, int]] = []
    for y in range(int(h * start_frac), int(h * 0.85)):
        row = [px[x, y] for x in range(int(w * 0.08), int(w * 0.92))]
        # Bucket to tolerate antialiasing and JPEG noise.
        buckets: Dict[Tuple[int, int, int], int] = {}
        for r, g, b in row:
            key = (r // 24, g // 24, b // 24)
            buckets[key] = buckets.get(key, 0) + 1
        key, count = max(buckets.items(), key=lambda kv: kv[1])
        sample = next(p for p in row if (p[0] // 24, p[1] // 24, p[2] // 24) == key)
        if count / len(row) > 0.6 and not is_paper(sample) and _lightness(sample) > 0.08:
            run.append(sample)
            continue
        # A strip at least ~1% of the page tall is a fill, not a rule. Its
        # edges are blended with the paper, so take the colour from the middle.
        if len(run) >= 3:
            return _hex(run[len(run) // 2])
        run = []
    return _hex(run[len(run) // 2]) if len(run) >= 3 else None


def _saturated_ink(img) -> Optional[str]:
    """
    Brand colour used only for thin text and rules — too few pixels to survive
    palette quantisation, but plainly there when you count saturated pixels.
    """
    from PIL import Image

    # Nearest-neighbour, not a smoothing thumbnail: averaging thin strokes
    # with the white page around them would wash the colour out.
    scale = min(1.0, 700 / max(img.size))
    small = img.resize((max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))),
                       resample=Image.NEAREST)
    buckets: Dict[Tuple[int, int, int], List[Tuple[int, int, int]]] = {}
    total = small.size[0] * small.size[1]
    pixels = getattr(small, "get_flattened_data", small.getdata)()  # Pillow 12+ renamed it
    for rgb in pixels:
        if _saturation(rgb) > 0.4 and 0.15 < _lightness(rgb) < 0.8:
            buckets.setdefault((rgb[0] // 32, rgb[1] // 32, rgb[2] // 32), []).append(rgb)
    if not buckets:
        return None
    best = max(buckets.values(), key=len)
    if len(best) < total * 0.0015:
        return None
    # Anti-aliased edges are paler than the real ink, so take the strong end.
    best.sort(key=lambda c: _saturation(c))
    return _hex(best[int(len(best) * 0.85)])


def _header_alignment(img, is_paper) -> Optional[str]:
    """
    "centered" when the top block's ink is symmetric and runs through the
    middle of the page, "split" when it sits at the edges (name left, title
    right), else None.
    """
    small = img.copy()
    small.thumbnail((300, 420))
    w, h = small.size
    px = small.load()
    cols = [0] * w
    for y in range(int(h * 0.02), int(h * 0.16)):
        row = [not is_paper(px[x, y]) for x in range(w)]
        # A row that is mostly ink is a rule or a filled band, not text —
        # it says nothing about how the text is aligned.
        if sum(row) > w * 0.5:
            continue
        for x in range(w):
            if row[x]:
                cols[x] += 1
    inked = [x for x in range(w) if cols[x]]
    if len(inked) < w * 0.1:
        return None
    left, right = inked[0], w - 1 - inked[-1]
    middle = sum(1 for x in range(int(w * 0.42), int(w * 0.58)) if cols[x])
    middle_frac = middle / max(1, int(w * 0.58) - int(w * 0.42))
    if abs(left - right) < w * 0.08 and middle_frac > 0.6:
        return "centered"
    if middle_frac < 0.4:
        return "split"
    return None


def safe_remove(path: Optional[str]) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", path, exc)
