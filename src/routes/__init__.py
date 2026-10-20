"""
src/routes — Blueprint modules.

Each blueprint is registered in `src/app.py`. Handlers here are intentionally
thin: session in, orchestrator or agent call, response out.
"""

from . import (agent_routes, analytics_routes, assistant_routes, dispatch_routes, dvi_routes,
               invoice_routes, lead_routes)

BLUEPRINTS = (
    analytics_routes.bp,
    dvi_routes.bp,
    dispatch_routes.bp,
    agent_routes.bp,
    lead_routes.bp,
    invoice_routes.bp,
    assistant_routes.bp,
)

__all__ = ["BLUEPRINTS", "agent_routes", "analytics_routes", "assistant_routes", "dispatch_routes",
           "dvi_routes", "invoice_routes", "lead_routes"]
