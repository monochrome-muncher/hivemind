"""Request filter parity: what each entry point hands the service as
``EntryFilters`` (SPEC §5.3).

Every surface builds its ``EntryFilters`` by hand from request input. This
sends each entry point a request with every filter it accepts set to a
distinct value, captures the filters at the service seam, and compares the
set fields with an explicit table. Each surface lists the fields it accepts
and the fields it deliberately does not; the two must cover every field of
``EntryFilters``, so a new field fails here until each surface declares it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from hivemind.domain.entry import EntryFilters, ImportanceSource, Kind
from hivemind.domain.recency import RecencyHint
from hivemind.mcp.app import McpHivemind, hive_list, hive_search
from hivemind.ports import Credential
from tests.fakes import make_search_config
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

ALL = frozenset(f.name for f in fields(EntryFilters))

# Fields no request can set on any surface: ``importance_source`` is internal
# (MetricsService) and ``reporter`` is filled by the read service from the
# credential.
NEVER = frozenset({"importance_source", "reporter"})

BASE = frozenset(
    {
        "kind",
        "tags",
        "entities",
        "scope",
        "author",
        "agent",
        "occurred_from",
        "occurred_to",
        "created_from",
        "created_to",
        "include_inactive",
        "flagged",
    }
)

# surface -> (accepted, deliberately not accepted)
SURFACES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "POST /v1/search": (BASE, ALL - BASE),
    "POST /v1/admin/search": (BASE | {"fleet_id"}, ALL - BASE - {"fleet_id"}),
    "POST /v1/portal/search": (BASE | {"fleet_id"}, ALL - BASE - {"fleet_id"}),
    "GET /v1/entries": (BASE | {"relied_on_changed"}, ALL - BASE - {"relied_on_changed"}),
    "GET /v1/admin/entries": (BASE | {"fleet_id"}, ALL - BASE - {"fleet_id"}),
    # The portal list is narrower than the others: no entities, agent or date ranges.
    "GET /v1/portal/entries": (
        {"kind", "tags", "scope", "fleet_id", "author", "include_inactive", "flagged"},
        ALL - {"kind", "tags", "scope", "fleet_id", "author", "include_inactive", "flagged"},
    ),
    "hive_search": (BASE, ALL - BASE),
    "hive_list": (BASE | {"relied_on_changed"}, ALL - BASE - {"relied_on_changed"}),
}

DT = {
    "occurred_from": datetime(2024, 1, 1, tzinfo=UTC),
    "occurred_to": datetime(2024, 2, 1, tzinfo=UTC),
    "created_from": datetime(2024, 3, 1, tzinfo=UTC),
    "created_to": datetime(2024, 4, 1, tzinfo=UTC),
}
# One distinct, non-default value per request-settable field.
VALUES: dict[str, Any] = {
    "kind": Kind.DECISION,
    "tags": ("t1", "t2"),
    "entities": ("e1", "e2"),
    "scope": "fleet",
    "fleet_id": "fleet-x",
    "author": "the-author",
    "agent": "the-agent",
    **DT,
    "include_inactive": True,
    "flagged": True,
    "relied_on_changed": True,
}


class _Spy:
    """Stands in for ``SearchService`` / ``ReadService`` and keeps the filters."""

    def __init__(self) -> None:
        self.filters: list[EntryFilters] = []

    async def search_result(self, query: str, filters: EntryFilters, *a: Any, **k: Any) -> Any:
        self.filters.append(filters)
        return type("R", (), {"hits": [], "degraded": False, "recency": RecencyHint.DEFAULT})()

    async def list(self, filters: EntryFilters, *a: Any, **k: Any) -> list[Any]:
        self.filters.append(filters)
        return []


def _wire(name: str, accepted: frozenset[str]) -> dict[str, Any]:
    """The request parameters for ``accepted``: JSON values for REST
    (``datetime`` as ISO text), plain values for the MCP tools."""
    out: dict[str, Any] = {}
    for f in accepted:
        v = VALUES[f]
        if isinstance(v, Kind):
            v = v.value
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, tuple):
            v = list(v)
        out[f] = v
    return out


def _captured(spy: _Spy) -> EntryFilters:
    assert len(spy.filters) == 1
    return spy.filters[0]


async def _rest(method: str, path: str, params: dict[str, Any], key: str) -> EntryFilters:
    spy = _Spy()
    app = replace(make_hivemind_app(), search_service=spy, read_service=spy)
    app.authenticator.add_key(  # type: ignore[attr-defined]
        "key-portal",
        Credential(user_id="p", agent_name="p", access_controlled=True),
    )
    async with make_client(app) as client:
        headers = {"X-API-Key": key}
        if method == "POST":
            resp = await client.post(path, json={"query": "q", **params}, headers=headers)
        else:
            resp = await client.get(path, params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return _captured(spy)


def _mcp_app(spy: _Spy) -> McpHivemind:
    return McpHivemind(
        read_service=spy,  # type: ignore[arg-type]
        store=None,  # type: ignore[arg-type]
        write_service=None,  # type: ignore[arg-type]
        search_service=spy,  # type: ignore[arg-type]
        governance_service=None,  # type: ignore[arg-type]
        access_service=None,  # type: ignore[arg-type]
        search_config=make_search_config(),
        credential=Credential(user_id="alice", agent_id="agent-1"),
    )


async def _mcp(tool: Callable[..., Awaitable[Any]], params: dict[str, Any]) -> EntryFilters:
    spy = _Spy()
    extra = {"query": "q"} if tool is hive_search else {}
    result = await tool(_mcp_app(spy), **extra, **params)
    assert "error" not in result, result
    return _captured(spy)


async def _run(surface: str, params: dict[str, Any]) -> EntryFilters:
    if surface == "hive_search":
        return await _mcp(hive_search, params)
    if surface == "hive_list":
        return await _mcp(hive_list, params)
    method, path = surface.split(" ")
    key = {"/v1/portal/search": "key-portal", "/v1/portal/entries": "key-portal"}.get(
        path, "key-admin"
    )
    return await _rest(method, path, params, key)


def test_every_surface_declares_every_field() -> None:
    for surface, (accepted, refused) in SURFACES.items():
        assert not accepted & refused, surface
        assert accepted | refused == ALL, (
            f"{surface}: undeclared EntryFilters fields {sorted(ALL - accepted - refused)}"
        )
        assert refused >= NEVER, surface
        assert accepted <= set(VALUES), surface


@pytest.mark.parametrize("surface", list(SURFACES))
async def test_surface_passes_exactly_the_filters_it_accepts(surface: str) -> None:
    accepted, refused = SURFACES[surface]
    got = await _run(surface, _wire(surface, accepted))
    default = EntryFilters()
    for f in ALL:
        if f in accepted:
            assert getattr(got, f) == VALUES[f], f"{surface} dropped or altered {f}"
        else:
            assert getattr(got, f) == getattr(default, f), f"{surface} set {f}"
    assert not {f for f in refused if getattr(got, f) != getattr(default, f)}
    assert got.importance_source is None and ImportanceSource  # internal, never from a request
