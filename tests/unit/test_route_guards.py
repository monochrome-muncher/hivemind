"""Admin and portal routes carry their key guard as a dependency, so a request
is refused (403) before its body is validated (422), and a new route cannot forget it."""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from hivemind.api.deps import create_app
from hivemind.api.routes.params import require_admin_key, require_agent_key
from tests.fakes import make_client
from tests.unit.test_api_endpoints import make_hivemind_app


def _calls(dependant: Dependant) -> set[object]:
    calls: set[object] = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _calls(sub)
    return calls


def _paths_guarded_by(prefix: str, guard: object) -> tuple[list[str], list[str]]:
    app = create_app(make_hivemind_app())
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(prefix)]
    return [r.path for r in routes], [r.path for r in routes if guard not in _calls(r.dependant)]


def test_every_admin_route_requires_an_admin_key() -> None:
    paths, unguarded = _paths_guarded_by("/v1/admin/", require_admin_key)
    assert paths
    assert unguarded == []


def test_the_metrics_route_requires_an_admin_key() -> None:
    _, unguarded = _paths_guarded_by("/v1/metrics", require_admin_key)
    assert unguarded == []


def test_every_portal_route_requires_an_agent_key() -> None:
    paths, unguarded = _paths_guarded_by("/v1/portal/", require_agent_key)
    assert paths
    assert unguarded == []


async def test_a_non_admin_key_with_a_malformed_body_is_403_not_422() -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post(
            "/v1/admin/agents", json={"nonsense": 1}, headers={"X-API-Key": "key-alice"}
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_a_missing_key_is_401_before_the_guard_and_the_body() -> None:
    async with make_client(make_hivemind_app()) as client:
        resp = await client.post("/v1/admin/agents", json={"nonsense": 1})
    assert resp.status_code == 401
