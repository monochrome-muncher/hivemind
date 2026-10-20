"""Characterization: every service failure, as each surface reports it.

Services signal failures with exception types; REST maps them to a status
and an error code, MCP to an error code. This pins the wire result of each
path (status, code and message) on both surfaces, so refactoring the
exception types or the mapping cannot change what a client sees.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp import app as mcp
from hivemind.mcp.app import McpHivemind
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

NO_SUCH = "00000000-0000-4000-8000-00000000dead"


def _agent(name: str, level: TrustLevel, fleet: str = "fleet-a") -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


CREDENTIALS = {
    "k-lead": _agent("lead", TrustLevel.PRIVILEGED),
    "k-dev": _agent("dev", TrustLevel.CONTRIBUTOR),
    "k-lurker": _agent("lurker", TrustLevel.LURKER),
    "k-user": Credential(user_id="legacy"),  # a legacy key with no agent identity
}


@dataclass
class World:
    client: httpx.AsyncClient
    store: MemoryStore
    authenticator: FakeAuthenticator
    fleet_entry: str  # an active fleet entry written by dev
    org_entry: str  # an active org entry written by the legacy key
    withdrawn: str  # a withdrawn fleet entry written by dev
    fleet_id: str  # the id of the existing fleet "alpha"
    app: FastAPI  # the REST app, for the services it holds

    def mcp(self, key: str) -> McpHivemind:
        embedder, config = make_embedder(), make_search_config()
        return McpHivemind(
            store=self.store,
            write_service=WriteService(self.store, embedder),
            search_service=SearchService(self.store, embedder, config),
            governance_service=GovernanceService(self.store, config),
            read_service=ReadService(self.store, GovernanceService(self.store, config), config),
            access_service=AccessService(self.store, self.authenticator),
            search_config=config,
            credential=self.authenticator._by_key[key],
        )


@pytest.fixture
async def world() -> World:
    store = MemoryStore(make_clock())
    authenticator = FakeAuthenticator(agent_credentials=dict(CREDENTIALS))
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=authenticator,
        search_config=make_search_config(),
    )
    fastapi_app = create_app(app)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://t")

    async def post(key: str, **body: Any) -> str:
        resp = await client.post(
            "/v1/entries", json={"kind": "fact", **body}, headers={"X-API-Key": key}
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    fleet_entry = await post("k-dev", summary="fleet fact")
    org_entry = await post("k-user", summary="org fact", agent="cli")
    withdrawn = await post("k-dev", summary="gone")
    resp = await client.post(
        f"/v1/entries/{withdrawn}/withdraw", json={}, headers={"X-API-Key": "k-dev"}
    )
    assert resp.status_code == 200, resp.text
    await store.register_agent("taken", "first-owner")
    fleet = await store.create_fleet("alpha")
    return World(
        client, store, authenticator, fleet_entry, org_entry, withdrawn, fleet.id, fastapi_app
    )


def _err(resp: httpx.Response) -> tuple[int, str, str]:
    body = resp.json()["error"]
    return resp.status_code, body["code"], body["message"]


def _mcp_err(result: dict[str, Any]) -> tuple[str, str]:
    return result["error"]["code"], result["error"]["message"]


Rest = Callable[[World], Awaitable[httpx.Response]]
Mcp = Callable[[World], Awaitable[dict[str, Any]]]


def _h(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


# (name, REST call, expected REST (status, code, message), MCP call, expected MCP (code, message))
# A message may contain "{fleet_entry}"-style placeholders, filled from the World.
CASES: list[tuple[str, Rest, tuple[int, str, str], Mcp, tuple[str, str]]] = [
    (
        "withdraw unknown",
        lambda w: w.client.post(f"/v1/entries/{NO_SUCH}/withdraw", json={}, headers=_h("k-dev")),
        (404, "not_found", f"unknown entry: {NO_SUCH}"),
        lambda w: mcp.hive_withdraw(w.mcp("k-dev"), NO_SUCH),
        ("not_found", f"unknown entry: {NO_SUCH}"),
    ),
    (
        "withdraw by a non-author",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/withdraw", json={}, headers=_h("k-lead")
        ),
        (403, "forbidden", "only the author or an admin may withdraw an entry"),
        lambda w: mcp.hive_withdraw(w.mcp("k-lead"), w.fleet_entry),
        ("permission_denied", "only the author or an admin may withdraw an entry"),
    ),
    (
        "withdraw twice",
        lambda w: w.client.post(
            f"/v1/entries/{w.withdrawn}/withdraw", json={}, headers=_h("k-dev")
        ),
        (
            409,
            "conflict",
            "entry {withdrawn} is already withdrawn; only active entries can be withdrawn",
        ),
        lambda w: mcp.hive_withdraw(w.mcp("k-dev"), w.withdrawn),
        (
            "not_active",
            "entry {withdrawn} is already withdrawn; only active entries can be withdrawn",
        ),
    ),
    (
        "withdraw with an oversized reason",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/withdraw",
            json={"reason": "x" * 2001},
            headers=_h("k-dev"),
        ),
        None,  # the REST schema rejects it before the service: not this test's subject
        lambda w: mcp.hive_withdraw(w.mcp("k-dev"), w.fleet_entry, reason="x" * 2001),
        ("invalid_input", "reason must be at most 2000 characters (got 2001)"),
    ),
    (
        "feedback on an unknown entry",
        lambda w: w.client.post(
            f"/v1/entries/{NO_SUCH}/feedback", json={"verdict": "helpful"}, headers=_h("k-dev")
        ),
        (404, "not_found", f"unknown entry: {NO_SUCH}"),
        lambda w: mcp.hive_feedback(w.mcp("k-dev"), NO_SUCH, "helpful"),
        ("not_found", f"unknown entry: {NO_SUCH}"),
    ),
    (
        "feedback with no agent identity",
        lambda w: w.client.post(
            f"/v1/entries/{w.org_entry}/feedback",
            json={"verdict": "helpful"},
            headers=_h("k-user"),
        ),
        (
            422,
            "agent_identity_required",
            "the caller's agent identity must be resolved before recording feedback (SPEC.md §8.1)",
        ),
        lambda w: mcp.hive_feedback(w.mcp("k-user"), w.org_entry, "helpful"),
        (
            "agent_unresolved",
            "the caller's agent identity must be resolved before recording feedback",
        ),
    ),
    (
        "list relied_on_changed with no agent identity",
        lambda w: w.client.get(
            "/v1/entries", params={"relied_on_changed": "true"}, headers=_h("k-user")
        ),
        (
            422,
            "agent_identity_required",
            "relied_on_changed needs the caller's agent identity (an agent or admin key)",
        ),
        lambda w: mcp.hive_list(w.mcp("k-user"), relied_on_changed=True),
        (
            "agent_unresolved",
            "relied_on_changed needs the caller's agent identity (an agent or admin key)",
        ),
    ),
    (
        "batch feedback naming an unknown entry",
        lambda w: w.client.post(
            "/v1/feedback",
            json={"entry_ids": [w.fleet_entry, NO_SUCH], "verdict": "helpful"},
            headers=_h("k-dev"),
        ),
        (404, "not_found", f"unknown entries: {NO_SUCH}"),
        lambda w: mcp.hive_feedback(
            w.mcp("k-dev"), verdict="helpful", entry_ids=[w.fleet_entry, NO_SUCH]
        ),
        ("not_found", f"unknown entries: {NO_SUCH}"),
    ),
    (
        "feedback on the caller's own entry",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/feedback",
            json={"verdict": "helpful"},
            headers=_h("k-dev"),
        ),
        (
            403,
            "forbidden",
            "you cannot rate your own entry ({fleet_entry}); "
            "supersede or withdraw it instead (ADR 0065)",
        ),
        lambda w: mcp.hive_feedback(w.mcp("k-dev"), w.fleet_entry, "helpful"),
        (
            "permission_denied",
            "you cannot rate your own entry ({fleet_entry}); "
            "supersede or withdraw it instead (ADR 0065)",
        ),
    ),
    (
        "batch feedback naming the caller's own entry",
        lambda w: w.client.post(
            "/v1/feedback",
            json={"entry_ids": [w.org_entry, w.fleet_entry], "verdict": "helpful"},
            headers=_h("k-dev"),
        ),
        (
            403,
            "forbidden",
            "you cannot rate your own entry ({fleet_entry}); "
            "supersede or withdraw it instead (ADR 0065)",
        ),
        lambda w: mcp.hive_feedback(
            w.mcp("k-dev"), verdict="helpful", entry_ids=[w.org_entry, w.fleet_entry]
        ),
        (
            "permission_denied",
            "you cannot rate your own entry ({fleet_entry}); "
            "supersede or withdraw it instead (ADR 0065)",
        ),
    ),
    (
        "pin an org entry",
        lambda w: w.client.put(f"/v1/entries/{w.org_entry}/pin", headers=_h("k-lead")),
        (422, "invalid_input", "only fleet entries can be pinned, to their own fleet"),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), w.org_entry),
        ("invalid_input", "only fleet entries can be pinned, to their own fleet"),
    ),
    (
        "pin by a lurker",
        lambda w: w.client.put(f"/v1/entries/{w.fleet_entry}/pin", headers=_h("k-lurker")),
        (
            403,
            "forbidden",
            "only a contributor or privileged agent of the entry's fleet, "
            "or an admin, may pin or unpin",
        ),
        lambda w: mcp.hive_pin(w.mcp("k-lurker"), w.fleet_entry),
        (
            "permission_denied",
            "only a contributor or privileged agent of the entry's fleet, "
            "or an admin, may pin or unpin",
        ),
    ),
    (
        "pin a withdrawn entry",
        lambda w: w.client.put(f"/v1/entries/{w.withdrawn}/pin", headers=_h("k-lead")),
        (409, "conflict", "entry {withdrawn} is withdrawn; only active entries can be pinned"),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), w.withdrawn),
        ("not_active", "entry {withdrawn} is withdrawn; only active entries can be pinned"),
    ),
    (
        "unpin an unknown entry",
        lambda w: w.client.delete(f"/v1/entries/{NO_SUCH}/pin", headers=_h("k-lead")),
        (404, "not_found", f"unknown entry: {NO_SUCH}"),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), NO_SUCH, unpin=True),
        ("not_found", f"unknown entry: {NO_SUCH}"),
    ),
    (
        "supersede an unknown entry",
        lambda w: w.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "v2", "supersedes": [NO_SUCH]},
            headers=_h("k-dev"),
        ),
        (
            403,
            "supersede_denied",
            f"not found or not supersedable by you: {NO_SUCH} (you may supersede active "
            "entries you can read, with a successor that reaches at least the same "
            "audience — ADR 0033; only the current head of a chain is supersedable — "
            "ADR 0034)",
        ),
        lambda w: mcp.hive_write(w.mcp("k-dev"), "fact", "v2", supersedes=[NO_SUCH]),
        (
            "supersede_denied",
            f"not found or not supersedable by you: {NO_SUCH} (you may supersede active "
            "entries you can read, with a successor that reaches at least the same "
            "audience — ADR 0033; only the current head of a chain is supersedable — "
            "ADR 0034)",
        ),
    ),
    (
        "write above the trust level",
        lambda w: w.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s", "scope": "fleet"},
            headers=_h("k-lurker"),
        ),
        (403, "forbidden", "trust level 1 may not write scope 'fleet' (ADR 0011)"),
        lambda w: mcp.hive_write(w.mcp("k-lurker"), "fact", "s", scope="fleet"),
        ("permission_denied", "trust level 1 may not write scope 'fleet' (ADR 0011)"),
    ),
    (
        "write with an unknown see_also",
        lambda w: w.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "s", "see_also": [NO_SUCH]},
            headers=_h("k-dev"),
        ),
        (422, "invalid_entry", f"unknown see_also entries: {NO_SUCH}"),
        lambda w: mcp.hive_write(w.mcp("k-dev"), "fact", "s", see_also=[NO_SUCH]),
        ("invalid_input", f"unknown see_also entries: {NO_SUCH}"),
    ),
    (
        "register a taken name",
        lambda w: w.client.post(
            "/v1/agents",
            json={"name": "taken", "owner_alias": "someone"},
            headers=_h("hm_org"),
        ),
        (
            409,
            "name_conflict",
            "agent name is already taken (names are never reused; pick another name)",
        ),
        lambda w: mcp.hive_register(w.mcp("hm_org"), "taken", owner_alias="someone"),
        (
            "name_conflict",
            "agent name is already taken (names are never reused; pick another name)",
        ),
    ),
    (
        "register with an agent key",
        lambda w: w.client.post("/v1/agents", json={"name": "new"}, headers=_h("k-dev")),
        (403, "forbidden", "registration requires an org or admin key (ADR 0012)"),
        lambda w: mcp.hive_register(w.mcp("hm_admin"), "new"),
        ("permission_denied", "hive_register is gated on the org key (SPEC §5.2)"),
    ),
    (
        "activate an unknown agent",
        lambda w: w.client.post(
            "/v1/admin/agents/ghost/activate",
            json={"trust_level": 2, "home_fleet_id": NO_SUCH},
            headers=_h("hm_admin"),
        ),
        (404, "not_found", f"'unknown fleet: {NO_SUCH}'"),
        None,
        None,
    ),
    (
        "create a fleet that exists",
        lambda w: w.client.post("/v1/admin/fleets", json={"name": "alpha"}, headers=_h("hm_admin")),
        (409, "fleet_exists", "fleet already exists: 'alpha'"),
        None,
        None,
    ),
    (
        "revoke an unknown agent",
        lambda w: w.client.post("/v1/admin/agents/ghost/revoke", headers=_h("hm_admin")),
        (404, "not_found", "'unknown agent: ghost'"),
        None,
        None,
    ),
    (
        "update an unknown agent in an existing fleet",
        lambda w: w.client.patch(
            "/v1/admin/agents/ghost", json={"home_fleet_id": w.fleet_id}, headers=_h("hm_admin")
        ),
        (404, "not_found", "'unknown agent: ghost'"),
        None,
        None,
    ),
    (
        "change the trust level of an unknown agent",
        lambda w: w.client.patch(
            "/v1/admin/agents/ghost", json={"trust_level": 2}, headers=_h("hm_admin")
        ),
        (404, "not_found", "'unknown agent: ghost'"),
        None,
        None,
    ),
    (
        "update an agent into an unknown fleet",
        lambda w: w.client.patch(
            "/v1/admin/agents/taken", json={"home_fleet_id": NO_SUCH}, headers=_h("hm_admin")
        ),
        (404, "not_found", f"'unknown fleet: {NO_SUCH}'"),
        None,
        None,
    ),
    (
        "activate an unknown agent in an existing fleet",
        lambda w: w.client.post(
            "/v1/admin/agents/ghost/activate",
            json={"trust_level": 2, "home_fleet_id": w.fleet_id},
            headers=_h("hm_admin"),
        ),
        (404, "not_found", "'unknown agent: ghost'"),
        None,
        None,
    ),
    (
        "register a reserved name",
        lambda w: w.client.post("/v1/agents", json={"name": "admin"}, headers=_h("hm_org")),
        (
            422,
            "invalid_input",
            "agent name 'admin' is reserved for a built-in identity (ADR 0033); pick another",
        ),
        lambda w: mcp.hive_register(w.mcp("hm_org"), "admin"),
        (
            "invalid_input",
            "agent name 'admin' is reserved for a built-in identity (ADR 0033); pick another",
        ),
    ),
    (
        "feedback with an oversized note",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/feedback",
            json={"verdict": "helpful", "note": "x" * 2001},
            headers=_h("k-lead"),
        ),
        None,  # the REST schema may reject it before the service: not this test's subject
        lambda w: mcp.hive_feedback(w.mcp("k-lead"), w.fleet_entry, "helpful", note="x" * 2001),
        ("invalid_input", "note must be at most 2000 characters (got 2001)"),
    ),
]


@pytest.mark.parametrize(
    ("rest", "rest_expected", "tool", "tool_expected"),
    [pytest.param(*case[1:], id=case[0]) for case in CASES],
)
async def test_each_failure_reads_the_same_on_the_wire(
    world: World,
    rest: Rest,
    rest_expected: tuple[int, str, str] | None,
    tool: Mcp | None,
    tool_expected: tuple[str, str] | None,
) -> None:
    fill = {"withdrawn": world.withdrawn, "fleet_entry": world.fleet_entry}
    if rest_expected is not None:
        status, code, message = rest_expected
        assert _err(await rest(world)) == (status, code, message.format(**fill))
    if tool is not None and tool_expected is not None:
        code, message = tool_expected
        assert _mcp_err(await tool(world)) == (code, message.format(**fill))


# The agent portal's withdraw (ADR 0095) has no MCP twin: REST only.
PORTAL_WITHDRAW_CASES: list[tuple[str, str, str, dict[str, str], tuple[int, str, str]]] = [
    (
        "unknown",
        "k-dev",
        NO_SUCH,
        {"reason": "old"},
        (404, "not_found", f"unknown entry: {NO_SUCH}"),
    ),
    (
        "non-author",
        "k-lead",
        "{fleet_entry}",
        {"reason": "old"},
        (403, "forbidden", "only the author may withdraw an entry from the agent portal"),
    ),
    (
        "twice",
        "k-dev",
        "{withdrawn}",
        {"reason": "old"},
        (
            409,
            "conflict",
            "entry {withdrawn} is already withdrawn; only active entries can be withdrawn",
        ),
    ),
    (
        "blank reason",
        "k-dev",
        NO_SUCH,
        {"reason": " "},
        (422, "invalid_input", "reason is required"),
    ),
    (
        "legacy key",
        "k-user",
        "{fleet_entry}",
        {"reason": "old"},
        (
            403,
            "forbidden",
            "the agent portal requires an agent key, not the admin or org key",
        ),
    ),
]


@pytest.mark.parametrize(
    ("key", "target", "body", "expected"),
    [pytest.param(*case[1:], id=case[0]) for case in PORTAL_WITHDRAW_CASES],
)
async def test_the_portal_withdraw_reads_its_failures_on_the_wire(
    world: World, key: str, target: str, body: dict[str, str], expected: tuple[int, str, str]
) -> None:
    fill = {"withdrawn": world.withdrawn, "fleet_entry": world.fleet_entry}
    resp = await world.client.post(
        f"/v1/portal/entries/{target.format(**fill)}/withdraw", json=body, headers=_h(key)
    )
    status, code, message = expected
    assert _err(resp) == (status, code, message.format(**fill))


def test_typed_errors_still_match_the_builtins_surfaces_catch() -> None:
    from hivemind.services import errors, governance

    assert issubclass(errors.EntryNotFound, LookupError)
    assert issubclass(errors.EntryNotActive, ValueError)
    assert issubclass(errors.AgentIdentityRequired, ValueError)
    assert issubclass(errors.SupersedeDenied, errors.PermissionDenied)
    # The old import path names the very same classes.
    assert governance.PermissionDenied is errors.PermissionDenied
    assert governance.SupersedeDenied is errors.SupersedeDenied
    # NameTaken moved here; the old import path names the same class.
    from hivemind.domain import access as domain_access
    from hivemind.services import access as services_access

    assert issubclass(errors.NameTaken, ValueError)
    assert services_access.NameTaken is errors.NameTaken
    assert issubclass(domain_access.UnknownAgent, KeyError)
    assert issubclass(domain_access.UnknownFleet, KeyError)
    assert issubclass(domain_access.FleetExists, ValueError)
    # KeyError keeps the quoted str() the REST 404 messages rely on.
    assert str(domain_access.UnknownFleet("unknown fleet: X")) == "'unknown fleet: X'"


# (REST call, MCP call, the service object's attribute path, the method it raises from).
# Each call reaches the service method only after every pre-check has passed.
UNEXPECTED: list[tuple[str, Rest, Mcp | None, str, str]] = [
    (
        "withdraw",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/withdraw", json={}, headers=_h("k-dev")
        ),
        lambda w: mcp.hive_withdraw(w.mcp("k-dev"), w.fleet_entry),
        "governance_service",
        "withdraw",
    ),
    (
        "pin",
        lambda w: w.client.put(f"/v1/entries/{w.fleet_entry}/pin", headers=_h("k-lead")),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), w.fleet_entry),
        "governance_service",
        "pin",
    ),
    (
        "unpin",
        lambda w: w.client.delete(f"/v1/entries/{w.fleet_entry}/pin", headers=_h("k-lead")),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), w.fleet_entry, unpin=True),
        "governance_service",
        "unpin",
    ),
    (
        "feedback",
        lambda w: w.client.post(
            f"/v1/entries/{w.fleet_entry}/feedback",
            json={"verdict": "helpful"},
            headers=_h("k-dev"),
        ),
        lambda w: mcp.hive_feedback(w.mcp("k-dev"), w.fleet_entry, "helpful"),
        "governance_service",
        "record_feedback",
    ),
    (
        "batch feedback",
        lambda w: w.client.post(
            "/v1/feedback",
            json={"entry_ids": [w.fleet_entry], "verdict": "helpful"},
            headers=_h("k-dev"),
        ),
        lambda w: mcp.hive_feedback(w.mcp("k-dev"), verdict="helpful", entry_ids=[w.fleet_entry]),
        "governance_service",
        "record_feedback_many",
    ),
    (
        "write",
        lambda w: w.client.post(
            "/v1/entries", json={"kind": "fact", "summary": "s"}, headers=_h("k-dev")
        ),
        lambda w: mcp.hive_write(w.mcp("k-dev"), "fact", "s"),
        "write_service",
        "write_and_relate",
    ),
    (
        "register",
        lambda w: w.client.post("/v1/agents", json={"name": "fresh"}, headers=_h("hm_org")),
        lambda w: mcp.hive_register(w.mcp("hm_org"), "fresh"),
        "access_service",
        "register",
    ),
    (
        "create fleet",
        lambda w: w.client.post("/v1/admin/fleets", json={"name": "beta"}, headers=_h("hm_admin")),
        None,
        "access_service",
        "create_fleet",
    ),
    (
        "activate",
        lambda w: w.client.post(
            "/v1/admin/agents/taken/activate",
            json={"trust_level": 2, "home_fleet_id": w.fleet_id},
            headers=_h("hm_admin"),
        ),
        None,
        "access_service",
        "activate",
    ),
    (
        "update home fleet",
        lambda w: w.client.patch(
            "/v1/admin/agents/taken", json={"home_fleet_id": w.fleet_id}, headers=_h("hm_admin")
        ),
        None,
        "access_service",
        "set_home_fleet",
    ),
    (
        "update trust level",
        lambda w: w.client.patch(
            "/v1/admin/agents/taken", json={"trust_level": 2}, headers=_h("hm_admin")
        ),
        None,
        "access_service",
        "set_trust_level",
    ),
    (
        "revoke",
        lambda w: w.client.post("/v1/admin/agents/taken/revoke", headers=_h("hm_admin")),
        None,
        "access_service",
        "revoke",
    ),
]


@pytest.mark.parametrize(
    "exc", [ValueError("boom"), LookupError("boom"), KeyError("boom")], ids=str
)
@pytest.mark.parametrize(
    ("rest", "tool", "service", "method"),
    [pytest.param(*case[1:], id=case[0]) for case in UNEXPECTED],
)
async def test_an_unexpected_builtin_error_is_never_disguised_as_a_friendly_one(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    rest: Rest,
    tool: Mcp | None,
    service: str,
    method: str,
    exc: Exception,
) -> None:
    """The policy: surfaces catch the typed errors only. A bare builtin from a
    service is a bug, so REST answers 500 and the MCP tool raises."""

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise exc

    # Patch the service class: the REST app and World.mcp() each hold their own instance.
    monkeypatch.setattr(type(getattr(world.app.state.hivemind, service)), method, boom)
    # raise_app_exceptions=False turns the unhandled error into the 500 a server sends.
    world.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.app, raise_app_exceptions=False),
        base_url="http://t",
    )
    assert (await rest(world)).status_code == 500
    if tool is not None:
        with pytest.raises(type(exc), match="boom"):
            await tool(world)


_RACE_NOT_ACTIVE = "entry {id} is withdrawn, not active"


async def test_a_store_race_on_withdraw_becomes_the_typed_errors(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store can still refuse after the service's pre-checks (a concurrent
    withdraw): its KeyError / ValueError become EntryNotFound / EntryNotActive,
    messages preserved."""
    from hivemind.services.errors import EntryNotActive, EntryNotFound

    service = GovernanceService(world.store, make_search_config())
    credential = world.authenticator._by_key["k-dev"]
    text = _RACE_NOT_ACTIVE.format(id=world.fleet_entry)

    async def not_active(*_a: Any, **_k: Any) -> None:
        raise ValueError(text)

    monkeypatch.setattr(world.store, "withdraw_entry", not_active)
    with pytest.raises(EntryNotActive) as active:
        await service.withdraw(credential, world.fleet_entry)
    assert str(active.value) == text

    async def unknown(*_a: Any, **_k: Any) -> None:
        raise KeyError("unknown entry: gone")

    monkeypatch.setattr(world.store, "withdraw_entry", unknown)
    with pytest.raises(EntryNotFound) as missing:
        await service.withdraw(credential, world.fleet_entry)
    assert str(missing.value) == str(KeyError("unknown entry: gone"))

    # Wire results of the ValueError race: REST 409 conflict, MCP not_active.
    monkeypatch.setattr(world.store, "withdraw_entry", not_active)
    resp = await world.client.post(
        f"/v1/entries/{world.fleet_entry}/withdraw", json={}, headers=_h("k-dev")
    )
    assert _err(resp) == (409, "conflict", text)
    assert _mcp_err(await mcp.hive_withdraw(world.mcp("k-dev"), world.fleet_entry)) == (
        "not_active",
        text,
    )
