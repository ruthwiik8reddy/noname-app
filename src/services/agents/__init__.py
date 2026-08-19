"""
src/services/agents — autonomous agents.

An orchestrator answers when asked. An agent goes looking.

    routes → AgentRunner → Agent.collect() → orchestrators → repositories
                        ↘ AgentRepository (runs + findings)

Agents never call an LLM directly; they go through an orchestrator, same as
everything else.
"""

from .base import SEVERITIES, AgentResult, BaseAgent, Finding
from .diagnosis_agent import DiagnosisAgent, VehicleDiagnosisOrchestrator
from .estimate_agent import EstimateAgent
from .inventory_agent import InventoryAgent
from .leads_agent import LeadsAgent
from .runtime import REGISTRY, AgentRunner, TriggerBus, available_agents, start_scheduler

__all__ = [
    "REGISTRY", "SEVERITIES", "AgentResult", "AgentRunner", "BaseAgent",
    "DiagnosisAgent", "EstimateAgent", "Finding", "InventoryAgent", "LeadsAgent",
    "TriggerBus", "VehicleDiagnosisOrchestrator", "available_agents", "start_scheduler",
]
