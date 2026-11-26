"""The audit log (ADR 0027) on both adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tests.integration.store_contract.helpers import (
    NO_SUCH_ID,
    T0,
    Adapter,
    _active,
    _backdate,
    _clean,
    _rate,
    write,
)

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.activity import FleetActivity
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import Feedback, Verdict


async def test_audit_log_is_newest_first_and_filters(adapter: Adapter) -> None:
    store = adapter.store

    def event(actor: str, target: str) -> AuditEvent:
        return AuditEvent(ActorKind.ADMIN_KEY, actor, AuditAction.FLEET_CREATE, target, {"n": 1})

    first = await store.record_audit(event("admin:a", "f1"))
    second = await store.record_audit(event("admin:b", "f2"))
    third = await store.record_audit(event("admin:a", "f3"))
    revoke = await store.record_audit(
        AuditEvent(ActorKind.CLI, "ops", AuditAction.AGENT_REVOKE, "alice")
    )
    assert (first.actor_kind, first.action, first.target) == (
        ActorKind.ADMIN_KEY,
        AuditAction.FLEET_CREATE,
        "f1",
    )
    assert second.detail == {"n": 1} and revoke.detail == {}
    assert await store.list_audit(AuditFilters(), 1) == [revoke]
    assert [r.id for r in await store.list_audit(AuditFilters(), 10)] == [
        revoke.id,
        third.id,
        second.id,
        first.id,
    ]
    by_actor = await store.list_audit(AuditFilters(actor="admin:a"), 10)
    assert [r.target for r in by_actor] == ["f3", "f1"]
    by_action = await store.list_audit(AuditFilters(action=AuditAction.AGENT_REVOKE), 10)
    assert by_action == [revoke]
    since = AuditFilters(since=second.occurred_at)
    assert [r.id for r in await store.list_audit(since, 10)] == [revoke.id, third.id, second.id]
    both = AuditFilters(actor="admin:a", since=second.occurred_at)
    assert [r.id for r in await store.list_audit(both, 10)] == [third.id]
    older = await store.list_audit(AuditFilters(before=third.id), 10)
    assert [r.id for r in older] == [second.id, first.id]
    assert await store.list_audit(AuditFilters(before=NO_SUCH_ID), 10) == []


async def test_audit_log_target_and_subject_filters(adapter: Adapter) -> None:
    """ADR 0123: ``target`` is exact; ``subjects`` keeps the rows about an
    agent in every shape that names it, and nothing that merely spells like it."""
    store = adapter.store
    admin, agent = ActorKind.ADMIN_KEY, ActorKind.AGENT_KEY

    async def row(kind, actor, action, target, detail=None):  # type: ignore[no-untyped-def]
        return await store.record_audit(AuditEvent(kind, actor, action, target, detail or {}))

    trust = await row(admin, "admin:a", AuditAction.AGENT_TRUST_LEVEL_SET, "amy", {"to": 2})
    other = await row(admin, "admin:a", AuditAction.AGENT_TRUST_LEVEL_SET, "bob", {"to": 2})
    pin = await row(agent, "amy", AuditAction.ENTRY_PIN, "entry-1", {"fleet_id": "f1"})
    withdraw = await row(admin, "admin:a", AuditAction.ENTRY_WITHDRAW, "entry-2", {"author": "amy"})
    restored = await row(admin, "admin:a", AuditAction.ENTRY_RESTORE, "entry-2", {"author": "amy"})
    redacted = await row(
        admin, "admin:a", AuditAction.ENTRY_REDACT, "entry-4", {"author": "amy", "reason": "x"}
    )
    await row(admin, "admin:a", AuditAction.ENTRY_REDACT, "entry-5", {"author": "bob"})
    moved = await row(
        admin, "admin:a", AuditAction.FLEET_MOVE_AGENTS, "f1", {"agents": ["bob", "amy"]}
    )
    not_moved = await row(
        admin, "admin:a", AuditAction.FLEET_MOVE_AGENTS, "f1", {"agents": ["bob"]}
    )
    retire = await row(admin, "admin:a", AuditAction.AGENT_RETIRE, "old", {"successor": "amy"})
    # Spelled like the agent but not about it: a fleet and an entry with its name as
    # target, a cli actor with its name, a count in `agents`.
    await row(admin, "admin:a", AuditAction.FLEET_RENAME, "amy", {"from": "a", "to": "b"})
    await row(admin, "admin:a", AuditAction.ENTRY_UNPIN, "amy", {"fleet_id": "f1"})
    await row(ActorKind.CLI, "amy", AuditAction.AGENT_REVOKE, "bob", {})
    await row(admin, "admin:a", AuditAction.FLEET_MOVE_UNDO, "f1", {"agents": 1})
    await row(admin, "admin:a", AuditAction.ENTRY_WITHDRAW, "entry-3", {"author": "bob"})

    async def ids(**kw):  # type: ignore[no-untyped-def]
        return [r.id for r in await store.list_audit(AuditFilters(**kw), 50)]

    assert await ids(target="amy") == [
        r.id for r in await store.list_audit(AuditFilters(target="amy"), 50)
    ]
    assert len(await ids(target="amy")) == 3  # trust, rename, unpin
    assert await ids(target="nobody") == []
    assert await ids(subjects=("amy",)) == [
        retire.id,
        moved.id,
        redacted.id,
        restored.id,
        withdraw.id,
        pin.id,
        trust.id,
    ]
    assert not_moved.id not in await ids(subjects=("amy",))
    both = await ids(subjects=("amy", "old"))
    assert (
        retire.id in both and trust.id in both and redacted.id in both and len(both) == 7
    )  # + the retire names old as target
    assert other.id in await ids(subjects=("bob",))
    # AND-ed with the other filters and the paging cursor.
    assert await ids(subjects=("amy",), action=AuditAction.ENTRY_PIN) == [pin.id]
    assert await ids(subjects=("amy",), before=moved.id) == [
        redacted.id,
        restored.id,
        withdraw.id,
        pin.id,
        trust.id,
    ]
    assert await ids(subjects=("amy",), target="entry-2") == [restored.id, withdraw.id]
    # A cursor row that is not about the subject matches nothing.
    assert await ids(subjects=("amy",), before=other.id) == []
    assert await ids(subjects=("amy",), actor="admin:a", since=withdraw.occurred_at) == [
        retire.id,
        moved.id,
        redacted.id,
        restored.id,
        withdraw.id,
    ]


async def test_record_feedback_many_upserts_each_row_in_one_call(adapter: Adapter) -> None:
    first = await write(adapter, "first")
    second = await write(adapter, "second")
    store = adapter.store
    await store.record_feedback(Feedback(first.id, "u1", "a", Verdict.HELPFUL, None, T0))
    await store.record_feedback_many(
        [
            Feedback(first.id, "u1", "a", Verdict.WRONG, "changed", T0 + timedelta(minutes=1)),
            Feedback(second.id, "u1", "a", Verdict.STALE, None, T0),
        ]
    )
    assert await store.feedback_counts(first.id) == (0, 0, 1)
    assert await store.feedback_counts(second.id) == (0, 1, 0)
    assert [(r.verdict, r.note) for r in await store.list_feedback(first.id, 10)] == [
        (Verdict.WRONG, "changed")
    ]


async def test_record_feedback_many_of_nothing_is_a_no_op(adapter: Adapter) -> None:
    entry = await write(adapter, "untouched")
    await adapter.store.record_feedback_many([])
    assert await adapter.store.feedback_counts(entry.id) == (0, 0, 0)


async def test_feedback_without_a_date_is_stamped_by_the_clock_pins_use(adapter: Adapter) -> None:
    """ADR 0079: the store dates feedback with the clock it dates pins with,
    so a verdict after a pin is newer than it and a re-pin after a verdict
    is newer than the verdict, whatever the application's clock says."""
    entry = await write(adapter, "pinned")
    other = await write(adapter, "other")
    store = adapter.store
    pin = await store.pin_entry("fleet-x", entry.id, "lead", 3)
    assert pin is not None
    stored = await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.STALE))
    assert stored.updated_at is not None and stored.updated_at > pin.pin.pinned_at
    assert [r.updated_at for r in await store.list_feedback(entry.id, 10)] == [stored.updated_at]
    many = await store.record_feedback_many(
        [
            Feedback(entry.id, "u2", "a", Verdict.WRONG),
            Feedback(other.id, "u2", "a", Verdict.WRONG),
            Feedback(other.id, "u3", "a", Verdict.HELPFUL, None, T0),
        ]
    )
    assert many[0].updated_at is not None and many[0].updated_at > stored.updated_at
    assert many[1].updated_at == many[0].updated_at  # one stamp per call
    assert many[2].updated_at == T0  # a given date is kept
    assert {r.user: r.updated_at for r in await store.list_feedback(other.id, 10)} == {
        "u2": many[0].updated_at,
        "u3": T0,
    }
    repin = await store.pin_entry("fleet-x", entry.id, "lead", 3)
    assert repin is not None and repin.pin.pinned_at > many[0].updated_at


