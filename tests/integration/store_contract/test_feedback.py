"""Feedback, last confirmed and the evidence on a hit (SPEC §4.2, §6.1; ADRs 0051, 0059) on both adapters."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from tests.integration.store_contract.helpers import NO_SUCH_ID, T0, Adapter, _vis, write

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import Entry, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import (
    Correction,
    Feedback,
    FeedbackLogFilters,
    FeedbackSignals,
    FlagBacklogFilters,
    Verdict,
    is_contested,
)

# -- feedback (SPEC §4.2, ADRs 0051, 0059) ---------------------------------------------


async def test_feedback_upserts_per_reporter_and_lists_newest_first(adapter: Adapter) -> None:
    entry = await write(adapter, "rated")
    other = await write(adapter, "unrated")
    store = adapter.store
    await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.HELPFUL, "ok", T0))
    await store.record_feedback(
        Feedback(entry.id, "u1", "a", Verdict.STALE, "now stale", T0 + timedelta(minutes=1))
    )
    await store.record_feedback(Feedback(entry.id, "u2", "a", Verdict.WRONG, None, T0))
    assert await store.feedback_counts(entry.id) == (0, 1, 1)
    rows = await store.list_feedback(entry.id, 10)
    assert [(r.user, r.verdict, r.note) for r in rows] == [
        ("u1", Verdict.STALE, "now stale"),
        ("u2", Verdict.WRONG, None),
    ]
    assert len(await store.list_feedback(entry.id, 1)) == 1
    many = await store.list_feedback_many([entry.id, other.id], 10)
    assert [r.user for r in many[entry.id]] == ["u1", "u2"]
    assert many[other.id] == []
    counts = await store.quality_counts([entry.id, other.id])
    assert counts == {entry.id: (0, 1, 1), other.id: (0, 0, 0)}
    # Keyed by (user, agent): the same user's second agent adds a verdict
    # instead of overwriting the first agent's.
    await store.record_feedback(
        Feedback(entry.id, "u1", "b", Verdict.HELPFUL, None, T0 + timedelta(minutes=2))
    )
    assert await store.feedback_counts(entry.id) == (1, 1, 1)


async def test_feedback_ties_break_on_the_reporter_and_limit_keeps_the_newest(
    adapter: Adapter,
) -> None:
    """ADR 0051: newest first, equal times ordered by reporter, notes included,
    at most ``limit`` rows; the batched read (ADR 0059) answers the same."""
    entry = await write(adapter, "A fact")
    other = await write(adapter, "Another fact")
    store = adapter.store
    for user, at, note in (
        ("b", T0, "old b"),
        ("a", T0, "old a"),
        ("c", T0 + timedelta(seconds=1), "newest"),
    ):
        await store.record_feedback(Feedback(entry.id, user, user, Verdict.STALE, note, at))
    await store.record_feedback(Feedback(other.id, "d", "d", Verdict.WRONG, None, T0))
    rows = await store.list_feedback(entry.id, 10)
    assert [(r.user, r.note, r.verdict) for r in rows] == [
        ("c", "newest", Verdict.STALE),
        ("a", "old a", Verdict.STALE),
        ("b", "old b", Verdict.STALE),
    ]
    assert rows[0].entry_id == entry.id
    assert rows[0].updated_at == T0 + timedelta(seconds=1)
    assert [r.user for r in await store.list_feedback(entry.id, 1)] == ["c"]
    assert await store.list_feedback("not-a-uuid", 10) == []
    many = await store.list_feedback_many([entry.id, other.id, "not-a-uuid"], 2)
    assert list(many) == [entry.id, other.id, "not-a-uuid"]
    assert many[entry.id] == rows[:2]
    assert [r.user for r in many[other.id]] == ["d"]
    assert many["not-a-uuid"] == []


async def test_a_revoked_reporters_feedback_does_not_count_until_reactivated(
    adapter: Adapter,
) -> None:
    """ADR 0067: rows stay, but are left out of every read while revoked."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    entry = await write(adapter, "rated")
    await store.register_agent("Scout")
    await store.record_feedback(Feedback(entry.id, "Scout", "Scout", Verdict.STALE, "old", T0))
    await store.record_feedback(Feedback(entry.id, "admin", "admin", Verdict.HELPFUL, None, T0))
    await store.record_feedback(Feedback(entry.id, "nobody", "nobody", Verdict.WRONG, None, T0))

    async def seen() -> tuple[object, ...]:
        flagged = await store.list_entries(EntryFilters(flagged=True))
        return (
            await store.feedback_counts(entry.id),
            (await store.quality_counts([entry.id]))[entry.id],
            sorted(r.user for r in await store.list_feedback(entry.id, 10)),
            sorted(r.user for r in (await store.list_feedback_many([entry.id], 10))[entry.id]),
            [e.id for e in flagged],
        )

    everyone = (
        (1, 1, 1),
        (1, 1, 1),
        ["Scout", "admin", "nobody"],
        ["Scout", "admin", "nobody"],
        [entry.id],
    )
    assert await seen() == everyone  # a pending agent still counts
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    # No agent record (admin, dev users, "nobody") keeps counting; "nobody" is wrong.
    assert await seen() == (
        (1, 0, 1),
        (1, 0, 1),
        ["admin", "nobody"],
        ["admin", "nobody"],
        [entry.id],
    )
    await store.record_feedback(Feedback(entry.id, "nobody", "nobody", Verdict.HELPFUL, None, T0))
    assert (await seen())[4] == []
    await store.record_feedback(Feedback(entry.id, "nobody", "nobody", Verdict.WRONG, None, T0))
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    assert await seen() == everyone


