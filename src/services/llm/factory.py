"""
services/llm/factory.py — composition root for AI backends.

Orchestrators ask for a capability ("something that can see"), never a vendor.
Adding llama.cpp required changing only this file and adding one provider —
no agent, orchestrator or route knows which backend is in use.

Set LLM_BACKEND=ollama (default) or LLM_BACKEND=llamacpp.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from ...config import Config
from .base import LLMProvider, NullProvider, VisionLLMProvider
from .llamacpp_provider import LlamaCppProvider
from .ollama_provider import OllamaProvider

_lock = threading.Lock()
_provider: Optional[VisionLLMProvider] = None


def _ai_enabled() -> bool:
    return str(getattr(Config, "AI_ENABLED", "1")).lower() not in ("0", "false", "no", "off")


class LLMProviderFactory:
    """All LLM construction happens here and only here."""

    @staticmethod
    def text_provider() -> LLMProvider:
        return LLMProviderFactory._get()

    @staticmethod
    def vision_provider() -> VisionLLMProvider:
        return LLMProviderFactory._get()

    @staticmethod
    def _get() -> VisionLLMProvider:
        global _provider
        if _provider is None:
            with _lock:
                if _provider is None:
                    _provider = LLMProviderFactory._build()
        return _provider

    @staticmethod
    def _build() -> VisionLLMProvider:
        if not _ai_enabled():
            return NullProvider("AI is disabled (set AI_ENABLED=1 to turn it back on)")

        backend = str(getattr(Config, "LLM_BACKEND", "ollama")).lower()

        if backend == "llamacpp":
            url = getattr(Config, "LLAMACPP_URL", "")
            if not url:
                return NullProvider("LLM_BACKEND=llamacpp but LLAMACPP_URL is not set")
            return LlamaCppProvider(
                base_url=url,
                model=getattr(Config, "LLAMACPP_MODEL", "local"),
                vision_url=getattr(Config, "LLAMACPP_VISION_URL", ""),
                vision_model=getattr(Config, "LLAMACPP_VISION_MODEL", "local-vision"),
                fast_url=getattr(Config, "LLAMACPP_FAST_URL", ""),
                fast_model=getattr(Config, "LLAMACPP_FAST_MODEL", ""),
                timeout=int(getattr(Config, "OLLAMA_TEXT_TIMEOUT", 90)),
            )

        if not getattr(Config, "OLLAMA_URL", ""):
            return NullProvider("OLLAMA_URL is not set in your .env")
        return OllamaProvider(
            base_url=Config.OLLAMA_URL,
            text_model=getattr(Config, "OLLAMA_MODEL", "llama3.1:8b"),
            vision_model=getattr(Config, "OLLAMA_VISION_MODEL", "llava"),
            fast_model=getattr(Config, "OLLAMA_FAST_MODEL", ""),
            text_timeout=int(getattr(Config, "OLLAMA_TEXT_TIMEOUT", 90)),
            vision_timeout=int(getattr(Config, "OLLAMA_VISION_TIMEOUT", 180)),
        )

    @staticmethod
    def reset() -> None:
        global _provider
        with _lock:
            _provider = None

    @staticmethod
    def override(provider: VisionLLMProvider) -> None:
        """Inject a provider — tests, or a future per-studio BYO-model feature."""
        global _provider
        with _lock:
            _provider = provider
