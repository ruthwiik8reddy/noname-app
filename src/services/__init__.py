"""
src/services — the application's service layer.

Layering rule enforced across this package:

    routes/controllers  →  orchestrators  →  repositories  →  SQLite
                                        ↘  llm providers  →  Ollama

A controller may import from `orchestrators`. It may NOT import from `llm`,
`prompts`, or `requests`. If you find yourself needing to, the logic belongs in
an orchestrator instead.
"""

from .orchestrators import (
    AssignmentOrchestrator,
    AssistantOrchestrator,
    DVIOrchestrator,
    EstimateOrchestrator,
    InventoryIntelligenceOrchestrator,
    OrchestratorError,
)

__all__ = [
    "AssignmentOrchestrator",
    "AssistantOrchestrator",
    "DVIOrchestrator",
    "EstimateOrchestrator",
    "InventoryIntelligenceOrchestrator",
    "OrchestratorError",
]