async def test_feedback_log_lists_rows_across_entries_with_their_entry(adapter: Adapter) -> None:
    store = adapter.store
    blue = await store.create_fleet("blue")
    red = await store.create_fleet("red")
    org = await write(adapter, "org fact")
    in_blue = await write(adapter, "blue fact", scope="fleet", fleet_id=blue.id, author="bob")
    in_red = await write(adapter, "red fact", scope="fleet", fleet_id=red.id, author="bob")
    mine = await write(adapter, "carol's own", scope="self", author="carol")
    t = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
    await store.record_feedback(Feedback(org.id, "u1", "a1", Verdict.HELPFUL, None, t(1)))
    await store.record_feedback(Feedback(in_blue.id, "u1", "a1", Verdict.WRONG, "no", t(2)))
    await store.record_feedback(Feedback(in_red.id, "u2", "a2", Verdict.STALE, None, t(3)))
    await store.record_feedback(Feedback(mine.id, "u2", "a2", Verdict.HELPFUL, "ok", t(4)))
    await store.register_agent("gone")
    await store.activate_agent("gone", trust_level=TrustLevel.LURKER, home_fleet_id=blue.id)
    await store.record_feedback(Feedback(org.id, "gone", "gone", Verdict.WRONG, None, t(5)))
    await store.revoke_agent("gone")  # ADR 0067: left out while revoked

    async def log(**kw: object) -> list[tuple[str, str]]:
        vis = kw.pop("visibility", None)
        limit = kw.pop("limit", 10)
        offset = kw.pop("offset", 0)
        rows = await store.list_feedback_log(
            FeedbackLogFilters(**kw),  # type: ignore[arg-type]
            limit,  # type: ignore[arg-type]
            offset,  # type: ignore[arg-type]
            visibility=vis,  # type: ignore[arg-type]
        )
        return [(r.entry_summary, r.feedback.user) for r in rows]

    everything = [
        ("carol's own", "u2"),
        ("red fact", "u2"),
        ("blue fact", "u1"),
        ("org fact", "u1"),
    ]
    assert await log() == everything
    assert await log(limit=2, offset=1) == everything[1:3]
    assert await log(reporter="u1") == everything[2:]
    assert await log(verdict=Verdict.HELPFUL) == [everything[0], everything[3]]
    assert await log(author="bob") == everything[1:3]
    assert await log(fleet_id=blue.id) == [("blue fact", "u1")]
    assert await log(entry_id=in_red.id.upper()) == [("red fact", "u2")]
    assert await log(entry_id="not-a-uuid") == []
    assert await log(entry_id="{" + in_red.id + "}") == []
    assert await log(fleet_id="not-a-uuid") == []
    # An agent of the blue fleet reads rows on org, blue and its own entries only.
    assert await log(visibility=_vis(TrustLevel.LURKER, "carol", blue.id)) == [
        ("carol's own", "u2"),
        ("blue fact", "u1"),
        ("org fact", "u1"),
    ]
    row = (await store.list_feedback_log(FeedbackLogFilters(entry_id=in_blue.id), 1))[0]
    assert (row.feedback.agent, row.feedback.verdict, row.feedback.note) == (
        "a1",
        Verdict.WRONG,
        "no",
    )
    assert (row.entry_kind, row.entry_author, row.entry_scope) == (Kind.FACT, "bob", "fleet")
    assert (row.entry_fleet_id, row.entry_state) == (blue.id, EntryState.ACTIVE)
    assert row.feedback.entry_id == in_blue.id


