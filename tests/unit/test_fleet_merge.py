"""Merging one fleet into another and undoing it (ADR 0107), through the
service seam over ``MemoryStore``, plus the REST routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import (
    FleetExists,
    FleetMerged,
    TrustLevel,
    UnknownFleet,
    Visibility,
)
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.fleet_merge import (
    DroppedPin,
    FleetMergeRefused,
    UnknownFleetMerge,
    plan_pin_restore,
)
from hivemind.domain.pin import PinRefusal
from hivemind.domain.validation import InvalidInput
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services import fleet_merge as fleet_merge_module
from hivemind.services.access import AccessService
from hivemind.services.errors import PermissionDenied
from tests.fakes import FakeAuthenticator, make_embedder, make_search_config

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
AGENT = Credential(user_id="dev", agent_name="dev", access_controlled=True)
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
VEC = [1.0, 0.0, 0.0, 0.0]
OTHER_VEC = [0.0, 1.0, 0.0, 0.0]


class TickingClock:
    """Every reading is one second later, so pins and merges order strictly."""

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
        self, fleet_id: str, summary: str, vec: list[float] | None = None, author: str = "a"
    ) -> str:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope="fleet",
            fleet_id=fleet_id,
        )
        return (await self.store.create_entry(draft, vec)).id

    async def pin(self, fleet_id: str, summary: str, by: str | None = None) -> str:
        """Pin a new entry; each pin by its own agent unless ``by`` names one
        (the per-agent cap of 3 then applies to an undo)."""
        entry_id = await self.entry(fleet_id, summary)
        written = await self.store.pin_entry(fleet_id, entry_id, by or f"by-{summary}", 100)
        assert not isinstance(written, PinRefusal)
        return entry_id

    async def agent(self, name: str, fleet_id: str, level: TrustLevel) -> None:
        await self.store.register_agent(name)
        await self.store.activate_agent(name, trust_level=level, home_fleet_id=fleet_id)

    async def fleet_of(self, entry_id: str) -> str | None:
        entry = await self.store.get_entry(entry_id)
        assert entry is not None
        return entry.fleet_id

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)


@pytest.fixture
def w() -> World:
    return World()


async def two_fleets(w: World) -> tuple[str, str]:
    return await w.fleet("a"), await w.fleet("b")


# -- the merge -----------------------------------------------------------------


async def test_merge_into_an_existing_fleet_moves_entries_agents_and_marks_the_source(
    w: World,
) -> None:
    a, b = await two_fleets(w)
    live = await w.entry(a, "live")
    old = await w.entry(a, "old")
    await w.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="v2",
            author="a",
            agent="a",
            scope="fleet",
            fleet_id=a,
            supersedes=(old,),
        ),
        OTHER_VEC,
    )
    gone = await w.entry(a, "gone")
    await w.store.withdraw_entry(gone, "x", "a")
    theirs = await w.entry(b, "theirs")
    await w.agent("amy", a, TrustLevel.PRIVILEGED)
    await w.agent("bob", b, TrustLevel.LURKER)
    await w.store.record_search(a, empty=True)
    await w.store.record_search(b, empty=False)

    out = await w.merges.merge(a, ADMIN, target_fleet_id=b)

    assert out.entries_moved == 4 and out.agents_moved == 1 and not out.target_created
    for entry_id in (live, old, gone, theirs):
        assert await w.fleet_of(entry_id) == b  # any state, plus the target's own
    amy = await w.store.get_agent("amy")
    assert amy is not None and amy.home_fleet_id == b and amy.trust_level is TrustLevel.PRIVILEGED
    source = await w.store.get_fleet(a)
    assert source is not None and source.merged_into == b and source.merged_at is not None
    counts = {c.fleet_id: (c.searches, c.empty) for c in await w.store.search_counts()}
    assert counts == {b: (2, 1)}  # the source row is folded in and deleted
    assert [f.id for f in await w.access.list_fleets(ADMIN)] == [b]
    shown = await w.access.list_fleets(ADMIN, include_merged=True)
    assert {f.id: f.merged_into for f in shown} == {a: b, b: None}


async def test_merge_into_a_new_fleet_creates_it_and_audits_both(w: World) -> None:
    a = await w.fleet("a")
    await w.entry(a, "one")
    out = await w.merges.merge(a, ADMIN, new_fleet_name="c")
    assert out.target_created and out.target.name == "c" and out.entries_moved == 1
    created = await w.audit(AuditAction.FLEET_CREATE)
    assert [r.target for r in created] == [out.target.id]
    (merged,) = await w.audit(AuditAction.FLEET_MERGE)
    assert merged.target == a
    assert merged.actor == "admin:abcdef012345"
    assert dict(merged.detail) == {
        "merge_id": out.record.id,
        "target_fleet_id": out.target.id,
        "target_fleet_name": "c",
        "created_target": True,
        "entries": 1,
        "agents": 0,
        "pins_kept": 0,
        "pins_dropped": 0,
    }


async def test_a_merge_changes_no_visible_memory_for_the_moved_agent(w: World) -> None:
    a, b = await two_fleets(w)
    mine = await w.entry(a, "mine")
    theirs = await w.entry(b, "theirs")
    await w.agent("amy", a, TrustLevel.CONTRIBUTOR)
    await w.merges.merge(a, ADMIN, target_fleet_id=b)
    amy = await w.store.get_agent("amy")
    assert amy is not None
    seen = Visibility(level=amy.trust_level, name="amy", home_fleet_id=amy.home_fleet_id)
    ids = {e.id for e in await w.store.list_entries(EntryFilters(), visibility=seen)}
    assert ids == {mine, theirs}


# -- the preview -----------------------------------------------------------------


async def test_preview_reports_counts_pins_and_duplicates_and_changes_nothing(w: World) -> None:
    a, b = await two_fleets(w)
    dup_a = await w.entry(a, "same fact", VEC)
    await w.entry(a, "distinct", OTHER_VEC)
    dup_b = await w.entry(b, "same fact again", VEC)
    await w.entry(b, "b only", [0.0, 0.0, 1.0, 0.0])
    gone = await w.entry(a, "gone")
    await w.store.withdraw_entry(gone, None, "a")
    await w.agent("amy", a, TrustLevel.LURKER)
    pin_a = await w.pin(a, "pinned in a")
    pin_b = await w.pin(b, "pinned in b")

    preview = await w.merges.preview(a, ADMIN, target_fleet_id=b)

    assert (preview.source.name, preview.target.name) == ("a", "b")
    assert preview.source.facts.agents == 1
    assert dict(preview.source.facts.entries) == {"active": 3, "withdrawn": 1}
    assert [p.pin.entry_id for p in preview.pins] == [pin_b, pin_a]  # newest pin first
    assert all(p.default_kept for p in preview.pins) and not preview.needs_pin_choice
    # Both fleets hold two active entries, so the source is the one looked up.
    assert [(d.entry_id, d.match_id) for d in preview.duplicates] == [(dup_a, dup_b)]
    assert preview.duplicates[0].similarity == pytest.approx(1.0)
    assert not preview.duplicates_truncated
    assert (await w.store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]
    assert await w.fleet_of(dup_a) == a and await w.audit(AuditAction.FLEET_MERGE) == []


async def test_preview_ticks_the_newest_ten_pins_and_asks_for_a_choice(w: World) -> None:
    a, b = await two_fleets(w)
    ids = [await w.pin(a if i % 2 else b, f"p{i}") for i in range(12)]
    preview = await w.merges.preview(a, ADMIN, target_fleet_id=b)
    assert preview.needs_pin_choice and preview.pin_limit == 10
    assert [p.pin.entry_id for p in preview.pins] == list(reversed(ids))
    assert [p.default_kept for p in preview.pins] == [True] * 10 + [False] * 2


async def test_preview_duplicate_lookup_is_capped_and_says_so(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await two_fleets(w)
    for i in range(3):
        await w.entry(a, f"a{i}", VEC)
    await w.entry(b, "b", VEC)
    await w.entry(b, "b2", VEC)
    monkeypatch.setattr(fleet_merge_module, "DUPLICATE_LOOKUP_CAP", 2)
    preview = await w.merges.preview(a, ADMIN, target_fleet_id=b)
    # b is the smaller fleet (two active vs three), so it is the one looked up.
    assert preview.duplicates_looked_up == 2 and not preview.duplicates_truncated
    await w.entry(b, "b3", VEC)
    await w.entry(b, "b4", VEC)
    preview = await w.merges.preview(a, ADMIN, target_fleet_id=b)
    assert preview.duplicates_looked_up == 2 and preview.duplicates_truncated


async def test_preview_to_a_new_fleet_has_an_empty_target(w: World) -> None:
    a = await w.fleet("a")
    await w.entry(a, "one")
    preview = await w.merges.preview(a, ADMIN, new_fleet_name="c")
    assert preview.target.fleet is None and preview.target.name == "c"
    assert preview.target.facts.agents == 0 and preview.duplicates == ()


# -- pins over the cap -----------------------------------------------------------


async def test_more_than_ten_pins_need_a_choice_and_the_rest_are_unpinned_and_audited(
    w: World,
) -> None:
    a, b = await two_fleets(w)
    ids = [await w.pin(a if i < 6 else b, f"p{i}") for i in range(12)]
    with pytest.raises(InvalidInput, match="keep_pin_ids"):
        await w.merges.merge(a, ADMIN, target_fleet_id=b)
    assert await w.fleet_of(ids[0]) == a  # a refusal changes nothing

    keep = ids[2:]  # drop two of the source's older pins
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b, keep_pin_ids=keep)

    assert {p.entry_id for p in await w.store.list_pins(b)} == set(keep)
    assert await w.store.list_pins(a) == []
    assert {p.entry_id for p in out.pins_dropped} == set(ids[:2])
    unpins = await w.audit(AuditAction.ENTRY_UNPIN)
    assert {r.target for r in unpins} == set(ids[:2])
    assert all(r.detail["reason"] == "fleet_merge" for r in unpins)


async def test_keep_pin_ids_is_checked(w: World) -> None:
    a, b = await two_fleets(w)
    ids = [await w.pin(a, f"p{i}") for i in range(12)]
    with pytest.raises(InvalidInput, match="at most 10"):
        await w.merges.merge(a, ADMIN, target_fleet_id=b, keep_pin_ids=ids[:11])
    with pytest.raises(InvalidInput, match="not pinned"):
        await w.merges.merge(a, ADMIN, target_fleet_id=b, keep_pin_ids=[ids[0], "nope"])


async def test_ten_or_fewer_pins_are_all_kept_unless_the_admin_drops_some(w: World) -> None:
    a, b = await two_fleets(w)
    pa = await w.pin(a, "pa")
    pb = await w.pin(b, "pb")
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b)
    assert {p.entry_id for p in await w.store.list_pins(b)} == {pa, pb}
    assert out.pins_dropped == ()

    c, d = await two_fleets_named(w, "c", "d")
    pc = await w.pin(c, "pc")
    await w.pin(d, "pd")
    await w.merges.merge(c, ADMIN, target_fleet_id=d, keep_pin_ids=[pc])
    assert [p.entry_id for p in await w.store.list_pins(d)] == [pc]


async def test_a_merge_with_expected_pins_refuses_when_they_changed(w: World) -> None:
    a, b = await two_fleets(w)
    seen = await w.pin(a, "seen")
    late = await w.pin(b, "late")
    with pytest.raises(FleetMergeRefused, match="preview again"):
        await w.merges.merge(a, ADMIN, target_fleet_id=b, expected_pin_ids=[seen])
    assert (await w.store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b, expected_pin_ids=[late, seen])
    assert len(out.pins_kept) == 2


def test_restoring_pins_respects_the_per_agent_cap_and_exempts_the_admin() -> None:
    def drop(name: str, minute: int, by: str) -> DroppedPin:
        return DroppedPin("f", name, by, T0 + timedelta(minutes=minute))

    dropped = [drop("a-old", 1, "amy"), drop("a-mid", 2, "amy"), drop("a-new", 3, "amy")]
    dropped += [drop("adm1", 4, "admin"), drop("adm2", 5, "admin"), drop("adm3", 6, "admin")]
    dropped += [drop("adm4", 7, "admin")]
    put, over = plan_pin_restore(dropped, ["amy", "amy"])  # amy holds two already
    assert [d.entry_id for d in over] == ["a-mid", "a-old"]
    assert {d.entry_id for d in put} == {"a-new", "adm1", "adm2", "adm3", "adm4"}
    # The fleet cap still applies: 8 held leave room for 2, newest first.
    put, over = plan_pin_restore(dropped, ["bob"] * 8)
    assert [d.entry_id for d in put] == ["adm4", "adm3"]


async def two_fleets_named(w: World, x: str, y: str) -> tuple[str, str]:
    return await w.fleet(x), await w.fleet(y)


# -- refusals --------------------------------------------------------------------


async def test_refusals(w: World) -> None:
    a, b = await two_fleets(w)
    with pytest.raises(FleetMergeRefused, match="itself"):
        await w.merges.merge(a, ADMIN, target_fleet_id=a)
    with pytest.raises(UnknownFleet):
        await w.merges.merge(a, ADMIN, target_fleet_id="00000000-0000-4000-8000-000000000001")
    with pytest.raises(UnknownFleet):
        await w.merges.merge("00000000-0000-4000-8000-000000000001", ADMIN, target_fleet_id=a)
    with pytest.raises(InvalidInput, match="exactly one"):
        await w.merges.merge(a, ADMIN)
    with pytest.raises(InvalidInput, match="exactly one"):
        await w.merges.merge(a, ADMIN, target_fleet_id=b, new_fleet_name="c")
    with pytest.raises(FleetExists):
        await w.merges.merge(a, ADMIN, new_fleet_name="b")
    with pytest.raises(PermissionDenied):
        await w.merges.merge(a, AGENT, target_fleet_id=b)
    with pytest.raises(PermissionDenied):
        await w.merges.preview(a, AGENT, target_fleet_id=b)
    with pytest.raises(PermissionDenied):
        await w.merges.undo("x", AGENT)
    with pytest.raises(PermissionDenied):
        await w.merges.list_merges(AGENT)
    assert (await w.store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]
    assert await w.audit(AuditAction.FLEET_MERGE) == []


async def test_a_merged_fleet_is_refused_as_source_or_target(w: World) -> None:
    a, b = await two_fleets(w)
    c = await w.fleet("c")
    await w.merges.merge(a, ADMIN, target_fleet_id=b)
    with pytest.raises(FleetMerged) as exc:
        await w.merges.merge(a, ADMIN, target_fleet_id=c)  # as source
    assert exc.value.target_id == b and exc.value.target_name == "b"
    with pytest.raises(FleetMerged):
        await w.merges.merge(c, ADMIN, target_fleet_id=a)  # as target
    with pytest.raises(FleetMerged):
        await w.merges.preview(c, ADMIN, target_fleet_id=a)


async def test_a_merged_fleet_takes_no_writes_pins_or_members(w: World) -> None:
    a, b = await two_fleets(w)
    entry = await w.entry(a, "e")
    await w.agent("amy", a, TrustLevel.CONTRIBUTOR)
    await w.store.register_agent("newbie")
    await w.merges.merge(a, ADMIN, target_fleet_id=b)

    draft = EntryDraft(
        kind=Kind.FACT, summary="late", author="amy", agent="amy", scope="fleet", fleet_id=a
    )
    with pytest.raises(FleetMerged, match="'b'"):
        await w.store.create_entry(draft, VEC)
    with pytest.raises(FleetMerged):
        await w.store.pin_entry(a, entry, "amy", 10)
    with pytest.raises(FleetMerged):
        await w.store.activate_agent("newbie", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    with pytest.raises(FleetMerged):
        await w.store.set_agent_home_fleet("amy", a)
    with pytest.raises(FleetMerged):
        await w.access.set_home_fleet("amy", a, ADMIN)
    with pytest.raises(FleetMerged):
        await w.access.activate("newbie", TrustLevel.LURKER, a, ADMIN)
    assert [e.summary for e in await w.store.list_entries(EntryFilters(fleet_id=a))] == []


# -- undo --------------------------------------------------------------------------


async def test_undo_restores_exactly_what_the_merge_moved(w: World) -> None:
    a, b = await two_fleets(w)
    e1 = await w.entry(a, "a1")
    e2 = await w.entry(a, "a2")
    b1 = await w.entry(b, "b1")
    await w.agent("amy", a, TrustLevel.CONTRIBUTOR)
    await w.agent("ann", a, TrustLevel.LURKER)
    pa = await w.pin(a, "pa")
    ids = [await w.pin(b, f"pb{i}") for i in range(10)]
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b, keep_pin_ids=[pa, *ids[1:]])
    assert [p.entry_id for p in out.pins_dropped] == [ids[0]]

    # After the merge: a new entry in the target, and ann moves on.
    late = await w.entry(b, "late")
    c = await w.fleet("c")
    await w.store.set_agent_home_fleet("ann", c)
    await w.store.record_search(b, empty=False)

    undone = await w.merges.undo(out.record.id, ADMIN)

    assert undone.entries_restored == 3  # a1, a2 and pa's entry; none of b's
    assert await w.fleet_of(e1) == a and await w.fleet_of(e2) == a and await w.fleet_of(pa) == a
    assert await w.fleet_of(b1) == b and await w.fleet_of(late) == b  # post-merge stays
    assert undone.agents_restored == ("amy",)
    amy, ann = await w.store.get_agent("amy"), await w.store.get_agent("ann")
    assert amy is not None and amy.home_fleet_id == a
    assert ann is not None and ann.home_fleet_id == c  # moved again since: left alone
    assert [p.entry_id for p in await w.store.list_pins(a)] == [pa]  # the pin went back
    assert {p.entry_id for p in await w.store.list_pins(b)} == set(ids)  # dropped pin restored
    assert undone.pins_skipped == () and [p.entry_id for p in undone.pins_restored] == [ids[0]]
    source = await w.store.get_fleet(a)
    assert source is not None and source.merged_into is None and source.merged_at is None
    assert {c.fleet_id: c.searches for c in await w.store.search_counts()} == {b: 1}
    (record,) = await w.store.list_fleet_merges(10)
    assert record.undone_at is not None
    (row,) = await w.audit(AuditAction.FLEET_UNMERGE)
    assert row.target == a and row.detail["merge_id"] == out.record.id
    assert row.detail["entries"] == 3


async def test_undo_restores_the_newest_dropped_pins_that_fit_and_reports_the_rest(
    w: World,
) -> None:
    a, b = await two_fleets(w)
    mine = [await w.pin(a, f"a{i}") for i in range(2)]
    theirs = [await w.pin(b, f"b{i}") for i in range(10)]
    # b's two oldest pins do not fit in the briefing.
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b, keep_pin_ids=[*mine, *theirs[2:]])
    assert {p.entry_id for p in out.pins_dropped} == set(theirs[:2])
    # Afterwards the admin swaps the moved pins for one new pin: b holds 9.
    for entry_id in mine:
        assert await w.store.unpin_entry(b, entry_id)
    fresh = await w.pin(b, "fresh")
    undone = await w.merges.undo(out.record.id, ADMIN)
    assert undone.pins_returned == 0
    assert [p.entry_id for p in undone.pins_restored] == [theirs[1]]  # the newer fits
    assert [p.entry_id for p in undone.pins_skipped] == [theirs[0]]  # the oldest is reported
    assert {p.entry_id for p in await w.store.list_pins(b)} == {*theirs[1:], fresh}
    (row,) = await w.audit(AuditAction.FLEET_UNMERGE)
    assert row.detail["pins_skipped"] == [theirs[0]]


async def test_undo_refusals(w: World) -> None:
    a, b = await two_fleets(w)
    c = await w.fleet("c")
    first = await w.merges.merge(a, ADMIN, target_fleet_id=c)
    second = await w.merges.merge(b, ADMIN, target_fleet_id=c)
    with pytest.raises(FleetMergeRefused, match="newer"):
        await w.merges.undo(first.record.id, ADMIN)  # same target, a newer standing merge
    views = {v.record.id: v for v in await w.merges.list_merges(ADMIN)}
    assert not views[first.record.id].undoable and views[second.record.id].undoable
    assert [v.record.id for v in await w.merges.list_merges(ADMIN)] == [
        second.record.id,
        first.record.id,
    ]  # newest first
    await w.merges.undo(second.record.id, ADMIN)
    with pytest.raises(FleetMergeRefused, match="already undone"):
        await w.merges.undo(second.record.id, ADMIN)
    await w.merges.undo(first.record.id, ADMIN)  # now it is the newest standing one
    with pytest.raises(UnknownFleetMerge):
        await w.merges.undo("00000000-0000-4000-8000-000000000001", ADMIN)


async def test_a_chained_merge_must_be_undone_in_order(w: World) -> None:
    a, b = await two_fleets(w)
    e = await w.entry(a, "e")
    first = await w.merges.merge(a, ADMIN, new_fleet_name="c")  # a -> c
    c = first.target.id
    second = await w.merges.merge(c, ADMIN, target_fleet_id=b)  # c -> b
    with pytest.raises(FleetMergeRefused):
        await w.merges.undo(first.record.id, ADMIN)  # c is merged since
    assert await w.fleet_of(e) == b
    await w.merges.undo(second.record.id, ADMIN)
    assert await w.fleet_of(e) == c
    await w.merges.undo(first.record.id, ADMIN)
    assert await w.fleet_of(e) == a


async def test_undo_makes_the_source_usable_again(w: World) -> None:
    a, b = await two_fleets(w)
    out = await w.merges.merge(a, ADMIN, target_fleet_id=b)
    await w.merges.undo(out.record.id, ADMIN)
    await w.agent("amy", a, TrustLevel.CONTRIBUTOR)  # accepted again
    assert [f.id for f in await w.access.list_fleets(ADMIN)] == [a, b]


# -- REST --------------------------------------------------------------------------


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


async def test_rest_preview_merge_list_and_undo(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    b = (await store.create_fleet("b")).id
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.LURKER, home_fleet_id=a)

    resp = await client.post(
        f"/v1/admin/fleets/{a}/merge", json={"target_fleet_id": b, "dry_run": True}, headers=ADMIN_H
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True and body["source"]["agents"] == 1
    assert body["target"]["name"] == "b" and body["needs_pin_choice"] is False

    resp = await client.post(
        f"/v1/admin/fleets/{a}/merge",
        json={"target_fleet_id": b, "dry_run": False},
        headers=ADMIN_H,
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["dry_run"] is False and done["agents_moved"] == ["amy"]
    assert done["target"]["id"] == b

    fleets = (await client.get("/v1/admin/fleets", headers=ADMIN_H)).json()
    assert [f["id"] for f in fleets] == [b]
    everything = (await client.get("/v1/admin/fleets?include_merged=true", headers=ADMIN_H)).json()
    merged = next(f for f in everything if f["id"] == a)
    assert merged["merged_into"] == b and merged["merged_at"] is not None

    listing = (await client.get("/v1/admin/fleet-merges", headers=ADMIN_H)).json()
    assert [(m["id"], m["undoable"]) for m in listing] == [(done["merge_id"], True)]
    assert listing[0]["source_fleet_name"] == "a" and listing[0]["target_fleet_name"] == "b"

    resp = await client.post(f"/v1/admin/fleet-merges/{done['merge_id']}/undo", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    assert resp.json()["agents_restored"] == ["amy"]
    resp = await client.post(f"/v1/admin/fleet-merges/{done['merge_id']}/undo", headers=ADMIN_H)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "merge_refused")


async def test_rest_errors(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    b = (await store.create_fleet("b")).id

    def merge(source: str, **body: object):  # type: ignore[no-untyped-def]
        return client.post(f"/v1/admin/fleets/{source}/merge", json=body, headers=ADMIN_H)

    resp = await merge(a, target_fleet_id=b)  # dry_run is required
    assert resp.status_code == 422
    resp = await merge(a, dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    resp = await merge(a, target_fleet_id=a, dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "merge_refused")
    resp = await merge("00000000-0000-4000-8000-000000000001", target_fleet_id=a, dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")
    resp = await merge(a, new_fleet_name="b", dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "fleet_exists")
    ids = []
    for i in range(12):
        entry = await store.create_entry(
            EntryDraft(
                kind=Kind.FACT, summary=f"p{i}", author="a", agent="a", scope="fleet", fleet_id=a
            ),
            VEC,
        )
        await store.pin_entry(a, entry.id, "a", 100)
        ids.append(entry.id)
    resp = await merge(a, target_fleet_id=b, dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    # A pin the preview did not show makes the merge refuse (409), nothing changes.
    resp = await merge(a, target_fleet_id=b, dry_run=False, expected_pin_ids=ids[:11])
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "merge_refused")
    assert "preview again" in resp.json()["error"]["message"]
    resp = await merge(
        a, target_fleet_id=b, dry_run=False, keep_pin_ids=ids[:10], expected_pin_ids=ids
    )
    assert resp.status_code == 200 and len(resp.json()["pins_dropped"]) == 2
    resp = await merge(a, target_fleet_id=b, dry_run=True)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "fleet_merged")


async def test_rest_non_admin_keys_get_403(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    a = (await store.create_fleet("a")).id
    for key in ("k-dev", "hm_org"):
        h = {"X-API-Key": key}
        for resp in (
            await client.post(
                f"/v1/admin/fleets/{a}/merge",
                json={"new_fleet_name": "c", "dry_run": True},
                headers=h,
            ),
            await client.get("/v1/admin/fleet-merges", headers=h),
            await client.post(f"/v1/admin/fleet-merges/{a}/undo", headers=h),
        ):
            assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"
    assert (await store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]


# -- agents keep reading with the same key -------------------------------------------


class StoreBackedAuthenticator(FakeAuthenticator):
    """Builds the credential from the agent's current record on every
    request, as ``PgAuthenticator`` does (no credential cache)."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store=store)
        self._records = store

    async def verify(self, key: str):  # type: ignore[no-untyped-def]
        if key != "k-amy":
            return await super().verify(key)
        agent = await self._records.get_agent("amy")
        assert agent is not None
        return Credential(
            user_id="amy",
            agent_id="amy",
            agent_name="amy",
            access_controlled=True,
            trust_level=agent.trust_level,
            home_fleet_id=agent.home_fleet_id,
        )


