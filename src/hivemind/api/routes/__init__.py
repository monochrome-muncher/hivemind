"""The REST routes (SPEC §5.1): thin, validation and service calls only; business rules live in
the services.

Typed service errors propagate to the table in ``errors.py``; a route catches one only where its
answer differs (``create_entry``: ``invalid_entry``).
"""

from __future__ import annotations

from functools import cache

from fastapi import APIRouter

from hivemind.api.routes import (
    admin_agents,
    admin_audit,
    admin_cleanups,
    admin_entries,
    admin_fleets,
    agents,
    entries,
    feedback,
    health,
    pins,
    portal,
)
from hivemind.api.routes.params import API_PREFIX

_AREAS_IN_ROUTE_ORDER = (
    health,
    entries,
    pins,
    feedback,
    agents,
    portal,
    admin_entries,
    admin_fleets,
    admin_cleanups,
    admin_agents,
    admin_audit,
)


@cache
def build_router() -> APIRouter:
    """The /v1 router (SPEC §5.1), built once per process: it holds no app state, and building it is
    the costly part of ``create_app`` (tests build hundreds of apps)."""
    router = APIRouter(prefix=API_PREFIX)
    for area in _AREAS_IN_ROUTE_ORDER:
        # Route objects, not include_router, which would nest them lazily.
        router.routes.extend(area.router.routes)
    return router
