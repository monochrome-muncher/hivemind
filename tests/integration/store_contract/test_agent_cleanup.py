"""Agent cleanups (ADR 0111) on both adapters."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.store_contract.helpers import (
    CLEAN,
    NO_SUCH_ID,
    T0,
    WIDE_SINCE,
    WIDE_UNTIL,
    Adapter,
    _clean,
    _junk,
    _rate,
    _state,
    write,
)

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.agent_cleanup import AgentCleanupRefused, UnknownAgentCleanup, cleanup_reason
from hivemind.domain.entry import EntryFilters, EntryState
from hivemind.domain.feedback import Feedback, FeedbackLogFilters, FlagBacklogFilters, Verdict
from hivemind.domain.pin import PinWrite
from hivemind.domain.validation import InvalidInput


async def test_cleanup_preview_counts_the_pin_rows_the_cleanup_removes(adapter: Adapter) -> None:
    """One memory pinned in two fleets is two pin rows: the preview says two,
    and the cleanup removes two."""
    store = adapter.store
    one = (await store.create_fleet("alpha")).id
    two = (await store.create_fleet("beta")).id
    junk = await _junk(adapter, "pinned twice", scope="org")
    for fleet in (one, two):
        assert isinstance(await store.pin_entry(fleet, junk.id, "bad", 10), PinWrite)
    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    out = await _clean(adapter)
    assert scan.pins_total == len(out.unpinned) == 2


async def test_cleanup_withdraws_ratings_pins_and_restores_in_one_go(adapter: Adapter) -> None:
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    good = await write(adapter, "good fact", author="good", scope="fleet", fleet_id=fleet)
    other = await write(adapter, "other good fact", author="good", scope="fleet", fleet_id=fleet)
    junk = await _junk(
        adapter, "junk over good", scope="fleet", fleet_id=fleet, supersedes=(good.id,)
    )
    plain = await _junk(adapter, "plain junk", scope="org")
    mine = await _junk(adapter, "self junk", scope="self")
    theirs = await write(adapter, "someone else's", author="good")
    written = await store.pin_entry(fleet, junk.id, "bad", 10)
    assert isinstance(written, PinWrite)
    await _rate(adapter, other.id, "bad", Verdict.WRONG)
    await _rate(adapter, other.id, "good", Verdict.HELPFUL)

    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert scan.withdraw_total == 3
    assert {e.id for e in scan.withdraw} == {junk.id, plain.id, mine.id}
    assert [e.summary for e in scan.withdraw] == ["self junk", "plain junk", "junk over good"]
    assert (scan.ratings_total, dict(scan.ratings_by_verdict), scan.pins_total) == (
        1,
        {"wrong": 1},
        1,
    )
    assert [(e.id, e.superseded_by) for e in scan.restorable] == [(good.id, junk.id)]
    assert scan.restorable_total == 1
    # The scan changed nothing.
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    assert await store.feedback_counts(other.id) == (1, 0, 1)

    out = await _clean(adapter, restore_ids=[good.id])

    assert out.record is not None
    assert (out.withdrawn, out.ratings_set_aside, out.restored) == (3, 1, 1)
    assert sorted(out.record.withdrawn_ids) == sorted([junk.id, plain.id, mine.id])
    assert [(r.entry_id, r.superseded_by) for r in out.record.restored] == [(good.id, junk.id)]
    assert [(p.fleet_id, p.entry_id, p.pinned_by) for p in out.unpinned] == [
        (fleet, junk.id, "bad")
    ]
    assert out.record.unpinned_pins == out.unpinned and out.record.ratings_set_aside == 1
    assert (out.record.agent, out.record.cleaned_by, out.record.revoked) == (
        "bad",
        "admin:1",
        False,
    )
    for entry_id in (junk.id, plain.id, mine.id):
        entry = await _state(adapter, entry_id)
        assert entry.state is EntryState.WITHDRAWN
        assert entry.withdrawn_reason == cleanup_reason(out.record.id)
    restored = await _state(adapter, good.id)
    assert restored.state is EntryState.ACTIVE and restored.superseded_by is None
    assert (await _state(adapter, theirs.id)).state is EntryState.ACTIVE
    assert await store.list_pins(fleet) == []
    assert await store.feedback_counts(other.id) == (1, 0, 0)
    assert await store.get_agent_cleanup(out.record.id) == out.record
    assert [c.id for c in await store.list_agent_cleanups(10)] == [out.record.id]
    assert await store.get_agent_cleanup(NO_SUCH_ID) is None
    assert await store.get_agent_cleanup("not-a-uuid") is None


async def test_cleanup_walks_the_agents_own_chain_and_unpins_older_versions(
    adapter: Adapter,
) -> None:
    """junk1 over good, junk2 over junk1: `good` is offered and junk1 is not.
    A chain through another author's memory is not offered. A pin on an older
    version whose chain ends at a withdrawn memory comes off unless restored,
    and an undo puts it back and replaces the restored memory behind the chain."""
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    good = await write(adapter, "good", author="good", scope="fleet", fleet_id=fleet)
    other = await write(adapter, "other good", author="good", scope="fleet", fleet_id=fleet)
    junk1 = await _junk(adapter, "junk1", scope="fleet", fleet_id=fleet, supersedes=(good.id,))
    junk2 = await _junk(adapter, "junk2", scope="fleet", fleet_id=fleet, supersedes=(junk1.id,))
    under_other = await write(adapter, "mid", author="good", scope="fleet", fleet_id=fleet)
    mixed = await _junk(
        adapter, "mixed", scope="fleet", fleet_id=fleet, supersedes=(under_other.id,)
    )
    await write(
        adapter,
        "good over mixed",
        author="good",
        scope="fleet",
        fleet_id=fleet,
        supersedes=(mixed.id,),
    )
    pinned_old = await write(adapter, "old pinned", author="good", scope="fleet", fleet_id=fleet)
    await _junk(adapter, "junk3", scope="fleet", fleet_id=fleet, supersedes=(pinned_old.id,))
    for entry_id, by in ((good.id, "good"), (pinned_old.id, "good")):
        assert isinstance(await store.pin_entry(fleet, entry_id, by, 10), PinWrite)
    assert other.id

    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert {e.id for e in scan.restorable} == {good.id, pinned_old.id}
    assert scan.restorable_total == 2 and junk1.id not in {e.id for e in scan.restorable}
    assert scan.pins_total == 0  # both pinned memories are offered, ticked

    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None and out.restored == 1
    assert [(r.entry_id, r.superseded_by, r.head) for r in out.record.restored] == [
        (good.id, junk1.id, junk2.id)
    ]
    assert {p.entry_id for p in out.unpinned} == {pinned_old.id}
    assert {p.entry_id for p in await store.list_pins(fleet)} == {good.id}
    assert (await _state(adapter, junk1.id)).state is EntryState.SUPERSEDED

    undone = await store.undo_agent_cleanup(out.record.id)
    assert (undone.resuperseded, undone.resupersede_skipped) == (1, 0)
    assert {p.entry_id for p in undone.pins_restored} == {pinned_old.id}
    assert (await _state(adapter, good.id)).superseded_by == junk1.id
    assert {p.entry_id for p in await store.list_pins(fleet)} == {good.id, pinned_old.id}


async def test_a_pin_on_an_older_version_of_a_restored_memory_stays(adapter: Adapter) -> None:
    """P -> X -> junk, P pinned, X restored: the pin shows X, so it stays. With
    X not restored it comes off."""
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    p = await write(adapter, "P", author="good", scope="fleet", fleet_id=fleet)
    x = await write(adapter, "X", author="good", scope="fleet", fleet_id=fleet, supersedes=(p.id,))
    await _junk(adapter, "junk", scope="fleet", fleet_id=fleet, supersedes=(x.id,))
    assert isinstance(await store.pin_entry(fleet, p.id, "good", 10), PinWrite)
    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert [e.id for e in scan.restorable] == [x.id] and scan.pins_total == 0

    out = await _clean(adapter, restore_ids=[x.id])
    assert out.unpinned == ()
    assert {q.entry_id for q in await store.list_pins(fleet)} == {p.id}


async def test_cleanup_sets_withdrawn_at_so_a_relied_on_listing_reads_it(
    adapter: Adapter,
) -> None:
    """A withdrawal by cleanup sets ``withdrawn_at`` like a normal one (ADR 0098):
    a helpful rating older than it lists the memory as changed."""
    store = adapter.store
    junk = await _junk(adapter, "junk")
    await _rate(adapter, junk.id, "good", Verdict.HELPFUL, at=T0)
    filters = EntryFilters(relied_on_changed=True, reporter=("good", "a"))
    assert await store.list_entries(filters) == []
    await _clean(adapter)
    assert [e.id for e in await store.list_entries(filters)] == [junk.id]


async def test_nothing_to_clean_keeps_no_record(adapter: Adapter) -> None:
    store = adapter.store
    await write(adapter, "good", author="good")
    out = await _clean(adapter)
    assert out.record is None and (out.withdrawn, out.ratings_set_aside, out.restored) == (0, 0, 0)
    assert await store.list_agent_cleanups(10) == []
    assert (await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)).withdraw_total == 0


async def test_the_window_is_on_the_write_time_for_memories_and_the_update_time_for_ratings(
    adapter: Adapter,
) -> None:
    store = adapter.store
    first = await _junk(adapter, "first")
    second = await _junk(adapter, "second")
    third = await _junk(adapter, "third")
    since, until = second.created_at, third.created_at
    scan = await store.scan_agent_cleanup("bad", since, until)
    assert [e.id for e in scan.withdraw] == [second.id]  # since inclusive, until exclusive
    old = await write(adapter, "old target", author="good")
    for i, at in enumerate((T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2))):
        target = await write(adapter, f"target {i}", author="good")
        await _rate(adapter, target.id, "bad", at=at)
    window = await store.scan_agent_cleanup("bad", T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    assert window.ratings_total == 1  # one hour in: since inclusive, until exclusive
    out = await store.cleanup_agent("bad", since, until, **CLEAN)
    assert out.record is not None and out.record.withdrawn_ids == (second.id,)
    assert (await _state(adapter, first.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, third.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, old.id)).state is EntryState.ACTIVE
    with pytest.raises(InvalidInput):
        await store.cleanup_agent("bad", until, since, **CLEAN)
    with pytest.raises(InvalidInput):
        await store.cleanup_agent("bad", since, since, **CLEAN)


async def test_every_feedback_read_skips_set_aside_ratings(adapter: Adapter) -> None:
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    target = await write(adapter, "target", author="good", scope="fleet", fleet_id=fleet)
    other = await write(adapter, "other", author="good", scope="fleet", fleet_id=fleet)
    now = T0 + timedelta(days=1)
    await _rate(adapter, target.id, "bad", Verdict.WRONG, "it is wrong")
    await _rate(adapter, other.id, "bad", Verdict.HELPFUL, "worked")
    await _rate(adapter, other.id, "good", Verdict.STALE, "old")
    link = await write(adapter, "links to target", author="good", see_also=(target.id,))
    flagged = EntryFilters(flagged=True)
    assert {e.id for e in await store.list_entries(flagged)} == {target.id, other.id}
    before = await store.fleet_health(now)
    assert (before.helpful, before.verdicts_90d) == (1, 3)
    assert [i.newest_flag_at for i in (await store.upstream_links([link.id]))[link.id]] == [T0]

    out = await _clean(adapter)
    assert out.record is not None and out.ratings_set_aside == 2

    assert await store.feedback_counts(target.id) == (0, 0, 0)
    assert await store.feedback_counts(other.id) == (0, 1, 0)
    assert await store.quality_counts([target.id, other.id]) == {
        target.id: (0, 0, 0),
        other.id: (0, 1, 0),
    }
    signals = await store.feedback_signals([target.id, other.id])
    assert signals[target.id].correction is None and signals[target.id].newest_flag_at is None
    assert signals[other.id].last_confirmed_at is None and signals[other.id].stale == 1
    assert await store.list_feedback(target.id, 10) == []
    assert [fb.user for fb in await store.list_feedback(other.id, 10)] == ["good"]
    many = await store.list_feedback_many([target.id, other.id], 10)
    assert many[target.id] == [] and [fb.user for fb in many[other.id]] == ["good"]
    log = await store.list_feedback_log(FeedbackLogFilters(), 10)
    assert [(i.feedback.user, i.feedback.entry_id) for i in log] == [("good", other.id)]
    assert await store.list_feedback_log(FeedbackLogFilters(reporter="bad"), 10) == []
    assert [e.id for e in await store.list_entries(flagged)] == [other.id]
    assert await store.count_entries(flagged) == 1
    backlog = await store.list_flag_backlog(FlagBacklogFilters(), now, 10)
    assert [f.entry_id for f in backlog] == [other.id]
    assert [f.user for f in await store.flag_counts()] == ["good"]
    health = await store.fleet_health(now)
    assert (health.helpful, health.verdicts_90d) == (0, 1)
    activity = await store.activity(T0 - timedelta(days=1))
    assert [r.reporter for r in activity.reporters] == ["good"]
    assert [i.newest_flag_at for i in (await store.upstream_links([link.id]))[link.id]] == [None]
    # The rows are kept: the same two ratings count again once the cleanup is undone.
    await store.undo_agent_cleanup(out.record.id)
    assert await store.feedback_counts(target.id) == (0, 0, 1)
    assert await store.feedback_counts(other.id) == (1, 1, 0)
    assert len(await store.list_feedback_log(FeedbackLogFilters(), 10)) == 3


async def test_a_set_aside_helpful_rating_does_not_list_a_changed_memory(adapter: Adapter) -> None:
    store = adapter.store
    liked = await write(adapter, "relied on", author="good")
    await _rate(adapter, liked.id, "bad", Verdict.HELPFUL, "worked")
    await store.withdraw_entry(liked.id, "gone", "good")
    filters = EntryFilters(relied_on_changed=True, reporter=("bad", "a"))
    assert [e.id for e in await store.list_entries(filters)] == [liked.id]
    await _clean(adapter)
    assert await store.list_entries(filters) == []


async def test_rating_again_clears_the_mark_and_only_that_row(adapter: Adapter) -> None:
    store = adapter.store
    one = await write(adapter, "one", author="good")
    two = await write(adapter, "two", author="good")
    await _rate(adapter, one.id, "bad", Verdict.WRONG)
    await _rate(adapter, two.id, "bad", Verdict.WRONG)
    out = await _clean(adapter)
    assert out.record is not None and out.ratings_set_aside == 2
    await _rate(adapter, one.id, "bad", Verdict.STALE, "again", at=None)
    assert await store.feedback_counts(one.id) == (0, 1, 0)
    assert await store.feedback_counts(two.id) == (0, 0, 0)
    await store.record_feedback_many([Feedback(two.id, "bad", "a", Verdict.HELPFUL, None)])
    assert await store.feedback_counts(two.id) == (1, 0, 0)
    undone = await store.undo_agent_cleanup(out.record.id)
    assert undone.ratings_cleared == 0  # both rows were already cleared by the agent itself


async def test_ratings_already_set_aside_are_left_to_the_first_cleanup(adapter: Adapter) -> None:
    store = adapter.store
    target = await write(adapter, "target", author="good")
    await _rate(adapter, target.id, "bad", Verdict.WRONG)
    first = await _clean(adapter)
    again = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert again.ratings_total == 0
    assert (await _clean(adapter)).record is None
    assert first.record is not None


async def test_cleanup_reaches_every_owned_author_but_only_the_agents_own_ratings(
    adapter: Adapter,
) -> None:
    """ADR 0118: the portal passes every author the agent owns; the ratings
    stay the one agent's, and the author-only default is unchanged."""
    store = adapter.store
    good = await write(adapter, "good fact", author="good")
    target = await write(adapter, "target", author="good")
    mine = await _junk(adapter, "junk of the agent")
    theirs = await write(adapter, "junk of the retired one", author="old", supersedes=(good.id,))
    await _rate(adapter, target.id, "bad", Verdict.WRONG)
    await _rate(adapter, target.id, "old", Verdict.STALE)

    alone = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert {e.id for e in alone.withdraw} == {mine.id}
    assert alone.restorable_total == 0

    both = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL, authors=["bad", "old"])
    assert {e.id for e in both.withdraw} == {mine.id, theirs.id}
    assert (both.ratings_total, dict(both.ratings_by_verdict)) == (1, {"wrong": 1})
    assert [(e.id, e.superseded_by) for e in both.restorable] == [(good.id, theirs.id)]

    out = await _clean(
        adapter,
        authors=["bad", "old"],
        self_service=True,
        restore_ids=[good.id],
        expected_withdraw=2,
        expected_ratings=1,
    )
    assert out.record is not None and out.record.self_service is True
    assert (out.withdrawn, out.ratings_set_aside, out.restored) == (2, 1, 1)
    assert out.record.agent == "bad"
    reason = cleanup_reason(out.record.id)
    for entry_id in (mine.id, theirs.id):
        entry = await _state(adapter, entry_id)
        assert entry.state is EntryState.WITHDRAWN and entry.withdrawn_reason == reason
    assert (await _state(adapter, good.id)).state is EntryState.ACTIVE
    assert await store.feedback_counts(target.id) == (0, 1, 0)  # old's stale still counts
    assert (await store.get_agent_cleanup(out.record.id)) == out.record

    undone = await store.undo_agent_cleanup(out.record.id)
    assert (undone.reactivated, undone.resuperseded, undone.ratings_cleared) == (2, 1, 1)
    assert (await _state(adapter, theirs.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, good.id)).state is EntryState.SUPERSEDED
    assert undone.record.self_service is True