async def test_an_agent_reads_its_old_fleet_entries_after_the_merge_with_the_same_key() -> None:
    import httpx

    store = MemoryStore(TickingClock())
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=StoreBackedAuthenticator(store),
        search_config=make_search_config(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )
    a = (await store.create_fleet("a")).id
    b = (await store.create_fleet("b")).id
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)
    amy = {"X-API-Key": "k-amy"}
    made = await client.post(
        "/v1/entries", json={"kind": "fact", "summary": "from a", "scope": "fleet"}, headers=amy
    )
    assert made.status_code == 201, made.text
    entry_id = made.json()["id"]
    await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="b's", author="bo", agent="bo", scope="fleet", fleet_id=b
        ),
        VEC,
    )

    merged = await client.post(
        f"/v1/admin/fleets/{a}/merge",
        json={"target_fleet_id": b, "dry_run": False},
        headers=ADMIN_H,
    )
    assert merged.status_code == 200, merged.text

    again = await client.get(f"/v1/entries/{entry_id}", headers=amy)
    assert again.status_code == 200 and again.json()["fleet_id"] == b
    whoami = (await client.get("/v1/whoami", headers=amy)).json()
    assert whoami["home_fleet_name"] == "b"
    listed = await client.get("/v1/entries", headers=amy)
    assert {e["summary"] for e in listed.json()} == {"from a", "b's"}
    new = await client.post(
        "/v1/entries", json={"kind": "fact", "summary": "after", "scope": "fleet"}, headers=amy
    )
    assert new.status_code == 201 and new.json()["fleet_id"] == b
    await client.aclose()


async def test_a_merge_and_an_undo_drop_the_cached_metrics_and_activity_scans() -> None:
    from hivemind.services.wiring import build_services

    store = MemoryStore(TickingClock())
    services = build_services(
        store, make_embedder(), make_search_config(), metrics_entry_counts_ttl=3600.0
    )
    w = World()
    w.store = store
    a, b = await w.fleet("a"), await w.fleet("b")
    for i in range(2):
        await w.entry(a, f"n{i}", VEC)

    async def seen() -> dict[str, int]:
        report = await services.metrics_service.usage_report()
        await services.activity_service.report()
        return dict(report.fleets.writes_by_fleet)

    assert await seen() == {"a": 2, "b": 0}
    assert services.activity_service._cache  # warm
    out = await services.access_service.merges.merge(a, ADMIN, target_fleet_id=b)
    assert not services.activity_service._cache
    assert await seen() == {"b": 2}  # the one-hour TTL would otherwise serve the old split
    await services.access_service.merges.undo(out.record.id, ADMIN)
    assert await seen() == {"a": 2, "b": 0}
