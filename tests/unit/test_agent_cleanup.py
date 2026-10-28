"""Cleaning up after a bad agent and undoing it (ADR 0111), through the
service seam over ``MemoryStore``, plus the REST routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.agent_cleanup import (
    AgentCleanupRecord,
    AgentCleanupRefused,
    UnknownAgentCleanup,
    check_restore_ids,
    cleanup_reason,
    undo_refusal,
)
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import Feedback, FeedbackLogFilters, Verdict
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
FAR = T0 + timedelta(days=30)
VEC = [1.0, 0.0, 0.0, 0.0]


class TickingClock:
    """Every reading is one second later, so writes order strictly."""

    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


class World:
    def __init__(self) -> None:
        self.clock = TickingClock()
        self.store = MemoryStore(self.clock)
        self.authenticator = FakeAuthenticator(store=self.store)
        self.access = AccessService(self.store, self.authenticator)
        self.cleanups = self.access.cleanups
        self.fleet = ""

    async def setup(self) -> World:
        self.fleet = (await self.store.create_fleet("alpha")).id
        for name in ("bad", "good"):
            await self.store.register_agent(name)
            await self.store.activate_agent(
                name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=self.fleet
            )
        return self

    async def entry(
        self,
        summary: str,
        author: str = "bad",
        scope: str = "org",
        supersedes: tuple[str, ...] = (),
    ) -> str:
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope=scope,
            fleet_id=self.fleet if scope == "fleet" else None,
            supersedes=supersedes,
        )
        return (await self.store.create_entry(draft, VEC)).id

    async def rate(
        self, entry_id: str, user: str = "bad", verdict: Verdict = Verdict.WRONG, note: str = "n"
    ) -> None:
        await self.store.record_feedback(Feedback(entry_id, user, "a", verdict, note))

    async def state(self, entry_id: str) -> EntryState:
        entry = await self.store.get_entry(entry_id)
        assert entry is not None
        return entry.state

    async def pin(self, entry_id: str, by: str) -> None:
        written = await self.store.pin_entry(self.fleet, entry_id, by, 100)
        assert not isinstance(written, PinRefusal)

    async def clean(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("since", T0)
        kwargs.setdefault("until", FAR)
        return await self.cleanups.cleanup("bad", ADMIN, **kwargs)

    async def audit(self, action: AuditAction) -> list:  # type: ignore[type-arg]
        return await self.store.list_audit(AuditFilters(action=action), 100)

    async def counts(self, entry_id: str) -> tuple[int, int, int]:
        return await self.store.feedback_counts(entry_id)


@pytest.fixture
async def w() -> World:
    return await World().setup()


# -- the withdraw ----------------------------------------------------------------------


async def test_it_withdraws_the_agents_active_memories_in_the_window_in_any_scope(
    w: World,
) -> None:
    before = await w.entry("before the window")
    since = w.clock()
    org = await w.entry("org junk", scope="org")
    fleet = await w.entry("fleet junk", scope="fleet")
    own = await w.entry("self junk", scope="self")
    theirs = await w.entry("good write", author="good")
    old = await w.entry("will be superseded by someone else")
    await w.entry("replacement", author="good", supersedes=(old,))
    gone = await w.entry("already withdrawn")
    await w.store.withdraw_entry(gone, "by hand", "bad")
    until = w.clock()
    after = await w.entry("after the window")

    out = await w.cleanups.cleanup("bad", ADMIN, since=since, until=until)

    assert out.record is not None and out.withdrawn == 3
    assert sorted(out.record.withdrawn_ids) == sorted([org, fleet, own])
    for entry_id in (org, fleet, own):
        entry = await w.store.get_entry(entry_id)
        assert entry is not None and entry.state is EntryState.WITHDRAWN
        assert entry.withdrawn_reason == cleanup_reason(out.record.id)
        assert entry.withdrawn_reason == f"admin cleanup {out.record.id}"
    assert await w.state(old) is EntryState.SUPERSEDED  # not the agent's to clean
    for entry_id in (before, theirs, after):
        assert await w.state(entry_id) is EntryState.ACTIVE
    withdrawn = (await w.store.get_entry(gone)).withdrawn_reason  # type: ignore[union-attr]
    assert withdrawn == "by hand"  # an earlier withdrawal keeps its own reason


async def test_the_window_is_on_the_write_time_since_inclusive_until_exclusive(
    w: World,
) -> None:
    first = await w.entry("first")
    second = await w.entry("second")
    created = (await w.store.get_entry(second)).created_at  # type: ignore[union-attr]
    out = await w.cleanups.cleanup("bad", ADMIN, since=created, until=created + timedelta(hours=1))
    assert out.record is not None and out.record.withdrawn_ids == (second,)  # since is inclusive
    created_first = (await w.store.get_entry(first)).created_at  # type: ignore[union-attr]
    out = await w.cleanups.cleanup("bad", ADMIN, since=T0, until=created_first)
    assert out.record is None  # until is exclusive: nothing before the first write


async def test_the_memory_date_does_not_decide_the_window(w: World) -> None:
    """``occurred_at`` is the memory date; only ``created_at`` counts."""
    draft = EntryDraft(
        kind=Kind.FACT,
        summary="happened long ago, written now",
        author="bad",
        agent="bad",
        scope="org",
        occurred_at=T0 - timedelta(days=400),
    )
    entry = await w.store.create_entry(draft, VEC)
    out = await w.clean()
    assert out.record is not None and out.record.withdrawn_ids == (entry.id,)


async def test_a_withdrawn_memory_leaves_the_fleet_pins_and_the_pins_are_recorded(
    w: World,
) -> None:
    pinned = await w.entry("pinned junk", scope="fleet")
    keep = await w.entry("someone else's pin", author="good", scope="fleet")
    await w.pin(pinned, "bad")
    await w.pin(keep, "good")
    out = await w.clean()
    assert [p.entry_id for p in await w.store.list_pins(w.fleet)] == [keep]
    assert out.record is not None
    assert [(p.entry_id, p.pinned_by) for p in out.record.unpinned_pins] == [(pinned, "bad")]
    unpins = await w.audit(AuditAction.ENTRY_UNPIN)
    assert [(r.target, r.detail["reason"]) for r in unpins] == [(pinned, "agent_cleanup")]
    assert unpins[0].detail["cleanup_id"] == out.record.id


async def test_a_withdrawn_memory_is_not_found_by_the_active_listing(w: World) -> None:
    junk = await w.entry("junk")
    await w.clean()
    listed = await w.store.list_entries(EntryFilters())
    assert junk not in {e.id for e in listed}


# -- the ratings -----------------------------------------------------------------------


async def test_ratings_in_the_window_are_set_aside_and_no_read_counts_them(w: World) -> None:
    target = await w.entry("target", author="good")
    other = await w.entry("other", author="good")
    await w.rate(target, "bad", Verdict.WRONG)
    await w.rate(target, "good", Verdict.HELPFUL, "ok")
    await w.rate(other, "bad", Verdict.HELPFUL)
    assert await w.counts(target) == (1, 0, 1)

    out = await w.clean()

    assert out.ratings_set_aside == 2 and out.withdrawn == 0 and out.record is not None
    assert out.record.ratings_set_aside == 2
    assert await w.counts(target) == (1, 0, 0)  # only good's rating is left
    assert await w.counts(other) == (0, 0, 0)
    assert (await w.store.quality_counts([target, other])) == {
        target: (1, 0, 0),
        other: (0, 0, 0),
    }
    signals = await w.store.feedback_signals([target])
    assert (signals[target].stale, signals[target].wrong) == (0, 0)
    assert signals[target].correction is None and signals[target].newest_flag_at is None
    assert [fb.user for fb in await w.store.list_feedback(target, 10)] == ["good"]
    log = await w.store.list_feedback_log(FeedbackLogFilters(), 10)
    assert {(item.feedback.user, item.feedback.entry_id) for item in log} == {("good", target)}
    assert await w.store.list_entries(EntryFilters(flagged=True)) == []
    assert not [f for f in await w.store.flag_counts() if f.user == "bad"]
    assert len(w.store._feedback) == 3  # the rows are kept


async def test_set_aside_ratings_leave_the_health_flag_backlog_and_activity(w: World) -> None:
    target = await w.entry("target", author="good")
    await w.rate(target, "bad", Verdict.STALE)
    from hivemind.domain.feedback import FlagBacklogFilters

    now = w.clock()
    assert len(await w.store.list_flag_backlog(FlagBacklogFilters(), now, 10)) == 1
    assert (await w.store.fleet_health(now)).verdicts_90d == 1
    await w.clean()
    assert await w.store.list_flag_backlog(FlagBacklogFilters(), now, 10) == []
    assert (await w.store.fleet_health(now)).verdicts_90d == 0
    assert (await w.store.activity(T0)).reporters == ()


async def test_a_set_aside_helpful_rating_no_longer_lists_a_changed_memory(w: World) -> None:
    liked = await w.entry("relied on", author="good")
    await w.store.record_feedback(Feedback(liked, "bad", "a", Verdict.HELPFUL, "worked"))
    await w.store.withdraw_entry(liked, "gone", "good")
    filters = EntryFilters(relied_on_changed=True, reporter=("bad", "a"))
    assert [e.id for e in await w.store.list_entries(filters)] == [liked]
    await w.clean()
    assert await w.store.list_entries(filters) == []


async def test_rating_the_same_memory_again_clears_the_mark(w: World) -> None:
    target = await w.entry("target", author="good")
    await w.rate(target, "bad", Verdict.WRONG)
    await w.clean()
    assert await w.counts(target) == (0, 0, 0)
    await w.rate(target, "bad", Verdict.STALE)
    assert await w.counts(target) == (0, 1, 0)


async def test_ratings_outside_the_window_or_by_others_are_untouched(w: World) -> None:
    target = await w.entry("target", author="good")
    old = await w.entry("old target", author="good")
    await w.store.record_feedback(
        Feedback(old, "bad", "a", Verdict.WRONG, "n", updated_at=T0 - timedelta(days=3))
    )
    await w.rate(target, "good", Verdict.WRONG)
    out = await w.cleanups.cleanup("bad", ADMIN, since=T0, until=FAR)
    assert out.record is None  # nothing in the window
    assert await w.counts(old) == (0, 0, 1) and await w.counts(target) == (0, 0, 1)


async def test_an_overlapping_second_cleanup_does_not_take_over_the_first_ones_marks(
    w: World,
) -> None:
    target = await w.entry("target", author="good")
    await w.rate(target, "bad", Verdict.WRONG)
    first = await w.clean()
    other = await w.entry("later target", author="good")
    await w.rate(other, "bad", Verdict.WRONG)
    second = await w.clean()
    assert first.record is not None and second.record is not None
    assert (first.ratings_set_aside, second.ratings_set_aside) == (1, 1)
    await w.cleanups.undo(second.record.id, ADMIN)
    assert await w.counts(other) == (0, 0, 1)
    assert await w.counts(target) == (0, 0, 0)  # still the first cleanup's


# -- the restore -----------------------------------------------------------------------


async def test_memories_the_agent_superseded_come_back_when_ticked(w: World) -> None:
    keep = await w.entry("a good fact", author="good")
    other = await w.entry("another good fact", author="good")
    junk = await w.entry("junk replacing both", supersedes=(keep, other))
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert scan.restorable_total == 2 and {e.id for e in scan.restorable} == {keep, other}

    out = await w.clean(restore_ids=[keep])

    assert out.restored == 1 and out.record is not None
    assert [(r.entry_id, r.superseded_by) for r in out.record.restored] == [(keep, junk)]
    restored = await w.store.get_entry(keep)
    assert restored is not None and restored.state is EntryState.ACTIVE
    assert restored.superseded_by is None
    assert await w.state(other) is EntryState.SUPERSEDED  # not ticked, stays superseded
    assert await w.state(junk) is EntryState.WITHDRAWN


async def test_nothing_is_restored_unless_named(w: World) -> None:
    kept = await w.entry("a good fact", author="good")
    await w.entry("junk", supersedes=(kept,))
    out = await w.clean()
    assert out.restored == 0 and await w.state(kept) is EntryState.SUPERSEDED


async def test_only_memories_still_superseded_by_the_agents_withdrawn_memory_are_restorable(
    w: World,
) -> None:
    a = await w.entry("fact a", author="good")
    b = await w.entry("fact b", author="good")
    junk_a = await w.entry("junk over a", supersedes=(a,))
    junk_b = await w.entry("junk over b", supersedes=(b,))
    # Someone else supersedes the junk over b: the head is no longer the agent's.
    await w.entry("good rewrite", author="good", supersedes=(junk_b,))
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert [e.id for e in scan.restorable] == [a]
    assert {e.id for e in scan.withdraw} == {junk_a}
    with pytest.raises(AgentCleanupRefused, match="cannot be restored"):
        await w.clean(restore_ids=[b])
    assert await w.state(junk_a) is EntryState.ACTIVE  # the refusal changed nothing


async def test_a_memory_that_is_not_restorable_is_refused_and_nothing_changes(w: World) -> None:
    junk = await w.entry("junk")
    plain = await w.entry("plain good", author="good")
    for bad_id in (plain, junk, "not-a-uuid", "00000000-0000-4000-8000-00000000dead"):
        with pytest.raises(AgentCleanupRefused):
            await w.clean(restore_ids=[bad_id])
    assert await w.state(junk) is EntryState.ACTIVE
    assert await w.store.list_agent_cleanups(10) == []


async def test_a_chain_of_the_agents_own_junk_is_walked_to_the_memory_under_it(w: World) -> None:
    """junk1 over good, junk2 over junk1: the good memory is offered, the
    agent's own superseded junk1 is not, and restoring leaves junk1 superseded."""
    good = await w.entry("good fact", author="good")
    first = await w.entry("junk one", supersedes=(good,))
    second = await w.entry("junk two", supersedes=(first,))
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert [e.id for e in scan.withdraw] == [second]
    assert [e.id for e in scan.restorable] == [good]
    outcome = await w.clean(restore_ids=[good])
    assert outcome.restored == 1
    assert await w.state(good) is EntryState.ACTIVE
    assert await w.state(first) is EntryState.SUPERSEDED
    assert await w.state(second) is EntryState.WITHDRAWN
    assert outcome.record is not None
    assert [(r.entry_id, r.superseded_by, r.head) for r in outcome.record.restored] == [
        (good, first, second)
    ]