async def test_activity_aggregates_hand_computed(adapter: Adapter) -> None:
    """ADR 0080: per (author, fleet), per reporter, received verdicts and
    per UTC day from ``since``; revoked reporters are left out."""
    store = adapter.store
    blue = await store.create_fleet("blue")
    for name in ("Scout", "Ghost"):
        await store.register_agent(name)
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=blue.id)
    await store.revoke_agent("Ghost")
    since = datetime(2030, 1, 10, tzinfo=UTC)
    kw = {"occurred_at": None}

    async def make(summary: str, at: datetime, **more: object) -> Entry:
        e = await write(adapter, summary, **kw, **more)
        await _backdate(adapter, e, at)
        return e

    old = await make("old", since - timedelta(hours=1), author="bob")
    a = await make("a", since + timedelta(hours=1), author="bob", scope="fleet", fleet_id=blue.id)
    b = await make("b", since + timedelta(hours=23, minutes=59), author="bob")
    c = await make("c", since + timedelta(days=2, hours=5), author="carol")
    d = await make("d", since + timedelta(days=2, hours=6), author="carol")
    await store.withdraw_entry(d.id, None, "carol")

    async def verdict(entry: Entry, user: str, v: Verdict, at: datetime) -> None:
        await store.record_feedback(Feedback(entry.id, user, user, v, None, at))

    await verdict(old, "Scout", Verdict.STALE, since - timedelta(days=3))
    await verdict(a, "Scout", Verdict.HELPFUL, since + timedelta(hours=2))
    await verdict(b, "Scout", Verdict.WRONG, since + timedelta(days=1, hours=1))
    await verdict(c, "Scout", Verdict.HELPFUL, since + timedelta(days=2))
    await verdict(a, "Ghost", Verdict.HELPFUL, since + timedelta(hours=3))  # revoked: ignored
    await verdict(c, "dora", Verdict.WRONG, since + timedelta(days=2, hours=1))

    got = await store.activity(since)
    authors = {(r.author, r.fleet_id): r for r in got.authors}
    assert set(authors) == {("bob", None), ("bob", blue.id), ("carol", None)}
    bob = authors[("bob", None)]
    assert (bob.entries, bob.active, bob.recent) == (2, 2, 1)
    assert bob.last_write_at == since + timedelta(hours=23, minutes=59)
    assert (authors[("bob", blue.id)].entries, authors[("bob", blue.id)].recent) == (1, 1)
    carol = authors[("carol", None)]
    assert (carol.entries, carol.active, carol.recent) == (2, 1, 2)
    reporters = {r.reporter: r for r in got.reporters}
    assert set(reporters) == {"Scout", "dora"}
    scout = reporters["Scout"]
    assert (scout.verdicts, scout.recent) == (4, 3)
    assert (scout.helpful, scout.stale, scout.wrong) == (2, 1, 1)
    assert scout.last_at == since + timedelta(days=2)
    received = {r.author: (r.helpful, r.stale, r.wrong) for r in got.received}
    assert received == {"bob": (1, 1, 1), "carol": (1, 0, 1)}
    days = {r.day: (r.writes, r.helpful, r.stale, r.wrong) for r in got.days}
    assert days == {
        since.date(): (2, 1, 0, 0),
        (since + timedelta(days=1)).date(): (0, 0, 0, 1),
        (since + timedelta(days=2)).date(): (2, 1, 0, 1),
    }
    assert list(days) == sorted(days)


