"""
services/invoices/ocr.py — exact text and positions from an invoice image.

Tesseract is optional. When it is installed (``brew install tesseract`` /
``apt install tesseract-ocr``) invoice learning switches to measured layout:
every line's real position, size and colour comes from here, and the model is
only asked what each line *is*. Without it, the archetype path still works.

Invoices put text on coloured bands (white on navy, white on orange). Plain
OCR misses that, so the page is first binarised against its *local*
background: a pixel is ink if it differs from its surroundings, whichever
way. Dark-on-light and light-on-dark both come out as black on white.
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60


@dataclass
class Word:
    text: str
    conf: float
    x: int
    y: int
    w: int
    h: int
    key: Tuple[int, int, int]   # (block, paragraph, line) from tesseract


@dataclass
class Line:
    """One printed line of text, in pixels of the scanned image."""
    text: str
    x: int
    y: int
    w: int
    h: int
    words: List[Word] = field(default_factory=list)
    # Set only for lines read from a PDF's own text layer, where they are exact:
    # {"name", "family", "bold", "italic", "size_px", "color"} and the true baseline.
    font: Optional[Dict[str, object]] = None
    base: Optional[int] = None

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


def available() -> bool:
    return shutil.which("tesseract") is not None


def binarize(image_path: str, out_path: str) -> str:
    """Write a black-text-on-white version of the page, whatever colour each region is."""
    from PIL import Image, ImageChops, ImageFilter

    gray = Image.open(image_path).convert("L")
    # A median filter wipes out text strokes but keeps bands and backgrounds,
    # giving each pixel its local background.
    small = gray.resize((max(1, gray.width // 4), max(1, gray.height // 4)))
    background = small.filter(ImageFilter.MedianFilter(9)).resize(gray.size)
    diff = ImageChops.difference(gray, background)
    ink = diff.point(lambda v: 0 if v > 38 else 255)
    ink.save(out_path)
    return out_path


def _run(image_path: str, psm: int) -> List[Word]:
    proc = subprocess.run(
        ["tesseract", image_path, "stdout", "--psm", str(psm), "tsv"],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract failed: {proc.stderr.strip()[:200]}")
    words: List[Word] = []
    for row in csv.DictReader(io.StringIO(proc.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
        except ValueError:
            conf = -1
        if not text or conf < 30:
            continue
        words.append(Word(
            text=text, conf=conf, x=int(row["left"]), y=int(row["top"]),
            w=int(row["width"]), h=int(row["height"]),
            key=(int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
        ))
    return words


def read_lines(image_path: str, work_path: str) -> List[Line]:
    """
    OCR the page and return its lines, top to bottom.

    Lines are re-split wherever words on one tesseract line are far apart
    horizontally: on an invoice, "Billed To" at the left margin and
    "Invoice # 1042" at the right are separate elements, not one sentence.
    """
    # Two passes: the original reads cleanest; the binarised copy also finds
    # light-on-dark text. Keep the original's words, and add binarised words
    # only where the original found nothing (the coloured bands).
    binarize(image_path, work_path)
    words = _run(image_path, psm=11)         # sparse text: invoices are not prose
    for extra in _run(work_path, psm=11):
        clashes = [w for w in words if _overlap(extra, w) > 0.3]
        if not clashes:
            words.append(extra)
        elif all(_quality(extra) > _quality(w) for w in clashes):
            # The original pass read junk here (typically light text on a
            # coloured band); the binarised reading is better.
            words = [w for w in words if w not in clashes] + [extra]
    if not words:
        return []

    # Group words into visual lines by vertical overlap, then split on big gaps.
    words.sort(key=lambda w: (w.y + w.h / 2, w.x))
    rows: List[List[Word]] = []
    for word in words:
        mid = word.y + word.h / 2
        for row in rows:
            ref = row[0]
            if abs((ref.y + ref.h / 2) - mid) < max(ref.h, word.h) * 0.55:
                row.append(word)
                break
        else:
            rows.append([word])

    lines: List[Line] = []
    for row in rows:
        row.sort(key=lambda w: w.x)
        median_h = sorted(w.h for w in row)[len(row) // 2]
        chunk: List[Word] = [row[0]]
        for word in row[1:]:
            prev = chunk[-1]
            gap = word.x - (prev.x + prev.w)
            # A space between words is ~⅓ of the letter height; a full letter
            # height or a jump in text size means a separate element.
            size_jump = max(word.h, prev.h) / max(1, min(word.h, prev.h)) > 1.45
            if gap > median_h * 1.0 or (size_jump and gap > median_h * 0.5):
                lines.append(_line(chunk))
                chunk = [word]
            else:
                chunk.append(word)
        lines.append(_line(chunk))
    _refine_with_ink(lines, work_path)
    lines.sort(key=lambda l: (l.y, l.x))
    return lines


def _refine_with_ink(lines: List[Line], binarized_path: str) -> None:
    """
    OCR boxes are loose on blurry photos. Measure each line from its own ink
    instead: dense rows run from the top of the capitals to the baseline;
    descenders (g, y, p) are sparse and fall below it.
    """
    from PIL import Image

    img = Image.open(binarized_path).convert("L")
    px = img.load()
    for line in lines:
        y0, y1 = max(0, line.y - 3), min(img.height, line.y2 + 3)
        x0, x1 = max(0, line.x), min(img.width, line.x2)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        rows = [sum(1 for x in range(x0, x1) if px[x, y] < 128) for y in range(y0, y1)]
        peak = max(rows)
        if peak == 0:
            continue
        # A loose box can reach into the line above or below: split into bands
        # at blank rows and keep the band holding most of the ink.
        bands, start = [], None
        for i, v in enumerate(rows + [0]):
            if v > peak * 0.05 and start is None:
                start = i
            elif v <= peak * 0.05 and start is not None:
                bands.append((start, i))
                start = None
        a, b = max(bands, key=lambda ab: sum(rows[ab[0]:ab[1]]))
        band = rows[a:b]
        band_peak = max(band)
        dense = [a + i for i, v in enumerate(band) if v >= band_peak * 0.4]
        top = a + next(i for i, v in enumerate(band) if v >= band_peak * 0.12)
        base = dense[-1] + 1
        # Only ever tighten what OCR gave: growing the box would let a line
        # swallow a band edge or the row next to it.
        new_top = max(line.y, y0 + top)
        new_base = min(line.y2, y0 + base)
        if new_base - new_top >= 4:
            line.base = new_base
            line.h = line.y2 - new_top
            line.y = new_top


def _quality(w: Word) -> float:
    """Confidence, nudged towards readings that look like real words."""
    letters = sum(ch.isalnum() for ch in w.text)
    return w.conf + min(letters, 6) * 3


def _overlap(a: Word, b: Word) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    return inter / max(1, min(a.w * a.h, b.w * b.h))


def _line(words: List[Word]) -> Line:
    x = min(w.x for w in words)
    y = min(w.y for w in words)
    x2 = max(w.x + w.w for w in words)
    y2 = max(w.y + w.h for w in words)
    return Line(text=" ".join(w.text for w in words), x=x, y=y, w=x2 - x, h=y2 - y, words=list(words))


def ink_color(image_path: str, line: Line) -> Optional[str]:
    """The colour of a line's text: the pixels furthest from the local background."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    box = img.crop((line.x, line.y, line.x2, line.y2))
    px = list(getattr(box, "get_flattened_data", box.getdata)())
    if not px:
        return None
    # Background = most common colour in the box; ink = most distant 15%.
    counts: Dict[Tuple[int, int, int], int] = {}
    for p in px:
        q = (p[0] // 16 * 16, p[1] // 16 * 16, p[2] // 16 * 16)
        counts[q] = counts.get(q, 0) + 1
    bg = max(counts, key=counts.get)
    dist = sorted(px, key=lambda p: -sum((p[i] - bg[i]) ** 2 for i in range(3)))
    core = dist[: max(1, len(dist) * 15 // 100)]
    r, g, b = (sum(p[i] for p in core) // len(core) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def background_color(image_path: str, line: Line) -> str:
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    pad = max(4, line.h // 3)
    ring = [img.getpixel((min(img.width - 1, max(0, x)), min(img.height - 1, max(0, y))))
            for x in range(line.x - pad, line.x2 + pad, max(1, line.w // 20))
            for y in (line.y - pad, line.y2 + pad)]
    r, g, b = (sorted(p[i] for p in ring)[len(ring) // 2] for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def is_bold(line: Line, image_path: str) -> bool:
    """Stroke thickness relative to letter height: bold faces are visibly heavier."""
    from PIL import Image

    work = Image.open(image_path).convert("L").crop((line.x, line.y, line.x2, line.y2))
    if work.width < 4 or work.height < 4:
        return False
    px = list(getattr(work, "get_flattened_data", work.getdata)())
    ink = sum(1 for v in px if v < 128) / len(px)
    return ink > 0.33
