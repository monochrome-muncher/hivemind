"""ADR 0082: a dev-mode credential sits at trust level 0 when stored keys
are required.

A *dev-mode* credential is one that is not a stored key — not an admin
key, not an org key, not an active agent's key: a hand-built identity such
as the dev runner's fixed ``dev``. When ``HIVEMIND_REQUIRE_STORED_KEYS`` is
on, such a credential gets exactly what a pending agent gets (SPEC §12.2):
reads return empty / not-found, writes are permission denied. Off by
default, so the keyless dev runner and local scripts are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import Verdict
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.errors import EntryNotFound
from hivemind.services.feedback import FeedbackService
from hivemind.services.wiring import build_services
from hivemind.services.write import WriteService
from tests.fakes import FakeAuthenticator, make_embedder, make_search_config, make_store


def dev_credential() -> Credential:
    """The dev runner's fixed identity: not stored, not access-controlled."""
    return Credential(user_id="dev", agent_id="hivemind-mcp")


async def _write_entry(credential: Credential) -> tuple[MemoryStore, str]:
    """One entry written by ``credential`` (a dev credential sees all)."""
    store = make_store()
    draft = EntryDraft(kind=Kind.FACT, summary="s", author=credential.user_id, agent="a")
    written = await WriteService(store, make_embedder()).write_and_relate(
        draft, writer=credential.visibility()
    )
    return store, written.entry.id


# -- the Credential predicate and the downgrade ---------------------------


@pytest.mark.parametrize(
    ("credential", "stored"),
    [
        (dev_credential(), False),
        (Credential(user_id="admin", is_admin=True), True),
        (Credential(user_id="org", is_org=True, access_controlled=True), True),
        (
            Credential(
                user_id="alice",
                agent_name="alice",
                access_controlled=True,
                trust_level=TrustLevel.LURKER,
            ),
            True,
        ),
    ],
)
def test_is_stored(credential: Credential, stored: bool) -> None:
    assert credential.is_stored is stored


def test_effective_downgrades_a_dev_mode_credential() -> None:
    original = dev_credential()
    downgraded = original.effective(require_stored_keys=True)
    assert downgraded is not original
    assert downgraded.user_id == "dev"
    assert downgraded.access_controlled is True
    assert downgraded.trust_level is TrustLevel.UNTRUSTED
    assert downgraded.is_admin is False
    assert downgraded.is_org is False
    assert downgraded.agent_name is None
    assert downgraded.agent_id == "hivemind-mcp"  # attribution survives
    # The stand-in sees nothing: level 0 on the trust matrix.
    assert downgraded.visibility().level is TrustLevel.UNTRUSTED


def test_effective_is_identity_when_off() -> None:
    credential = dev_credential()
    assert credential.effective(require_stored_keys=False) is credential


@pytest.mark.parametrize(
    "credential",
    [
        Credential(user_id="admin", is_admin=True),
        Credential(user_id="org", is_org=True, access_controlled=True),
        Credential(
            user_id="alice",
            agent_name="alice",
            access_controlled=True,
            trust_level=TrustLevel.PRIVILEGED,
        ),
    ],
)
def test_effective_keeps_stored_credentials(credential: Credential) -> None:
    assert credential.effective(require_stored_keys=True) is credential


# -- the service seam (build_services) ------------------------------------


async def test_dev_mode_feedback_is_not_found_when_stored_keys_required() -> None:
    store, entry_id = await _write_entry(dev_credential())
    service = FeedbackService(store, make_search_config(), require_stored_keys=True)
    with pytest.raises(EntryNotFound):
        await service.record_feedback(dev_credential(), entry_id, Verdict.HELPFUL)


async def test_dev_mode_feedback_works_when_flag_off() -> None:
    store, entry_id = await _write_entry(dev_credential())
    service = FeedbackService(store, make_search_config())
    # A second dev credential: the entry's own author cannot rate it (ADR 0065).
    outcome = await service.record_feedback(
        Credential(user_id="dev2", agent_id="other"), entry_id, Verdict.HELPFUL
    )
    assert outcome.feedback.entry_id == entry_id


async def test_build_services_propagates_the_flag() -> None:
    store, entry_id = await _write_entry(dev_credential())
    services = build_services(
        store, make_embedder(), make_search_config(), require_stored_keys=True
    )
    with pytest.raises(EntryNotFound):
        await services.governance_service.record_feedback(
            dev_credential(), entry_id, Verdict.HELPFUL
        )


# -- the surfaces: REST and MCP downgrade a dev credential before any service ---------

HOME_FLEET = "fleet-home"


async def _pool() -> tuple[MemoryStore, str, str]:
    """A pool with an org entry the dev identity wrote itself (it could
    withdraw it if nothing downgraded it) and a colleague's fleet entry."""
    store = make_store()
    embedder = make_embedder()
    ids = []
    for author, scope, fleet in (("dev", "org", None), ("alice", "fleet", HOME_FLEET)):
        draft = EntryDraft(
            kind=Kind.FACT,
            summary="release checklist",
            author=author,
            agent="a",
            scope=scope,
            fleet_id=fleet,
        )
        ids.append((await store.create_entry(draft, await embedder.embed_entry(draft))).id)
    return store, ids[0], ids[1]


