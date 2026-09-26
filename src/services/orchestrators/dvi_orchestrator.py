"""
services/orchestrators/dvi_orchestrator.py — Phase 2: Multimodal Digital Vehicle Inspection.

Pipeline:

    Technician captures photos (phone camera → /dvi/job/<id>)
        ↓  save to static/uploads/studio_<id>/dvi/
    DVIRepository persists photo rows
        ↓  local vision model (LLaVA via Ollama), one call per photo
    Raw findings → validated against a closed defect vocabulary
        ↓  UpchargeCalculator (deterministic, uses the studio's own price list)
    Priced, consolidated recommendations
        ↓  local text model
    Customer-facing summary → optionally converted into a real estimate

Three deliberate constraints:

* **Per-photo calls, not batched.** Local vision models degrade badly with
  multiple images in one prompt — findings bleed between photos and panel
  attribution becomes unreliable. Slower, but the output is attributable to a
  specific image, which matters when a customer asks "where?".

* **The model never sees prices.** It reports what it sees; Python prices it.
  A hallucinated defect costs a false line item; a hallucinated *price* costs
  trust.

* **Partial failure is normal.** If photo 3 of 5 fails, the other four still
  produce findings. Only a total failure marks the inspection as failed.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Sequence

from werkzeug.utils import secure_filename

from ..llm.base import LLMError, LLMUnavailableError, VisionLLMProvider
from ..pricing.upcharge_calculator import UpchargeCalculator
from ..prompts.dvi_prompts import DEFECT_TYPES, PANELS, DVIPrompts
from ..repositories.dvi_repository import DVIRepository
from ..repositories.job_repository import JobRepository
from .base import BaseOrchestrator, OrchestratorError

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}
MAX_PHOTOS_PER_INSPECTION = 24
SUMMARY_SCHEMA_HINT = (
    '{"customer_summary": "...", "condition_headline": "...", '
    '"recommendations": [], "technician_notes": []}'
)


class DVIOrchestrator(BaseOrchestrator):

    cache_ttl_seconds = 120

    def __init__(
        self,
        dvi_repository: Optional[DVIRepository] = None,
        job_repository: Optional[JobRepository] = None,
        provider: Optional[VisionLLMProvider] = None,
        upload_base: Optional[str] = None,
    ):
        super().__init__(provider)
        self.repo = dvi_repository or DVIRepository()
        self.jobs = job_repository or JobRepository()
        self._upload_base = upload_base

    @property
    def provider(self) -> VisionLLMProvider:  # type: ignore[override]
        if self._provider is None:
            from ..llm.factory import LLMProviderFactory

            self._provider = LLMProviderFactory.vision_provider()
        return self._provider  # type: ignore[return-value]

    # ── storage ───────────────────────────────────────────────────────────

    def upload_dir(self, studio_id: int) -> str:
        from ...config import Config
        base = self._upload_base or Config.PRIVATE_UPLOAD_ROOT
        path = os.path.abspath(os.path.join(base, f"studio_{studio_id}", "dvi"))
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def is_allowed_image(filename: str) -> bool:
        return (
            "." in filename
            and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS
        )

    # ── inspection lifecycle ──────────────────────────────────────────────

    def start_inspection(self, studio_id: int, job_id: int, actor: str) -> Dict[str, Any]:
        job = self.jobs.get(studio_id, job_id)
        if not job:
            raise OrchestratorError(f"Job {job_id} not found for this studio")
        inspection = self.repo.get_or_create_for_job(studio_id, job_id, actor)
        return {"inspection": inspection, "job": job}

    def add_photos(
        self,
        studio_id: int,
        inspection_id: int,
        files: Sequence[Any],
        panel: str,
        actor: str,
    ) -> Dict[str, Any]:
        """`files` are werkzeug FileStorage objects. Returns saved/rejected counts."""
        inspection = self.repo.get_inspection(studio_id, inspection_id)
        if not inspection:
            raise OrchestratorError("Inspection not found")

        existing = len(self.repo.photos(inspection_id))
        if existing >= MAX_PHOTOS_PER_INSPECTION:
            raise OrchestratorError(
                f"This inspection already has the maximum of {MAX_PHOTOS_PER_INSPECTION} photos"
            )

        panel = panel if panel in PANELS else "unspecified"
        directory = self.upload_dir(studio_id)
        saved: List[Dict[str, Any]] = []
        rejected: List[Dict[str, str]] = []

        for storage in files:
            original = getattr(storage, "filename", "") or ""
            if not original:
                continue
            if not self.is_allowed_image(original):
                rejected.append({"filename": original, "reason": "Unsupported file type"})
                continue
            if existing + len(saved) >= MAX_PHOTOS_PER_INSPECTION:
                rejected.append({"filename": original, "reason": "Photo limit reached"})
                continue

            ext = original.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"dvi_{inspection_id}_{panel}_{uuid.uuid4().hex[:10]}.{ext}")
            filepath = os.path.join(directory, filename)
            try:
                storage.save(filepath)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed saving DVI photo %s: %s", original, exc)
                rejected.append({"filename": original, "reason": "Could not be saved"})
                continue

            photo_id = self.repo.add_photo(
                studio_id=studio_id,
                inspection_id=inspection_id,
                filename=filename,
                filepath=filepath,
                panel=panel,
                uploaded_by=actor,
                original_name=original[:120],
            )
            saved.append(
                {
                    "id": photo_id,
                    "filename": filename,
                    "panel": panel,
                    "url": f"/dvi/photo/{studio_id}/{filename}",
                }
            )

        if saved:
            self.repo.set_inspection_status(inspection_id, "draft")

        return {"saved": saved, "rejected": rejected, "total": existing + len(saved)}

    # ── the vision pass ───────────────────────────────────────────────────

    def analyze(self, studio_id: int, inspection_id: int, reanalyze: bool = False) -> Dict[str, Any]:
        inspection = self.repo.get_inspection(studio_id, inspection_id)
        if not inspection:
            raise OrchestratorError("Inspection not found")

        photos = self.repo.photos(inspection_id)
        if not photos:
            raise OrchestratorError("Add at least one photo before running the analysis")

        if not self.ai_available():
            raise LLMUnavailableError(
                "The local vision model is unreachable. Start Ollama (`ollama serve`) and make sure "
                "the vision model is installed (`ollama pull llava`)."
            )

        job = self.jobs.get(studio_id, inspection["job_id"]) or {}
        vehicle = job.get("car", "vehicle")

        if reanalyze:
            self.repo.clear_findings(inspection_id)
            targets = photos
        else:
            targets = self.repo.unanalyzed_photos(inspection_id) or photos
            if targets is photos:
                self.repo.clear_findings(inspection_id)

        self.repo.set_inspection_status(inspection_id, "analyzing")

        services = self._studio_services(studio_id)
        calculator = UpchargeCalculator(services)

        raw_findings: List[Dict[str, Any]] = []
        failures: List[Dict[str, str]] = []
        quality_flags: List[Dict[str, str]] = []

        for photo in targets:
            try:
                parsed = self.call_json(
                    DVIPrompts.single_photo(vehicle, photo.get("panel", "unspecified")),
                    required_keys=("findings",),
                    images=[photo["filepath"]],
                    repair_prompt_builder=DVIPrompts.repair,
                    schema_hint='{"image_quality": "good", "findings": []}',
                )
            except (LLMError, OrchestratorError) as exc:
                logger.warning("DVI photo %s failed analysis: %s", photo["id"], exc)
                self.repo.mark_photo_analyzed(photo["id"], ok=False, error=str(exc))
                failures.append({"photo_id": photo["id"], "error": str(exc)})
                continue

            quality = str(parsed.get("image_quality", "good")).lower()
            if quality == "poor":
                quality_flags.append(
                    {"photo_id": photo["id"], "filename": photo["filename"],
                     "note": "Image too dark, blurry or close-cropped to assess reliably"}
                )

            for item in self._normalize_findings(parsed.get("findings", []), photo):
                priced = calculator.price_finding(item)
                finding_id = self.repo.add_finding(studio_id, inspection_id, photo["id"], priced)
                priced["id"] = finding_id
                raw_findings.append(priced)

            self.repo.mark_photo_analyzed(photo["id"], ok=True)

        if failures and not raw_findings and len(failures) == len(targets):
            self.repo.set_inspection_status(inspection_id, "failed")
            raise LLMUnavailableError(
                f"Every photo failed analysis. First error: {failures[0]['error']}"
            )

        return self._finalize(
            studio_id, inspection_id, job, raw_findings, calculator, len(photos), failures, quality_flags
        )

    def _normalize_findings(self, findings: Any, photo: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Enforce the contract. Anything outside the closed vocabulary is dropped,
        not coerced — a defect type we can't price is a defect we can't bill for,
        and silently mapping it to a neighbour would invent a charge.
        """
        if not isinstance(findings, list):
            return []

        cleaned: List[Dict[str, Any]] = []
        for entry in findings:
            if not isinstance(entry, dict):
                continue

            defect = str(entry.get("defect_type", "")).strip().lower().replace(" ", "_")
            if defect not in DEFECT_TYPES:
                logger.debug("Dropping out-of-vocabulary defect type %r", defect)
                continue

            panel = str(entry.get("panel", "")).strip().lower().replace(" ", "_")
            if panel not in PANELS:
                panel = photo.get("panel", "unspecified")

            try:
                severity = max(1, min(int(float(entry.get("severity", 1))), 5))
            except (TypeError, ValueError):
                severity = 1

            try:
                confidence = max(0.0, min(float(entry.get("confidence", 0.5)), 1.0))
            except (TypeError, ValueError):
                confidence = 0.5

            description = str(entry.get("description", "")).strip()[:400]
            location = str(entry.get("location_note", "")).strip()[:200]
            if location and location.lower() not in description.lower():
                description = f"{description} ({location})".strip()

            cleaned.append(
                {
                    "defect_type": defect,
                    "panel": panel,
                    "severity": severity,
                    "confidence": confidence,
                    "description": description or f"{defect.replace('_', ' ').title()} observed.",
                    "raw": entry,
                }
            )
        return cleaned

    def _finalize(
        self,
        studio_id: int,
        inspection_id: int,
        job: Dict[str, Any],
        findings: List[Dict[str, Any]],
        calculator: UpchargeCalculator,
        photo_count: int,
        failures: List[Dict[str, str]],
        quality_flags: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        lines = calculator.consolidate(findings)
        total_cents = sum(line.amount_cents for line in lines)
        score = UpchargeCalculator.condition_score(findings)
        total_display = f"${total_cents / 100:,.2f}"

        summary: Dict[str, Any]
        degraded = False
        degraded_reason = ""

        try:
            summary = self.call_json(
                DVIPrompts.inspection_summary(
                    vehicle=job.get("car", "vehicle"),
                    service=job.get("service", ""),
                    findings=[
                        {
                            "defect_type": f["defect_type"],
                            "panel": f["panel"],
                            "severity": f["severity"],
                            "description": f["description"],
                            "suggested_service": f.get("suggested_service", ""),
                        }
                        for f in findings
                    ],
                    upcharge_total_display=total_display,
                    photo_count=photo_count,
                ),
                required_keys=("customer_summary", "condition_headline", "recommendations", "technician_notes"),
                repair_prompt_builder=DVIPrompts.repair,
                schema_hint=SUMMARY_SCHEMA_HINT,
            )
        except (LLMError, OrchestratorError) as exc:
            logger.info("DVI summary degraded: %s", exc)
            degraded, degraded_reason = True, str(exc)
            summary = self._fallback_summary(findings, lines, score, total_display)

        model_used = str(summary.get("_meta", {}).get("model", "unknown"))
        self.repo.save_summary(
            inspection_id=inspection_id,
            summary=str(summary.get("customer_summary", ""))[:2000],
            condition_score=score,
            total_upcharge_cents=total_cents,
            model_used=model_used,
            degraded=1 if degraded else 0,
        )

        payload = {
            "inspection_id": inspection_id,
            "job": job,
            "condition_score": score,
            "photo_count": photo_count,
            "findings": findings,
            "upcharge_lines": [line.as_dict() for line in lines],
            "total_upcharge_cents": total_cents,
            "total_upcharge_display": total_display,
            "summary": summary,
            "failed_photos": failures,
            "quality_flags": quality_flags,
        }
        return self.envelope(
            payload,
            degraded=degraded,
            degraded_reason=degraded_reason,
            source="deterministic" if degraded else "ai",
        )

    @staticmethod
    def _fallback_summary(
        findings: List[Dict[str, Any]], lines: List[Any], score: float, total_display: str
    ) -> Dict[str, Any]:
        if not findings:
            return {
                "customer_summary": "We inspected your vehicle and found no significant paint defects. "
                                    "It's in excellent condition.",
                "condition_headline": "Excellent condition",
                "recommendations": [],
                "technician_notes": [],
                "_generated_by": "deterministic_fallback",
            }

        worst = max(findings, key=lambda f: int(f.get("severity", 1)))
        return {
            "customer_summary": (
                f"Our inspection identified {len(findings)} item(s) across your vehicle's panels, "
                f"most notably {worst['defect_type'].replace('_', ' ')} on the "
                f"{worst['panel'].replace('_', ' ')}. Recommended additional work totals {total_display}. "
                f"Overall condition scored {score}/10."
            ),
            "condition_headline": f"Condition {score}/10 — {len(findings)} item(s) noted",
            "recommendations": [
                {
                    "service": line.service_name,
                    "reason": f"Addresses {line.defect_type.replace('_', ' ')} across "
                              f"{len(line.panels)} panel(s) at severity {line.max_severity}/5.",
                    "priority": "high" if line.max_severity >= 4 else "medium" if line.max_severity == 3 else "low",
                }
                for line in lines
            ],
            "technician_notes": [
                f"{f['panel'].replace('_', ' ')}: {f['description']}" for f in findings[:8]
            ],
            "_generated_by": "deterministic_fallback",
        }

    # ── reporting & conversion ────────────────────────────────────────────

    def report(self, studio_id: int, inspection_id: int) -> Dict[str, Any]:
        """Rebuilds the stored report without re-running any model."""
        inspection = self.repo.get_inspection(studio_id, inspection_id)
        if not inspection:
            raise OrchestratorError("Inspection not found")

        findings = self.repo.findings(inspection_id)
        calculator = UpchargeCalculator(self._studio_services(studio_id))
        lines = calculator.consolidate(findings)
        accepted = [f for f in findings if f.get("decision") == "accepted"]
        accepted_total = sum(int(f.get("suggested_upcharge_cents", 0) or 0) for f in accepted)

        return {
            "inspection": inspection,
            "job": self.jobs.get(studio_id, inspection["job_id"]),
            "photos": self.repo.photos(inspection_id),
            "findings": findings,
            "upcharge_lines": [line.as_dict() for line in lines],
            "total_upcharge_cents": sum(line.amount_cents for line in lines),
            "total_upcharge_display": f"${sum(line.amount_cents for line in lines) / 100:,.2f}",
            "accepted_count": len(accepted),
            "accepted_total_cents": accepted_total,
            "accepted_total_display": f"${accepted_total / 100:,.2f}",
        }

    def decide(self, studio_id: int, finding_id: int, decision: str, actor: str) -> bool:
        if decision not in ("accepted", "declined", "pending"):
            raise OrchestratorError(f"Invalid decision '{decision}'")
        return self.repo.set_decision(studio_id, finding_id, decision, actor)

    def to_estimate(self, studio_id: int, inspection_id: int, actor: str) -> Dict[str, Any]:
        """
        Converts accepted findings into a real estimate row + line items, so the
        DVI feeds the money side of the app instead of dead-ending in a report.
        """
        inspection = self.repo.get_inspection(studio_id, inspection_id)
        if not inspection:
            raise OrchestratorError("Inspection not found")

        accepted = self.repo.accepted_findings(inspection_id)
        if not accepted:
            raise OrchestratorError("Accept at least one recommendation before creating an estimate")

        job = self.jobs.get(studio_id, inspection["job_id"]) or {}
        customer = self._job_customer(studio_id, job)

        calculator = UpchargeCalculator(self._studio_services(studio_id))
        lines = calculator.consolidate(accepted)

        subtotal_cents = sum(line.amount_cents for line in lines)
        tax_percent = 8.5
        tax_cents = int(subtotal_cents * tax_percent / 100)
        total_cents = subtotal_cents + tax_cents

        with self.repo._conn() as conn:  # single transaction for header + items
            cur = conn.execute(
                """
                INSERT INTO estimates
                  (studio_id, customer_name, customer_email, customer_phone, vehicle, status,
                   subtotal, tax_percent, tax_amount, total, notes, internal_notes, customer_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    studio_id,
                    customer.get("name", "Walk-in Customer"),
                    customer.get("email", ""),
                    customer.get("phone", ""),
                    job.get("car", ""),
                    "Draft",
                    subtotal_cents,
                    tax_percent,
                    tax_cents,
                    total_cents,
                    f"Generated from Digital Vehicle Inspection #{inspection_id}.",
                    f"Created by {actor} from DVI #{inspection_id}. "
                    f"Condition score: {inspection.get('condition_score')}/10.",
                    job.get("customer_id"),
                ),
            )
            estimate_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO estimate_items (estimate_id, name, description, quantity, unit_price, total) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (
                        estimate_id,
                        line.service_name or line.defect_type.replace("_", " ").title(),
                        f"{line.defect_type.replace('_', ' ').title()} on "
                        f"{', '.join(p.replace('_', ' ') for p in line.panels)} "
                        f"(severity {line.max_severity}/5)",
                        1,
                        line.amount_cents,
                        line.amount_cents,
                    )
                    for line in lines
                ],
            )
            conn.execute(
                "UPDATE dvi_inspections SET estimate_id=?, status='converted', updated_at=datetime('now') "
                "WHERE id=?",
                (estimate_id, inspection_id),
            )
            conn.commit()

        return {
            "estimate_id": estimate_id,
            "line_count": len(lines),
            "subtotal_cents": subtotal_cents,
            "total_cents": total_cents,
            "total_display": f"${total_cents / 100:,.2f}",
        }

    # ── helpers ───────────────────────────────────────────────────────────

    def _studio_services(self, studio_id: int) -> List[Dict[str, Any]]:
        return self.repo.fetch_all(
            "SELECT id, name, price, duration_hr FROM services WHERE studio_id=?", (studio_id,)
        )

    def _job_customer(self, studio_id: int, job: Dict[str, Any]) -> Dict[str, Any]:
        if job.get("customer_id"):
            found = self.repo.fetch_one(
                "SELECT name, email, phone FROM customers WHERE id=? AND studio_id=?",
                (job["customer_id"], studio_id),
            )
            if found:
                return found
        return {"name": "Walk-in Customer", "email": "", "phone": ""}