async def test_a_chain_broken_by_another_author_or_a_withdrawal_is_not_offered(w: World) -> None:
    good = await w.entry("good fact", author="good")
    mid = await w.entry("junk one", supersedes=(good,))
    other = await w.entry("good rewrite", author="good", supersedes=(mid,))
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert scan.restorable == () and scan.withdraw == ()
    assert await w.state(other) is EntryState.ACTIVE


async def test_the_agents_own_superseded_window_memory_is_never_offered(w: World) -> None:
    first = await w.entry("junk one")
    await w.entry("junk two", supersedes=(first,))
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert scan.restorable == () and scan.restorable_total == 0


async def test_undo_replaces_a_restored_memory_only_while_the_chain_head_is_active(
    w: World,
) -> None:
    good = await w.entry("good fact", author="good")
    first = await w.entry("junk one", supersedes=(good,))
    second = await w.entry("junk two", supersedes=(first,))
    outcome = await w.clean(restore_ids=[good])
    assert outcome.record is not None
    undone = await w.cleanups.undo(outcome.record.id, ADMIN)
    assert (undone.resuperseded, undone.resupersede_skipped) == (1, 0)
    assert await w.state(good) is EntryState.SUPERSEDED
    assert await w.state(second) is EntryState.ACTIVE
    # The head stays withdrawn (an admin re-withdrew it by hand, so its reason is no longer
    # this cleanup's): the undo cannot re-activate it, so the restored memory stays active.
    again = await w.clean(restore_ids=[good])
    assert again.record is not None
    from dataclasses import replace

    held = w.store._entries[second]  # type: ignore[attr-defined]
    w.store._entries[second] = replace(held, withdrawn_reason="by hand")  # type: ignore[attr-defined]
    undone = await w.cleanups.undo(again.record.id, ADMIN)
    assert (undone.resuperseded, undone.resupersede_skipped) == (0, 1)
    assert await w.state(good) is EntryState.ACTIVE


