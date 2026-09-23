"""
src/routes — Blueprint modules.

Each blueprint is registered in `src/app.py`. Handlers here are intentionally
thin: session in, orchestrator or agent call, response out.
"""

from . import approval_routes, followup_routes, job_record_routes, intelligence_routes, agent_routes, analytics_routes, dispatch_routes, dvi_routes, lead_routes

BLUEPRINTS = (
    approval_routes.bp,
    followup_routes.bp,
    job_record_routes.bp,
    intelligence_routes.bp,
    analytics_routes.bp,
    dvi_routes.bp,
    dispatch_routes.bp,
    agent_routes.bp,
    lead_routes.bp,
)

__all__ = ["BLUEPRINTS", "agent_routes", "analytics_routes", "dispatch_routes",
           "dvi_routes", "lead_routes"]