async def test_activity_of_an_empty_pool(adapter: Adapter) -> None:
    got = await adapter.store.activity(T0)
    assert got.authors == got.reporters == got.received == got.days == ()


async def test_fleet_activity_counts_member_writes_and_ratings_per_day(adapter: Adapter) -> None:
    """ADR 0165: only members of the fleet, only entries the reader may read, per UTC day from
    ``since``; a re-rating counts on its latest day, set-aside and revoked raters are left out, a
    quiet active member is counted and a revoked one with writes is a row."""
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    red = (await store.create_fleet("red")).id
    for name in ("alice", "bob", "cara", "dan"):
        await _active(adapter, name, blue)
    await _active(adapter, "eve", red)
    since = datetime(2030, 1, 10, tzinfo=UTC)
    until = since + timedelta(days=30)
    day0, day1 = since.date(), (since + timedelta(days=1)).date()

    async def make(author: str, at: datetime, **more: object) -> Entry:
        e = await write(adapter, f"{author} {at.isoformat()}", author=author, **more)
        await _backdate(adapter, e, at)
        return e

    await make("alice", since - timedelta(minutes=1))
    await make("alice", until)  # the window is over
    await make("cara", until + timedelta(days=3))
    await make("alice", since + timedelta(hours=1))
    await make("alice", since + timedelta(hours=23, minutes=59), scope="fleet", fleet_id=blue)
    gone = await make("alice", since + timedelta(days=1, hours=2))
    await store.withdraw_entry(gone.id, None, "alice")
    secret = await make("bob", since + timedelta(hours=5), scope="self")
    await make("dan", since + timedelta(days=1), scope="fleet", fleet_id=blue)
    await make("eve", since + timedelta(hours=1))
    await make("outsider", since + timedelta(hours=1))
    shared = await make("zoe", since - timedelta(days=30))

    async def rate(entry: Entry, user: str, at: datetime) -> None:
        await store.record_feedback(Feedback(entry.id, user, user, Verdict.HELPFUL, None, at))

    await rate(shared, "alice", since + timedelta(days=1, hours=1))
    await rate(shared, "alice", since + timedelta(days=1, hours=3))  # the same rating, re-given
    await rate(gone, "bob", since + timedelta(hours=2))
    await rate(secret, "bob", since + timedelta(hours=3))  # bob's own private memory: not alice's
    await rate(shared, "bob", since - timedelta(days=1))  # before the window
    await rate(shared, "eve", since + timedelta(hours=1))  # not a member
    await rate(shared, "cara", until)  # after the window
    await rate(shared, "dan", since + timedelta(days=1))
    await store.revoke_agent("dan")

    def rows(got: FleetActivity) -> dict[tuple[str, date], tuple[int, int]]:
        return {(r.agent, r.day): (r.writes, r.ratings) for r in got.days}

    alice = Visibility(TrustLevel.CONTRIBUTOR, "alice", home_fleet_id=blue)
    got = await store.fleet_activity(blue, since, until, alice)
    assert rows(got) == {
        ("alice", day0): (2, 0),
        ("alice", day1): (1, 1),
        ("bob", day0): (0, 1),  # bob's private memory is not alice's to read, nor its rating
        ("dan", day1): (1, 0),  # revoked: its write stays, its rating is left out
    }
    assert got.quiet == 1  # cara

    admin = Visibility(TrustLevel.PRIVILEGED, "root", is_admin=True)
    assert rows(await store.fleet_activity(blue, since, until, admin))[("bob", day0)] == (1, 2)


