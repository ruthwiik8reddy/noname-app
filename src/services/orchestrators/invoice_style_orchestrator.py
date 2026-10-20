"""
orchestrators/invoice_style_orchestrator.py — learn a studio's invoice style.

    upload (PDF/photo) → scan.to_image → scan.palette        (deterministic)
                                     ↘ vision model → layout + wording
                       → merge → style.normalize_style → stored template

Division of labour:
  * pixels decide colour (the model guesses hex badly),
  * the model decides layout archetype and reads the exact wording,
  * the studio's own records fill the business details the model missed,
  * the owner gets the last word in the template editor.

If the model is unreachable, the studio still gets a template: real colours,
a layout inferred from whether there's a coloured top band, and their
business details. Degraded, not broken.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from ..invoices import scan
from ..invoices.style import DEFAULT_STYLE, normalize_style, readable_on
from ..llm.base import LLMError, VisionLLMProvider
from ..prompts.invoice_prompts import InvoicePrompts
from ..repositories.base import BaseRepository
from ..repositories.settings_repository import SettingsRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)


_DATA_PATTERN = re.compile(r"\d{3,}|\$\s*\d|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|@")


def _looks_like_data(text: str) -> bool:
    """
    A heading is words ("Vehicle", "Invoice #"). Something with a long number,
    an amount, a date or an email is a value from the sample invoice — often
    a customer's — and must never become a heading on every future invoice.
    """
    return bool(_DATA_PATTERN.search(text or ""))


class InvoiceStyleOrchestrator(BaseOrchestrator):

    tier = "vision"

    def __init__(self, provider: Optional[VisionLLMProvider] = None,
                 settings: Optional[SettingsRepository] = None):
        super().__init__(provider)
        self.settings = settings or SettingsRepository()

    @property
    def provider(self) -> VisionLLMProvider:  # type: ignore[override]
        if self._provider is None:
            from ..llm.factory import LLMProviderFactory

            self._provider = LLMProviderFactory.vision_provider()
        return self._provider  # type: ignore[return-value]

    # ── public ────────────────────────────────────────────────────────────

    def studio_defaults(self, studio_id: int) -> Dict[str, Any]:
        """A sensible starting style from what the studio has already told us."""
        studio = BaseRepository().fetch_one("SELECT name, city FROM studios WHERE id=?", (studio_id,))
        cfg = self.settings.all(studio_id)
        lines = [cfg.get(k, "") for k in ("business_address", "business_phone", "business_email",
                                           "business_website")]
        if not cfg.get("business_address") and studio.get("city"):
            lines[0] = studio["city"]
        style = dict(DEFAULT_STYLE)
        style.update({
            "business_name": studio.get("name", ""),
            "business_lines": [l for l in lines if l],
            "number_prefix": cfg.get("invoice_number_prefix", "INV-"),
            "terms_text": cfg.get("invoice_terms", ""),
            "payment_instructions": cfg.get("invoice_payment_instructions", ""),
            "footer_text": cfg.get("invoice_footer", "") or DEFAULT_STYLE["footer_text"],
        })
        return normalize_style(style)

    def learn(self, studio_id: int, image_path: str) -> Dict[str, Any]:
        """
        Build a style from a rendered invoice image (see scan.to_image).

        Returns an envelope: {"style", "palette", "read_by_ai", "notes"} plus
        the usual `_status`.
        """
        base = self.studio_defaults(studio_id)
        try:
            colors = scan.palette(image_path)
        except Exception as exc:  # noqa: BLE001
            raise OrchestratorError(f"Couldn't read the image: {exc}") from exc

        described: Dict[str, Any] = {}
        degraded, reason = False, ""
        try:
            described = self.call_json(
                InvoicePrompts.describe_invoice(),
                images=[image_path],
                repair_prompt_builder=InvoicePrompts.repair,
                required_keys=("layout",),
            )
            described.pop("_meta", None)
        except (LLMError, OrchestratorError) as exc:
            logger.warning("Invoice style read failed, using colours only: %s", exc)
            degraded, reason = True, str(exc)

        style, notes = self._merge(base, described, colors)
        return self.envelope(
            {"style": style, "palette": colors, "read_by_ai": not degraded, "notes": notes},
            degraded=degraded, degraded_reason=reason,
            source="deterministic" if degraded else "ai",
        )

    # ── exact mode ────────────────────────────────────────────────────────

    def learn_exact(self, image_path: str, folder: str, pdf_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Turn the uploaded invoice into a fill-in template (see services/invoices/exact.py).

        A PDF with a text layer is read and edited directly — exact fonts,
        sizes and colours, artwork kept as vectors, no OCR needed. Scans and
        photos go through OCR.

        Returns {"exact": map or None, "blank_file": name, "note": why not}.
        Never raises: exact mode is an upgrade; the rebuilt style always remains.
        """
        import os
        import uuid

        from ..invoices import exact, ocr, pdf_text

        use_pdf = bool(pdf_path) and pdf_text.has_text_layer(pdf_path)
        if not use_pdf and not ocr.available():
            return {"exact": None, "blank_file": "",
                    "note": "Exact copies of scans and photos need the free Tesseract OCR engine on the server "
                            "(brew install tesseract / apt install tesseract-ocr). Uploading the PDF your "
                            "invoicing software produced works without it."}
        work = os.path.join(folder, f"work_{uuid.uuid4().hex}.png")
        cleanup = [work]
        try:
            page_pt = None
            if use_pdf:
                page_pt = pdf_text.page_size(pdf_path)
                scale = 1240 / page_pt[0]
                analysis = os.path.join(folder, f"analysis_{uuid.uuid4().hex}.png")
                cleanup.append(analysis)
                pdf_text.render(pdf_path, analysis, scale)
                lines = pdf_text.read_lines(pdf_path, scale)
            else:
                analysis = image_path
                lines = [l for l in ocr.read_lines(image_path, work)
                         if re.search(r"[A-Za-z0-9]", l.text) and l.h >= 6]
            roles: Dict[int, str] = {}
            stop = exact.find_table_top(lines)
            header = [l for l in lines if l.y2 <= stop]
            if not any(exact.BILL_TO.match(l.text) for l in header):
                # No "Bill To"-style label to anchor on: ask which lines are the customer's.
                try:
                    from PIL import Image
                    W, H = Image.open(analysis).size
                    numbered = exact.header_lines_for_model(lines, W, H, stop)
                    reply = self.call_json(InvoicePrompts.header_roles(numbered), required_keys=("roles",),
                                           tier="standard")
                    roles = {int(k): str(v) for k, v in (reply.get("roles") or {}).items() if str(k).isdigit()}
                except (LLMError, OrchestratorError) as exc:
                    logger.info("Header role model unavailable: %s", exc)
            template = exact.analyse(analysis, roles, lines, page_pt=page_pt)

            if use_pdf:
                blank_name = f"blank_{uuid.uuid4().hex}.pdf"
                blank_path = os.path.join(folder, blank_name)
                stamp = template["slots"].get("stamp")
                pdf_text.redact(pdf_path, blank_path, scale,
                                [e["box"] for e in template["erase"]],
                                [e["box"] for e in template["erase"] if e.get("graphics")],
                                stamp_centres=[stamp["centre"]] if stamp else [])
                if stamp:
                    # A second blank that keeps their own PAID stamp exactly as it was,
                    # used once an invoice is marked paid.
                    paid_name = f"blankpaid_{uuid.uuid4().hex}.pdf"
                    sbox = stamp["box"]
                    keep = [[sbox[0] - 6, sbox[1] - 6, sbox[2] + 6, sbox[3] + 30]]   # frame may run behind the table band
                    pdf_text.redact(pdf_path, os.path.join(folder, paid_name), scale,
                                    [e["box"] for e in template["erase"] if not e.get("stamp")],
                                    [e["box"] for e in template["erase"] if e.get("graphics") and not e.get("stamp")],
                                    keep_boxes=keep)
                    template["blank_paid"] = paid_name
                view = f"blankview_{uuid.uuid4().hex}.png"
                pdf_text.render(blank_path, os.path.join(folder, view), scale)
                template["blank_view"] = view
            else:
                blank_name = f"blank_{uuid.uuid4().hex}.png"
                blank_path = os.path.join(folder, blank_name)
                exact.make_blank(image_path, template["erase"], blank_path)
                template["blank_view"] = blank_name
                if template["slots"].get("stamp"):
                    paid_name = f"blankpaid_{uuid.uuid4().hex}.png"
                    paid_path = os.path.join(folder, paid_name)
                    exact.make_blank(image_path, [e for e in template["erase"] if not e.get("stamp")], paid_path)
                    # Put their stamp's own pixels back, in case an erased area covered it.
                    from PIL import Image
                    sx0, sy0, sx1, sy1 = (int(v) for v in template["slots"]["stamp"]["box"])
                    original = Image.open(image_path).convert("RGB").crop((sx0 - 3, sy0 - 3, sx1 + 3, sy1 + 3))
                    page = Image.open(paid_path).convert("RGB")
                    page.paste(original, (sx0 - 3, sy0 - 3))
                    page.save(paid_path, "PNG")
                    template["blank_paid"] = paid_name

            leaks = exact.find_leaks(blank_path, template, work)
            if template.get("blank_paid") and not leaks:
                stamp_text = template["slots"]["stamp"]["text"]
                paid_check = dict(template, erased_texts=[t for t in template["erased_texts"]
                                                          if t.strip().upper() != stamp_text])
                leaks = exact.find_leaks(os.path.join(folder, template["blank_paid"]), paid_check, work)
            if leaks or not template["slots"].get("customer"):
                scan.safe_remove(blank_path)
                if use_pdf:
                    scan.safe_remove(os.path.join(folder, template["blank_view"]))
                note = ("Couldn't fully remove the sample customer's details from your invoice "
                        f"({', '.join(leaks[:3])}), so the exact copy is switched off to protect their privacy. "
                        "Try a cleaner file, or use the rebuilt style.") if leaks else \
                    "Couldn't find where the customer's details go on your invoice."
                return {"exact": None, "blank_file": "", "note": note}
            return {"exact": template, "blank_file": blank_name, "note": "; ".join(template.get("notes", []))}
        except exact.ExactTemplateError as exc:
            return {"exact": None, "blank_file": "", "note": str(exc)}
        except Exception as exc:  # noqa: BLE001 - exact mode must never break the upload
            logger.exception("Exact template build failed")
            return {"exact": None, "blank_file": "", "note": f"Exact copy failed: {exc}"[:300]}
        finally:
            for path in cleanup:
                scan.safe_remove(path)

    # ── merge ─────────────────────────────────────────────────────────────

    @staticmethod
    def _merge(base: Dict[str, Any], described: Dict[str, Any],
               colors: Dict[str, Any]) -> tuple[Dict[str, Any], list]:
        notes = []
        raw: Dict[str, Any] = {}

        # Wording and structure from the model — but never blank out what the
        # studio already told us with an empty string the model returned.
        for key, value in (described or {}).items():
            if key == "invoice_number_example":
                # The model reads the whole number reliably but won't split
                # it; strip the digits here. "Q-0007" → "Q-", "#1791" → "#".
                m = re.match(r"^\s*([^\d]*?)\s*\d", str(value or ""))
                if m:
                    raw["number_prefix"] = m.group(1)
            elif key == "has_logo":
                # Their invoice has no logo → don't add one they never print.
                raw["show_logo"] = str(value).strip().lower() in ("true", "1", "yes")
            elif key == "labels" and isinstance(value, dict):
                raw["labels"] = {k: v for k, v in value.items()
                                 if isinstance(v, str) and v.strip() and not _looks_like_data(v)}
                dropped = [k for k, v in value.items() if isinstance(v, str) and _looks_like_data(v)]
                if dropped:
                    notes.append("Ignored some text that looked like customer details rather than "
                                 "headings: " + ", ".join(sorted(dropped)) + ".")
            elif key == "business_lines":
                if isinstance(value, list) and any(str(v).strip() for v in value):
                    raw[key] = value
            elif isinstance(value, str):
                if value.strip():
                    raw[key] = value
            elif value is not None:
                raw[key] = value

        # A rate or quantity heading means there IS a quantity × rate table,
        # whatever the model said about show_quantity.
        model_labels = raw.get("labels") or {}
        if model_labels.get("quantity") or model_labels.get("rate"):
            raw["show_quantity"] = True

        # A document title equal to the business name is the model confusing
        # the letterhead for the heading. Drop it and keep the default.
        title = str(raw.get("title_text", "")).strip().lower()
        if title and title == str(raw.get("business_name", "")).strip().lower():
            raw.pop("title_text", None)
            notes.append("Couldn't read the document title separately from your business name — "
                         "check the Title field.")

        # Colours from pixels.
        top_band, accent = colors.get("top_band"), colors.get("accent")

        # Layout: a banner needs a band the pixels can actually see; a centred
        # letterhead is visible from where the ink sits. Pixels beat the model.
        align = colors.get("header_align")
        if raw.get("layout") == "banner" and not top_band:
            raw["layout"] = "centered" if align == "centered" else "classic"
        elif not top_band and align == "centered" and raw.get("layout") in (None, "classic"):
            raw["layout"] = "centered"
        # No band, no table fill and no colour anywhere: that's a minimal design.
        if not top_band and not colors.get("table_header") and not accent \
                and raw.get("layout") in (None, "classic"):
            raw["layout"] = "minimal"
        if top_band:
            raw["header_bg"] = top_band
            raw["header_text_color"] = readable_on(top_band)
            if not described.get("layout"):
                raw["layout"] = "banner"
                notes.append("Detected a coloured band across the top, so the banner layout was chosen.")
        table_header = colors.get("table_header") or accent
        if accent:
            raw["accent_color"] = accent
            if not top_band:
                raw.setdefault("header_bg", accent)
        if table_header:
            raw["table_header_bg"] = table_header
            raw["table_header_text"] = readable_on(table_header)
            # A solid header row seen in the pixels beats the model's guess —
            # "lined" and "boxed" have no fill, so they can't be right.
            if colors.get("table_header") and raw.get("table_style") not in ("filled", "striped"):
                raw["table_style"] = "filled"
        if colors.get("text"):
            raw["text_color"] = colors["text"]

        if not described:
            notes.append("The AI model wasn't reachable, so only colours were read from your invoice. "
                         "Adjust the layout and wording below, or try again once the model is running.")
        style = normalize_style(raw, base=base)
        return style, notes
