"""
services/llm/ollama_provider.py — the one and only place that speaks HTTP to Ollama.

Everything privacy-sensitive stops here: inventory logs, technician photos and
customer vehicle data are POSTed to 127.0.0.1 and nowhere else. If you ever
need to prove "no studio data leaves the building", this file is the audit
surface — it is the only outbound call site in the AI stack.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence

import requests

from .base import (
    LLMInvalidResponseError,
    LLMResult,
    LLMTimeoutError,
    LLMUnavailableError,
    VisionLLMProvider,
)

logger = logging.getLogger(__name__)

# Vision payloads are big and local vision models are slow — they get their own budget.
DEFAULT_TEXT_TIMEOUT = 90
DEFAULT_VISION_TIMEOUT = 180
HEALTH_CACHE_SECONDS = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class OllamaProvider(VisionLLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        text_model: str,
        vision_model: str = "llava",
        text_timeout: int = DEFAULT_TEXT_TIMEOUT,
        vision_timeout: int = DEFAULT_VISION_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.text_model = text_model
        self.vision_model = vision_model
        self.text_timeout = text_timeout
        self.vision_timeout = vision_timeout
        self._session = session or requests.Session()
        self._health: Optional[bool] = None
        self._health_checked_at: float = 0.0

    # ── health ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Never raises. Result is cached briefly so page loads don't stampede."""
        if not self.base_url:
            return False
        now = time.time()
        if self._health is not None and (now - self._health_checked_at) < HEALTH_CACHE_SECONDS:
            return self._health
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=3)
            self._health = resp.status_code == 200
        except Exception as exc:  # noqa: BLE001 - health checks must not propagate
            logger.debug("Ollama health check failed: %s", exc)
            self._health = False
        self._health_checked_at = now
        return self._health

    def installed_models(self) -> List[str]:
        try:
            resp = self._session.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            return [m.get("name", "") for m in resp.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "text_model": self.text_model,
            "vision_model": self.vision_model,
            "available": self.is_available(),
            "installed_models": self.installed_models(),
        }

    # ── completions ───────────────────────────────────────────────────────

    def complete(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
    ) -> LLMResult:
        payload: Dict[str, Any] = {
            "model": model or self.text_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        return self._post(payload, timeout or self.text_timeout, json_mode, 0)

    def complete_vision(
        self,
        prompt: str,
        images: Sequence[str],
        *,
        json_mode: bool = True,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        model: Optional[str] = None,
    ) -> LLMResult:
        encoded = self._encode_images(images)
        if not encoded:
            raise LLMInvalidResponseError("No readable images supplied for vision analysis")

        payload: Dict[str, Any] = {
            "model": model or self.vision_model,
            "prompt": prompt,
            "images": encoded,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"
        return self._post(payload, timeout or self.vision_timeout, json_mode, len(encoded))

    # ── internals ─────────────────────────────────────────────────────────

    def _encode_images(self, images: Sequence[str]) -> List[str]:
        encoded: List[str] = []
        for path in images:
            try:
                if not os.path.exists(path):
                    logger.warning("Vision image missing on disk: %s", path)
                    continue
                if os.path.getsize(path) > MAX_IMAGE_BYTES:
                    logger.warning("Skipping oversized image (>8MB): %s", path)
                    continue
                with open(path, "rb") as handle:
                    encoded.append(base64.b64encode(handle.read()).decode("utf-8"))
            except OSError as exc:
                logger.warning("Could not read image %s: %s", path, exc)
        return encoded

    def _post(self, payload: Dict[str, Any], timeout: int, json_mode: bool, image_count: int) -> LLMResult:
        if not self.base_url:
            raise LLMUnavailableError("OLLAMA_URL is not configured")

        started = time.time()
        try:
            resp = self._session.post(f"{self.base_url}/api/generate", json=payload, timeout=timeout)
        except requests.Timeout as exc:
            raise LLMTimeoutError(f"Ollama timed out after {timeout}s ({payload['model']})") from exc
        except requests.RequestException as exc:
            self._health = False
            raise LLMUnavailableError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc

        if resp.status_code == 404:
            raise LLMUnavailableError(
                f"Model '{payload['model']}' is not installed. Run: ollama pull {payload['model']}"
            )
        if resp.status_code >= 400:
            raise LLMUnavailableError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMInvalidResponseError("Ollama returned a non-JSON envelope") from exc

        text = (body.get("response") or "").strip()
        if not text:
            raise LLMInvalidResponseError(f"Ollama returned an empty completion for {payload['model']}")

        return LLMResult(
            text=text,
            model=payload["model"],
            provider=self.name,
            latency_ms=int((time.time() - started) * 1000),
            json_mode=json_mode,
            image_count=image_count,
            raw={k: body.get(k) for k in ("eval_count", "total_duration", "done_reason")},
        )