async def test_flag_backlog_lists_flagged_active_entries_oldest_flag_first(
    adapter: Adapter,
) -> None:
    """ADR 0093. Times are relative to the first write, as in the fleet
    health test, so the 14-day and 30-day edges are exact."""
    store = adapter.store
    blue = await store.create_fleet("blue")
    red = await store.create_fleet("red")
    for name in ("Gone", "Ghost"):
        await store.register_agent(name)
        await store.activate_agent(name, trust_level=TrustLevel.LURKER, home_fleet_id=red.id)
    old = await write(adapter, "old flag", author="bob", scope="fleet", fleet_id=blue.id)
    recent = await write(adapter, "recent flag", author="carol")
    gone = await write(adapter, "gone author", author="Gone", scope="fleet", fleet_id=red.id)
    ghosted = await write(adapter, "only a revoked reporter", author="bob")
    liked = await write(adapter, "only helpful", author="bob")
    replaced = await write(adapter, "superseded", author="bob")
    pulled = await write(adapter, "withdrawn", author="bob")
    await write(adapter, "successor", author="bob", supersedes=(replaced.id,))
    await store.withdraw_entry(pulled.id, None, by_user="bob")
    now = old.created_at + timedelta(hours=1)
    day = timedelta(days=1)
    W, S, H = Verdict.WRONG, Verdict.STALE, Verdict.HELPFUL
    for entry, user, verdict, at in (
        (old, "Scout", W, now - 20 * day),
        (old, "Rival", H, now - day),
        (old, "Ghost", S, now - 30 * day),  # revoked below: neither counted nor dated
        (recent, "Scout", S, now - 3 * day),
        (recent, "Rival", W, now - day),
        (gone, "Scout", S, now - 5 * day),
        (ghosted, "Ghost", W, now - 2 * day),
        (liked, "Scout", H, now - 2 * day),
        (replaced, "Scout", W, now - 2 * day),
        (pulled, "Scout", W, now - 2 * day),
    ):
        await store.record_feedback(Feedback(entry.id, user, user, verdict, None, at))
    await store.revoke_agent("Ghost")
    await store.revoke_agent("Gone")

    async def backlog(at: datetime = now, **kw: object) -> list[str]:
        vis = kw.pop("visibility", None)
        limit = kw.pop("limit", 10)
        offset = kw.pop("offset", 0)
        rows = await store.list_flag_backlog(
            FlagBacklogFilters(**kw),  # type: ignore[arg-type]
            at,
            limit,  # type: ignore[arg-type]
            offset,  # type: ignore[arg-type]
            visibility=vis,  # type: ignore[arg-type]
        )
        return [r.summary for r in rows]

    everything = ["old flag", "gone author", "recent flag"]
    assert await backlog() == everything
    assert await backlog(limit=1, offset=1) == ["gone author"]
    assert await backlog(flagged_before=now - 14 * day) == ["old flag"]
    assert await backlog(flagged_before=now - 5 * day) == ["old flag"]  # strictly older
    assert await backlog(orphaned=True) == ["gone author"]  # its author is revoked
    assert await backlog(fleet_id=blue.id) == ["old flag"]
    # The authors filter (ADR 0115): any of the names, none = no narrowing.
    assert await backlog(authors=("bob",)) == ["old flag"]
    assert await backlog(authors=("carol", "Gone")) == ["gone author", "recent flag"]
    assert await backlog(authors=("nobody",)) == []
    assert await backlog(fleet_id=blue.id.upper()) == ["old flag"]
    assert await backlog(fleet_id="not-a-uuid") == []
    assert await backlog(fleet_id="{" + blue.id + "}") == []
    # A red-fleet agent reads org entries and its home fleet's, not blue's.
    assert await backlog(visibility=_vis(TrustLevel.LURKER, "dora", red.id)) == [
        "gone author",
        "recent flag",
    ]
    # Every filter at once: the fleet, a member's visibility and the age.
    member = _vis(TrustLevel.LURKER, "dora", red.id)
    assert await backlog(fleet_id=red.id, visibility=member, flagged_before=now - 4 * day) == [
        "gone author"
    ]
    # A month on, nobody has written for 30 days: every author is gone.
    assert await backlog(now + 30 * day, orphaned=True) == everything
    # The list holds what the two Health counts count (ADR 0075), at any time.
    for at in (now, now + 30 * day):
        health = await store.fleet_health(at)
        assert len(await backlog(at, flagged_before=at - 14 * day)) == sum(
            r.flagged_backlog_14d for r in health.by_fleet
        )
        assert len(await backlog(at, orphaned=True)) == sum(
            r.orphaned_flagged for r in health.by_fleet
        )
    rows = await store.list_flag_backlog(FlagBacklogFilters(), now, 10)
    first, _, last = rows
    assert (first.entry_id, first.kind, first.author, first.scope) == (
        old.id,
        Kind.FACT,
        "bob",
        "fleet",
    )
    assert first.fleet_id == blue.id
    assert (first.first_flag_at, first.last_flag_at) == (now - 20 * day, now - 20 * day)
    assert (first.helpful, first.stale, first.wrong, first.orphaned) == (1, 0, 1, False)
    assert (last.first_flag_at, last.last_flag_at) == (now - 3 * day, now - day)
    assert (last.helpful, last.stale, last.wrong, last.fleet_id) == (0, 1, 1, None)
    assert rows[1].orphaned is True


