"""Server-enforced access on every path that touches an existing entry
(ADR 0033): reads by id, the supersession chain, feedback, withdrawal,
write provenance, the supersession-scope rule and reserved agent names.

The headline regression is the reported one: two contributors with
separate keys, one fetching the other's ``self`` entry by id.
"""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_withdraw,
    hive_write,
)
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import (
    FakeAuthenticator,
    make_clock,
    make_embedder,
    make_search_config,
)

HOME, OTHER = "fleet-home", "fleet-other"


def agent(name: str, level: TrustLevel = TrustLevel.CONTRIBUTOR, fleet: str = HOME) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


ALICE = agent("alice")
BOB = agent("bob")
LURKER = agent("lurk", TrustLevel.LURKER)
OUTSIDER = agent("carol", TrustLevel.PRIVILEGED, fleet=OTHER)  # reads every fleet
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
ADMIN = Credential(user_id="admin", is_admin=True)


class Pool:
    """One in-memory pool; ``as_(cred)`` is the MCP surface for that key."""

    def __init__(self) -> None:
        self.clock = make_clock()
        self.store = MemoryStore(self.clock)
        self.embedder = make_embedder()
        self.config = make_search_config()

    def as_(self, credential: Credential) -> McpHivemind:
        return McpHivemind(
            store=self.store,
            write_service=WriteService(self.store, self.embedder),
            search_service=SearchService(self.store, self.embedder, self.config, now_fn=self.clock),
            governance_service=GovernanceService(self.store),
            access_service=AccessService(self.store),
            search_config=self.config,
            credential=credential,
        )


async def _write(pool: Pool, cred: Credential, summary: str, **kw: object) -> dict[str, object]:
    out = await hive_write(pool.as_(cred), kind="fact", summary=summary, **kw)  # type: ignore[arg-type]
    assert "error" not in out, out
    return out


# -- 1. reads by id ------------------------------------------------------------


async def test_a_contributor_cannot_get_another_contributors_self_entry() -> None:
    """The reported bug: separate keys, both contributors."""
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private note", scope="self")
    got = await hive_get(pool.as_(BOB), entry_id=str(secret["id"]))
    assert got["error"]["code"] == "not_found"  # type: ignore[index]
    assert "alice private note" not in str(got)


@pytest.mark.parametrize("reader", [ORG, LURKER, OUTSIDER], ids=["org-key", "lurker", "privileged"])
async def test_nobody_else_gets_a_self_entry_by_id(reader: Credential) -> None:
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private note", scope="self")
    got = await hive_get(pool.as_(reader), entry_id=str(secret["id"]))
    assert got["error"]["code"] == "not_found"  # type: ignore[index]


async def test_invisible_and_nonexistent_ids_answer_identically() -> None:
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private note", scope="self")
    hidden = await hive_get(pool.as_(BOB), entry_id=str(secret["id"]))
    missing = await hive_get(pool.as_(BOB), entry_id="00000000-0000-0000-0000-000000000000")
    assert hidden["error"]["code"] == missing["error"]["code"] == "not_found"  # type: ignore[index]


async def test_the_author_and_admin_still_get_it_and_fleet_rules_still_hold() -> None:
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private note", scope="self")
    shared = await _write(pool, ALICE, "home fleet note")  # omitted scope -> fleet
    assert (await hive_get(pool.as_(ALICE), entry_id=str(secret["id"])))["id"] == secret["id"]
    assert (await hive_get(pool.as_(ADMIN), entry_id=str(secret["id"])))["id"] == secret["id"]
    assert (await hive_get(pool.as_(BOB), entry_id=str(shared["id"])))["id"] == shared["id"]
    # A lurker in another fleet may not; a privileged reader may (read-broad).
    other_lurker = agent("dave", TrustLevel.LURKER, fleet=OTHER)
    denied = await hive_get(pool.as_(other_lurker), entry_id=str(shared["id"]))
    assert denied["error"]["code"] == "not_found"  # type: ignore[index]
    assert (await hive_get(pool.as_(OUTSIDER), entry_id=str(shared["id"])))["id"] == shared["id"]


async def test_history_leaves_out_versions_the_reader_cannot_see() -> None:
    pool = Pool()
    draft = await _write(pool, ALICE, "alice draft", scope="self")
    public = await _write(pool, ALICE, "published version", supersedes=[str(draft["id"])])
    as_bob = await hive_get(pool.as_(BOB), entry_id=str(public["id"]), include_history=True)
    assert as_bob["superseded"] == []
    as_alice = await hive_get(pool.as_(ALICE), entry_id=str(public["id"]), include_history=True)
    assert [e["id"] for e in as_alice["superseded"]] == [draft["id"]]  # type: ignore[union-attr]