async def test_fleet_activity_splits_ratings_by_verdict(adapter: Adapter) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    await _active(adapter, "rater", blue)
    for i, verdict in enumerate((Verdict.HELPFUL, Verdict.HELPFUL, Verdict.STALE, Verdict.WRONG)):
        entry = await write(adapter, f"note {i}", author="writer")
        await _rate(adapter, entry.id, "rater", verdict, at=T0)
    reader = Visibility(TrustLevel.CONTRIBUTOR, "rater", home_fleet_id=blue)
    got = await store.fleet_activity(blue, T0 - timedelta(days=1), T0 + timedelta(days=1), reader)
    assert [(r.helpful, r.stale, r.wrong, r.ratings) for r in got.days] == [(2, 1, 1, 4)]


async def test_fleet_activity_leaves_out_ratings_an_agent_cleanup_set_aside(
    adapter: Adapter,
) -> None:
    """ADR 0111: the rows a cleanup sets aside leave the chart, as they leave every feedback read."""
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    await _active(adapter, "bad", blue)
    theirs = await write(adapter, "theirs", author="good")
    await _rate(adapter, theirs.id, "bad", Verdict.STALE, at=T0)
    reader = Visibility(TrustLevel.CONTRIBUTOR, "bad", home_fleet_id=blue)
    got = await store.fleet_activity(blue, T0 - timedelta(days=1), T0 + timedelta(days=1), reader)
    assert [(r.agent, r.ratings) for r in got.days] == [("bad", 1)]
    await _clean(adapter)
    got = await store.fleet_activity(blue, T0 - timedelta(days=1), T0 + timedelta(days=1), reader)
    assert got == FleetActivity(quiet=1)


