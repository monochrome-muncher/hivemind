"""Retiring an agent with a successor (ADR 0113): who owns which memories.

The ownership checks are exercised both ways at the HTTP seam over
``MemoryStore``: the successor may, an unrelated agent still may not, the
retired agent's key is dead, and after an undo the successor loses it all.
The authenticator below builds the credential from the store on every
request, as ``PgAuthenticator`` does (its own tests are in
``tests/integration/test_successions_pg.py``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    Visibility,
    entry_is_visible,
    may_supersede,
)
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.feedback import FeedbackLogFilters
from hivemind.domain.pin import Pin, PinRefusal, pin_refusal
from hivemind.domain.succession import (
    AgentRetired,
    SuccessionRefused,
    UnknownSuccession,
    owned_authors,
)
from hivemind.domain.validation import InvalidInput
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.errors import PermissionDenied
from hivemind.services.ownership import own_author_filter
from tests.fakes import FakeAuthenticator, make_embedder, make_search_config

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class TickingClock:
    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


class StoreAuthenticator(FakeAuthenticator):
    """Resolves ``k-<name>`` from the agent's record and its successions on
    every request, and only while the agent is active (ADR 0039)."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store=store)
        self._records = store

    async def verify(self, key: str) -> Credential | None:
        if not key.startswith("k-"):
            return await super().verify(key)
        agent = await self._records.get_agent(key[2:])
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            return None
        return Credential(
            user_id=agent.name,
            agent_id=agent.name,
            agent_name=agent.name,
            access_controlled=True,
            trust_level=agent.trust_level,
            home_fleet_id=agent.home_fleet_id,
            inherits_from=await self._records.inherited_authors(agent.name),
        )


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(TickingClock())
        self.auth = StoreAuthenticator(self.store)
        self.app = create_app_for_config(
            Settings(),
            store=self.store,
            embedder=make_embedder(),
            authenticator=self.auth,
            search_config=make_search_config(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.app)), base_url="http://t"
        )
        self.successions = self.app.access_service.successions
        self.blue = ""
        self.red = ""

    async def setup(self) -> World:
        self.blue = (await self.store.create_fleet("blue")).id
        self.red = (await self.store.create_fleet("red")).id
        return self

    async def agent(
        self, name: str, fleet: str, level: TrustLevel = TrustLevel.CONTRIBUTOR
    ) -> None:
        await self.store.register_agent(name)
        await self.store.activate_agent(name, trust_level=level, home_fleet_id=fleet)

    async def entry(self, author: str, summary: str, scope: str = "self", fleet: str | None = None):
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope=scope,
            fleet_id=fleet,
        )
        return await self.store.create_entry(draft, await make_embedder().embed_text(summary))

    def h(self, name: str) -> dict[str, str]:
        return {"X-API-Key": f"k-{name}"}

    async def get(self, name: str, entry_id: str) -> int:
        return (await self.client.get(f"/v1/entries/{entry_id}", headers=self.h(name))).status_code

    async def withdraw(self, name: str, entry_id: str, path: str = "/v1/entries/{}/withdraw"):
        return await self.client.post(
            path.format(entry_id), json={"reason": "obsolete"}, headers=self.h(name)
        )

    async def rate(self, name: str, entry_id: str) -> httpx.Response:
        return await self.client.post(
            f"/v1/entries/{entry_id}/feedback", json={"verdict": "helpful"}, headers=self.h(name)
        )

    async def write(self, name: str, summary: str, **body) -> httpx.Response:  # type: ignore[no-untyped-def]
        return await self.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": summary, **body},
            headers=self.h(name),
        )

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)


@pytest.fixture
async def w() -> World:
    world = await World().setup()
    await world.agent("old", world.red)
    await world.agent("heir", world.blue)
    await world.agent("other", world.blue)
    await world.agent("peer", world.red)
    return world


async def retire(w: World, retired: str = "old", successor: str = "heir") -> None:
    await w.successions.retire(retired, successor, ADMIN)


# -- the domain rules -----------------------------------------------------------------


def test_owned_authors_is_the_name_then_the_inherited_without_repeats() -> None:
    assert owned_authors("c", ("a", "b")) == ("c", "a", "b")
    assert owned_authors("c", ()) == ("c",)
    assert owned_authors("c", ("c", "a", "a")) == ("c", "a")