async def test_feedback_lists_informative_reports_before_bare_helpful_ones(
    adapter: Adapter,
) -> None:
    """ADR 0064: a correction older than five bare helpful reports stays in
    the capped list, first; each group is newest first."""
    entry = await write(adapter, "corrected")
    other = await write(adapter, "other")
    store = adapter.store
    await store.record_feedback(Feedback(entry.id, "old", "a", Verdict.STALE, "now node 22", T0))
    await store.record_feedback(
        Feedback(entry.id, "noted", "a", Verdict.HELPFUL, "worked", T0 + timedelta(minutes=1))
    )
    await store.record_feedback(
        Feedback(entry.id, "empty", "a", Verdict.HELPFUL, "", T0 + timedelta(minutes=2))
    )
    for i in range(5):
        await store.record_feedback(
            Feedback(
                entry.id, f"bare{i}", "a", Verdict.HELPFUL, None, T0 + timedelta(minutes=10 + i)
            )
        )
    await store.record_feedback(
        Feedback(other.id, "b", "a", Verdict.HELPFUL, None, T0 + timedelta(minutes=1))
    )
    await store.record_feedback(Feedback(other.id, "w", "a", Verdict.WRONG, None, T0))
    expected = ["noted", "old", "bare4", "bare3", "bare2"]
    assert [r.user for r in await store.list_feedback(entry.id, 5)] == expected
    many = await store.list_feedback_many([entry.id, other.id], 5)
    assert [r.user for r in many[entry.id]] == expected
    assert [r.user for r in many[other.id]] == ["w", "b"]
    assert await store.feedback_counts(entry.id) == (7, 1, 0)


async def test_quality_counts_cover_every_well_formed_id(adapter: Adapter) -> None:
    counts = await adapter.store.quality_counts([NO_SUCH_ID, "not-a-uuid"])
    assert counts == {NO_SUCH_ID: (0, 0, 0)}


@pytest.mark.parametrize("respell", [str.upper, lambda i: i.replace("-", "")])
async def test_quality_counts_key_by_the_canonical_id_whatever_the_spelling(
    adapter: Adapter, respell: Callable[[str], str]
) -> None:
    entry = await write(adapter, "rated")
    await adapter.store.record_feedback(Feedback(entry.id, "u", "a", Verdict.WRONG, "n", _min(1)))
    counts = await adapter.store.quality_counts([respell(entry.id), entry.id])
    assert counts == {entry.id: (0, 0, 1)}


