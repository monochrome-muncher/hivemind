"""Moving agents with their memories to another fleet, and undoing it (ADR 0112),
through the service seam over ``MemoryStore``, plus the REST routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import (
    FleetBusy,
    FleetExists,
    FleetMerged,
    TrustLevel,
    UnknownFleet,
    Visibility,
)
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.fleet_merge import (
    KIND_MOVE,
    FleetMergeRefused,
    FleetMoveRefused,
    UnknownFleetMerge,
)
from hivemind.domain.pin import PinRefusal
from hivemind.domain.validation import InvalidInput
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.errors import PermissionDenied
from tests.fakes import FakeAuthenticator, make_embedder, make_search_config

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
AGENT = Credential(user_id="dev", agent_name="dev", access_controlled=True)
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
VEC = [1.0, 0.0, 0.0, 0.0]


class TickingClock:
    """Every reading is one second later, so pins and moves order strictly."""

    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(TickingClock())
        self.access = AccessService(self.store)
        self.merges = self.access.merges

    async def fleet(self, name: str) -> str:
        return (await self.store.create_fleet(name)).id

    async def entry(
        self,
        fleet_id: str | None,
        summary: str,
        author: str,
        *,
        scope: str = "fleet",
        supersedes: tuple[str, ...] = (),
    ) -> str:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope=scope,
            fleet_id=fleet_id,
            supersedes=supersedes,
        )
        return (await self.store.create_entry(draft, VEC)).id

    async def pin(self, fleet_id: str, summary: str, author: str, by: str | None = None) -> str:
        entry_id = await self.entry(fleet_id, summary, author)
        written = await self.store.pin_entry(fleet_id, entry_id, by or f"by-{summary}", 100)
        assert not isinstance(written, PinRefusal)
        return entry_id

    async def agent(self, name: str, fleet_id: str, level: TrustLevel = TrustLevel.CONTRIBUTOR):  # type: ignore[no-untyped-def]
        await self.store.register_agent(name)
        await self.store.activate_agent(name, trust_level=level, home_fleet_id=fleet_id)

    async def fleet_of(self, entry_id: str) -> str | None:
        entry = await self.store.get_entry(entry_id)
        assert entry is not None
        return entry.fleet_id

    async def home_of(self, name: str) -> str | None:
        agent = await self.store.get_agent(name)
        assert agent is not None
        return agent.home_fleet_id

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)


@pytest.fixture
def w() -> World:
    return World()


async def fleet_with_agents(w: World) -> tuple[str, str]:
    """Fleet ``a`` with agents amy, ann (movers in most tests) and bob; an empty fleet ``b``."""
    a, b = await w.fleet("a"), await w.fleet("b")
    for name in ("amy", "ann", "bob"):
        await w.agent(name, a)
    return a, b


# -- the move ----------------------------------------------------------------------------


async def test_a_move_takes_the_agents_and_exactly_the_fleet_memories_they_wrote(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    live = await w.entry(a, "amy live", "amy")
    old = await w.entry(a, "amy old", "amy")
    await w.entry(a, "amy v2", "amy", supersedes=(old,))
    gone = await w.entry(a, "ann gone", "ann")
    await w.store.withdraw_entry(gone, "x", "ann")
    bobs = await w.entry(a, "bob's", "bob")
    amy_self = await w.entry(None, "amy self", "amy", scope="self")
    amy_org = await w.entry(None, "amy org", "amy", scope="org")
    theirs = await w.entry(b, "b's own", "bea")
    await w.store.record_search(a, empty=True)

    out = await w.merges.move(a, ["amy", "ann"], ADMIN, target_fleet_id=b)

    assert out.record.kind == KIND_MOVE and not out.target_created
    assert out.entries_moved == 4 and out.entries_held_back == 0 and out.agents_moved == 2
    for entry_id in (live, old, gone):
        assert await w.fleet_of(entry_id) == b  # any state
    assert await w.fleet_of(bobs) == a  # a stayer's memory stays
    assert await w.fleet_of(theirs) == b
    assert await w.fleet_of(amy_self) is None and await w.fleet_of(amy_org) is None
    assert [await w.home_of(n) for n in ("amy", "ann", "bob")] == [b, b, a]
    source = await w.store.get_fleet(a)
    assert source is not None and source.merged_into is None  # not marked, unlike a merge
    assert [c.searches for c in await w.store.search_counts() if c.fleet_id == a] == [1]  # stay
    assert [f.id for f in await w.access.list_fleets(ADMIN)] == [a, b]


async def test_a_move_into_a_new_fleet_creates_it_and_audits_both(w: World) -> None:
    a, _ = await fleet_with_agents(w)
    await w.entry(a, "one", "amy")
    out = await w.merges.move(a, ["amy"], ADMIN, new_fleet_name="split")
    assert out.target_created and out.target.name == "split" and out.entries_moved == 1
    assert await w.home_of("amy") == out.target.id
    (created,) = await w.audit(AuditAction.FLEET_CREATE)
    assert created.target == out.target.id and created.detail["via"] == "fleet_move"
    (moved,) = await w.audit(AuditAction.FLEET_MOVE_AGENTS)
    assert moved.target == a and moved.actor == "admin:abcdef012345"
    assert dict(moved.detail) == {
        "move_id": out.record.id,
        "target_fleet_id": out.target.id,
        "target_fleet_name": "split",
        "created_target": True,
        "agents": ["amy"],
        "entries": 1,
        "entries_held_back": 0,
        "pins_kept": 0,
        "pins_dropped": 0,
    }


async def test_moving_every_agent_leaves_the_source_a_normal_fleet(w: World) -> None:
    a, b = await fleet_with_agents(w)
    kept = await w.entry(a, "orphan", "nobody")  # a fleet memory of an author who is no mover
    await w.merges.move(a, ["amy", "ann", "bob"], ADMIN, target_fleet_id=b)
    assert {await w.home_of(n) for n in ("amy", "ann", "bob")} == {b}
    assert await w.fleet_of(kept) == a
    await w.agent("cat", a)  # the source still takes members and memories
    await w.entry(a, "again", "cat")


async def test_the_mover_reads_the_moved_memories_and_the_stayer_no_longer_does(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    mine = await w.entry(a, "mine", "amy")
    bobs = await w.entry(a, "bobs", "bob")
    await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)

    async def seen(name: str) -> set[str]:
        agent = await w.store.get_agent(name)
        assert agent is not None
        vis = Visibility(level=agent.trust_level, name=name, home_fleet_id=agent.home_fleet_id)
        return {e.id for e in await w.store.list_entries(EntryFilters(), visibility=vis)}

    assert await seen("amy") == {mine}
    assert await seen("bob") == {bobs}


# -- the chain rule ----------------------------------------------------------------------


async def test_a_memory_moves_only_when_its_author_and_the_head_of_its_chain_move(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    # amy's v1, superseded by bob: the head stays, so v1 stays.
    v1 = await w.entry(a, "amy v1", "amy")
    head_bob = await w.entry(a, "bob v2", "bob", supersedes=(v1,))
    # bob's v1 superseded by amy: amy's head moves, bob's old version is not amy's.
    bob_old = await w.entry(a, "bob old", "bob")
    amy_head = await w.entry(a, "amy head", "amy", supersedes=(bob_old,))
    # amy v1 -> bob v2 -> amy v3: the head is amy's, so amy's v1 moves, bob's v2 stays.
    c1 = await w.entry(a, "c1", "amy")
    c2 = await w.entry(a, "c2", "bob", supersedes=(c1,))
    c3 = await w.entry(a, "c3", "amy", supersedes=(c2,))
    # amy v1 -> ann v2 (both movers): both move.
    d1 = await w.entry(a, "d1", "amy")
    d2 = await w.entry(a, "d2", "ann", supersedes=(d1,))

    preview = await w.merges.preview_move(a, ["amy", "ann"], ADMIN, target_fleet_id=b)
    assert (preview.leaving, preview.held_back) == (5, 1)  # amy_head, c1, c3, d1, d2; held: v1
    out = await w.merges.move(a, ["amy", "ann"], ADMIN, target_fleet_id=b)

    assert (out.entries_moved, out.entries_held_back) == (5, 1)
    for moved in (amy_head, c1, c3, d1, d2):
        assert await w.fleet_of(moved) == b
    for stayed in (v1, head_bob, bob_old, c2):
        assert await w.fleet_of(stayed) == a


async def test_a_chain_that_ends_in_another_fleet_stays(w: World) -> None:
    """After an earlier move or an undo a chain can cross fleets; the old
    version of a mover stays when its head is not in the source fleet."""
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    old = await w.entry(a, "v1", "amy")
    head = await w.entry(c, "v2", "cal", supersedes=(old,))  # the head lives in c
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    assert (out.entries_moved, out.entries_held_back) == (0, 1)
    assert await w.fleet_of(old) == a and await w.fleet_of(head) == c


# -- the preview -------------------------------------------------------------------------


async def test_preview_says_how_many_leave_who_stays_and_changes_nothing(w: World) -> None:
    a, b = await fleet_with_agents(w)
    await w.entry(a, "m1", "amy")
    gone = await w.entry(a, "m2", "amy")
    await w.store.withdraw_entry(gone, "x", "amy")
    await w.entry(a, "bob's", "bob")
    await w.entry(b, "b's", "bea")

    preview = await w.merges.preview_move(a, ["amy", "amy", "ann"], ADMIN, target_fleet_id=b)

    assert preview.agents == ("amy", "ann") and preview.staying_agents == ("bob",)
    assert preview.leaving == 2 and dict(preview.leaving_by_state) == {"active": 1, "withdrawn": 1}
    assert preview.held_back == 0
    assert preview.source.name == "a" and preview.source.facts.entries == {
        "active": 2,
        "withdrawn": 1,
    }
    assert preview.target.name == "b" and preview.target.facts.entries == {"active": 1}
    assert not preview.needs_pin_choice and preview.pins == ()
    assert await w.home_of("amy") == a  # nothing changed
    assert await w.store.list_fleet_merges(10) == []
    assert await w.audit(AuditAction.FLEET_MOVE_AGENTS) == []


async def test_preview_to_a_new_fleet_has_an_empty_target(w: World) -> None:
    a, _ = await fleet_with_agents(w)
    preview = await w.merges.preview_move(a, ["amy"], ADMIN, new_fleet_name="split")
    assert preview.target.fleet is None and preview.target.name == "split"
    assert preview.target.facts.agents == 0


# -- pins ----------------------------------------------------------------------------------


async def test_pins_follow_their_memory_and_the_rest_stay(w: World) -> None:
    a, b = await fleet_with_agents(w)
    mine = await w.pin(a, "amy pinned", "amy")
    bobs = await w.pin(a, "bob pinned", "bob")
    theirs = await w.pin(b, "b pinned", "bea")
    # A stayer's pin on a mover's memory moves with the memory.
    cross = await w.entry(a, "amy, pinned by bob", "amy")
    await w.store.pin_entry(a, cross, "bob", 100)

    preview = await w.merges.preview_move(a, ["amy"], ADMIN, target_fleet_id=b)
    assert {p.pin.entry_id for p in preview.pins} == {mine, theirs, cross}  # not bob's
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)

    assert len(out.pins_kept) == 3 and out.pins_dropped == ()
    assert {p.entry_id for p in await w.store.list_pins(a)} == {bobs}
    assert {p.entry_id for p in await w.store.list_pins(b)} == {mine, theirs, cross}


async def test_over_ten_pins_need_a_choice_and_the_rest_are_unpinned_and_audited(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    mine = [await w.pin(a, f"a{i}", "amy") for i in range(6)]
    theirs = [await w.pin(b, f"b{i}", "bea") for i in range(6)]
    bobs = await w.pin(a, "bob", "bob")  # not in play

    preview = await w.merges.preview_move(a, ["amy"], ADMIN, target_fleet_id=b)
    assert preview.needs_pin_choice and len(preview.pins) == 12
    assert sum(p.default_kept for p in preview.pins) == 10  # the newest ten
    with pytest.raises(InvalidInput, match="keep_pin_ids"):
        await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    shown = [p.pin.entry_id for p in preview.pins]
    with pytest.raises(FleetMoveRefused, match="preview again"):
        await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b, expected_pin_ids=shown[:-1])
    keep = [*mine[:5], *theirs[:5]]
    out = await w.merges.move(
        a, ["amy"], ADMIN, target_fleet_id=b, keep_pin_ids=keep, expected_pin_ids=shown
    )

    assert {p.entry_id for p in out.pins_dropped} == {mine[5], theirs[5]}
    assert {p.entry_id for p in await w.store.list_pins(b)} == set(keep)
    assert {p.entry_id for p in await w.store.list_pins(a)} == {bobs}
    unpins = await w.audit(AuditAction.ENTRY_UNPIN)
    assert {r.target for r in unpins} == {mine[5], theirs[5]}
    assert all(r.detail["reason"] == "fleet_move" for r in unpins)
    assert (await w.audit(AuditAction.FLEET_MOVE_AGENTS))[0].detail["pins_dropped"] == 2
    with pytest.raises(InvalidInput):
        await w.merges.move(a, ["ann"], ADMIN, target_fleet_id=b, keep_pin_ids=["nope"])


# -- refusals ------------------------------------------------------------------------------


async def test_refusals_change_nothing(w: World) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    await w.agent("cy", c)
    await w.entry(a, "m", "amy")
    nothing = await w.store.list_fleet_merges(10)

    with pytest.raises(FleetMoveRefused, match="cy, ghost"):  # named, sorted
        await w.merges.move(a, ["amy", "ghost", "cy"], ADMIN, target_fleet_id=b)
    with pytest.raises(FleetMoveRefused):
        await w.merges.preview_move(a, ["cy"], ADMIN, target_fleet_id=b)
    with pytest.raises(FleetMoveRefused, match="they are in"):
        await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=a)
    with pytest.raises(InvalidInput, match="at least one agent"):
        await w.merges.move(a, [], ADMIN, target_fleet_id=b)
    with pytest.raises(InvalidInput, match="exactly one"):
        await w.merges.move(a, ["amy"], ADMIN)
    with pytest.raises(InvalidInput, match="exactly one"):
        await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b, new_fleet_name="n")
    with pytest.raises(InvalidInput):
        await w.merges.move(a, ["amy"], ADMIN, new_fleet_name="   ")
    with pytest.raises(FleetExists):
        await w.merges.move(a, ["amy"], ADMIN, new_fleet_name="b")
    with pytest.raises(FleetExists):
        await w.merges.preview_move(a, ["amy"], ADMIN, new_fleet_name="b")
    with pytest.raises(UnknownFleet):
        await w.merges.move(
            "00000000-0000-4000-8000-000000000001", ["amy"], ADMIN, target_fleet_id=b
        )
    with pytest.raises(UnknownFleet):
        await w.merges.move(
            a, ["amy"], ADMIN, target_fleet_id="00000000-0000-4000-8000-000000000001"
        )
    with pytest.raises(UnknownFleet):
        await w.merges.move("not-a-uuid", ["amy"], ADMIN, target_fleet_id=b)
    assert await w.home_of("amy") == a and await w.store.list_fleet_merges(10) == nothing
    assert await w.audit(AuditAction.FLEET_CREATE) == []


async def test_a_merged_fleet_is_refused_as_source_or_target(w: World) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    await w.agent("cy", c)
    await w.merges.merge(c, ADMIN, target_fleet_id=b)  # c merged into b
    with pytest.raises(FleetMerged):
        await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=c)
    with pytest.raises(FleetMerged):
        await w.merges.move(c, ["cy"], ADMIN, target_fleet_id=a)
    with pytest.raises(FleetMerged):
        await w.merges.preview_move(a, ["amy"], ADMIN, target_fleet_id=c)
    assert await w.home_of("amy") == a


async def test_only_an_admin_may_move_or_preview(w: World) -> None:
    a, b = await fleet_with_agents(w)
    for cred in (AGENT, Credential(user_id="org")):
        with pytest.raises(PermissionDenied):
            await w.merges.move(a, ["amy"], cred, target_fleet_id=b)
        with pytest.raises(PermissionDenied):
            await w.merges.preview_move(a, ["amy"], cred, target_fleet_id=b)
    assert await w.home_of("amy") == a


async def test_a_write_in_flight_for_a_moved_author_is_not_filed_in_the_old_fleet(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    stale = EntryDraft(
        kind=Kind.FACT, summary="late", author="amy", agent="amy", scope="fleet", fleet_id=a
    )
    with pytest.raises(FleetBusy, match="home fleet of amy changed"):
        await w.store.create_entry(stale, VEC)
    fresh = EntryDraft(
        kind=Kind.FACT, summary="late", author="amy", agent="amy", scope="fleet", fleet_id=b
    )
    assert (await w.store.create_entry(fresh, VEC)).fleet_id == b


# -- undo ------------------------------------------------------------------------------------


async def test_undo_restores_exactly_what_the_move_moved(w: World) -> None:
    a, b = await fleet_with_agents(w)
    e1 = await w.entry(a, "a1", "amy")
    bobs = await w.entry(a, "bob", "bob")
    b1 = await w.entry(b, "b1", "bea")
    pa = await w.pin(a, "pa", "amy")
    ids = [await w.pin(b, f"pb{i}", "bea") for i in range(10)]
    out = await w.merges.move(
        a, ["amy", "ann"], ADMIN, target_fleet_id=b, keep_pin_ids=[pa, *ids[1:]]
    )
    assert [p.entry_id for p in out.pins_dropped] == [ids[0]]

    # After the move: a new memory in the target, and ann moves on.
    late = await w.entry(b, "late", "amy")
    c = await w.fleet("c")
    await w.store.set_agent_home_fleet("ann", c)

    undone = await w.merges.undo(out.record.id, ADMIN)

    assert undone.record.kind == KIND_MOVE and undone.entries_restored == 2  # e1, pa's entry
    assert await w.fleet_of(e1) == a and await w.fleet_of(pa) == a
    assert await w.fleet_of(bobs) == a and await w.fleet_of(b1) == b  # untouched
    assert await w.fleet_of(late) == b  # post-move memories stay
    assert undone.agents_restored == ("amy",)
    assert await w.home_of("amy") == a and await w.home_of("ann") == c  # ann moved since
    assert {p.entry_id for p in await w.store.list_pins(a)} == {pa}  # the pin went back
    assert {p.entry_id for p in await w.store.list_pins(b)} == set(ids)  # dropped pin restored
    source = await w.store.get_fleet(a)
    assert source is not None and source.merged_into is None
    (row,) = await w.audit(AuditAction.FLEET_MOVE_UNDO)
    assert row.target == a and row.detail["move_id"] == out.record.id
    assert row.detail["entries"] == 2 and row.detail["agents"] == 1
    assert await w.audit(AuditAction.FLEET_UNMERGE) == []
    (record,) = await w.store.list_fleet_merges(10)
    assert record.undone_at is not None
    with pytest.raises(FleetMoveRefused, match="already undone"):
        await w.merges.undo(out.record.id, ADMIN)


async def test_undo_leaves_a_successor_written_after_the_move(w: World) -> None:
    a, b = await fleet_with_agents(w)
    v1 = await w.entry(a, "v1", "amy")
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    v2 = await w.entry(b, "v2", "amy", supersedes=(v1,))
    await w.merges.undo(out.record.id, ADMIN)
    assert await w.fleet_of(v1) == a and await w.fleet_of(v2) == b


async def test_a_move_can_be_undone_only_while_it_is_the_newest_standing_one_on_either_fleet(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    await w.agent("cy", c)
    await w.agent("dee", c)
    first = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)

    async def blocked(move_id: str) -> str | None:
        view = next(v for v in await w.merges.list_merges(ADMIN) if v.record.id == move_id)
        return view.blocked

    # A newer move out of the same target blocks it ...
    second = await w.merges.move(b, ["amy"], ADMIN, target_fleet_id=c)
    assert "newer" in (await blocked(first.record.id) or "")
    with pytest.raises(FleetMoveRefused, match="newer"):
        await w.merges.undo(first.record.id, ADMIN)
    await w.merges.undo(second.record.id, ADMIN)
    assert await blocked(first.record.id) is None

    # ... so does a newer move out of the same SOURCE (it was a source of ``first``) ...
    third = await w.merges.move(a, ["ann"], ADMIN, target_fleet_id=c)
    assert "newer" in (await blocked(first.record.id) or "")
    await w.merges.undo(third.record.id, ADMIN)

    # ... and a newer merge of the target into somewhere else.
    merge = await w.merges.merge(b, ADMIN, target_fleet_id=c)
    with pytest.raises(FleetMoveRefused):
        await w.merges.undo(first.record.id, ADMIN)
    await w.merges.undo(merge.record.id, ADMIN)
    await w.merges.undo(first.record.id, ADMIN)
    assert await w.home_of("amy") == a


async def test_a_move_is_blocked_while_its_source_or_target_is_merged_away(w: World) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    first = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    await w.merges.merge(a, ADMIN, target_fleet_id=c)  # the source is merged away afterwards
    with pytest.raises(FleetMoveRefused):
        await w.merges.undo(first.record.id, ADMIN)


async def test_an_older_merge_is_blocked_by_a_newer_move_into_its_target(w: World) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    await w.agent("cy", c)
    merged = await w.merges.merge(c, ADMIN, target_fleet_id=b)
    moved = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    with pytest.raises(FleetMergeRefused, match="newer"):
        await w.merges.undo(merged.record.id, ADMIN)
    assert not isinstance(
        FleetMergeRefused("x"), FleetMoveRefused
    )  # a merge's refusal stays a merge refusal
    await w.merges.undo(moved.record.id, ADMIN)
    await w.merges.undo(merged.record.id, ADMIN)
    assert await w.home_of("cy") == c


async def test_the_history_lists_merges_and_moves_with_their_kind(w: World) -> None:
    a, b = await fleet_with_agents(w)
    c = await w.fleet("c")
    moved = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    merged = await w.merges.merge(c, ADMIN, target_fleet_id=b)
    views = await w.merges.list_merges(ADMIN)
    assert [(v.record.id, v.record.kind) for v in views] == [
        (merged.record.id, "merge"),
        (moved.record.id, "move"),
    ]
    assert (views[1].source_name, views[1].target_name) == ("a", "b")
    with pytest.raises(UnknownFleetMerge):
        await w.merges.undo("00000000-0000-4000-8000-000000000001", ADMIN)


# -- REST ------------------------------------------------------------------------------------


@pytest.fixture
async def rest():  # type: ignore[no-untyped-def]
    import httpx

    store = MemoryStore(TickingClock())
    authenticator = FakeAuthenticator(
        agent_credentials={
            "k-dev": Credential(
                user_id="dev",
                agent_name="dev",
                access_controlled=True,
                trust_level=TrustLevel.PRIVILEGED,
                home_fleet_id="x",
            )
        }
    )
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
    yield client, store
    await client.aclose()


ADMIN_H = {"X-API-Key": "hm_admin"}


async def test_rest_preview_move_list_and_undo(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    b = (await store.create_fleet("b")).id
    for name in ("amy", "bob"):
        await store.register_agent(name)
        await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)
    mine = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="mine", author="amy", agent="amy", scope="fleet", fleet_id=a
        ),
        VEC,
    )

    resp = await client.post(
        f"/v1/admin/fleets/{a}/move",
        json={"agents": ["amy"], "target_fleet_id": b, "dry_run": True},
        headers=ADMIN_H,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["leaving"] == 1 and body["held_back"] == 0
    assert body["agents"] == ["amy"] and body["staying_agents"] == ["bob"]
    assert body["source"]["name"] == "a" and body["target"]["name"] == "b"
    assert body["leaving_by_state"] == {"active": 1} and body["needs_pin_choice"] is False

    resp = await client.post(
        f"/v1/admin/fleets/{a}/move",
        json={"agents": ["amy"], "target_fleet_id": b, "dry_run": False},
        headers=ADMIN_H,
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["dry_run"] is False and done["agents_moved"] == ["amy"]
    assert done["entries_moved"] == 1 and done["target"]["id"] == b
    moved = await store.get_entry(mine.id)
    assert moved is not None and moved.fleet_id == b
    fleets = (await client.get("/v1/admin/fleets", headers=ADMIN_H)).json()
    assert [f["id"] for f in fleets] == [a, b]  # the source is not marked

    listing = (await client.get("/v1/admin/fleet-merges", headers=ADMIN_H)).json()
    assert [(m["id"], m["kind"], m["undoable"]) for m in listing] == [
        (done["merge_id"], "move", True)
    ]
    assert listing[0]["moved_agents"] == ["amy"]

    resp = await client.post(f"/v1/admin/fleet-merges/{done['merge_id']}/undo", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "move" and resp.json()["agents_restored"] == ["amy"]
    resp = await client.post(f"/v1/admin/fleet-merges/{done['merge_id']}/undo", headers=ADMIN_H)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "move_refused")


async def test_rest_errors(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    b = (await store.create_fleet("b")).id
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)

    def move(source: str, **body: object):  # type: ignore[no-untyped-def]
        return client.post(f"/v1/admin/fleets/{source}/move", json=body, headers=ADMIN_H)

    resp = await move(a, agents=["amy"], target_fleet_id=b)  # dry_run is required
    assert resp.status_code == 422
    resp = await move(a, target_fleet_id=b, dry_run=False)  # agents are required
    assert resp.status_code == 422
    resp = await move(a, agents=[], target_fleet_id=b, dry_run=False)
    assert resp.status_code == 422
    resp = await move(a, agents=["amy"], dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    resp = await move(a, agents=["amy"], target_fleet_id=a, dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "move_refused")
    resp = await move(a, agents=["amy", "zed"], target_fleet_id=b, dry_run=True)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "move_refused")
    assert "zed" in resp.json()["error"]["message"]
    resp = await move(
        "00000000-0000-4000-8000-000000000001", agents=["amy"], target_fleet_id=a, dry_run=False
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")
    resp = await move(a, agents=["amy"], new_fleet_name="b", dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "fleet_exists")
    # A pin that changed since the preview refuses the move (409), nothing changes.
    entry = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="p", author="amy", agent="amy", scope="fleet", fleet_id=a
        ),
        VEC,
    )
    await store.pin_entry(a, entry.id, "amy", 100)
    resp = await move(a, agents=["amy"], target_fleet_id=b, dry_run=False, expected_pin_ids=[])
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "move_refused")
    assert "preview again" in resp.json()["error"]["message"]
    await store.merge_fleets(b, new_fleet_name="z", merged_by="x")
    resp = await move(a, agents=["amy"], target_fleet_id=b, dry_run=True)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "fleet_merged")


async def test_rest_non_admin_keys_get_403(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)
    for key in ("k-dev", "hm_org"):
        h = {"X-API-Key": key}
        for dry in (True, False):
            resp = await client.post(
                f"/v1/admin/fleets/{a}/move",
                json={"agents": ["amy"], "new_fleet_name": "c", "dry_run": dry},
                headers=h,
            )
            assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"
    resp = await client.post(
        f"/v1/admin/fleets/{a}/move",
        json={"agents": ["amy"], "new_fleet_name": "c", "dry_run": True},
    )
    assert resp.status_code == 401
    agent = await store.get_agent("amy")
    assert agent is not None and agent.home_fleet_id == a
    assert [f.name for f in await store.list_fleets()] == ["a"]


async def test_undo_of_a_move_keeps_the_source_within_the_pin_cap(w: World) -> None:
    a, b = await fleet_with_agents(w)
    moved = [await w.pin(a, f"m{i}", "amy", by=f"p{i}") for i in range(5)]
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    assert {p.entry_id for p in await w.store.list_pins(b)} == set(moved)
    for i in range(10):  # the source refills its briefing after the move
        await w.pin(a, f"s{i}", "bob", by=f"q{i}")

    undone = await w.merges.undo(out.record.id, ADMIN)

    assert len(await w.store.list_pins(a)) == 10  # never over the cap
    assert undone.pins_returned == 0
    assert {p.entry_id for p in undone.pins_skipped} == set(moved)
    assert {p.entry_id for p in undone.pins_unpinned} == set(moved)
    assert await w.store.list_pins(b) == []  # their memories left the target
    unpins = await w.audit(AuditAction.ENTRY_UNPIN)
    assert {r.target for r in unpins} == set(moved)
    assert all(r.detail["reason"] == "fleet_move_undo" for r in unpins)


async def test_undo_of_a_move_returns_the_pins_that_fit_newest_first(w: World) -> None:
    a, b = await fleet_with_agents(w)
    moved = [await w.pin(a, f"m{i}", "amy", by=f"p{i}") for i in range(4)]
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b)
    for i in range(8):
        await w.pin(a, f"s{i}", "bob", by=f"q{i}")
    undone = await w.merges.undo(out.record.id, ADMIN)  # room for two
    assert undone.pins_returned == 2
    assert {p.entry_id for p in undone.pins_unpinned} == set(moved[:2])  # the oldest go
    assert {p.entry_id for p in await w.store.list_pins(a)} >= set(moved[2:])
    assert len(await w.store.list_pins(a)) == 10
    assert {p.entry_id for p in await w.store.list_pins(b)} == set()


async def test_undo_of_a_move_does_not_restore_a_dropped_pin_that_is_pinned_again(
    w: World,
) -> None:
    a, b = await fleet_with_agents(w)
    mine = [await w.pin(a, f"m{i}", "amy", by=f"p{i}") for i in range(2)]
    out = await w.merges.move(a, ["amy"], ADMIN, target_fleet_id=b, keep_pin_ids=[mine[1]])
    assert [p.entry_id for p in out.pins_dropped] == [mine[0]]
    assert not isinstance(await w.store.pin_entry(b, mine[0], "later", 10), PinRefusal)
    undone = await w.merges.undo(out.record.id, ADMIN)
    assert undone.pins_returned == 2  # the dropped one came back as a returning pin
    assert undone.pins_restored == () and undone.pins_skipped == ()
    assert {p.entry_id for p in await w.store.list_pins(a)} == set(mine)