def _rest(store: MemoryStore, *, require: bool) -> httpx.AsyncClient:
    hivemind = create_app_for_config(
        Settings(require_stored_keys=require),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k-dev": dev_credential()}),
        search_config=make_search_config(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(hivemind)), base_url="http://t"
    )


# (operation, expected REST status when stored keys are required)
REST_OPERATIONS = {
    "list": 200,
    "get": 404,
    "search": 200,
    "write": 403,
    "withdraw": 404,
    "pin": 404,
    "feedback": 404,
}


async def _rest_call(client: httpx.AsyncClient, op: str, own: str, fleet: str) -> httpx.Response:
    headers = {"X-API-Key": "k-dev"}
    if op == "list":
        return await client.get("/v1/entries", headers=headers)
    if op == "get":
        return await client.get(f"/v1/entries/{fleet}", headers=headers)
    if op == "search":
        return await client.post("/v1/search", json={"query": "release checklist"}, headers=headers)
    if op == "write":
        body = {"kind": "fact", "summary": "new note"}
        return await client.post("/v1/entries", json=body, headers=headers)
    if op == "withdraw":
        return await client.post(f"/v1/entries/{own}/withdraw", json={}, headers=headers)
    if op == "pin":
        return await client.put(f"/v1/entries/{fleet}/pin", headers=headers)
    return await client.post(
        f"/v1/entries/{fleet}/feedback", json={"verdict": "helpful"}, headers=headers
    )


@pytest.mark.parametrize("op", REST_OPERATIONS)
async def test_rest_dev_credential_is_level_zero_when_stored_keys_required(op: str) -> None:
    store, own, fleet = await _pool()
    async with _rest(store, require=True) as client:
        response = await _rest_call(client, op, own, fleet)
    assert response.status_code == REST_OPERATIONS[op]
    if op in ("list", "search"):
        assert response.json() == []
    assert await store.count_entries(EntryFilters()) == 2
    assert (await store.get_entry(own)).state is EntryState.ACTIVE  # type: ignore[union-attr]
    assert await store.list_pins(HOME_FLEET) == []
    assert await store.list_feedback(fleet, 10) == []


@pytest.mark.parametrize("op", ["list", "get", "search", "write", "withdraw"])
async def test_rest_dev_credential_keeps_full_access_when_the_flag_is_off(op: str) -> None:
    store, own, fleet = await _pool()
    async with _rest(store, require=False) as client:
        response = await _rest_call(client, op, own, fleet)
    assert response.status_code in (200, 201)
    if op in ("list", "search"):
        assert len(response.json()) == 2


def _mcp(store: MemoryStore, *, require: bool) -> Any:
    services = build_services(
        store, make_embedder(), make_search_config(), require_stored_keys=require
    )
    app = McpHivemind.from_services(store, services, credential=dev_credential())
    return build_server(app, credential_provider=dev_credential, require_stored_keys=require)


# (tool, arguments from the two entry ids, the MCP error code when stored keys are required)
MCP_OPERATIONS = {
    "hive_get": (lambda own, fleet: {"entry_id": fleet}, "not_found"),
    "hive_write": (lambda own, fleet: {"kind": "fact", "summary": "new note"}, "permission_denied"),
    "hive_withdraw": (lambda own, fleet: {"entry_id": own}, "not_found"),
    "hive_pin": (lambda own, fleet: {"entry_id": fleet}, "not_found"),
    "hive_feedback": (lambda own, fleet: {"entry_id": fleet, "verdict": "helpful"}, "not_found"),
}


@pytest.mark.parametrize("tool", MCP_OPERATIONS)
async def test_mcp_dev_credential_is_level_zero_when_stored_keys_required(tool: str) -> None:
    store, own, fleet = await _pool()
    arguments, code = MCP_OPERATIONS[tool]
    result = await _mcp(store, require=True).call_tool(tool, arguments(own, fleet))
    assert json.loads(result.content[0].text)["error"]["code"] == code
    assert await store.count_entries(EntryFilters()) == 2
    assert (await store.get_entry(own)).state is EntryState.ACTIVE  # type: ignore[union-attr]
    assert await store.list_pins(HOME_FLEET) == []


async def test_mcp_dev_credential_reads_nothing_when_stored_keys_required() -> None:
    store, _own, _fleet = await _pool()
    server = _mcp(store, require=True)
    searched = await server.call_tool("hive_search", {"query": "release checklist"})
    listed = await server.call_tool("hive_list", {})
    assert json.loads(searched.content[0].text)["hits"] == []
    assert json.loads(listed.content[0].text)["entries"] == []


async def test_mcp_dev_credential_reads_and_writes_when_the_flag_is_off() -> None:
    store, _own, _fleet = await _pool()
    server = _mcp(store, require=False)
    searched = await server.call_tool("hive_search", {"query": "release checklist"})
    wrote = await server.call_tool("hive_write", {"kind": "fact", "summary": "new note"})
    assert len(json.loads(searched.content[0].text)["hits"]) == 2
    assert "id" in json.loads(wrote.content[0].text)


async def test_build_services_downgrades_pins_too() -> None:
    store, _own, fleet = await _pool()
    services = build_services(
        store, make_embedder(), make_search_config(), require_stored_keys=True
    )
    with pytest.raises(EntryNotFound):
        await services.governance_service.pin(dev_credential(), fleet)
    assert await store.list_pins(HOME_FLEET) == []
