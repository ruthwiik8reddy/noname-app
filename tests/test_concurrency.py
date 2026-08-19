"""
tests/test_concurrency.py — the inference gate under load.

These assert the property that matters: **a person waiting on a page must not
queue behind background agents.** Everything else in this file supports that.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.llm.gate import GateTimeout, InferenceGate, Priority
from src.services.llm.llamacpp_provider import LlamaCppProvider
from src.services.llm.ollama_provider import OllamaProvider


class GateBasicTests(unittest.TestCase):

    def test_respects_concurrency_limit(self):
        gate = InferenceGate(max_concurrent=2)
        peak = {"n": 0}
        active = {"n": 0}
        lock = threading.Lock()

        def work():
            with gate.slot(Priority.INTERACTIVE):
                with lock:
                    active["n"] += 1
                    peak["n"] = max(peak["n"], active["n"])
                time.sleep(0.05)
                with lock:
                    active["n"] -= 1

        threads = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        self.assertLessEqual(peak["n"], 2)
        self.assertEqual(active["n"], 0)

    def test_slot_released_on_exception(self):
        """A crashing call must not leak its slot, or the gate deadlocks."""
        gate = InferenceGate(max_concurrent=1)
        with self.assertRaises(ValueError):
            with gate.slot(Priority.INTERACTIVE):
                raise ValueError("boom")

        with gate.slot(Priority.INTERACTIVE, timeout=1):
            pass  # would hang if the slot had leaked

    def test_timeout_raises_and_does_not_leak(self):
        gate = InferenceGate(max_concurrent=1)
        holder_done = threading.Event()

        def hold():
            with gate.slot(Priority.BACKGROUND):
                holder_done.wait(1.0)

        t = threading.Thread(target=hold)
        t.start()
        time.sleep(0.05)

        with self.assertRaises(GateTimeout):
            with gate.slot(Priority.INTERACTIVE, timeout=0.1):
                pass

        holder_done.set()
        t.join()
        with gate.slot(Priority.INTERACTIVE, timeout=1):
            pass


class PriorityTests(unittest.TestCase):

    def test_interactive_overtakes_queued_background(self):
        """
        The whole reason the gate exists. Fill the gate with background work,
        then queue several more background calls plus one interactive call —
        the interactive one must be served first.
        """
        gate = InferenceGate(max_concurrent=1)
        order: list[str] = []
        lock = threading.Lock()
        release = threading.Event()

        def blocker():
            with gate.slot(Priority.BACKGROUND, label="blocker"):
                release.wait(2.0)

        def worker(name: str, priority: Priority):
            with gate.slot(priority, timeout=3, label=name):
                with lock:
                    order.append(name)
                time.sleep(0.01)

        b = threading.Thread(target=blocker)
        b.start()
        time.sleep(0.08)   # ensure the blocker holds the only slot

        waiters = [threading.Thread(target=worker, args=(f"bg{i}", Priority.BACKGROUND))
                   for i in range(4)]
        [t.start() for t in waiters]
        time.sleep(0.08)   # all four are queued

        user = threading.Thread(target=worker, args=("USER", Priority.INTERACTIVE))
        user.start()
        time.sleep(0.05)   # queued last, but highest priority

        release.set()
        b.join(); user.join()
        [t.join() for t in waiters]

        self.assertEqual(order[0], "USER",
                         f"interactive call should be served first, got {order}")

    def test_triggered_sits_between_interactive_and_background(self):
        self.assertLess(int(Priority.INTERACTIVE), int(Priority.TRIGGERED))
        self.assertLess(int(Priority.TRIGGERED), int(Priority.BACKGROUND))

    def test_stats_track_queueing(self):
        gate = InferenceGate(max_concurrent=1)

        def work():
            with gate.slot(Priority.BACKGROUND, timeout=2):
                time.sleep(0.02)

        threads = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        stats = gate.stats()
        self.assertEqual(stats["served"], 4)
        self.assertEqual(stats["active"], 0)
        self.assertEqual(stats["queued"], 0)
        self.assertGreater(stats["peak_queue"], 0)


class ThroughputTests(unittest.TestCase):

    def test_parallel_gate_is_faster_than_serial(self):
        """Two slots should roughly halve wall time for eight 50ms jobs."""
        def measure(concurrency: int) -> float:
            gate = InferenceGate(max_concurrent=concurrency)

            def work():
                with gate.slot(Priority.INTERACTIVE, timeout=10):
                    time.sleep(0.05)

            started = time.time()
            threads = [threading.Thread(target=work) for _ in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            return time.time() - started

        serial = measure(1)
        parallel = measure(4)
        self.assertLess(parallel, serial * 0.6,
                        f"4 slots ({parallel:.2f}s) should beat 1 slot ({serial:.2f}s)")


class ProviderContractTests(unittest.TestCase):
    """Every provider must accept the same kwargs — Liskov, enforced."""

    def test_all_providers_accept_priority_and_tier(self):
        import inspect
        from src.services.llm.base import NullProvider, RecordingProvider

        for cls in (OllamaProvider, LlamaCppProvider):
            sig = inspect.signature(cls.complete)
            for kwarg in ("priority", "tier", "gate_timeout"):
                self.assertIn(kwarg, sig.parameters,
                              f"{cls.__name__}.complete is missing '{kwarg}'")

        # The stubs accept **kwargs, so they tolerate anything a caller passes.
        for stub in (NullProvider(), RecordingProvider(["{}"])):
            try:
                stub.complete("x", priority=10, tier="fast", gate_timeout=1.0)
            except Exception as exc:  # noqa: BLE001
                self.assertNotIsInstance(exc, TypeError,
                                         f"{type(stub).__name__} rejected the standard kwargs")

    def test_fast_tier_selects_a_different_model(self):
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            text_model="llama3.1:8b",
            fast_model="llama3.2:3b",
        )
        self.assertEqual(provider.model_for_tier("fast"), "llama3.2:3b")
        self.assertEqual(provider.model_for_tier("standard"), "llama3.1:8b")

    def test_fast_tier_falls_back_when_unconfigured(self):
        provider = OllamaProvider(base_url="http://x", text_model="llama3.1:8b")
        self.assertEqual(provider.model_for_tier("fast"), "llama3.1:8b")


class AgentPriorityTests(unittest.TestCase):

    def test_agents_borrow_logic_not_urgency(self):
        """
        An agent reusing an orchestrator must not inherit its INTERACTIVE
        priority — otherwise background work competes with a live page.
        """
        from src.services.agents.base import BaseAgent
        from src.services.orchestrators.inventory_orchestrator import (
            InventoryIntelligenceOrchestrator,
        )

        orch = InventoryIntelligenceOrchestrator()
        self.assertEqual(orch.priority, int(Priority.INTERACTIVE))

        with BaseAgent.background(orch):
            self.assertEqual(orch.priority, int(Priority.BACKGROUND))
            self.assertIsNotNone(orch.gate_timeout)

        self.assertEqual(orch.priority, int(Priority.INTERACTIVE))

    def test_priority_restored_even_on_exception(self):
        from src.services.agents.base import BaseAgent
        from src.services.orchestrators.inventory_orchestrator import (
            InventoryIntelligenceOrchestrator,
        )

        orch = InventoryIntelligenceOrchestrator()
        with self.assertRaises(ValueError):
            with BaseAgent.background(orch):
                raise ValueError("boom")
        self.assertEqual(orch.priority, int(Priority.INTERACTIVE))

    def test_leads_drafter_is_background_and_fast(self):
        from src.services.agents.leads_agent import _Drafter

        self.assertEqual(_Drafter.priority, int(Priority.BACKGROUND))
        self.assertEqual(_Drafter.tier, "fast")
        self.assertIsNotNone(_Drafter.gate_timeout)

    def test_drafting_is_capped(self):
        """Unbounded per-lead drafting was the original bug. It must stay capped."""
        from src.services.agents.leads_agent import MAX_DRAFTS
        self.assertLessEqual(MAX_DRAFTS, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