def test_a_credential_owns_its_name_and_what_hands_down_to_it() -> None:
    cred = Credential(
        user_id="heir", agent_name="heir", access_controlled=True, inherits_from=("old", "older")
    )
    assert cred.owned_authors == ("heir", "old", "older")
    assert cred.owns("heir") and cred.owns("old") and cred.owns("older")
    assert not cred.owns("other")
    assert cred.visibility().inherited == ("old", "older")
    assert Credential(user_id="admin", is_admin=True).owned_authors == ("admin",)


def _entry(author: str, scope: str = "self", fleet: str | None = None):  # type: ignore[no-untyped-def]
    from hivemind.domain.entry import Entry, EntryState, ImportanceSource

    return Entry(
        id="e",
        kind=Kind.FACT,
        summary="s",
        body=None,
        author=author,
        agent=author,
        scope=scope,
        fleet_id=fleet,
        state=EntryState.ACTIVE,
        importance=3,
        importance_source=ImportanceSource.DEFAULT,
        occurred_at=T0,
        created_at=T0,
    )


def test_visibility_treats_inherited_authors_as_own_both_ways() -> None:
    heir = Visibility(TrustLevel.LURKER, "heir", "blue", inherited=("old",))
    plain = Visibility(TrustLevel.LURKER, "heir", "blue")
    assert entry_is_visible(_entry("old", "self"), heir)
    assert not entry_is_visible(_entry("old", "self"), plain)  # without the succession
    assert not entry_is_visible(_entry("stranger", "self"), heir)
    # An own fleet entry stays readable wherever it ended up (ADR 0011).
    assert entry_is_visible(_entry("old", "fleet", "red"), heir)
    assert not entry_is_visible(_entry("old", "fleet", "red"), plain)
    assert not entry_is_visible(_entry("stranger", "fleet", "red"), heir)


def test_may_supersede_follows_ownership_and_the_fleet_rules() -> None:
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", "blue", inherited=("old",))
    note = _entry("old", "self")
    assert may_supersede(note, new_scope="self", new_fleet_id=None, writer=heir)
    assert not may_supersede(
        note, new_scope="self", new_fleet_id=None, writer=Visibility(TrustLevel.CONTRIBUTOR, "x")
    )
    # A fleet memory in red: the heir may supersede it only into red.
    red = _entry("old", "fleet", "red")
    assert not may_supersede(red, new_scope="fleet", new_fleet_id="blue", writer=heir)
    assert may_supersede(red, new_scope="fleet", new_fleet_id="red", writer=heir)
    # The admin's rule for its own notes is unchanged.
    admin = Visibility(TrustLevel.PRIVILEGED, "admin", is_admin=True)
    assert may_supersede(_entry("admin", "self"), new_scope="self", new_fleet_id=None, writer=admin)


def test_own_author_filter_widens_only_the_callers_own_name() -> None:
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", "blue", inherited=("old",))
    widened = own_author_filter(EntryFilters(author="heir"), heir)
    assert widened.author is None and widened.authors == ("heir", "old")
    # Another name stays literal; no filter stays no filter; nothing inherited, nothing widened.
    assert own_author_filter(EntryFilters(author="old"), heir).author == "old"
    assert own_author_filter(EntryFilters(), heir).authors == ()
    solo = Visibility(TrustLevel.CONTRIBUTOR, "heir", "blue")
    assert own_author_filter(EntryFilters(author="heir"), solo).author == "heir"
    admin = Visibility(TrustLevel.PRIVILEGED, "admin", is_admin=True, inherited=("old",))
    assert own_author_filter(EntryFilters(author="admin"), admin).author == "admin"
    log = own_author_filter(FeedbackLogFilters(author="heir"), heir)
    assert log.authors == ("heir", "old") and log.author is None


def test_entry_filters_authors_match_any_of_them() -> None:
    f = EntryFilters(authors=("heir", "old"))
    assert f.matches(_entry("old")) and f.matches(_entry("heir"))
    assert not f.matches(_entry("other"))


def test_pins_of_inherited_agents_count_toward_the_agent_cap() -> None:
    pins = [Pin("f", f"e{i}", "old", T0) for i in range(3)]
    assert pin_refusal(pins, "new", "heir", 10, 3, ()) is None  # not counted without the succession
    assert pin_refusal(pins, "new", "heir", 10, 3, (), ("old",)) is PinRefusal.AGENT
    # An admin (no agent cap) is exempt as before; the fleet cap is unchanged.
    assert pin_refusal(pins, "new", "admin", 10, None, (), ("old",)) is None
    assert pin_refusal(pins, "new", "heir", 3, 3, (), ("old",)) is PinRefusal.FLEET
    # Moving the inherited pin onto the new version never raises the count.
    assert pin_refusal(pins, "new", "heir", 10, 3, ("e0",), ("old",)) is None