async def test_a_pin_on_an_older_version_of_a_withdrawn_memory_comes_off_unless_restored(
    w: World,
) -> None:
    kept = await w.entry("old pinned a", author="good", scope="fleet")
    dropped = await w.entry("old pinned b", author="good", scope="fleet")
    await w.entry("junk over a", scope="fleet", supersedes=(kept,))
    await w.entry("junk over b", scope="fleet", supersedes=(dropped,))
    await w.pin(kept, "good")
    await w.pin(dropped, "good")
    outcome = await w.clean(restore_ids=[kept])
    assert {p.entry_id for p in outcome.unpinned} == {dropped}
    assert {p.entry_id for p in await w.store.list_pins(w.fleet)} == {kept}
    assert outcome.record is not None
    undone = await w.cleanups.undo(outcome.record.id, ADMIN)
    assert {p.entry_id for p in undone.pins_restored} == {dropped}
    assert {p.entry_id for p in await w.store.list_pins(w.fleet)} == {kept, dropped}


# -- preview ----------------------------------------------------------------------------


async def test_the_preview_changes_nothing_and_reports_counts(w: World) -> None:
    kept = await w.entry("good fact", author="good")
    pinned = await w.entry("pinned junk", scope="fleet", supersedes=(kept,))
    await w.pin(pinned, "bad")
    await w.rate(kept, "bad", Verdict.WRONG)
    await w.rate(kept, "good", Verdict.HELPFUL)

    preview = await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)

    assert preview.agent.name == "bad" and (preview.since, preview.until) == (T0, FAR)
    scan = preview.scan
    assert [e.id for e in scan.withdraw] == [pinned] and scan.withdraw_total == 1
    assert (scan.ratings_total, dict(scan.ratings_by_verdict)) == (1, {"wrong": 1})
    assert scan.pins_total == 1 and [e.id for e in scan.restorable] == [kept]
    assert await w.state(pinned) is EntryState.ACTIVE
    assert await w.counts(kept) == (1, 0, 1)
    assert len(await w.store.list_pins(w.fleet)) == 1
    assert (
        await w.store.list_agent_cleanups(10) == []
        and await w.audit(AuditAction.AGENT_CLEANUP) == []
    )