# -- last confirmed (SPEC §4.2) --------------------------------------------------------


def _min(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


async def _confirmed(adapter: Adapter, entry: Entry) -> datetime | None:
    return (await adapter.store.feedback_signals([entry.id]))[entry.id].last_confirmed_at


async def test_last_confirmed_needs_a_note(adapter: Adapter) -> None:
    entry = await write(adapter, "checked")
    store = adapter.store
    await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.HELPFUL, None, _min(1)))
    await store.record_feedback(Feedback(entry.id, "u2", "a", Verdict.HELPFUL, "  \n\t ", _min(2)))
    await store.record_feedback(Feedback(entry.id, "u3", "a", Verdict.HELPFUL, "", _min(3)))
    signals = (await store.feedback_signals([entry.id]))[entry.id]
    assert signals == FeedbackSignals(3, 0, 0, None)
    await store.record_feedback(Feedback(entry.id, "u4", "a", Verdict.HELPFUL, "ran it", _min(4)))
    assert await _confirmed(adapter, entry) == _min(4)


async def test_last_confirmed_is_cancelled_by_a_later_or_equal_flag_only(
    adapter: Adapter,
) -> None:
    store = adapter.store
    for verdict in (Verdict.STALE, Verdict.WRONG):
        entry = await write(adapter, f"flagged {verdict.value}")
        await store.record_feedback(
            Feedback(entry.id, "ok", "a", Verdict.HELPFUL, "ran it", _min(5))
        )
        # An older flag does not cancel it.
        await store.record_feedback(Feedback(entry.id, "old", "a", verdict, None, _min(4)))
        assert await _confirmed(adapter, entry) == _min(5)
        # A tie is no confirmation.
        await store.record_feedback(Feedback(entry.id, "tie", "a", verdict, None, _min(5)))
        assert await _confirmed(adapter, entry) is None
        # A later flag cancels it.
        await store.record_feedback(Feedback(entry.id, "tie", "a", verdict, None, _min(6)))
        assert await _confirmed(adapter, entry) is None
        # A newer noted helpful restores it.
        await store.record_feedback(
            Feedback(entry.id, "new", "a", Verdict.HELPFUL, "again", _min(7))
        )
        assert await _confirmed(adapter, entry) == _min(7)


async def test_last_confirmed_is_the_newest_date_whatever_the_number_of_confirmations(
    adapter: Adapter,
) -> None:
    entry = await write(adapter, "popular")
    store = adapter.store
    for i, minute in enumerate((3, 9, 1)):
        await store.record_feedback(
            Feedback(entry.id, f"u{i}", "a", Verdict.HELPFUL, f"checked {i}", _min(minute))
        )
    assert (await store.feedback_signals([entry.id]))[entry.id] == FeedbackSignals(3, 0, 0, _min(9))


async def test_a_revoked_reporters_confirmation_and_flag_are_ignored(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    entry = await write(adapter, "rated")
    await store.register_agent("Scout")
    await store.record_feedback(Feedback(entry.id, "ok", "a", Verdict.HELPFUL, "ran it", _min(1)))
    await store.record_feedback(Feedback(entry.id, "Scout", "Scout", Verdict.STALE, "old", _min(2)))
    assert await _confirmed(adapter, entry) is None
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    assert await _confirmed(adapter, entry) == _min(1)  # the revoked flag no longer cancels
    await store.record_feedback(Feedback(entry.id, "Scout", "Scout", Verdict.HELPFUL, "x", _min(9)))
    assert await _confirmed(adapter, entry) == _min(1)  # nor does its confirmation count
    assert (await store.feedback_signals([entry.id]))[entry.id].counts == (1, 0, 0)


async def test_newest_flag_at_is_the_newest_stale_or_wrong_row_of_any_kind(
    adapter: Adapter,
) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    entry = await write(adapter, "flag dates")
    unflagged = await write(adapter, "only helpful")
    await store.record_feedback(Feedback(unflagged.id, "u", "a", Verdict.HELPFUL, "ok", _min(9)))
    await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.HELPFUL, "ran it", _min(8)))
    signals = await store.feedback_signals([entry.id, unflagged.id])
    assert signals[entry.id].newest_flag_at is None
    assert signals[unflagged.id].newest_flag_at is None
    # A bare (note-less) flag counts, and the newest of several wins.
    await store.record_feedback(Feedback(entry.id, "u2", "a", Verdict.STALE, None, _min(3)))
    await store.record_feedback(Feedback(entry.id, "u3", "a", Verdict.WRONG, "no", _min(5)))
    await store.record_feedback(Feedback(entry.id, "u4", "a", Verdict.STALE, None, _min(4)))
    assert (await store.feedback_signals([entry.id]))[entry.id].newest_flag_at == _min(5)
    # A later helpful row neither hides nor moves it (it is a date, not a verdict).
    await store.record_feedback(Feedback(entry.id, "u5", "a", Verdict.HELPFUL, "again", _min(9)))
    assert (await store.feedback_signals([entry.id]))[entry.id].newest_flag_at == _min(5)
    # A revoked reporter's flag is ignored (ADR 0067).
    await store.register_agent("Scout")
    await store.record_feedback(Feedback(entry.id, "Scout", "Scout", Verdict.STALE, None, _min(7)))
    assert (await store.feedback_signals([entry.id]))[entry.id].newest_flag_at == _min(7)
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    assert (await store.feedback_signals([entry.id]))[entry.id].newest_flag_at == _min(5)