# -- reading --------------------------------------------------------------------------


async def test_the_successor_reads_the_retired_agents_notes_and_others_still_cannot(
    w: World,
) -> None:
    note = await w.entry("old", "old private kaniko note")
    mine = await w.entry("heir", "heir private note")
    assert await w.get("heir", note.id) == 404
    await retire(w)
    assert await w.get("heir", note.id) == 200
    assert await w.get("heir", mine.id) == 200
    assert await w.get("other", note.id) == 404
    assert await w.get("peer", note.id) == 404
    assert await w.get("old", note.id) == 401  # the retired agent's key is dead
    got = (await w.client.get(f"/v1/entries/{note.id}", headers=w.h("heir"))).json()
    assert got["author"] == "old"  # memories are immutable: the author stays


async def test_the_successor_reads_the_retired_agents_fleet_memories_in_any_fleet(
    w: World,
) -> None:
    in_red = await w.entry("old", "red fleet fact", scope="fleet", fleet=w.red)
    assert await w.get("heir", in_red.id) == 404  # heir is in blue
    await retire(w)
    assert await w.get("heir", in_red.id) == 200  # exactly what authorship grants
    assert await w.get("other", in_red.id) == 404  # a blue agent still cannot
    assert await w.get("peer", in_red.id) == 200  # the red fleet still can


async def test_search_and_list_show_inherited_memories_to_the_successor_only(w: World) -> None:
    note = await w.entry("old", "old private kaniko note")
    await retire(w)
    async with w.client as c:
        found = await c.post("/v1/search", json={"query": "kaniko"}, headers=w.h("heir"))
        hidden = await c.post("/v1/search", json={"query": "kaniko"}, headers=w.h("other"))
        own = await c.get("/v1/entries", params={"author": "heir"}, headers=w.h("heir"))
        literal = await c.get("/v1/entries", params={"author": "old"}, headers=w.h("heir"))
        stranger = await c.get("/v1/entries", params={"author": "old"}, headers=w.h("other"))
    assert [h["entry_id"] for h in found.json()] == [note.id]
    assert hidden.json() == []
    # ``author`` = your own name means everything you own; another name stays literal.
    assert [e["id"] for e in own.json()] == [note.id]
    assert [e["id"] for e in literal.json()] == [note.id]
    assert stranger.json() == []


async def test_after_an_undo_the_successor_loses_it_all(w: World) -> None:
    note = await w.entry("old", "old private note")
    in_red = await w.entry("old", "red fleet fact", scope="fleet", fleet=w.red)
    await retire(w)
    assert await w.get("heir", note.id) == 200
    await w.successions.undo("old", ADMIN)
    assert await w.get("heir", note.id) == 404
    assert await w.get("heir", in_red.id) == 404
    assert await w.get("old", note.id) == 401  # the old agent stays revoked
    assert (await w.store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]


# -- withdrawing ----------------------------------------------------------------------


async def test_the_successor_withdraws_inherited_memories_and_others_cannot(w: World) -> None:
    note = await w.entry("old", "old note")
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w)
    # An unrelated agent cannot, even where it can read (the red fleet).
    assert (await w.withdraw("peer", in_red.id)).status_code == 403
    assert (await w.withdraw("other", note.id)).status_code == 404  # unreadable
    assert (await w.withdraw("heir", note.id)).status_code == 200
    assert (await w.withdraw("heir", in_red.id)).status_code == 200
    assert (await w.store.get_entry(note.id)).state.value == "withdrawn"  # type: ignore[union-attr]


async def test_withdraw_stops_for_the_successor_after_an_undo(w: World) -> None:
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w)
    await w.successions.undo("old", ADMIN)
    assert (await w.withdraw("heir", in_red.id)).status_code == 404


async def test_the_portal_withdraw_accepts_the_successor_and_refuses_others(w: World) -> None:
    note = await w.entry("old", "old note")
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w)
    portal = "/v1/portal/entries/{}/withdraw"
    assert (await w.withdraw("peer", in_red.id, portal)).status_code == 403
    assert (await w.withdraw("heir", note.id, portal)).status_code == 200
    (row,) = await w.audit(AuditAction.ENTRY_WITHDRAW)
    assert row.detail["via"] == "portal" and row.detail["author"] == "old"