async def test_cleanup_with_a_visibility_never_offers_or_restores_what_the_reader_cannot_read(
    adapter: Adapter,
) -> None:
    """ADR 0118: a retired agent may have replaced a memory in a fleet the
    successor cannot read. The self-service path drops it entirely."""
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    red = (await store.create_fleet("red")).id
    secret = await write(adapter, "red secret", author="peer", scope="fleet", fleet_id=red)
    open_ = await write(adapter, "org fact", author="peer", scope="org")
    await write(
        adapter,
        "old junk over secret",
        author="old",
        scope="fleet",
        fleet_id=red,
        supersedes=(secret.id,),
    )
    await write(adapter, "old junk over org", author="old", scope="org", supersedes=(open_.id,))
    reader = Visibility(
        level=TrustLevel.CONTRIBUTOR, name="bad", home_fleet_id=blue, inherited=("old",)
    )
    authors = ["bad", "old"]

    everything = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL, authors=authors)
    assert {e.id for e in everything.restorable} == {secret.id, open_.id}
    scan = await store.scan_agent_cleanup(
        "bad", WIDE_SINCE, WIDE_UNTIL, authors=authors, visibility=reader
    )
    assert [e.id for e in scan.restorable] == [open_.id] and scan.restorable_total == 1
    assert scan.withdraw_total == 2  # what the agent owns is withdrawn either way

    with pytest.raises(AgentCleanupRefused):
        await _clean(adapter, authors=authors, visibility=reader, restore_ids=[secret.id])
    out = await _clean(adapter, authors=authors, visibility=reader, restore_ids=[open_.id])
    assert out.restored == 1
    assert (await _state(adapter, open_.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, secret.id)).state is EntryState.SUPERSEDED


