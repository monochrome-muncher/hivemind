"""The Hivemind REST routes (SPEC.md §5.1).

Routes stay thin: pydantic validation and service calls. No business
rules live here — those are in the service layer (SPEC.md §5, ADR 0001).
Typed service errors propagate to the handlers ``create_app`` registers from
the table in ``errors.py``; a route catches one only where its answer differs
(``create_entry`` answers a malformed draft as ``invalid_entry``).

Error codes: ``missing_api_key``/``unknown_api_key`` (401),
``forbidden`` (403), ``not_found`` (404), ``conflict`` (409),
``embedding_unavailable`` (502), and ``invalid_entry``/
``agent_identity_required`` (422).
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
    """The /v1 router (SPEC.md §5.1), built once per process and shared by
    every app: it holds no app state, and the handlers take the
    ``HivemindApp`` and the Prometheus metrics from the request. Building it
    is the costly part of ``create_app``, and the tests build hundreds of
    apps."""
    router = APIRouter(prefix=API_PREFIX)
    for area in _AREAS_IN_ROUTE_ORDER:
        # The area's route objects themselves, not include_router: ``create_app``
        # hands them to every app, and an include would nest them lazily.
        router.routes.extend(area.router.routes)
    return router