async def test_the_admin_still_withdraws_anyones_memory_with_an_audit_row(w: World) -> None:
    note = await w.entry("old", "old note")
    await retire(w)
    resp = await w.client.post(
        f"/v1/admin/entries/{note.id}/withdraw", json={}, headers={"X-API-Key": "hm_admin"}
    )
    assert resp.status_code == 200
    assert len(await w.audit(AuditAction.ENTRY_WITHDRAW)) == 1


# -- rating ---------------------------------------------------------------------------


async def test_the_successor_cannot_rate_inherited_memories_but_others_can(w: World) -> None:
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    foreign = await w.entry("other", "blue fact", scope="fleet", fleet=w.blue)
    await retire(w)
    assert (await w.rate("heir", in_red.id)).status_code == 403  # it counts as its own
    assert (await w.rate("peer", in_red.id)).status_code == 200  # a reader that does not own it
    assert (await w.rate("other", in_red.id)).status_code == 404  # unreadable stays unreadable
    assert (await w.rate("heir", foreign.id)).status_code == 200  # others' memories: fine
    # Batch: all or nothing, the inherited id refused, nothing recorded.
    batch = await w.client.post(
        "/v1/feedback",
        json={"entry_ids": [foreign.id, in_red.id], "verdict": "wrong"},
        headers=w.h("heir"),
    )
    assert batch.status_code == 403
    assert (await w.store.feedback_counts(foreign.id))[2] == 0
    ok = await w.client.post(
        "/v1/feedback", json={"entry_ids": [foreign.id], "verdict": "wrong"}, headers=w.h("heir")
    )
    assert ok.status_code == 200


async def test_after_an_undo_the_former_successor_may_rate_those_memories_again(w: World) -> None:
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await w.agent("heir2", w.red)
    await retire(w, "old", "heir2")
    assert (await w.rate("heir2", in_red.id)).status_code == 403
    await w.successions.undo("old", ADMIN)
    assert (await w.rate("heir2", in_red.id)).status_code == 200  # a red reader, not an owner


async def test_feedback_received_on_inherited_memories_shows_in_the_portal(w: World) -> None:
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w)
    assert (await w.rate("peer", in_red.id)).status_code == 200
    resp = await w.client.get(
        "/v1/portal/feedback", params={"direction": "received"}, headers=w.h("heir")
    )
    assert [i["entry"]["id"] for i in resp.json()] == [in_red.id]
    none = await w.client.get(
        "/v1/portal/feedback", params={"direction": "received"}, headers=w.h("other")
    )
    assert none.json() == []


# -- superseding ------------------------------------------------------------------------


async def test_the_successor_supersedes_an_inherited_note_and_others_cannot(w: World) -> None:
    note = await w.entry("old", "old private note")
    await retire(w)
    denied = await w.write("other", "new text", scope="self", supersedes=[note.id])
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "supersede_denied"
    ok = await w.write("heir", "new text", scope="self", supersedes=[note.id])
    assert ok.status_code == 201, ok.text
    assert (await w.store.get_entry(note.id)).state.value == "superseded"  # type: ignore[union-attr]
    assert ok.json()["author"] == "heir"  # the new version is the successor's


async def test_the_successor_supersedes_an_inherited_fleet_memory_only_as_fleet_rules_allow(
    w: World,
) -> None:
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w)
    # heir writes into its own (blue) fleet: a fleet memory may only be
    # superseded into the same fleet, so this is refused, as for its author.
    narrow = await w.write("heir", "blue text", scope="fleet", supersedes=[in_red.id])
    assert narrow.status_code == 403 and narrow.json()["error"]["code"] == "supersede_denied"
    private = await w.write("heir", "private text", scope="self", supersedes=[in_red.id])
    assert private.status_code == 403
    # A red agent still may (unchanged).
    ok = await w.write("peer", "red text", scope="fleet", supersedes=[in_red.id])
    assert ok.status_code == 201


async def test_a_successor_whose_home_fleet_is_the_memorys_fleet_supersedes_it(
    w: World,
) -> None:
    await w.agent("heir2", w.red)
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    await retire(w, "old", "heir2")
    ok = await w.write("heir2", "red text", scope="fleet", supersedes=[in_red.id])
    assert ok.status_code == 201


# -- pins -----------------------------------------------------------------------------