async def test_the_preview_cuts_its_lists_and_keeps_exact_totals(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hivemind.memstore as memstore

    monkeypatch.setattr(memstore, "PREVIEW_WITHDRAW_LIMIT", 2)
    for i in range(5):
        await w.entry(f"junk {i}")
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    assert (len(scan.withdraw), scan.withdraw_total) == (2, 5)
    assert [e.summary for e in scan.withdraw] == ["junk 4", "junk 3"]  # newest first


async def test_until_defaults_to_now_and_is_returned(w: World) -> None:
    await w.entry("junk")
    preview = await w.cleanups.preview("bad", ADMIN, since=T0)
    assert preview.until > T0 and preview.scan.withdraw_total == 1


# -- the guards -------------------------------------------------------------------------


async def test_the_apply_is_refused_when_the_preview_is_stale(w: World) -> None:
    await w.entry("junk")
    target = await w.entry("target", author="good")
    await w.rate(target, "bad")
    scan = (await w.cleanups.preview("bad", ADMIN, since=T0, until=FAR)).scan
    await w.entry("more junk after the preview")
    with pytest.raises(AgentCleanupRefused, match="preview again"):
        await w.clean(expected_withdraw=scan.withdraw_total, expected_ratings=scan.ratings_total)
    assert await w.store.list_agent_cleanups(10) == []
    assert (await w.counts(target)) == (0, 0, 1)
    with pytest.raises(AgentCleanupRefused, match="preview again"):
        await w.clean(expected_withdraw=2, expected_ratings=0)
    out = await w.clean(expected_withdraw=2, expected_ratings=1)
    assert (out.withdrawn, out.ratings_set_aside) == (2, 1)


async def test_an_empty_window_is_refused(w: World) -> None:
    for call in (
        w.cleanups.preview("bad", ADMIN, since=T0, until=T0),
        w.cleanups.cleanup("bad", ADMIN, since=FAR, until=T0),
    ):
        with pytest.raises(InvalidInput):
            await call


async def test_nothing_to_clean_keeps_no_record_and_writes_no_audit(w: World) -> None:
    out = await w.clean()
    assert out.record is None and (out.withdrawn, out.ratings_set_aside, out.restored) == (0, 0, 0)
    assert await w.store.list_agent_cleanups(10) == []
    assert await w.audit(AuditAction.AGENT_CLEANUP) == []


async def test_an_empty_cleanup_does_not_block_the_undo_of_an_older_one(w: World) -> None:
    await w.entry("junk")
    first = await w.clean()
    await w.clean()  # nothing left, no record
    assert first.record is not None
    undone = await w.cleanups.undo(first.record.id, ADMIN)
    assert undone.reactivated == 1


# -- admin only --------------------------------------------------------------------------


async def test_every_call_is_admin_only_and_refuses_before_looking(w: World) -> None:
    await w.entry("junk")
    done = await w.clean()
    assert done.record is not None
    for cred in (AGENT, Credential(user_id="org", is_org=True)):
        for call in (
            w.cleanups.preview("bad", cred, since=T0, until=FAR),
            w.cleanups.preview("ghost", cred, since=T0, until=FAR),  # no 404 for a non-admin
            w.cleanups.cleanup("bad", cred, since=T0, until=FAR),
            w.cleanups.cleanup("ghost", cred, since=FAR, until=T0),  # no 422 either
            w.cleanups.undo(done.record.id, cred),
            w.cleanups.undo("not-a-uuid", cred),
            w.cleanups.list_cleanups(cred),
        ):
            with pytest.raises(PermissionDenied):
                await call
    assert len(await w.store.list_agent_cleanups(10)) == 1
    assert (await w.store.list_agent_cleanups(10))[0].undone_at is None


async def test_an_unknown_agent_is_refused(w: World) -> None:
    with pytest.raises(UnknownAgent):
        await w.cleanups.preview("ghost", ADMIN, since=T0, until=FAR)
    with pytest.raises(UnknownAgent):
        await w.cleanups.cleanup("ghost", ADMIN, since=T0, until=FAR)


# -- also revoke --------------------------------------------------------------------------


async def test_also_revoke_revokes_after_the_cleanup_as_its_own_audited_action(
    w: World,
) -> None:
    await w.entry("junk")
    out = await w.clean(also_revoke=True)
    assert out.revoked and out.record is not None and out.record.revoked
    assert (await w.store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    stored = await w.store.get_agent_cleanup(out.record.id)
    assert stored is not None and stored.revoked
    cleanup_rows = await w.audit(AuditAction.AGENT_CLEANUP)
    revoke_rows = await w.audit(AuditAction.AGENT_REVOKE)
    assert [r.target for r in cleanup_rows] == ["bad"] and [r.target for r in revoke_rows] == [
        "bad"
    ]
    assert cleanup_rows[0].occurred_at <= revoke_rows[0].occurred_at
    assert cleanup_rows[0].detail["also_revoke"] is True


async def test_without_the_tick_the_agent_stays_as_it_is(w: World) -> None:
    await w.entry("junk")
    out = await w.clean()
    assert not out.revoked and out.record is not None and not out.record.revoked
    assert (await w.store.get_agent("bad")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert await w.audit(AuditAction.AGENT_REVOKE) == []


async def test_an_agent_that_is_already_revoked_is_left_alone(w: World) -> None:
    await w.entry("junk")
    await w.access.revoke("bad", ADMIN)
    out = await w.clean(also_revoke=True)
    assert out.withdrawn == 1 and not out.revoked and out.already_revoked
    assert len(await w.audit(AuditAction.AGENT_REVOKE)) == 1  # only the first one


async def test_also_revoke_without_an_authenticator_refuses_before_anything_changes() -> None:
    store = MemoryStore(TickingClock())
    service = AccessService(store).cleanups  # no authenticator: dev mode
    await store.register_agent("bad")
    await store.activate_agent("bad", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id="f")
    entry = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="junk", author="bad", agent="bad", scope="org"), VEC
    )
    with pytest.raises(PermissionDenied):
        await service.cleanup("bad", ADMIN, since=T0, until=FAR, also_revoke=True)
    stored = await store.get_entry(entry.id)
    assert stored is not None and stored.state is EntryState.ACTIVE
    out = await service.cleanup("bad", ADMIN, since=T0, until=FAR)  # without the tick it works
    assert out.withdrawn == 1


async def test_a_failed_revoke_after_the_commit_is_reported_not_raised(w: World) -> None:
    junk = await w.entry("junk")

    async def broken(name: str, credential: object) -> None:
        raise RuntimeError("keys backend down")

    w.cleanups._revoke = broken  # type: ignore[assignment]
    out = await w.clean(also_revoke=True)
    assert out.withdrawn == 1 and not out.revoked and not out.already_revoked
    assert out.revoke_error is not None and "RuntimeError" in out.revoke_error
    assert "keys backend down" not in out.revoke_error  # no raw exception text
    assert out.record is not None and not out.record.revoked
    assert await w.state(junk) is EntryState.WITHDRAWN  # the cleanup stands
    assert len(await w.audit(AuditAction.AGENT_CLEANUP)) == 1


async def test_a_revoke_that_loses_a_race_to_another_revoke_counts_as_already_revoked(
    w: World,
) -> None:
    from hivemind.domain.access import InvalidAgentStatus

    await w.entry("junk")

    async def raced(name: str, credential: object) -> None:
        raise InvalidAgentStatus(name, AgentStatus.REVOKED, "revoke")

    w.cleanups._revoke = raced  # type: ignore[assignment]
    out = await w.clean(also_revoke=True)
    assert out.withdrawn == 1 and out.already_revoked and out.revoke_error is None


async def test_nothing_to_clean_still_revokes_when_asked(w: World) -> None:
    out = await w.clean(also_revoke=True)
    assert out.record is None and out.revoked
    assert (await w.store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert await w.audit(AuditAction.AGENT_CLEANUP) == []


# -- audit --------------------------------------------------------------------------------


async def test_the_cleanup_and_its_undo_are_audited_with_counts(w: World) -> None:
    kept = await w.entry("good", author="good")
    pinned = await w.entry("pinned junk", scope="fleet", supersedes=(kept,))
    await w.entry("more junk")
    await w.pin(pinned, "bad")
    await w.rate(kept, "bad")
    out = await w.clean(restore_ids=[kept])
    assert out.record is not None
    (row,) = await w.audit(AuditAction.AGENT_CLEANUP)
    assert (row.actor_kind.value, row.actor, row.target) == (
        "admin_key",
        "admin:abcdef012345",
        "bad",
    )
    assert row.detail["cleanup_id"] == out.record.id
    counts = {
        k: row.detail[k] for k in ("withdrawn", "ratings_set_aside", "restored", "pins_removed")
    }
    assert counts == {"withdrawn": 2, "ratings_set_aside": 1, "restored": 1, "pins_removed": 1}
    assert out.record.cleaned_by == "admin:abcdef012345"

    await w.cleanups.undo(out.record.id, ADMIN)
    (undo_row,) = await w.audit(AuditAction.AGENT_CLEANUP_UNDO)
    assert undo_row.target == "bad" and undo_row.detail["cleanup_id"] == out.record.id
    assert {
        k: undo_row.detail[k]
        for k in ("reactivated", "resuperseded", "ratings_cleared", "pins_restored")
    } == {
        "reactivated": 2,
        "resuperseded": 1,
        "ratings_cleared": 1,
        "pins_restored": 1,
    }
    assert [r.detail["reason"] for r in await w.audit(AuditAction.ENTRY_PIN)] == [
        "agent_cleanup_undo"
    ]


# -- undo ---------------------------------------------------------------------------------


async def test_undo_restores_exactly_what_the_cleanup_changed(w: World) -> None:
    good = await w.entry("good fact", author="good")
    pinned = await w.entry("pinned junk", scope="fleet", supersedes=(good,))
    plain = await w.entry("plain junk")
    rated = await w.entry("rated", author="good")
    await w.pin(pinned, "bad")
    await w.rate(rated, "bad", Verdict.WRONG)
    out = await w.clean(restore_ids=[good])
    assert out.record is not None

    undone = await w.cleanups.undo(out.record.id, ADMIN)

    assert (undone.reactivated, undone.reactivate_skipped) == (2, 0)
    assert (undone.resuperseded, undone.resupersede_skipped) == (1, 0)
    assert undone.ratings_cleared == 1 and undone.record.undone_at is not None
    for entry_id in (pinned, plain):
        entry = await w.store.get_entry(entry_id)
        assert entry is not None and entry.state is EntryState.ACTIVE
        assert entry.withdrawn_reason is None
    back = await w.store.get_entry(good)
    assert back is not None and back.state is EntryState.SUPERSEDED and back.superseded_by == pinned
    assert await w.counts(rated) == (0, 0, 1)
    assert [p.entry_id for p in await w.store.list_pins(w.fleet)] == [pinned]
    assert w.store._set_aside == {}


async def test_undo_leaves_a_memory_that_is_no_longer_withdrawn_by_this_cleanup(
    w: World,
) -> None:
    one = await w.entry("junk one")
    two = await w.entry("junk two")
    out = await w.clean()
    assert out.record is not None
    # Someone re-activated one by hand, as a later cleanup's undo or a script would.
    w.store._entries[one] = replace(
        w.store._entries[one], state=EntryState.ACTIVE, withdrawn_reason=None
    )
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert (undone.reactivated, undone.reactivate_skipped) == (1, 1)
    assert await w.state(two) is EntryState.ACTIVE


async def test_undo_does_not_resupersede_a_memory_someone_superseded_since(w: World) -> None:
    good = await w.entry("good fact", author="good")
    await w.entry("junk", supersedes=(good,))
    out = await w.clean(restore_ids=[good])
    assert out.record is not None
    later = await w.entry("a better fact", author="good", supersedes=(good,))
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert (undone.resuperseded, undone.resupersede_skipped) == (0, 1)
    entry = await w.store.get_entry(good)
    assert entry is not None and entry.superseded_by == later


async def test_undo_does_not_resupersede_when_the_old_successor_is_not_active(w: World) -> None:
    good = await w.entry("good fact", author="good")
    junk = await w.entry("junk", supersedes=(good,))
    out = await w.clean(restore_ids=[good])
    assert out.record is not None
    # The successor cannot come back: it was changed after the cleanup.
    w.store._entries[junk] = replace(w.store._entries[junk], withdrawn_reason="by hand")
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert undone.reactivated == 0 and undone.resuperseded == 0
    assert await w.state(good) is EntryState.ACTIVE  # stays as it is


async def test_undo_repins_where_the_fleet_is_under_the_cap_and_reports_the_rest(
    w: World,
) -> None:
    pinned = [await w.entry(f"junk {i}", scope="fleet") for i in range(2)]
    for i, entry_id in enumerate(pinned):
        await w.pin(entry_id, f"pinner-{i}")
    out = await w.clean()
    assert out.record is not None and len(out.unpinned) == 2
    # The fleet fills up in the meantime.
    for i in range(10):
        await w.pin(await w.entry(f"fresh {i}", author="good", scope="fleet"), f"other-{i}")
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert (len(undone.pins_restored), len(undone.pins_skipped)) == (0, 2)
    assert len(await w.store.list_pins(w.fleet)) == 10
    # Both are active again, just not pinned.
    for entry_id in pinned:
        assert await w.state(entry_id) is EntryState.ACTIVE


async def test_undo_repins_the_ones_that_fit_newest_first(w: World) -> None:
    pinned = [await w.entry(f"junk {i}", scope="fleet") for i in range(3)]
    for i, entry_id in enumerate(pinned):
        await w.pin(entry_id, f"pinner-{i}")
    out = await w.clean()
    assert out.record is not None
    for i in range(8):
        await w.pin(await w.entry(f"fresh {i}", author="good", scope="fleet"), f"other-{i}")
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert [p.entry_id for p in undone.pins_restored] == [pinned[2], pinned[1]]
    assert [p.entry_id for p in undone.pins_skipped] == [pinned[0]]


async def test_only_the_newest_standing_cleanup_of_an_agent_can_be_undone(w: World) -> None:
    await w.entry("junk one")
    first = await w.clean()
    await w.entry("junk two")
    second = await w.clean()
    assert first.record is not None and second.record is not None
    views = await w.cleanups.list_cleanups(ADMIN)
    assert [(v.record.id, v.undoable) for v in views] == [
        (second.record.id, True),
        (first.record.id, False),
    ]
    assert views[1].blocked is not None and second.record.id in views[1].blocked
    with pytest.raises(AgentCleanupRefused, match="newer"):
        await w.cleanups.undo(first.record.id, ADMIN)
    await w.cleanups.undo(second.record.id, ADMIN)
    with pytest.raises(AgentCleanupRefused, match="already undone"):
        await w.cleanups.undo(second.record.id, ADMIN)
    await w.cleanups.undo(first.record.id, ADMIN)  # the newest standing one now


async def test_a_cleanup_of_another_agent_does_not_block_an_undo(w: World) -> None:
    await w.entry("junk one")
    first = await w.clean()
    await w.entry("junk of good", author="good")
    await w.cleanups.cleanup("good", ADMIN, since=T0, until=FAR)
    assert first.record is not None
    await w.cleanups.undo(first.record.id, ADMIN)


async def test_undo_of_an_unknown_cleanup_is_refused(w: World) -> None:
    for bad in ("00000000-0000-4000-8000-00000000dead", "not-a-uuid"):
        with pytest.raises(UnknownAgentCleanup):
            await w.cleanups.undo(bad, ADMIN)


async def test_undo_does_not_unrevoke_the_agent(w: World) -> None:
    await w.entry("junk")
    out = await w.clean(also_revoke=True)
    assert out.record is not None
    undone = await w.cleanups.undo(out.record.id, ADMIN)
    assert undone.record.revoked
    assert (await w.store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]


async def test_a_second_cleanup_after_an_undo_cleans_again(w: World) -> None:
    junk = await w.entry("junk")
    first = await w.clean()
    assert first.record is not None
    await w.cleanups.undo(first.record.id, ADMIN)
    second = await w.clean()
    assert second.withdrawn == 1 and second.record is not None
    entry = await w.store.get_entry(junk)
    assert entry is not None and entry.withdrawn_reason == cleanup_reason(second.record.id)


# -- the pure rules --------------------------------------------------------------------------


def _record(id_: str, agent: str = "bad", undone: bool = False) -> AgentCleanupRecord:
    return AgentCleanupRecord(
        id=id_,
        agent=agent,
        since=T0,
        until=FAR,
        cleaned_by="x",
        cleaned_at=T0,
        undone_at=T0 if undone else None,
    )


def test_undo_refusal_rule() -> None:
    newest, older, other, done = (
        _record("n"),
        _record("o"),
        _record("x", "good"),
        _record("d", undone=True),
    )
    listing = [newest, other, older, done]
    assert undo_refusal(newest, listing) is None
    assert undo_refusal(other, listing) is None  # another agent's, newest of its own
    assert "newer" in (undo_refusal(older, listing) or "")
    assert "already undone" in (undo_refusal(done, listing) or "")
    # A newer cleanup that was undone does not block.
    assert undo_refusal(older, [_record("n2", undone=True), older]) is None


def test_restore_ids_must_all_be_restorable() -> None:
    assert check_restore_ids(["a", "b"], ["b", "b", "a"]) == ["b", "a"]
    assert check_restore_ids(["a"], None) == []
    with pytest.raises(AgentCleanupRefused, match="x"):
        check_restore_ids(["a"], ["a", "x"])


# -- caches -----------------------------------------------------------------------------------


async def test_a_cleanup_and_an_undo_drop_the_cached_metrics_and_activity_scans() -> None:
    from hivemind.services.wiring import build_services

    store = MemoryStore(TickingClock())
    services = build_services(
        store, make_embedder(), make_search_config(), metrics_entry_counts_ttl=3600.0
    )
    await store.register_agent("bad")
    for i in range(2):
        await store.create_entry(
            EntryDraft(kind=Kind.FACT, summary=f"n{i}", author="bad", agent="bad", scope="org"), VEC
        )
    await services.activity_service.report()
    assert services.activity_service._cache  # warm
    out = await services.access_service.cleanups.cleanup("bad", ADMIN, since=T0, until=FAR)
    assert out.record is not None
    assert not services.activity_service._cache
    await services.activity_service.report()
    await services.access_service.cleanups.undo(out.record.id, ADMIN)
    assert not services.activity_service._cache


# -- REST ---------------------------------------------------------------------------------------


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
        },
        store=store,
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
    await store.register_agent("bad")
    await store.activate_agent("bad", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id="x")
    yield client, store
    await client.aclose()


ADMIN_H = {"X-API-Key": "hm_admin"}
WINDOW = {"since": "2026-10-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}


NONE_YET = {"expected_withdraw": 0, "expected_ratings": 0}


def _url(name: str = "bad") -> str:
    return f"/v1/admin/agents/{name}/cleanup"


async def _junk(store: MemoryStore, summary: str, supersedes: tuple[str, ...] = ()) -> str:
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author="bad",
        agent="bad",
        scope="org",
        supersedes=supersedes,
    )
    return (await store.create_entry(draft, VEC)).id


async def test_rest_preview_apply_list_and_undo(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    good = (
        await store.create_entry(
            EntryDraft(kind=Kind.FACT, summary="good", author="g", agent="g", scope="org"), VEC
        )
    ).id
    junk = await _junk(store, "junk", supersedes=(good,))
    await store.record_feedback(Feedback(good, "bad", "a", Verdict.WRONG, "n"))

    resp = await client.post(_url(), json={**WINDOW, "dry_run": True}, headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["dry_run"] is True and preview["agent"] == "bad"
    assert preview["withdraw_total"] == 1 and preview["withdraw"][0]["id"] == junk
    assert preview["withdraw"][0]["summary"] == "junk" and preview["withdraw_truncated"] is False
    assert preview["ratings_total"] == 1 and preview["ratings_by_verdict"] == {"wrong": 1}
    assert [e["id"] for e in preview["restorable"]] == [good]
    assert preview["restorable"][0]["superseded_by"] == junk
    assert preview["nothing_to_clean"] is False and preview["until"].startswith("2027-01-01")
    assert (await store.get_entry(junk)).state is EntryState.ACTIVE  # type: ignore[union-attr]

    resp = await client.post(
        _url(),
        json={
            **WINDOW,
            "dry_run": False,
            "restore_ids": [good],
            "expected_withdraw": preview["withdraw_total"],
            "expected_ratings": preview["ratings_total"],
        },
        headers=ADMIN_H,
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["dry_run"] is False and done["cleanup_id"] and done["nothing_to_clean"] is False
    assert (done["withdrawn"], done["ratings_set_aside"], done["restored"]) == (1, 1, 1)
    assert done["revoked"] is False and done["pins_removed"] == []
    assert (await store.get_entry(good)).state is EntryState.ACTIVE  # type: ignore[union-attr]

    listing = (await client.get("/v1/admin/agent-cleanups", headers=ADMIN_H)).json()
    assert [(c["id"], c["undoable"], c["agent"]) for c in listing] == [
        (done["cleanup_id"], True, "bad")
    ]
    assert (listing[0]["withdrawn"], listing[0]["restored"], listing[0]["revoked"]) == (1, 1, False)

    resp = await client.post(f"/v1/admin/agent-cleanups/{done['cleanup_id']}/undo", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    undone = resp.json()
    assert (undone["reactivated"], undone["resuperseded"], undone["ratings_cleared"]) == (1, 1, 1)
    assert undone["note"] is None
    resp = await client.post(f"/v1/admin/agent-cleanups/{done['cleanup_id']}/undo", headers=ADMIN_H)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "cleanup_refused")


async def test_rest_also_revoke_and_the_undo_note(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    await _junk(store, "junk")
    resp = await client.post(
        _url(),
        json={**WINDOW, "dry_run": False, "also_revoke": True, **NONE_YET, "expected_withdraw": 1},
        headers=ADMIN_H,
    )
    done = resp.json()
    assert resp.status_code == 200 and done["revoked"] is True
    resp = await client.post(f"/v1/admin/agent-cleanups/{done['cleanup_id']}/undo", headers=ADMIN_H)
    assert "stays revoked" in resp.json()["note"]
    assert (await store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    again = await client.post(
        _url(),
        json={**WINDOW, "dry_run": False, "also_revoke": True, **NONE_YET, "expected_withdraw": 1},
        headers=ADMIN_H,
    )
    assert again.json()["already_revoked"] is True and again.json()["revoked"] is False


async def test_rest_nothing_to_clean_is_200_with_zero_counts(rest) -> None:  # type: ignore[no-untyped-def]
    client, _ = rest
    preview = await client.post(_url(), json={**WINDOW, "dry_run": True}, headers=ADMIN_H)
    assert preview.status_code == 200 and preview.json()["nothing_to_clean"] is True
    resp = await client.post(_url(), json={**WINDOW, "dry_run": False, **NONE_YET}, headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cleanup_id"] is None and body["nothing_to_clean"] is True
    assert (body["withdrawn"], body["ratings_set_aside"], body["restored"]) == (0, 0, 0)
    assert (await client.get("/v1/admin/agent-cleanups", headers=ADMIN_H)).json() == []


async def test_rest_errors(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    junk = await _junk(store, "junk")

    def post(name: str = "bad", **body: object):  # type: ignore[no-untyped-def]
        return client.post(_url(name), json=body, headers=ADMIN_H)

    assert (await post(**WINDOW)).status_code == 422  # dry_run is required
    assert (await post(until=WINDOW["until"], dry_run=True)).status_code == 422  # since too
    assert (await post(since="not a date", dry_run=True)).status_code == 422
    resp = await post(since=WINDOW["until"], until=WINDOW["since"], dry_run=False)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    resp = await post("ghost", **WINDOW, dry_run=True)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")
    resp = await post("ghost", **WINDOW, dry_run=False, **NONE_YET)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")
    # An apply must carry the preview's totals.
    for missing in ({}, {"expected_withdraw": 1}, {"expected_ratings": 0}):
        resp = await post(**WINDOW, dry_run=False, **missing)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_input")
    resp = await post(
        **WINDOW, dry_run=False, restore_ids=[junk], expected_withdraw=1, expected_ratings=0
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "cleanup_refused")
    resp = await post(**WINDOW, dry_run=False, expected_withdraw=7, expected_ratings=0)
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "cleanup_refused")
    assert "preview again" in resp.json()["error"]["message"]
    assert (await store.get_entry(junk)).state is EntryState.ACTIVE  # type: ignore[union-attr]
    resp = await client.post("/v1/admin/agent-cleanups/not-a-uuid/undo", headers=ADMIN_H)
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "not_found")


async def test_rest_a_naive_window_is_read_as_utc(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    await _junk(store, "junk")
    resp = await client.post(
        _url(),
        json={"since": "2026-10-01T00:00:00", "dry_run": True, "until": "2027-01-01T00:00:00"},
        headers=ADMIN_H,
    )
    assert resp.status_code == 200 and resp.json()["withdraw_total"] == 1


async def test_rest_non_admin_keys_get_403_before_anything_is_looked_at(rest) -> None:  # type: ignore[no-untyped-def]
    client, store = rest
    junk = await _junk(store, "junk")
    for key in ("k-dev", "hm_org"):
        h = {"X-API-Key": key}
        for resp in (
            await client.post(_url(), json={**WINDOW, "dry_run": True}, headers=h),
            await client.post(_url(), json={**WINDOW, "dry_run": False}, headers=h),
            await client.post(_url("ghost"), json={**WINDOW, "dry_run": False}, headers=h),
            await client.get("/v1/admin/agent-cleanups", headers=h),
            await client.post("/v1/admin/agent-cleanups/not-a-uuid/undo", headers=h),
        ):
            assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"
    assert (await store.get_entry(junk)).state is EntryState.ACTIVE  # type: ignore[union-attr]
    assert await store.list_agent_cleanups(10) == []
