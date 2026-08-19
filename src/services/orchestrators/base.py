"""
services/orchestrators/base.py — the Orchestrator Pattern's shared machinery.

An orchestrator is the ONLY kind of object permitted to call an LLM. It:
  1. gathers data through repositories,
  2. computes whatever can be computed deterministically,
  3. asks a model for the part that genuinely needs language,
  4. and returns a plain dict a controller can `jsonify` without thinking.

Controllers get no `requests` import, no prompt strings, no JSON parsing, no
retry logic. If a controller ever needs to know an LLM exists, the pattern has
been broken.

Everything defensive about small local models lives here so no subclass
reimplements it: fence-stripping, brace-extraction, one repair retry, schema
validation, TTL caching, and a consistent envelope whose shape does not change
when the model is unreachable. The frontend renders one contract, always.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from abc import ABC
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..llm.gate import Priority
from ..llm.base import (
    LLMError,
    LLMProvider,
    LLMResult,
    LLMUnavailableError,
    VisionLLMProvider,
)

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


class OrchestratorError(RuntimeError):
    """Raised for orchestration failures a controller should surface as 4xx/5xx."""


class TTLCache:
    """Small thread-safe TTL cache. Local inference is slow; repeat views shouldn't pay twice."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._data: Dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            stamped, value = entry
            if time.time() - stamped > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def invalidate(self, predicate: Optional[Callable[[Any], bool]] = None) -> None:
        with self._lock:
            if predicate is None:
                self._data.clear()
                return
            for key in [k for k in self._data if predicate(k)]:
                self._data.pop(key, None)


class BaseOrchestrator(ABC):

    cache_ttl_seconds = 300

    def __init__(self, provider: Optional[LLMProvider] = None):
        self._provider = provider
        self._cache = TTLCache(self.cache_ttl_seconds)

    # ── provider access ───────────────────────────────────────────────────

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            from ..llm.factory import LLMProviderFactory

            self._provider = LLMProviderFactory.text_provider()
        return self._provider

    def ai_available(self) -> bool:
        try:
            return self.provider.is_available()
        except Exception:  # noqa: BLE001
            return False

    # ── JSON coercion ─────────────────────────────────────────────────────

    @staticmethod
    def extract_json(text: str) -> Optional[Dict[str, Any]]:
        """
        Small models wrap JSON in fences, prose, or both. Try increasingly
        forgiving strategies rather than failing on a stray backtick.
        """
        if not text:
            return None

        candidate = _FENCE.sub("", text).strip()
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            pass

        # Fall back to the outermost balanced {...} block.
        start = candidate.find("{")
        if start == -1:
            return None
        depth, in_string, escaped = 0, False, False
        for idx in range(start, len(candidate)):
            char = candidate[idx]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(candidate[start : idx + 1])
                        return parsed if isinstance(parsed, dict) else None
                    except json.JSONDecodeError:
                        return None
        return None

    # ── guarded LLM calls ─────────────────────────────────────────────────

    # Default priority for this orchestrator's calls. Agents override this to
    # BACKGROUND so a person waiting on a page always overtakes them.
    priority: int = int(Priority.INTERACTIVE)
    gate_timeout: Optional[float] = None
    tier: str = "standard"

    def call_json(
        self,
        prompt: str,
        *,
        required_keys: Sequence[str] = (),
        images: Optional[Sequence[str]] = None,
        repair_prompt_builder: Optional[Callable[[str, str], str]] = None,
        schema_hint: str = "",
        timeout: Optional[int] = None,
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ask for JSON, get JSON — or raise. One repair round-trip is attempted
        before giving up, because local models frequently succeed on the retry.

        Raises LLMUnavailableError / OrchestratorError so callers can decide
        between degrading and failing.
        """
        result = self._raw_call(prompt, images, timeout, tier or self.tier)
        parsed = self.extract_json(result.text)

        if parsed is None and repair_prompt_builder is not None:
            logger.info("Unparseable JSON from %s — attempting one repair pass", result.model)
            repair = repair_prompt_builder(result.text, schema_hint)
            try:
                retry = self.provider.complete(
                    repair, json_mode=True, timeout=timeout,
                    priority=self.priority, tier=tier or self.tier,
                    gate_timeout=self.gate_timeout,
                )
                parsed = self.extract_json(retry.text)
                if parsed is not None:
                    result = retry
            except LLMError as exc:
                logger.warning("Repair pass failed: %s", exc)

        if parsed is None:
            raise OrchestratorError(f"Model {result.model} did not return usable JSON")

        missing = [k for k in required_keys if k not in parsed]
        if missing:
            logger.warning("Model omitted keys %s — filling with empty values", missing)
            for key in missing:
                parsed[key] = [] if key.endswith(("s", "list", "actions", "notes")) else ""

        parsed["_meta"] = result.as_dict()
        return parsed

    def _raw_call(
        self, prompt: str, images: Optional[Sequence[str]], timeout: Optional[int],
        tier: str = "standard",
    ) -> LLMResult:
        if images:
            provider = self.provider
            if not isinstance(provider, VisionLLMProvider):
                raise LLMUnavailableError("Configured provider cannot process images")
            return provider.complete_vision(
                prompt, images, json_mode=True, timeout=timeout,
                priority=self.priority, gate_timeout=self.gate_timeout,
            )
        return self.provider.complete(
            prompt, json_mode=True, timeout=timeout,
            priority=self.priority, tier=tier, gate_timeout=self.gate_timeout,
        )

    # ── response envelope ─────────────────────────────────────────────────

    @staticmethod
    def envelope(
        data: Dict[str, Any],
        *,
        degraded: bool = False,
        degraded_reason: str = "",
        source: str = "ai",
        generated_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        One response shape for every orchestrator, healthy or degraded. The UI
        binds to this once and never branches on whether the model was up.
        """
        payload = dict(data)
        payload["_status"] = {
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "source": source,  # "ai" | "deterministic" | "cache"
            "generated_at": generated_at or time.time(),
        }
        return payload

    # ── caching ───────────────────────────────────────────────────────────

    def cached(self, key: Any) -> Optional[Dict[str, Any]]:
        hit = self._cache.get(key)
        if hit is None:
            return None
        payload = dict(hit)
        status = dict(payload.get("_status", {}))
        status["source"] = "cache"
        payload["_status"] = status
        return payload

    def store(self, key: Any, value: Dict[str, Any]) -> Dict[str, Any]:
        self._cache.set(key, value)
        return value

    def invalidate_studio(self, studio_id: int) -> None:
        self._cache.invalidate(lambda k: isinstance(k, tuple) and k and k[0] == studio_id)