async def test_inherited_pins_count_toward_the_successors_cap_and_are_never_dropped(
    w: World,
) -> None:
    await w.agent("heir2", w.red)
    entries = [await w.entry("old", f"fact {i}", scope="fleet", fleet=w.red) for i in range(5)]
    for e in entries[:3]:
        assert not isinstance(await w.store.pin_entry(w.red, e.id, "old", 10), PinRefusal)
    await retire(w, "old", "heir2")
    # The inherited pins stay (nothing was dropped) ...
    assert {p.entry_id for p in await w.store.list_pins(w.red)} == {e.id for e in entries[:3]}
    # ... and use up heir2's three, so it cannot pin a fourth; a peer still can.
    refused = await w.client.put(f"/v1/entries/{entries[3].id}/pin", headers=w.h("heir2"))
    assert refused.status_code == 422, refused.text
    assert "3 of this fleet's pins" in refused.text
    assert (
        await w.client.put(f"/v1/entries/{entries[3].id}/pin", headers=w.h("peer"))
    ).status_code == 200
    # heir2 unpins an inherited pin and may pin again.
    assert (
        await w.client.delete(f"/v1/entries/{entries[0].id}/pin", headers=w.h("heir2"))
    ).status_code == 200
    assert (
        await w.client.put(f"/v1/entries/{entries[4].id}/pin", headers=w.h("heir2"))
    ).status_code == 200


# -- chains and cycles ------------------------------------------------------------------


async def test_a_chain_gives_the_last_successor_every_earlier_agents_memories(w: World) -> None:
    await w.agent("c", w.blue)
    a_note = await w.entry("old", "a note")
    b_note = await w.entry("heir", "b note")
    await retire(w, "old", "heir")  # old -> heir
    await retire(w, "heir", "c")  # later heir -> c
    assert await w.store.inherited_authors("c") == ("heir", "old")
    assert await w.get("c", a_note.id) == 200
    assert await w.get("c", b_note.id) == 200
    assert await w.get("other", a_note.id) == 404
    assert await w.get("heir", b_note.id) == 401  # heir is retired too
    # Undoing the first link takes only that agent's memories from the chain.
    await w.successions.undo("old", ADMIN)
    assert await w.get("c", a_note.id) == 404
    assert await w.get("c", b_note.id) == 200


async def test_a_cycle_is_refused(w: World) -> None:
    await retire(w, "old", "heir")
    # Re-activate old behind the guard's back (an older pod could): old would
    # then be the successor of the agent that hands down to it.
    store = w.store
    store._agents["old"] = replace(store._agents["old"], status=AgentStatus.ACTIVE)
    with pytest.raises(SuccessionRefused, match="cycle"):
        await w.successions.retire("heir", "old", ADMIN)
    with pytest.raises(SuccessionRefused, match="cycle"):
        await w.successions.preview("heir", "old", ADMIN)
    assert await store.get_succession("heir") is None
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    # A longer loop: c -> d, then d -> c.
    await w.agent("c", w.blue)
    await w.agent("d", w.blue)
    await retire(w, "c", "d")
    store._agents["c"] = replace(store._agents["c"], status=AgentStatus.ACTIVE)
    with pytest.raises(SuccessionRefused, match="cycle"):
        await w.successions.retire("d", "c", ADMIN)


# -- the MCP surface and whoami ----------------------------------------------------------


async def test_mcp_tools_apply_the_same_ownership(w: World) -> None:
    from hivemind.mcp.app import McpHivemind, hive_get, hive_whoami, hive_withdraw
    from hivemind.services.governance import GovernanceService
    from hivemind.services.read import ReadService
    from hivemind.services.search import SearchService
    from hivemind.services.write import WriteService
    from tests.fakes import make_clock

    note = await w.entry("old", "old private note")
    await retire(w)

    def mcp_for(name: str, inherits: tuple[str, ...]) -> McpHivemind:
        gov = GovernanceService(w.store)
        config = make_search_config()
        return McpHivemind(
            store=w.store,
            write_service=WriteService(w.store, make_embedder()),
            search_service=SearchService(w.store, make_embedder(), config, now_fn=make_clock()),
            governance_service=gov,
            read_service=ReadService(w.store, gov, config),
            access_service=w.app.access_service,
            search_config=config,
            credential=Credential(
                user_id=name,
                agent_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=TrustLevel.CONTRIBUTOR,
                home_fleet_id=w.blue,
                inherits_from=inherits,
            ),
        )

    heir, stranger = mcp_for("heir", ("old",)), mcp_for("other", ())
    assert (await hive_whoami(heir))["inherits_from"] == ["old"]
    assert (await hive_whoami(stranger))["inherits_from"] == []
    assert (await hive_get(heir, note.id))["author"] == "old"
    hidden = await hive_get(stranger, note.id)
    assert hidden.get("author") is None and "error" in str(hidden).lower()
    assert (await hive_withdraw(heir, note.id, "obsolete"))["state"] == "withdrawn"