async def test_list_agent_cleanups_can_be_limited_to_one_agent(adapter: Adapter) -> None:
    store = adapter.store
    await write(adapter, "junk a", author="bad")
    await write(adapter, "junk b", author="worse")
    mine = await _clean(adapter)
    other = await store.cleanup_agent("worse", WIDE_SINCE, WIDE_UNTIL, **CLEAN)
    assert mine.record is not None and other.record is not None
    assert [c.id for c in await store.list_agent_cleanups(10)] == [other.record.id, mine.record.id]
    assert [c.id for c in await store.list_agent_cleanups(10, agent="bad")] == [mine.record.id]
    assert await store.list_agent_cleanups(10, agent="nobody") == []


async def test_an_admin_cleanup_is_not_marked_self_service(adapter: Adapter) -> None:
    await _junk(adapter, "junk")
    out = await _clean(adapter)
    assert out.record is not None and out.record.self_service is False
    entry = await _state(adapter, out.record.withdrawn_ids[0])
    assert entry.withdrawn_reason == cleanup_reason(out.record.id)


async def test_only_memories_still_superseded_by_a_withdrawn_memory_are_restorable(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a = await write(adapter, "fact a", author="good")
    b = await write(adapter, "fact b", author="good")
    c = await write(adapter, "fact c", author="good")
    junk_a = await _junk(adapter, "junk over a", supersedes=(a.id,))
    junk_b = await _junk(adapter, "junk over b", supersedes=(b.id,))
    await _junk(adapter, "junk over c", supersedes=(c.id,))
    rewrite = await write(adapter, "good rewrite", author="good", supersedes=(junk_b.id,))
    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert {e.id for e in scan.restorable} == {a.id, c.id}
    assert junk_b.id not in {e.id for e in scan.withdraw}  # not active: not the agent's to clean
    for bad_id in (b.id, junk_a.id, rewrite.id, NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(AgentCleanupRefused):
            await _clean(adapter, restore_ids=[bad_id])
    assert await store.list_agent_cleanups(10) == []
    assert (await _state(adapter, junk_a.id)).state is EntryState.ACTIVE  # nothing changed
    out = await _clean(adapter, restore_ids=[a.id, a.id.upper()])
    assert out.restored == 1 and (await _state(adapter, a.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, c.id)).state is EntryState.SUPERSEDED  # not ticked


async def test_the_apply_is_refused_when_the_counts_differ_from_the_preview(
    adapter: Adapter,
) -> None:
    store = adapter.store
    target = await write(adapter, "target", author="good")
    junk = await _junk(adapter, "junk")
    await _rate(adapter, target.id, "bad")
    for expected in ({"expected_withdraw": 2}, {"expected_ratings": 0}):
        with pytest.raises(AgentCleanupRefused, match="preview again"):
            await _clean(adapter, **expected)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    assert await store.feedback_counts(target.id) == (0, 0, 1)
    out = await _clean(adapter, expected_withdraw=1, expected_ratings=1)
    assert (out.withdrawn, out.ratings_set_aside) == (1, 1)


async def test_cleanup_ids_are_read_the_same_on_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    junk = await _junk(adapter, "junk")
    out = await _clean(adapter)
    assert out.record is not None
    undone = await store.undo_agent_cleanup(out.record.id.upper())
    assert undone.record.id == out.record.id
    for bad in (NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(UnknownAgentCleanup):
            await store.undo_agent_cleanup(bad)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE


async def test_undo_restores_exactly_what_the_cleanup_changed(adapter: Adapter) -> None:
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    good = await write(adapter, "good fact", author="good", scope="fleet", fleet_id=fleet)
    junk = await _junk(adapter, "pinned junk", scope="fleet", fleet_id=fleet, supersedes=(good.id,))
    plain = await _junk(adapter, "plain junk")
    rated = await write(adapter, "rated", author="good")
    await store.pin_entry(fleet, junk.id, "bad", 10)
    await _rate(adapter, rated.id, "bad", Verdict.WRONG)
    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None

    undone = await store.undo_agent_cleanup(out.record.id)

    assert (undone.reactivated, undone.reactivate_skipped) == (2, 0)
    assert (undone.resuperseded, undone.resupersede_skipped) == (1, 0)
    assert undone.ratings_cleared == 1 and undone.record.undone_at is not None
    for entry_id in (junk.id, plain.id):
        entry = await _state(adapter, entry_id)
        assert (entry.state, entry.withdrawn_reason) == (EntryState.ACTIVE, None)
    back = await _state(adapter, good.id)
    assert (back.state, back.superseded_by) == (EntryState.SUPERSEDED, junk.id)
    assert await store.feedback_counts(rated.id) == (0, 0, 1)
    assert [(p.entry_id, p.pinned_by) for p in await store.list_pins(fleet)] == [(junk.id, "bad")]
    assert [p.entry_id for p in undone.pins_restored] == [junk.id] and undone.pins_skipped == ()
    stored = await store.get_agent_cleanup(out.record.id)
    assert stored is not None and stored.undone_at is not None
    again = await _clean(adapter)  # a second cleanup works after the undo
    assert again.withdrawn == 2 and again.record is not None


async def test_undo_leaves_what_changed_since_the_cleanup(adapter: Adapter) -> None:
    store = adapter.store
    good = await write(adapter, "good fact", author="good")
    other = await write(adapter, "other fact", author="good")
    junk = await _junk(adapter, "junk", supersedes=(good.id, other.id))
    out = await _clean(adapter, restore_ids=[good.id, other.id])
    assert out.record is not None
    # Someone supersedes one restored memory with a better one meanwhile.
    later = await write(adapter, "a better fact", author="good", supersedes=(good.id,))
    undone = await store.undo_agent_cleanup(out.record.id)
    assert (undone.reactivated, undone.resuperseded, undone.resupersede_skipped) == (1, 1, 1)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    skipped = await _state(adapter, good.id)
    assert (skipped.state, skipped.superseded_by) == (EntryState.SUPERSEDED, later.id)
    back = await _state(adapter, other.id)
    assert (back.state, back.superseded_by) == (EntryState.SUPERSEDED, junk.id)


async def test_undo_only_the_newest_standing_cleanup_of_an_agent(adapter: Adapter) -> None:
    store = adapter.store
    await _junk(adapter, "one")
    first = await _clean(adapter)
    await _junk(adapter, "two")
    second = await _clean(adapter)
    await write(adapter, "good's junk", author="good")
    other = await store.cleanup_agent("good", WIDE_SINCE, WIDE_UNTIL, **CLEAN)
    assert first.record and second.record and other.record
    assert [c.id for c in await store.list_agent_cleanups(10)] == [
        other.record.id,
        second.record.id,
        first.record.id,
    ]
    with pytest.raises(AgentCleanupRefused, match="newer"):
        await store.undo_agent_cleanup(first.record.id)
    await store.undo_agent_cleanup(second.record.id)
    with pytest.raises(AgentCleanupRefused, match="already undone"):
        await store.undo_agent_cleanup(second.record.id)
    await store.undo_agent_cleanup(first.record.id)  # the newest standing one of "bad" now
    await store.undo_agent_cleanup(other.record.id)  # another agent's never blocked it


async def test_undo_repins_the_newest_that_fit_and_reports_the_rest(adapter: Adapter) -> None:
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    junk = [await _junk(adapter, f"junk {i}", scope="fleet", fleet_id=fleet) for i in range(3)]
    for i, entry in enumerate(junk):
        written = await store.pin_entry(fleet, entry.id, f"pinner-{i}", 10)
        assert isinstance(written, PinWrite)
    out = await _clean(adapter)
    assert out.record is not None and len(out.unpinned) == 3
    for i in range(8):
        fresh = await write(adapter, f"fresh {i}", author="good", scope="fleet", fleet_id=fleet)
        assert isinstance(await store.pin_entry(fleet, fresh.id, f"other-{i}", 10), PinWrite)
    undone = await store.undo_agent_cleanup(out.record.id)
    assert [p.entry_id for p in undone.pins_restored] == [junk[2].id, junk[1].id]
    assert [p.entry_id for p in undone.pins_skipped] == [junk[0].id]
    assert len(await store.list_pins(fleet)) == 10


async def test_a_repeated_cleanup_records_its_pins_again(adapter: Adapter) -> None:
    store = adapter.store
    fleet = (await store.create_fleet("alpha")).id
    junk = await _junk(adapter, "junk", scope="fleet", fleet_id=fleet)
    await store.pin_entry(fleet, junk.id, "bad", 10)
    out = await _clean(adapter)
    assert out.record is not None
    undone = await store.undo_agent_cleanup(out.record.id)
    assert [p.entry_id for p in undone.pins_restored] == [junk.id]
    second = await _clean(adapter)
    assert second.record is not None and [p.entry_id for p in second.unpinned] == [junk.id]


async def test_mark_cleanup_revoked_is_recorded(adapter: Adapter) -> None:
    store = adapter.store
    await _junk(adapter, "junk")
    out = await _clean(adapter)
    assert out.record is not None and out.record.revoked is False
    await store.mark_cleanup_revoked(out.record.id)
    await store.mark_cleanup_revoked("not-a-uuid")
    stored = await store.get_agent_cleanup(out.record.id)
    assert stored is not None and stored.revoked is True
    undone = await store.undo_agent_cleanup(out.record.id)
    assert undone.record.revoked is True  # an undo never un-revokes