async def test_feedback_signals_cover_every_well_formed_id(adapter: Adapter) -> None:
    entry = await write(adapter, "rated")
    await adapter.store.record_feedback(
        Feedback(entry.id, "u", "a", Verdict.HELPFUL, "ok", _min(1))
    )
    signals = await adapter.store.feedback_signals([NO_SUCH_ID, "not-a-uuid", entry.id])
    assert signals == {
        NO_SUCH_ID: FeedbackSignals(0, 0, 0, None),
        entry.id: FeedbackSignals(1, 0, 0, _min(1)),
    }
    assert signals[entry.id].counts == (await adapter.store.quality_counts([entry.id]))[entry.id]
    assert await adapter.store.feedback_signals([]) == {}


@pytest.mark.parametrize("respell", [str.upper, lambda i: i.replace("-", "")])
async def test_feedback_signals_key_by_the_canonical_id_whatever_the_spelling(
    adapter: Adapter, respell: Callable[[str], str]
) -> None:
    entry = await write(adapter, "rated")
    await adapter.store.record_feedback(Feedback(entry.id, "u", "a", Verdict.WRONG, "n", _min(1)))
    for supersedes in (False, True):
        signals = await adapter.store.feedback_signals(
            [respell(entry.id), entry.id], count_supersedes=supersedes
        )
        assert list(signals) == [entry.id]
        assert signals[entry.id].counts == (0, 0, 1)


# -- evidence on a hit: correction and supersedes (SPEC §6.1) -------------------------


async def _correction(adapter: Adapter, entry: Entry) -> Correction | None:
    return (await adapter.store.feedback_signals([entry.id]))[entry.id].correction


async def test_correction_is_the_newest_flag_with_a_note(adapter: Adapter) -> None:
    entry = await write(adapter, "disputed")
    store = adapter.store
    assert await _correction(adapter, entry) is None
    await store.record_feedback(
        Feedback(entry.id, "u1", "a1", Verdict.STALE, "moved to v2", _min(1))
    )
    await store.record_feedback(
        Feedback(entry.id, "u2", "a2", Verdict.WRONG, "never true", _min(3))
    )
    await store.record_feedback(Feedback(entry.id, "u3", "a3", Verdict.HELPFUL, "fine", _min(5)))
    assert await _correction(adapter, entry) == Correction(
        Verdict.WRONG, "never true", "u2", _min(3)
    )
    # A reporter's latest verdict replaces theirs (upsert): u2 now says helpful.
    await store.record_feedback(Feedback(entry.id, "u2", "a2", Verdict.HELPFUL, "ok", _min(6)))
    assert await _correction(adapter, entry) == Correction(
        Verdict.STALE, "moved to v2", "u1", _min(1)
    )


async def test_correction_ignores_bare_and_blank_flags(adapter: Adapter) -> None:
    entry = await write(adapter, "disputed")
    store = adapter.store
    await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.STALE, "real note", _min(1)))
    for i, note in enumerate((None, "", "  \n\t ")):
        await store.record_feedback(
            Feedback(entry.id, f"b{i}", "a", Verdict.WRONG, note, _min(5 + i))
        )
    assert await _correction(adapter, entry) == Correction(
        Verdict.STALE, "real note", "u1", _min(1)
    )