async def test_whoami_lists_the_agents_that_hand_down_to_the_caller(w: World) -> None:
    await retire(w)
    resp = await w.client.get("/v1/whoami", headers=w.h("heir"))
    assert resp.json()["inherits_from"] == ["old"]
    assert (await w.client.get("/v1/whoami", headers=w.h("other"))).json()["inherits_from"] == []
    admin = await w.client.get("/v1/whoami", headers={"X-API-Key": "hm_admin"})
    assert admin.json()["inherits_from"] == []


# -- the service: preview, retire, undo, refusals ----------------------------------------


async def test_the_preview_counts_what_passes_over_and_changes_nothing(w: World) -> None:
    await w.entry("old", "private one")
    await w.entry("old", "private two")
    # A fleet write must be in the author's home fleet (ADR 0112): old wrote in
    # blue while blue was its home, then moved to red.
    await w.store.set_agent_home_fleet("old", w.blue)
    await w.entry("old", "blue fact", scope="fleet", fleet=w.blue)
    await w.store.set_agent_home_fleet("old", w.red)
    in_red = await w.entry("old", "red fact", scope="fleet", fleet=w.red)
    done = await w.entry("old", "withdrawn private")
    await w.store.withdraw_entry(done.id, "gone", by_user="old")
    await w.store.pin_entry(w.red, in_red.id, "old", 10)
    await w.entry("other", "someone else's note")
    preview = await w.successions.preview("old", "heir", ADMIN)
    facts = preview.handed_down
    assert (facts.self_notes, facts.fleet_memories, facts.pins) == (2, 2, 1)
    assert facts.fleet_memories_elsewhere == 1  # the red one: heir's home is blue
    assert facts.authors == ("old",)
    assert preview.successor_owns == ("heir", "old")
    assert preview.revokes is True
    assert await w.store.list_successions() == []
    assert (await w.store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert await w.audit(AuditAction.AGENT_RETIRE) == []


async def test_the_preview_of_a_chain_counts_every_agent_that_passes_over(w: World) -> None:
    await w.agent("c", w.blue)
    await w.entry("old", "a note")
    await w.entry("heir", "b note")
    await retire(w, "old", "heir")
    preview = await w.successions.preview("heir", "c", ADMIN)
    assert preview.handed_down.authors == ("heir", "old")
    assert preview.handed_down.self_notes == 2
    assert preview.successor_owns == ("c", "heir", "old")


async def test_retire_records_the_succession_revokes_the_agent_and_audits(w: World) -> None:
    succession = await w.successions.retire("old", "heir", ADMIN)
    assert (succession.retired_agent, succession.successor) == ("old", "heir")
    assert succession.by == ADMIN.audit_actor() == "admin:abcdef012345"
    assert (await w.store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    (retire_row,) = await w.audit(AuditAction.AGENT_RETIRE)
    assert retire_row.target == "old" and retire_row.detail["successor"] == "heir"
    (revoke_row,) = await w.audit(AuditAction.AGENT_REVOKE)  # the revoke is audited as usual
    assert revoke_row.target == "old"
    assert revoke_row.detail["via"] == "retire" and revoke_row.detail["from"] == "active"


async def test_retire_deletes_the_retired_agents_key(w: World) -> None:
    key = await w.auth.issue_agent_key("old")
    assert await w.auth.verify(key) is not None
    await retire(w)
    assert await w.auth.verify(key) is None


async def test_retiring_an_already_revoked_agent_still_hands_its_memories_down(w: World) -> None:
    note = await w.entry("old", "old note")
    await w.app.access_service.revoke("old", ADMIN)
    await retire(w)
    assert await w.get("heir", note.id) == 200
    assert await w.audit(AuditAction.AGENT_REVOKE) != []  # the earlier explicit revoke only
    assert len(await w.audit(AuditAction.AGENT_REVOKE)) == 1


async def test_refusals(w: World) -> None:
    await w.store.register_agent("waiting")  # pending
    with pytest.raises(InvalidInput):
        await w.successions.retire("old", "old", ADMIN)
    with pytest.raises(UnknownAgent):
        await w.successions.retire("ghost", "heir", ADMIN)
    with pytest.raises(UnknownAgent):
        await w.successions.retire("old", "ghost", ADMIN)
    with pytest.raises(InvalidAgentStatus, match="make a successor of"):
        await w.successions.retire("old", "waiting", ADMIN)
    with pytest.raises(InvalidAgentStatus, match="retire"):
        await w.successions.retire("waiting", "heir", ADMIN)
    await w.app.access_service.revoke("peer", ADMIN)
    with pytest.raises(InvalidAgentStatus):  # a revoked successor
        await w.successions.retire("old", "peer", ADMIN)
    assert await w.store.list_successions() == []
    assert (await w.store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert await w.audit(AuditAction.AGENT_RETIRE) == []
    await retire(w)
    with pytest.raises(SuccessionRefused, match="already retired"):
        await w.successions.retire("old", "other", ADMIN)
    with pytest.raises(SuccessionRefused, match="already retired"):
        await w.successions.preview("old", "other", ADMIN)


async def test_the_refused_preview_matches_the_refused_retire(w: World) -> None:
    for retired, successor in (("old", "old"), ("ghost", "heir"), ("old", "ghost")):
        with pytest.raises((InvalidInput, UnknownAgent)) as a:
            await w.successions.preview(retired, successor, ADMIN)
        with pytest.raises((InvalidInput, UnknownAgent)) as b:
            await w.successions.retire(retired, successor, ADMIN)
        assert type(a.value) is type(b.value)


async def test_only_an_admin_key_may_retire_preview_undo_or_list(w: World) -> None:
    agent_key = Credential(user_id="heir", agent_name="heir", access_controlled=True)
    for bad in (agent_key, Credential(user_id="org", is_org=True)):
        with pytest.raises(PermissionDenied):
            await w.successions.retire("old", "heir", bad)
        with pytest.raises(PermissionDenied):
            await w.successions.preview("old", "heir", bad)
        with pytest.raises(PermissionDenied):
            await w.successions.undo("old", bad)
        with pytest.raises(PermissionDenied):
            await w.successions.list_successions(bad)
    assert await w.store.list_successions() == []


async def test_undo_deletes_the_succession_audits_and_allows_a_new_one(w: World) -> None:
    await retire(w)
    undone = await w.successions.undo("old", ADMIN)
    assert undone.successor == "heir"
    assert await w.store.list_successions() == []
    (row,) = await w.audit(AuditAction.AGENT_RETIRE_UNDO)
    assert row.target == "old" and row.detail["successor"] == "heir"
    with pytest.raises(UnknownSuccession):
        await w.successions.undo("old", ADMIN)
    # The agent stays revoked; with the succession gone it may be activated again.
    assert (await w.store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    await w.store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=w.red)
    await w.successions.retire("old", "other", ADMIN)  # and retired to someone else
    assert [s.successor for s in await w.store.list_successions()] == ["other"]


async def test_a_retired_agent_cannot_be_activated_until_the_retirement_is_undone(
    w: World,
) -> None:
    await retire(w)
    with pytest.raises(AgentRetired, match="undo the retirement"):
        await w.store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=w.red)
    with pytest.raises(AgentRetired):
        await w.app.access_service.activate("old", TrustLevel.LURKER, w.red, ADMIN)
    assert (await w.store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    await w.successions.undo("old", ADMIN)
    await w.store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=w.red)


async def test_listing_is_newest_first(w: World) -> None:
    await retire(w, "old", "heir")
    await retire(w, "peer", "other")
    assert [s.retired_agent for s in await w.successions.list_successions(ADMIN)] == [
        "peer",
        "old",
    ]


# -- the admin routes ------------------------------------------------------------------


ADMIN_H = {"X-API-Key": "hm_admin"}


async def test_the_retire_route_previews_then_retires(w: World) -> None:
    await w.entry("old", "note")
    in_red = await w.entry("old", "red", scope="fleet", fleet=w.red)
    await w.store.pin_entry(w.red, in_red.id, "old", 10)
    async with w.client as c:
        preview = await c.post(
            "/v1/admin/agents/old/retire",
            json={"successor": "heir", "dry_run": True},
            headers=ADMIN_H,
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["dry_run"] is True
        assert (body["self_notes"], body["fleet_memories"], body["pins"]) == (1, 1, 1)
        assert body["fleet_memories_elsewhere"] == 1
        assert body["passes_over_from"] == ["old"] and body["successor_owns"] == ["heir", "old"]
        assert body["revokes_retired"] is True
        assert (await c.get("/v1/admin/successions", headers=ADMIN_H)).json() == []
        done = await c.post(
            "/v1/admin/agents/old/retire",
            json={"successor": "heir", "dry_run": False},
            headers=ADMIN_H,
        )
        assert done.status_code == 200, done.text
        assert done.json()["dry_run"] is False and done.json()["successor"] == "heir"
        listed = (await c.get("/v1/admin/successions", headers=ADMIN_H)).json()
        assert [(s["retired_agent"], s["successor"]) for s in listed] == [("old", "heir")]
        undone = await c.post("/v1/admin/successions/old/undo", headers=ADMIN_H)
        assert undone.status_code == 200 and undone.json()["retired_agent"] == "old"
        assert (await c.get("/v1/admin/successions", headers=ADMIN_H)).json() == []


async def test_the_retire_routes_answer_typed_errors(w: World) -> None:
    await w.store.register_agent("waiting")
    path = "/v1/admin/agents/{}/retire"

    async def post(name: str, successor: str, dry_run: bool = False) -> httpx.Response:
        return await w.client.post(
            path.format(name), json={"successor": successor, "dry_run": dry_run}, headers=ADMIN_H
        )

    async with w.client:
        for dry in (True, False):
            unknown = await post("ghost", "heir", dry)
            assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
            unknown_successor = await post("old", "ghost", dry)
            assert unknown_successor.status_code == 404
            pending = await post("old", "waiting", dry)
            assert (pending.status_code, pending.json()["error"]["code"]) == (409, "invalid_status")
            same = await post("old", "old", dry)
            assert (same.status_code, same.json()["error"]["code"]) == (422, "invalid_input")
        assert (await post("old", "heir")).status_code == 200
        again = await post("old", "other")
        assert (again.status_code, again.json()["error"]["code"]) == (409, "succession_refused")
        gone = await w.client.post("/v1/admin/successions/nobody/undo", headers=ADMIN_H)
        assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")
        activate = await w.client.post(
            "/v1/admin/agents/old/activate",
            json={"trust_level": 1, "home_fleet_id": w.red},
            headers=ADMIN_H,
        )
        assert (activate.status_code, activate.json()["error"]["code"]) == (409, "agent_retired")


async def test_non_admin_keys_get_403_before_anything_is_looked_up(w: World) -> None:
    for headers in (w.h("heir"), {"X-API-Key": "hm_org"}):
        for method, url, body in (
            ("POST", "/v1/admin/agents/ghost/retire", {"successor": "ghost", "dry_run": True}),
            ("POST", "/v1/admin/agents/old/retire", {"successor": "heir", "dry_run": False}),
            ("GET", "/v1/admin/successions", None),
            ("POST", "/v1/admin/successions/ghost/undo", None),
        ):
            resp = await w.client.request(method, url, json=body, headers=headers)
            assert resp.status_code == 403, (url, resp.text)
            assert resp.json()["error"]["code"] == "forbidden"
    assert await w.store.list_successions() == []
    assert (await w.store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]


async def test_a_successors_withdrawal_of_an_inherited_memory_is_audited(w: World) -> None:
    inherited = await w.entry("old", "old note")
    own = await w.entry("heir", "heir note")
    await retire(w)
    assert (await w.withdraw("heir", own.id)).status_code == 200
    assert await w.audit(AuditAction.ENTRY_WITHDRAW) == []  # its own: unaudited, as before
    assert (await w.withdraw("heir", inherited.id)).status_code == 200
    (row,) = await w.audit(AuditAction.ENTRY_WITHDRAW)
    assert row.target == inherited.id and row.actor == "heir"
    assert row.detail == {"author": "old", "reason": "obsolete", "via": "succession"}


async def test_the_revoke_audit_follows_the_status_seen_under_the_lock(w: World) -> None:
    succession = await w.store.retire_agent("old", "heir", "x")
    assert succession.was is AgentStatus.ACTIVE
    await w.app.access_service.revoke("peer", ADMIN)
    assert (await w.store.retire_agent("peer", "other", "x")).was is AgentStatus.REVOKED
