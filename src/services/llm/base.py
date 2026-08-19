"""
services/llm/base.py — LLM provider contracts.

This is the *only* place the rest of the app is allowed to learn what an
"LLM" is. Orchestrators depend on these abstractions, never on `requests`,
never on Ollama, never on a URL. That is the Dependency Inversion Principle
made concrete: swap OllamaProvider for vLLM, llama.cpp, or a cloud provider
and not one orchestrator line changes.

Interface Segregation: text-only callers depend on `LLMProvider`. Only the
DVI pipeline depends on `VisionLLMProvider`. A text-only backend is never
forced to implement (or fake) an image method it cannot honour.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


class LLMError(RuntimeError):
    """Base class for every LLM failure. Callers catch this, not `Exception`."""


class LLMUnavailableError(LLMError):
    """The backend could not be reached at all (down, bad URL, timeout)."""


class LLMTimeoutError(LLMUnavailableError):
    """The backend accepted the request but did not answer in time."""


class LLMInvalidResponseError(LLMError):
    """The backend answered, but not with anything we can use."""


@dataclass
class LLMResult:
    """A single completion, plus the metadata we need for observability."""

    text: str
    model: str
    provider: str
    latency_ms: int = 0
    json_mode: bool = False
    image_count: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "json_mode": self.json_mode,
            "image_count": self.image_count,
            "chars": len(self.text or ""),
        }


class LLMProvider(ABC):
    """Text completion contract."""

    name: str = "abstract"

    @abstractmethod
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
        """
        Return a completion or raise an `LLMError` subclass. Never returns None.

        `priority`     — see llm.gate.Priority. Lower is more urgent.
        `tier`         — "fast" | "standard" | "vision". Lets a caller ask for a
                         smaller model when the task is small; drafting a
                         two-sentence follow-up does not need an 8B model.
        `gate_timeout` — how long to wait for an inference slot before giving up.
                         Background callers should set this and degrade on
                         GateTimeout rather than queue indefinitely.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap health check. Must never raise — returns False instead."""

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "available": self.is_available()}


class VisionLLMProvider(LLMProvider):
    """Adds multimodal completion. Only DVI depends on this narrower contract."""

    @abstractmethod
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
        """`images` are filesystem paths. Implementations handle encoding."""


class NullProvider(VisionLLMProvider):
    """
    Honest no-op provider (Liskov-safe): used when AI is disabled or no
    backend is configured. It raises the same errors a dead Ollama raises,
    so every caller's degraded path gets exercised in dev and in tests.
    """

    name = "null"

    def __init__(self, reason: str = "No LLM backend configured"):
        self.reason = reason

    def complete(self, prompt: str, **kwargs: Any) -> LLMResult:
        raise LLMUnavailableError(self.reason)

    def complete_vision(self, prompt: str, images: Sequence[str], **kwargs: Any) -> LLMResult:
        raise LLMUnavailableError(self.reason)

    def is_available(self) -> bool:
        return False


class RecordingProvider(VisionLLMProvider):
    """
    Test double. Returns canned responses in order and records every prompt,
    letting us assert on prompt construction without a running model.
    """

    name = "recording"

    def __init__(self, responses: Optional[List[str]] = None, available: bool = True):
        self.responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []
        self._available = available

    def _next(self, prompt: str, images: Sequence[str], json_mode: bool) -> LLMResult:
        self.calls.append({"prompt": prompt, "images": list(images), "json_mode": json_mode})
        if not self.responses:
            raise LLMUnavailableError("RecordingProvider ran out of canned responses")
        started = time.time()
        return LLMResult(
            text=self.responses.pop(0),
            model="recording",
            provider=self.name,
            latency_ms=int((time.time() - started) * 1000),
            json_mode=json_mode,
            image_count=len(images),
        )

    def complete(self, prompt: str, *, json_mode: bool = False, **kwargs: Any) -> LLMResult:
        return self._next(prompt, [], json_mode)

    def complete_vision(
        self, prompt: str, images: Sequence[str], *, json_mode: bool = True, **kwargs: Any
    ) -> LLMResult:
        return self._next(prompt, images, json_mode)

    def is_available(self) -> bool:
        return self._available
