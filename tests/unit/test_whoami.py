"""``hive_whoami`` / ``GET /v1/whoami`` (ADR 0030).

The standing each kind of key reports, at every trust level, and that it
agrees with what the data plane actually enforces.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.mcp.app import McpHivemind, hive_whoami
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService, PermissionDenied, resolve_write_scope
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.fakes import (
    FakeAuthenticator,
    make_clock,
    make_embedder,
    make_search_config,
    make_store,
)

ADMIN = Credential(user_id="admin", is_admin=True)
ORG = Credential(user_id="org", is_org=True, access_controlled=True)


def agent(level: TrustLevel, fleet: str | None = "f1") -> Credential:
    return Credential(
        user_id="alice",
        agent_name="alice",
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


async def _service() -> tuple[AccessService, str]:
    store = make_store()
    service = AccessService(store, FakeAuthenticator())
    fleet = await service.create_fleet("platform", ADMIN)
    await service.register("alice", ORG, owner_alias="john")
    await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet.id, ADMIN)
    return service, fleet.id


@pytest.mark.parametrize(
    ("level", "can_read", "can_write"),
    [
        (TrustLevel.UNTRUSTED, (), ()),
        (TrustLevel.LURKER, ("own", "home_fleet", "org"), ("self",)),
        (TrustLevel.CONTRIBUTOR, ("own", "home_fleet", "org"), ("self", "fleet")),
        (TrustLevel.PRIVILEGED, ("own", "all_fleets", "org"), ("self", "fleet")),
    ],
)
async def test_an_agent_key_reports_its_level(
    level: TrustLevel, can_read: tuple[str, ...], can_write: tuple[str, ...]
) -> None:
    service, fleet = await _service()
    standing = await service.whoami(agent(level, fleet))
    assert standing.key_kind == "agent"
    assert standing.name == "alice"
    assert standing.status is AgentStatus.ACTIVE
    assert standing.trust_level is level
    assert (standing.home_fleet_id, standing.home_fleet_name) == (fleet, "platform")
    assert standing.can_read == can_read
    assert standing.can_write_scopes == can_write


async def test_write_scopes_agree_with_the_write_seam() -> None:
    """Every scope whoami offers is accepted by resolve_write_scope, and
    every scope it withholds is rejected."""
    service, fleet = await _service()
    for level in TrustLevel:
        for home in (fleet, None):
            credential = agent(level, home)
            offered = (await service.whoami(credential)).can_write_scopes
            for scope in ("self", "fleet", "org"):
                try:
                    resolve_write_scope(credential, scope)
                    accepted = True
                except PermissionDenied:
                    accepted = False
                assert accepted == (scope in offered), (level, home, scope)


async def test_a_contributor_without_a_home_fleet_cannot_write_fleet() -> None:
    service, _ = await _service()
    standing = await service.whoami(agent(TrustLevel.CONTRIBUTOR, fleet=None))
    assert standing.can_write_scopes == ("self",)


async def test_the_org_key_can_do_nothing_but_learns_it_must_register() -> None:
    service, _ = await _service()
    standing = await service.whoami(ORG)
    assert standing.key_kind == "org"
    assert standing.status is None
    assert standing.can_read == ()
    assert standing.can_write_scopes == ()


async def test_admin_and_legacy_keys() -> None:
    service, _ = await _service()
    admin = await service.whoami(ADMIN)
    legacy = await service.whoami(Credential(user_id="dev", agent_id="hivemind-mcp"))
    assert (admin.key_kind, admin.can_read) == ("admin", ("everything",))
    assert admin.can_write_scopes == ("self", "org")  # no fleet of its own (ADR 0060)
    assert (legacy.key_kind, legacy.can_read) == ("legacy", ("everything",))


# -- both surfaces return the same shape -------------------------------------


async def test_rest_whoami_for_org_and_admin_keys_and_401_without_one() -> None:
    hivemind = create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(hivemind))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        org = await client.get("/v1/whoami", headers={"X-API-Key": "hm_org"})
        admin = await client.get("/v1/whoami", headers={"X-API-Key": "hm_admin"})
        missing = await client.get("/v1/whoami")
    assert org.status_code == 200
    assert org.json() == {
        "key_kind": "org",
        "name": "org",
        "status": None,
        "trust_level": 0,
        "trust_level_name": "untrusted",
        "home_fleet_id": None,
        "home_fleet_name": None,
        "can_read": [],
        "can_write_scopes": [],
        "inherits_from": [],
        "key_last_used_at": None,
    }
    assert admin.json()["key_kind"] == "admin"
    assert missing.status_code == 401


async def test_mcp_whoami_tool_returns_the_standing() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    embedder = make_embedder()
    config = make_search_config()
    access = AccessService(store, FakeAuthenticator())
    fleet = await access.create_fleet("platform", ADMIN)
    await store.register_agent("alice", "john")
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    app = McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        read_service=ReadService(store, GovernanceService(store), config),
        access_service=access,
        search_config=config,
        credential=agent(TrustLevel.LURKER, fleet.id),
    )
    direct = await hive_whoami(app)
    assert direct["status"] == "active"
    assert direct["trust_level_name"] == "lurker"
    assert direct["can_write_scopes"] == ["self"]
    result = await build_server(app).call_tool("hive_whoami", {})
    assert result.is_error is False
    assert json.loads(result.content[0].text) == direct


# -- the key's last use (ADR 0114) -------------------------------------------


async def test_an_agent_key_reports_when_it_was_last_used() -> None:
    store = make_store()
    service = AccessService(store, FakeAuthenticator())
    fleet = await service.create_fleet("platform", ADMIN)
    await service.register("alice", ORG)
    await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet.id, ADMIN)
    cred = agent(TrustLevel.CONTRIBUTOR, fleet.id)

    first = await service.whoami(cred)
    assert first.key_last_used_at is None
    assert first.as_dict()["key_last_used_at"] is None

    assert isinstance(store, MemoryStore)
    used = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    store.record_key_use("alice", used)
    standing = await service.whoami(cred)
    assert standing.key_last_used_at == used
    assert standing.as_dict()["key_last_used_at"] == used.isoformat()


async def test_admin_org_and_legacy_keys_report_no_last_use() -> None:
    service, _ = await _service()
    for cred in (ADMIN, ORG, Credential(user_id="dev", agent_id="hivemind-mcp")):
        assert (await service.whoami(cred)).key_last_used_at is None


async def test_rest_and_mcp_whoami_carry_the_agent_keys_last_use() -> None:
    """ADR 0114: any agent key sees its own last-use time, over both surfaces."""
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("platform")
    hivemind = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator({"hm_alice": agent(TrustLevel.LURKER, fleet.id)}),
        search_config=make_search_config(),
    )
    await store.register_agent("alice")
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    used = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
    store.record_key_use("alice", used)
    transport = httpx.ASGITransport(app=create_app(hivemind))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/v1/whoami", headers={"X-API-Key": "hm_alice"})
    assert resp.status_code == 200
    assert resp.json()["key_last_used_at"] == used.isoformat()
