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

    def mcp(self, key: str) -> McpHivemind:
        embedder, config = make_embedder(), make_search_config()
        return McpHivemind(
            store=self.store,
            write_service=WriteService(self.store, embedder),
            search_service=SearchService(self.store, embedder, config),
            governance_service=GovernanceService(self.store, config),
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
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )

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
    return World(client, store, authenticator, fleet_entry, org_entry, withdrawn)


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
        "pin an org entry",
        lambda w: w.client.put(f"/v1/entries/{w.org_entry}/pin", headers=_h("k-lead")),
        (422, "invalid_input", "only fleet entries can be pinned, to their own fleet"),
        lambda w: mcp.hive_pin(w.mcp("k-lead"), w.org_entry),
        ("invalid_input", "only fleet entries can be pinned, to their own fleet"),
    ),
    (
        "pin by a contributor",
        lambda w: w.client.put(f"/v1/entries/{w.fleet_entry}/pin", headers=_h("k-dev")),
        (
            403,
            "forbidden",
            "only a privileged agent of the entry's fleet or an admin may pin or unpin",
        ),
        lambda w: mcp.hive_pin(w.mcp("k-dev"), w.fleet_entry),
        (
            "permission_denied",
            "only a privileged agent of the entry's fleet or an admin may pin or unpin",
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


def test_typed_errors_still_match_the_builtins_surfaces_catch() -> None:
    from hivemind.services import errors, governance

    assert issubclass(errors.EntryNotFound, LookupError)
    assert issubclass(errors.EntryNotActive, ValueError)
    assert issubclass(errors.AgentIdentityRequired, ValueError)
    assert issubclass(errors.SupersedeDenied, errors.PermissionDenied)
    # The old import path names the very same classes.
    assert governance.PermissionDenied is errors.PermissionDenied
    assert governance.SupersedeDenied is errors.SupersedeDenied