async def test_correction_ignores_a_revoked_reporter(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    entry = await write(adapter, "disputed")
    await store.register_agent("Scout")
    await store.record_feedback(Feedback(entry.id, "ok", "a", Verdict.STALE, "kept", _min(1)))
    await store.record_feedback(
        Feedback(entry.id, "Scout", "Scout", Verdict.WRONG, "gone", _min(2))
    )
    assert (await _correction(adapter, entry)) == Correction(
        Verdict.WRONG, "gone", "Scout", _min(2)
    )
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    assert (await _correction(adapter, entry)) == Correction(Verdict.STALE, "kept", "ok", _min(1))


async def test_correction_note_is_cut_cleanly_at_160_characters(adapter: Adapter) -> None:
    store = adapter.store
    long = await write(adapter, "long")
    await store.record_feedback(
        Feedback(long.id, "u", "a", Verdict.STALE, "  " + "é" * 200 + "  ", _min(1))
    )
    cut = await _correction(adapter, long)
    assert cut is not None and cut.note == "é" * 160 + "…"
    exact = await write(adapter, "exact")
    await store.record_feedback(Feedback(exact.id, "u", "a", Verdict.WRONG, "😀" * 160, _min(1)))
    kept = await _correction(adapter, exact)
    assert kept is not None and kept.note == "😀" * 160  # no ellipsis when it fits
    # Surrounding space never counts and never ends up before the ellipsis.
    spaced = await write(adapter, "spaced")
    await store.record_feedback(
        Feedback(spaced.id, "u", "a", Verdict.WRONG, "x" * 159 + " " + "y" * 40, _min(1))
    )
    trimmed = await _correction(adapter, spaced)
    assert trimmed is not None and trimmed.note == "x" * 159 + "…"


async def test_supersedes_counts_readable_predecessors_only(adapter: Adapter) -> None:
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    store = adapter.store
    org = await write(adapter, "org v1", scope="org", author="w")
    home_v1 = await write(adapter, "home v1", scope="fleet", fleet_id=home, author="w")
    other_v1 = await write(adapter, "other v1", scope="fleet", fleet_id=other, author="w")
    secret = await write(adapter, "secret v1", scope="self", author="w")
    new = await write(
        adapter,
        "v2",
        scope="org",
        author="w",
        supersedes=(org.id, home_v1.id, other_v1.id, secret.id),
    )
    lone = await write(adapter, "lonely")

    async def counts(v: Visibility | None) -> dict[str, int]:
        signals = await store.feedback_signals(
            [new.id, lone.id], count_supersedes=True, visibility=v
        )
        return {eid: s.supersedes for eid, s in signals.items()}

    assert await counts(None) == {new.id: 4, lone.id: 0}
    assert await counts(Visibility(TrustLevel.PRIVILEGED, "x", is_admin=True)) == {
        new.id: 4,
        lone.id: 0,
    }
    assert await counts(_vis(TrustLevel.UNTRUSTED, fleet=home)) == {new.id: 0, lone.id: 0}
    assert await counts(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == {new.id: 2, lone.id: 0}
    assert await counts(_vis(TrustLevel.PRIVILEGED, fleet=home)) == {new.id: 3, lone.id: 0}
    assert await counts(_vis(TrustLevel.LURKER, name="w", fleet=other)) == {new.id: 4, lone.id: 0}
    # Not asked for: zero, whatever the chain.
    plain = await store.feedback_signals([new.id])
    assert plain[new.id].supersedes == 0


async def test_contested_inputs_are_the_counts_after_exclusions(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    entry = await write(adapter, "rated")
    await store.register_agent("Scout")
    await store.record_feedback(Feedback(entry.id, "ok", "a", Verdict.HELPFUL, None, _min(1)))
    await store.record_feedback(Feedback(entry.id, "Scout", "Scout", Verdict.WRONG, None, _min(2)))
    assert is_contested(*(await store.feedback_signals([entry.id]))[entry.id].counts)
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    assert not is_contested(*(await store.feedback_signals([entry.id]))[entry.id].counts)