# -- 2. write provenance -------------------------------------------------------


async def test_mcp_write_has_no_author_parameter() -> None:
    pool = Pool()
    tools = {t.name: t for t in await build_server(pool.as_(ALICE)).list_tools()}
    assert "author" not in tools["hive_write"].input_schema["properties"]
    with pytest.raises(TypeError):
        await hive_write(pool.as_(ALICE), kind="fact", summary="x", author="bob")  # type: ignore[call-arg]


async def test_an_agent_key_stamps_author_and_agent_whatever_is_supplied() -> None:
    pool = Pool()
    entry = await _write(pool, ALICE, "note", scope="self", agent="bob")
    assert (entry["author"], entry["agent"]) == ("alice", "alice")
    got = await hive_get(pool.as_(BOB), entry_id=str(entry["id"]))
    assert got["error"]["code"] == "not_found"  # type: ignore[index]


# -- 3. supersession scope ------------------------------------------------------


async def test_self_supersedes_own_self_only() -> None:
    pool = Pool()
    mine = await _write(pool, ALICE, "v1", scope="self")
    fleet = await _write(pool, BOB, "fleet fact")
    ok = await _write(pool, ALICE, "v2", scope="self", supersedes=[str(mine["id"])])
    assert ok["scope"] == "self"
    bad = await hive_write(
        pool.as_(ALICE), kind="fact", summary="hide it", scope="self", supersedes=[str(fleet["id"])]
    )
    assert bad["error"]["code"] == "supersede_denied"  # type: ignore[index]
    assert (await pool.store.get_entry(str(fleet["id"]))).state.value == "active"  # type: ignore[union-attr]


async def test_a_lurker_cannot_hide_a_fleet_entry() -> None:
    pool = Pool()
    fleet = await _write(pool, ALICE, "fleet fact")
    bad = await hive_write(
        pool.as_(LURKER), kind="fact", summary="correction", supersedes=[str(fleet["id"])]
    )
    assert bad["error"]["code"] == "supersede_denied"  # type: ignore[index]
    assert (await pool.store.get_entry(str(fleet["id"]))).state.value == "active"  # type: ignore[union-attr]


async def test_fleet_supersedes_a_colleagues_fleet_entry_in_its_own_fleet() -> None:
    pool = Pool()
    old = await _write(pool, ALICE, "old fleet fact")
    new = await _write(pool, BOB, "corrected fleet fact", supersedes=[str(old["id"])])
    stored = await pool.store.get_entry(str(old["id"]))
    assert stored is not None and stored.superseded_by == new["id"]


async def test_no_superseding_across_fleets_or_into_someone_elses_self() -> None:
    pool = Pool()
    home_fact = await _write(pool, ALICE, "home fleet fact")
    alice_self = await _write(pool, ALICE, "alice private", scope="self")
    # A privileged agent reads every fleet but writes in its own (read-broad, write-local).
    cross = await hive_write(
        pool.as_(OUTSIDER), kind="fact", summary="x", supersedes=[str(home_fact["id"])]
    )
    assert cross["error"]["code"] == "supersede_denied"  # type: ignore[index]
    hidden = await hive_write(
        pool.as_(BOB), kind="fact", summary="x", supersedes=[str(alice_self["id"])]
    )
    assert hidden["error"]["code"] == "supersede_denied"  # type: ignore[index]
    assert "not found or not supersedable" in hidden["error"]["message"]  # type: ignore[index]


async def test_one_bad_target_rejects_the_whole_write() -> None:
    pool = Pool()
    mine = await _write(pool, ALICE, "mine v1", scope="self")
    theirs = await _write(pool, BOB, "bob private", scope="self")
    before = len(await pool.store.list_entries(_all(), limit=100))
    bad = await hive_write(
        pool.as_(ALICE),
        kind="fact",
        summary="v2",
        scope="self",
        supersedes=[str(mine["id"]), str(theirs["id"])],
    )
    assert bad["error"]["code"] == "supersede_denied"  # type: ignore[index]
    assert str(theirs["id"]) in bad["error"]["message"]  # type: ignore[index]
    assert len(await pool.store.list_entries(_all(), limit=100)) == before
    assert (await pool.store.get_entry(str(mine["id"]))).state.value == "active"  # type: ignore[union-attr]


