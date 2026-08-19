"""
services/llm/gate.py — the traffic controller for local inference.

The problem this solves: local inference is a **single scarce resource**. One
GPU, a fixed amount of VRAM, and a model that can serve N requests at once — not
unlimited. Before this existed, a background agent looping over eleven leads
would hold Ollama for 88 seconds while a person waiting on `/analytics/` sat
behind it. The agent didn't know a person was waiting. Nothing did.

So every LLM call now passes through a gate that knows two things:

1. **How many calls can run at once** (`InferenceGate.max_concurrent`), matched
   to what the backend is actually configured for. Setting this higher than
   `OLLAMA_NUM_PARALLEL` doesn't buy concurrency — it just moves the queue from
   here to there, where we can't see or prioritise it.

2. **Who is waiting.** A person staring at a spinner outranks a scheduled agent
   every time. `Priority.INTERACTIVE` jumps the queue; `Priority.BACKGROUND`
   yields. Without this, fairness means the agent and the human get equal
   treatment, which in practice means the human waits for the robot.

Background work also gets a **deadline**: an agent that can't get a slot within
its patience window gives up and degrades to deterministic output rather than
queueing forever. Nobody is watching an agent, so nobody benefits from it
waiting ten minutes.
"""

from __future__ import annotations

import heapq
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Lower value wins. Interactive work always outranks background work."""

    INTERACTIVE = 0   # a person is watching a spinner
    TRIGGERED = 5     # an agent fired by something a person just did
    BACKGROUND = 10   # the scheduler


class GateTimeout(RuntimeError):
    """Couldn't get an inference slot before the deadline."""


@dataclass(order=True)
class _Waiter:
    priority: int
    sequence: int
    event: threading.Event = field(compare=False, default_factory=threading.Event)
    label: str = field(compare=False, default="")


class InferenceGate:
    """
    A priority semaphore.

    `threading.Semaphore` would be simpler, but it is strictly FIFO — a page
    request arriving behind four agent calls waits for all four. Here waiters sit
    in a heap ordered by priority, so an interactive call is served next
    regardless of when it arrived.
    """

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max(1, int(max_concurrent))
        self._lock = threading.Lock()
        self._active = 0
        self._heap: list[_Waiter] = []
        self._seq = 0
        self._stats: Dict[str, Any] = {
            "served": 0, "timeouts": 0, "peak_queue": 0, "total_wait_ms": 0,
        }

    # ── acquisition ───────────────────────────────────────────────────────

    @contextmanager
    def slot(
        self,
        priority: Priority = Priority.INTERACTIVE,
        timeout: Optional[float] = None,
        label: str = "",
    ) -> Iterator[None]:
        """
        Hold an inference slot for the duration of the block.

        Raises `GateTimeout` if no slot frees up in time. Background callers
        should catch it and fall back to their deterministic path.
        """
        started = time.time()
        acquired = self._acquire(priority, timeout, label)
        if not acquired:
            with self._lock:
                self._stats["timeouts"] += 1
            raise GateTimeout(
                f"No inference slot for '{label or 'call'}' after {timeout}s "
                f"({self._active}/{self.max_concurrent} busy)"
            )

        wait_ms = int((time.time() - started) * 1000)
        with self._lock:
            self._stats["served"] += 1
            self._stats["total_wait_ms"] += wait_ms
        if wait_ms > 1000:
            logger.debug("Waited %dms for an inference slot (%s)", wait_ms, label)

        try:
            yield
        finally:
            self._release()

    def _acquire(self, priority: Priority, timeout: Optional[float], label: str) -> bool:
        with self._lock:
            if self._active < self.max_concurrent and not self._heap:
                self._active += 1
                return True
            self._seq += 1
            waiter = _Waiter(priority=int(priority), sequence=self._seq, label=label)
            heapq.heappush(self._heap, waiter)
            self._stats["peak_queue"] = max(self._stats["peak_queue"], len(self._heap))

        if waiter.event.wait(timeout):
            return True

        # Timed out — withdraw from the queue if still in it.
        with self._lock:
            if waiter in self._heap:
                self._heap.remove(waiter)
                heapq.heapify(self._heap)
                return False
            # We were signalled between the timeout and the lock: the slot is
            # ours whether we want it or not, so release it cleanly.
        self._release()
        return False

    def _release(self) -> None:
        with self._lock:
            if self._heap:
                # Hand the slot straight to the highest-priority waiter rather
                # than decrementing — otherwise a newly-arriving call could
                # overtake everyone queued.
                heapq.heappop(self._heap).event.set()
            else:
                self._active = max(0, self._active - 1)

    # ── introspection ─────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            served = self._stats["served"] or 1
            return {
                **self._stats,
                "max_concurrent": self.max_concurrent,
                "active": self._active,
                "queued": len(self._heap),
                "avg_wait_ms": int(self._stats["total_wait_ms"] / served),
            }


def _default_concurrency() -> int:
    """
    Match the backend's real capability.

    Ollama serves `OLLAMA_NUM_PARALLEL` requests per model (1 on older builds,
    4 on newer). Claiming more here doesn't create concurrency — it just hides
    the queue inside Ollama where we can't prioritise it.
    """
    explicit = os.getenv("LLM_MAX_CONCURRENT")
    if explicit and explicit.isdigit():
        return max(1, int(explicit))
    ollama_parallel = os.getenv("OLLAMA_NUM_PARALLEL")
    if ollama_parallel and ollama_parallel.isdigit():
        return max(1, int(ollama_parallel))
    return 2  # conservative: two in flight is safe on a laptop-class GPU


# Process-wide singleton. Every provider call goes through this one instance —
# that is the entire point, and a second gate would defeat it.
GATE = InferenceGate(_default_concurrency())
