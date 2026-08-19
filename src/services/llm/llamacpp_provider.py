"""
services/llm/llamacpp_provider.py — llama.cpp server backend.

Why this exists alongside Ollama: `llama-server` supports **continuous batching**
across `--parallel N` slots. Several requests are decoded together in one forward
pass rather than one after another, so four concurrent calls cost far less than
four times one call. Ollama can serve parallel requests too, but it manages model
loading opportunistically and will evict and reload models under memory pressure
— and a reload costs far more than the queueing it was avoiding.

The trade-off is honest: llama.cpp needs you to start a server per model and
point at it. Ollama is one daemon that handles everything. If you run one text
model constantly, llama.cpp is faster; if you switch models often, Ollama's
management is worth the serialisation.

Adding this required **zero changes** to any agent, orchestrator or route. That
is what `LLMProvider` was for — the abstraction earns its keep the first time you
swap a backend.

Start a server:

    llama-server -m models/llama-3.1-8b-instruct-Q4_K_M.gguf \
                 --port 8080 --parallel 4 --cont-batching --ctx-size 8192

Then set:

    LLM_BACKEND=llamacpp
    LLAMACPP_URL=http://127.0.0.1:8080
"""

from __future__ import annotations

import base64
import json
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
from .gate import GATE, GateTimeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120
HEALTH_CACHE_SECONDS = 30
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class LlamaCppProvider(VisionLLMProvider):
    """
    Talks to `llama-server`'s OpenAI-compatible `/v1/chat/completions`.

    The gate is still used even though llama.cpp batches internally: the server
    has a fixed slot count, and exceeding it queues requests inside llama.cpp
    where we can't prioritise them. Better to hold the queue here, where an
    interactive call can overtake a background one.
    """

    name = "llamacpp"

    def __init__(
        self,
        base_url: str,
        model: str = "local",
        vision_url: str = "",
        vision_model: str = "local-vision",
        fast_url: str = "",
        fast_model: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        # Separate servers per model: llama-server hosts one model each, so a
        # vision or fast tier means another process on another port.
        self.vision_url = (vision_url or "").rstrip("/")
        self.vision_model = vision_model
        self.fast_url = (fast_url or "").rstrip("/") or self.base_url
        self.fast_model = fast_model or model
        self.timeout = timeout
        self._session = session or self._build_session()
        self._health: Optional[bool] = None
        self._checked_at = 0.0

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    # ── health ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        if not self.base_url:
            return False
        now = time.time()
        if self._health is not None and (now - self._checked_at) < HEALTH_CACHE_SECONDS:
            return self._health
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=3)
            self._health = resp.status_code == 200
        except Exception:  # noqa: BLE001
            self._health = False
        self._checked_at = now
        return self._health

    def slots_info(self) -> Dict[str, Any]:
        """How many parallel slots the server has, and how many are busy."""
        try:
            resp = self._session.get(f"{self.base_url}/slots", timeout=3)
            if resp.status_code != 200:
                return {}
            slots = resp.json()
            return {
                "total": len(slots),
                "busy": sum(1 for s in slots if s.get("is_processing")),
            }
        except Exception:  # noqa: BLE001
            return {}

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "text_model": self.model,
            "fast_model": self.fast_model,
            "vision_model": self.vision_model if self.vision_url else "(not configured)",
            "available": self.is_available(),
            "slots": self.slots_info(),
        }

    # ── completions ───────────────────────────────────────────────────────

    def _url_and_model(self, tier: str, model: Optional[str]) -> tuple[str, str]:
        if tier == "vision" and self.vision_url:
            return self.vision_url, (model or self.vision_model)
        if tier == "fast":
            return self.fast_url, (model or self.fast_model)
        return self.base_url, (model or self.model)

    def complete(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        priority: int = 0,
        tier: str = "standard",
        gate_timeout: Optional[float] = None,
    ) -> LLMResult:
        url, model_name = self._url_and_model(tier, model)
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        return self._post(url, payload, timeout or self.timeout, json_mode, 0,
                          priority, gate_timeout)

    def complete_vision(
        self,
        prompt: str,
        images: Sequence[str],
        *,
        json_mode: bool = True,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        model: Optional[str] = None,
        priority: int = 0,
        gate_timeout: Optional[float] = None,
    ) -> LLMResult:
        if not self.vision_url:
            raise LLMUnavailableError(
                "No llama.cpp vision server configured. Set LLAMACPP_VISION_URL, "
                "or use the Ollama backend for DVI."
            )

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        count = 0
        for path in images:
            encoded = self._encode(path)
            if not encoded:
                continue
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            })
            count += 1

        if not count:
            raise LLMInvalidResponseError("No readable images supplied for vision analysis")

        payload = {
            "model": model or self.vision_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        return self._post(self.vision_url, payload, timeout or self.timeout * 2,
                          json_mode, count, priority, gate_timeout)

    @staticmethod
    def _encode(path: str) -> Optional[str]:
        try:
            if not os.path.exists(path) or os.path.getsize(path) > MAX_IMAGE_BYTES:
                return None
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode("utf-8")
        except OSError as exc:
            logger.warning("Could not read image %s: %s", path, exc)
            return None

    # ── transport ─────────────────────────────────────────────────────────

    def _post(
        self, url: str, payload: Dict[str, Any], timeout: int, json_mode: bool,
        image_count: int, priority: int, gate_timeout: Optional[float],
    ) -> LLMResult:
        if not url:
            raise LLMUnavailableError("LLAMACPP_URL is not configured")

        try:
            with GATE.slot(priority=priority, timeout=gate_timeout,
                           label=f"llamacpp:{payload['model']}"):
                return self._do_post(url, payload, timeout, json_mode, image_count)
        except GateTimeout as exc:
            raise LLMUnavailableError(str(exc)) from exc

    def _do_post(
        self, url: str, payload: Dict[str, Any], timeout: int,
        json_mode: bool, image_count: int,
    ) -> LLMResult:
        started = time.time()
        try:
            resp = self._session.post(f"{url}/v1/chat/completions", json=payload, timeout=timeout)
        except requests.Timeout as exc:
            raise LLMTimeoutError(f"llama.cpp timed out after {timeout}s") from exc
        except requests.RequestException as exc:
            self._health = False
            raise LLMUnavailableError(f"Cannot reach llama.cpp at {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMUnavailableError(f"llama.cpp returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
            text = (body["choices"][0]["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMInvalidResponseError("llama.cpp returned an unexpected envelope") from exc

        if not text:
            raise LLMInvalidResponseError(f"llama.cpp returned an empty completion")

        usage = body.get("usage", {}) or {}
        return LLMResult(
            text=text,
            model=payload["model"],
            provider=self.name,
            latency_ms=int((time.time() - started) * 1000),
            json_mode=json_mode,
            image_count=image_count,
            raw={"eval_count": usage.get("completion_tokens"),
                 "prompt_tokens": usage.get("prompt_tokens")},
        )
