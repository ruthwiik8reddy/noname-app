"""
src/routes — Blueprint modules for the Phase 1-3 feature set.

Each blueprint is registered in `src/app.py`. Handlers here are intentionally
thin: session in, orchestrator call, response out.
"""

from . import analytics_routes, dispatch_routes, dvi_routes

BLUEPRINTS = (
    analytics_routes.bp,
    dvi_routes.bp,
    dispatch_routes.bp,
)

__all__ = ["BLUEPRINTS", "analytics_routes", "dispatch_routes", "dvi_routes"]