async def test_fleet_activity_for_an_heir_and_for_a_level_three_key(adapter: Adapter) -> None:
    """An heir reads the retired agent's private memories (ADR 0113); level 3 reads other fleets' memories."""
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    green = (await store.create_fleet("green")).id
    await _active(adapter, "old", blue)
    await _active(adapter, "heir", blue)
    for summary, author, more in (
        ("old private", "old", {"scope": "self"}),
        ("heir fleet", "heir", {"scope": "fleet", "fleet_id": blue}),
    ):
        await _backdate(adapter, await write(adapter, summary, author=author, **more), T0)
    await store.retire_agent("old", "heir", "admin:1")
    since, until = T0 - timedelta(days=1), T0 + timedelta(days=1)

    async def writes(reader: Visibility) -> dict[str, int]:
        got = await store.fleet_activity(blue, since, until, reader)
        return {r.agent: r.writes for r in got.days}

    assert await writes(Visibility(TrustLevel.CONTRIBUTOR, "heir", blue)) == {"heir": 1}
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue, inherited=("old",))
    assert await writes(heir) == {"heir": 1, "old": 1}
    assert await writes(Visibility(TrustLevel.CONTRIBUTOR, "zed", green)) == {}
    assert await writes(Visibility(TrustLevel.PRIVILEGED, "pat", green)) == {"heir": 1}


async def test_fleet_activity_of_a_fleet_without_activity_or_members(adapter: Adapter) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    reader = Visibility(TrustLevel.CONTRIBUTOR, "alice", home_fleet_id=blue)
    assert await store.fleet_activity(blue, T0, T0 + timedelta(days=1), reader) == FleetActivity()
    await _active(adapter, "alice", blue)
    assert await store.fleet_activity(blue, T0, T0 + timedelta(days=1), reader) == FleetActivity(
        quiet=1
    )
