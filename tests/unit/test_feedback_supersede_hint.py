"""A stale or wrong report says when the reporter could fix the entry itself
(ADR 0128): only when a write at its default scope would supersede it."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.feedback import Verdict
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, fleet: str, level: TrustLevel) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


AUTHOR = _agent("author", "fleet-a", TrustLevel.CONTRIBUTOR)
CONTRIBUTOR = _agent("peer", "fleet-a", TrustLevel.CONTRIBUTOR)
LURKER = _agent("lurker", "fleet-a", TrustLevel.LURKER)
FOREIGN_PRIVILEGED = _agent("roamer", "fleet-b", TrustLevel.PRIVILEGED)
ADMIN = Credential(user_id="admin", agent_id="admin", is_admin=True)
LEGACY = Credential(user_id="legacy", agent_id="legacy")  # no access control

FIX_PREFIX = "You may supersede this entry: hive_write the correction with supersedes:"


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        read_service=ReadService(store, GovernanceService(store), config),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _fleet_entry(store: MemoryStore, summary: str = "billing runs on node 20") -> str:
    entry = await hive_write(_surface(store, AUTHOR), kind="fact", summary=summary)
    return str(entry["id"])


async def test_stale_by_a_contributor_on_a_home_fleet_entry_hints() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, CONTRIBUTOR), eid, verdict="stale")
    assert reply["fix"] == f'{FIX_PREFIX} ["{eid}"] and no scope.'


async def test_wrong_hints_too() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, CONTRIBUTOR), eid, verdict="wrong")
    assert "fix" in reply


async def test_helpful_never_hints() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, CONTRIBUTOR), eid, verdict="helpful")
    assert "fix" not in reply


async def test_a_privileged_agent_reading_a_foreign_fleet_entry_gets_no_hint() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, FOREIGN_PRIVILEGED), eid, verdict="stale")
    assert "error" not in reply
    assert "fix" not in reply


async def test_a_legacy_org_entry_gets_no_hint() -> None:
    store = MemoryStore()
    entry = await hive_write(_surface(store, LEGACY), kind="fact", summary="org wide fact")
    reply = await hive_feedback(_surface(store, CONTRIBUTOR), str(entry["id"]), verdict="stale")
    assert "error" not in reply
    assert "fix" not in reply


async def test_the_admin_key_gets_no_hint() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, ADMIN), eid, verdict="wrong")
    assert "error" not in reply
    assert "fix" not in reply


async def test_a_level_one_agent_that_writes_only_self_gets_no_hint() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, LURKER), eid, verdict="stale")
    assert "error" not in reply
    assert "fix" not in reply


async def test_batch_lists_only_the_ids_the_caller_may_supersede() -> None:
    store = MemoryStore()
    fleet_id = await _fleet_entry(store)
    org = await hive_write(_surface(store, LEGACY), kind="fact", summary="org wide fact")
    reply = await hive_feedback(
        _surface(store, CONTRIBUTOR), entry_ids=[fleet_id, str(org["id"])], verdict="stale"
    )
    assert reply["fix"] == {
        "may_supersede": [fleet_id],
        "how": "hive_write each correction with supersedes: [<id>] and no scope.",
    }


async def test_batch_with_nothing_fixable_has_no_fix_field() -> None:
    store = MemoryStore()
    org = await hive_write(_surface(store, LEGACY), kind="fact", summary="org wide fact")
    reply = await hive_feedback(
        _surface(store, CONTRIBUTOR), entry_ids=[str(org["id"])], verdict="wrong"
    )
    assert "fix" not in reply


async def test_the_service_outcome_carries_the_flag() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    outcome = await GovernanceService(store).record_feedback(CONTRIBUTOR, eid, Verdict.STALE)
    assert outcome.may_supersede is True


async def test_rest_carries_the_same_flag() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": CONTRIBUTOR}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"X-API-Key": "k"}
    ) as client:
        stale = await client.post(f"/v1/entries/{eid}/feedback", json={"verdict": "stale"})
        helpful = await client.post(f"/v1/entries/{eid}/feedback", json={"verdict": "helpful"})
    assert stale.json()["may_supersede"] is True
    assert helpful.json()["may_supersede"] is False


async def test_a_legacy_reporter_gets_no_hint() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    reply = await hive_feedback(_surface(store, LEGACY), eid, verdict="stale")
    assert "error" not in reply
    assert "fix" not in reply


async def test_a_superseded_entry_gets_no_hint() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    await hive_write(_surface(store, AUTHOR), kind="fact", summary="node 22", supersedes=[eid])
    reply = await hive_feedback(_surface(store, CONTRIBUTOR), eid, verdict="stale")
    assert "error" not in reply
    assert "fix" not in reply


async def test_the_hint_leads_to_a_write_that_succeeds() -> None:
    store = MemoryStore()
    eid = await _fleet_entry(store)
    me = _surface(store, CONTRIBUTOR)
    reply = await hive_feedback(me, eid, verdict="stale")
    assert eid in str(reply["fix"])
    written = await hive_write(me, kind="fact", summary="billing runs on node 22", supersedes=[eid])
    assert "error" not in written


async def test_rest_batch_route_carries_the_flag() -> None:
    store = MemoryStore()
    fleet_id = await _fleet_entry(store)
    org = await hive_write(_surface(store, LEGACY), kind="fact", summary="org wide fact")
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": CONTRIBUTOR}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"X-API-Key": "k"}
    ) as client:
        resp = await client.post(
            "/v1/feedback", json={"entry_ids": [fleet_id, org["id"]], "verdict": "wrong"}
        )
    flags = {row["entry_id"]: row["may_supersede"] for row in resp.json()}
    assert flags == {fleet_id: True, org["id"]: False}
