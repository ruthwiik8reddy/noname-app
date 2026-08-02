"""
services/llm/factory.py — composition root for AI backends.

Orchestrators ask the factory for a capability ("give me something that can
see"), never for a vendor. Providers are memoised per-process so we reuse the
underlying HTTP connection pool and the cached health check.

To add a backend later: implement LLMProvider, register it here. Nothing else
in the codebase needs to know it happened.
"""

from __future__ import annotations

import threading
from typing import Optional

from ...config import Config
from .base import LLMProvider, NullProvider, VisionLLMProvider
from .ollama_provider import OllamaProvider

_lock = threading.Lock()
_text_provider: Optional[LLMProvider] = None
_vision_provider: Optional[VisionLLMProvider] = None


def _ai_enabled() -> bool:
    return str(getattr(Config, "AI_ENABLED", "1")).lower() not in ("0", "false", "no", "off")


class LLMProviderFactory:
    """All LLM construction happens here and only here."""

    @staticmethod
    def text_provider() -> LLMProvider:
        global _text_provider
        if _text_provider is None:
            with _lock:
                if _text_provider is None:
                    _text_provider = LLMProviderFactory._build()
        return _text_provider

    @staticmethod
    def vision_provider() -> VisionLLMProvider:
        global _vision_provider
        if _vision_provider is None:
            with _lock:
                if _vision_provider is None:
                    built = LLMProviderFactory._build()
                    _vision_provider = (
                        built if isinstance(built, VisionLLMProvider)
                        else NullProvider("Configured backend has no vision capability")
                    )
        return _vision_provider

    @staticmethod
    def _build() -> VisionLLMProvider:
        if not _ai_enabled():
            return NullProvider("AI is disabled (set AI_ENABLED=1 to turn it back on)")
        if not getattr(Config, "OLLAMA_URL", ""):
            return NullProvider("OLLAMA_URL is not set in your .env")
        return OllamaProvider(
            base_url=Config.OLLAMA_URL,
            text_model=getattr(Config, "OLLAMA_MODEL", "llama3.1:8b"),
            vision_model=getattr(Config, "OLLAMA_VISION_MODEL", "llava"),
            text_timeout=int(getattr(Config, "OLLAMA_TEXT_TIMEOUT", 90)),
            vision_timeout=int(getattr(Config, "OLLAMA_VISION_TIMEOUT", 180)),
        )

    @staticmethod
    def reset() -> None:
        """Drop memoised providers — used by tests and after a config change."""
        global _text_provider, _vision_provider
        with _lock:
            _text_provider = None
            _vision_provider = None

    @staticmethod
    def override(provider: VisionLLMProvider) -> None:
        """Inject a provider (tests, or a future per-studio BYO-model feature)."""
        global _text_provider, _vision_provider
        with _lock:
            _text_provider = provider
            _vision_provider = provider