async def test_superseding_an_already_superseded_entry_is_denied() -> None:
    """ADR 0034: only the current head is supersedable. A second claim on
    the same target would be silently dropped (``superseded_by`` is
    single-valued), so it is rejected with ``supersede_denied``."""
    pool = Pool()
    v1 = await _write(pool, ALICE, "original")
    v2 = await _write(pool, BOB, "correction v2", supersedes=[str(v1["id"])])
    bad = await hive_write(
        pool.as_(ALICE), kind="fact", summary="correction v3", supersedes=[str(v1["id"])]
    )
    assert bad["error"]["code"] == "supersede_denied"  # type: ignore[index]
    # The current head remains supersedable — re-target the correction.
    v3 = await _write(pool, ALICE, "correction v3 on the head", supersedes=[str(v2["id"])])
    head = await pool.store.get_entry(str(v2["id"]))
    assert head is not None and head.state.value == "superseded" and head.superseded_by == v3["id"]


async def test_superseding_a_withdrawn_entry_is_denied() -> None:
    pool = Pool()
    v1 = await _write(pool, ALICE, "retracted")
    await hive_withdraw(pool.as_(ALICE), entry_id=str(v1["id"]), reason="no longer reliable")
    bad = await hive_write(
        pool.as_(ALICE), kind="fact", summary="correction", supersedes=[str(v1["id"])]
    )
    assert bad["error"]["code"] == "supersede_denied"  # type: ignore[index]


async def test_admin_supersedes_anything() -> None:
    pool = Pool()
    theirs = await _write(pool, BOB, "bob private", scope="self")
    await _write(pool, ADMIN, "admin correction", supersedes=[str(theirs["id"])], agent="ops")
    assert (await pool.store.get_entry(str(theirs["id"]))).state.value == "superseded"  # type: ignore[union-attr]


def _all():
    from hivemind.domain.entry import EntryFilters

    return EntryFilters(include_inactive=True)


# -- 4. feedback and withdrawal follow readability --------------------------------


async def test_feedback_and_withdraw_on_an_invisible_entry_are_not_found() -> None:
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private", scope="self")
    fb = await hive_feedback(pool.as_(BOB), entry_id=str(secret["id"]), verdict="wrong")
    wd = await hive_withdraw(pool.as_(BOB), entry_id=str(secret["id"]))
    assert fb["error"]["code"] == "not_found"  # type: ignore[index]
    assert wd["error"]["code"] == "not_found"  # type: ignore[index]
    # A visible entry someone else wrote: still "not yours", not "not found".
    shared = await _write(pool, ALICE, "fleet fact")
    wd2 = await hive_withdraw(pool.as_(BOB), entry_id=str(shared["id"]))
    assert wd2["error"]["code"] == "permission_denied"  # type: ignore[index]


# -- 5. reserved names ----------------------------------------------------------


@pytest.mark.parametrize("name", ["admin", "org", "dev", "shared"])
async def test_built_in_identity_names_cannot_be_registered(name: str) -> None:
    service = AccessService(MemoryStore(make_clock()), FakeAuthenticator())
    with pytest.raises(ValueError, match="reserved"):
        await service.register(name, ORG)


# -- REST mirrors MCP ------------------------------------------------------------


def _rest(pool: Pool) -> tuple[HivemindApp, httpx.AsyncClient]:
    hivemind = create_app_for_config(
        Settings(),
        store=pool.store,
        embedder=pool.embedder,
        authenticator=FakeAuthenticator(agent_credentials={"k-alice": ALICE, "k-bob": BOB}),
        search_config=pool.config,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(hivemind)), base_url="http://t"
    )
    return hivemind, client


async def test_rest_get_feedback_and_supersede_enforce_the_same_rules() -> None:
    pool = Pool()
    secret = await _write(pool, ALICE, "alice private", scope="self")
    draft = await _write(pool, ALICE, "alice draft", scope="self")
    public = await _write(pool, ALICE, "published", supersedes=[str(draft["id"])])
    _, client = _rest(pool)
    bob = {"X-API-Key": "k-bob"}
    async with client:
        get = await client.get(f"/v1/entries/{secret['id']}", headers=bob)
        fb = await client.post(
            f"/v1/entries/{secret['id']}/feedback", json={"verdict": "wrong"}, headers=bob
        )
        hist = await client.get(f"/v1/entries/{public['id']}?history=true", headers=bob)
        sup = await client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": "x", "supersedes": [str(secret["id"])]},
            headers=bob,
        )
        own = await client.get(f"/v1/entries/{secret['id']}", headers={"X-API-Key": "k-alice"})
    assert get.status_code == 404
    assert fb.status_code == 404
    assert hist.json()["history"]["superseded"] == []
    assert sup.status_code == 403
    assert sup.json()["error"]["code"] == "supersede_denied"
    assert own.status_code == 200
