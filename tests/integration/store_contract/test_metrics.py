"""The usage counters (ROADMAP §3.3, ADR 0056) on both adapters."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.store_contract.helpers import T0, Adapter, _agent_in, _backdate, draft, write

from hivemind.domain.access import TrustLevel
from hivemind.domain.agent_role import AgentRole
from hivemind.domain.entry import Entry, ImportanceSource, Kind, Source, SourceType
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.health import FleetHealth, HealthCounts, RoleActivity


async def test_search_counters_accumulate_per_fleet(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    await adapter.store.record_search(fleet, empty=True)
    await adapter.store.record_search(fleet, empty=False)
    await adapter.store.record_search(None, empty=False)
    counts = {c.fleet_id: (c.searches, c.empty) for c in await adapter.store.search_counts()}
    assert counts == {fleet: (2, 1), None: (1, 0)}


async def test_explore_searches_are_counted_beside_the_others(adapter: Adapter) -> None:
    """ADR 0078: ``explore`` is one more counter on the same per-fleet row; an
    explore search is also a search, and omitting it counts as not explore."""
    fleet = str(uuid.uuid4())
    await adapter.store.record_search(fleet, empty=False, explore=True)
    await adapter.store.record_search(fleet, empty=True, explore=True)
    await adapter.store.record_search(fleet, empty=False)
    await adapter.store.record_search(None, empty=False)
    counts = {
        c.fleet_id: (c.searches, c.empty, c.explore) for c in await adapter.store.search_counts()
    }
    assert counts == {fleet: (3, 1, 2), None: (1, 0, 0)}


async def test_flag_counts_confirm_flags_a_later_supersession_proved_right(
    adapter: Adapter,
) -> None:
    """ADR 0068. Row times are set around the successor's ``created_at``,
    which both adapters assign themselves."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout")
    await store.register_agent("Ghost")
    flagged = await write(adapter, "flagged then superseded")
    never = await write(adapter, "flagged, never superseded")
    already = await write(adapter, "superseded before it was flagged")
    ok = await write(adapter, "only rated helpful")
    successor = await write(adapter, "v2", supersedes=(flagged.id,))
    await write(adapter, "v2 of already", supersedes=(already.id,))
    after = (await store.get_entry(successor.id)).created_at + timedelta(minutes=1)  # type: ignore[union-attr]
    before = after - timedelta(minutes=2)

    def row(entry: Entry, user: str, verdict: Verdict, at: datetime) -> Feedback:
        return Feedback(entry.id, user, user, verdict, None, at)

    await store.record_feedback(row(flagged, "Scout", Verdict.STALE, before))  # confirmed
    await store.record_feedback(row(never, "Scout", Verdict.WRONG, before))  # flag only
    await store.record_feedback(row(already, "Scout", Verdict.WRONG, after))  # excluded
    await store.record_feedback(row(ok, "Scout", Verdict.HELPFUL, before))  # ignored
    await store.record_feedback(row(flagged, "admin", Verdict.WRONG, before))  # no agent record
    await store.record_feedback(row(flagged, "Ghost", Verdict.WRONG, before))
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Ghost")  # revoked reporter: left out

    async def counts() -> dict[str, tuple[int, int]]:
        return {c.user: (c.flags, c.confirmed) for c in await store.flag_counts()}

    assert await counts() == {"Scout": (2, 1), "admin": (1, 1)}
    # A re-rate moves updated_at: the confirmed flag, rewritten after the
    # supersession, no longer predicted anything; the unsuperseded one still counts.
    await store.record_feedback(row(flagged, "Scout", Verdict.STALE, after))
    assert await counts() == {"Scout": (1, 0), "admin": (1, 1)}
    # Re-activating the revoked reporter brings its flag back.
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    assert (await counts())["Ghost"] == (1, 1)


async def test_fleet_health_aggregates_hand_computed(adapter: Adapter) -> None:
    """ADR 0075. Times are set relative to the first write (``created_at``
    differs between the adapters' clocks), so every window is exact."""
    store = adapter.store
    blue = await store.create_fleet("blue")
    red = await store.create_fleet("red")
    for name in ("Scout", "Rival", "Ghost"):
        await store.register_agent(name)
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=red.id)
    await store.revoke_agent("Ghost")
    old = datetime(2020, 1, 1, tzinfo=UTC)  # far older than any window
    kw = {"occurred_at": None}
    a = await write(adapter, "a", sources=1, **kw)
    b = await write(
        adapter,
        "b",
        author="bob",
        scope="fleet",
        fleet_id=blue.id,
        tags=("dead-end", "anomaly"),
        **kw,
    )
    c = await write(
        adapter, "c", kind=Kind.INSIGHT, scope="fleet", fleet_id=blue.id, sources=1,
        see_also=(a.id, b.id), **kw,
    )  # fmt: skip
    d = await write(adapter, "d", kind=Kind.INSIGHT, see_also=(a.id,), **kw)
    e = await write(
        adapter, "e", author="bob", kind=Kind.DECISION, tags=("open-question",), occurred_at=old
    )
    await write(adapter, "f", author="carol", scope="self", tags=("anomaly",), occurred_at=old)
    g = await write(adapter, "g", author="Ghost", scope="fleet", fleet_id=red.id, sources=1, **kw)
    h = await write(adapter, "h", author="bob", scope="fleet", fleet_id=red.id, **kw)
    j = await write(adapter, "j", author="bob", occurred_at=old)
    k = await write(adapter, "k", author="carol", occurred_at=old)
    z1 = await write(adapter, "z1", author="bob", **kw)
    z2 = await write(adapter, "z2", author="carol", scope="self", **kw)
    z3 = await write(adapter, "z3", author="dora", **kw)
    z1n = await write(adapter, "z1n", supersedes=(z1.id,), **kw)
    z2n = await write(
        adapter, "z2n", author="carol", scope="fleet", fleet_id=blue.id, supersedes=(z2.id,), **kw
    )
    z3n = await write(adapter, "z3n", supersedes=(z3.id,), **kw)
    now = a.created_at + timedelta(hours=1)
    day = timedelta(days=1)

    async def verdict(entry: Entry, user: str, v: Verdict, at: datetime, note: str | None = None):
        await store.record_feedback(Feedback(entry.id, user, user, v, note, at))

    W, S, H = Verdict.WRONG, Verdict.STALE, Verdict.HELPFUL
    await verdict(g, "Scout", W, now - 20 * day)
    await verdict(g, "Rival", S, now - 3 * day)
    await verdict(h, "Scout", S, now - 2 * day)
    await verdict(j, "Scout", H, now - 10 * day, "verified: ok")
    await verdict(e, "Scout", H, now - 100 * day)  # outside the 90 days
    await verdict(a, "Rival", H, now - day, "  ")  # blank note
    await verdict(b, "Rival", H, now - day, "confirmed on prod")
    await verdict(k, "Ghost", H, now - 5 * day)  # revoked reporter: ignored
    await verdict(c, "Mira", H, now - day, "x")
    await verdict(c, "Nox", H, now - day)
    await verdict(d, "Pax", H, now - day, "ok")
    s1, s2, s3 = (x.created_at for x in (z1n, z2n, z3n))
    await verdict(z1, "Scout", W, s1 - 3 * day)
    await verdict(z1, "Rival", S, s1 - day)
    await verdict(z2, "Scout", S, s2 - day)
    await verdict(z3, "Scout", W, s3 + timedelta(minutes=1))  # after the successor

    health = await store.fleet_health(now)
    none, blue_id, red_id = None, blue.id, red.id

    def hc(fleet_id: str | None, **n: int) -> HealthCounts:
        return HealthCounts(fleet_id=fleet_id, **n)

    assert {r.fleet_id: r for r in health.by_fleet} == {
        none: hc(none, sourceable=6, sourced=1, insights=1, floating_gap=3, gap_anomaly=1,
                 gap_open_question=1, active_entries=8, writes_7d=11, verdicts_7d=6,
                 supersessions_7d=3),
        blue_id: hc(blue_id, sourceable=3, sourced=1, insights=1, synthesizing=1, gap_anomaly=1,
                    gap_dead_end=1, active_entries=3, writes_7d=3, verdicts_7d=3),
        red_id: hc(red_id, sourceable=2, sourced=1, flagged_backlog_14d=1, orphaned_flagged=1,
                   active_entries=2, writes_7d=2, verdicts_7d=2),
    }  # fmt: skip
    assert (health.helpful, health.helpful_with_note) == (7, 4)
    assert sorted(health.correction_latencies) == [86400.0, 259200.0]
    assert (health.writes_90d, health.top3_author_writes_90d, health.authors_90d) == (16, 14, 5)
    assert (health.verdicts_90d, health.top3_reporter_verdicts_90d, health.reporters_90d) == (
        13,
        11,
        5,
    )
    assert (health.supersessions_90d, health.self_to_fleet_promotions_90d) == (3, 1)
    # A month on, nobody has written for 30 days: both flagged entries are orphans.
    later = await store.fleet_health(now + 30 * day)
    assert sum(r.orphaned_flagged for r in later.by_fleet) == 2
    # An empty window: no 90-day writes, verdicts or corrections.
    gone = await store.fleet_health(now + 400 * day)
    assert (gone.writes_90d, gone.verdicts_90d, gone.supersessions_90d) == (0, 0, 0)
    assert gone.correction_latencies == ()
    assert gone.top3_author_writes_90d == gone.authors_90d == 0


async def test_fleet_health_breathing_counts_the_last_seven_days(adapter: Adapter) -> None:
    """ADR 0142. Each count belongs to the fleet of the entry concerned: a verdict to the entry
    it rates, a supersession to the entry that was superseded (not its successor), a
    withdrawal to the entry withdrawn. A revoked reporter's verdict is left out."""
    store = adapter.store
    blue = await store.create_fleet("blue")
    red = await store.create_fleet("red")
    await store.register_agent("Ghost")
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=red.id)
    await store.revoke_agent("Ghost")
    old_blue = await write(adapter, "old blue", scope="fleet", fleet_id=blue.id)
    gone = await write(adapter, "gone", scope="fleet", fleet_id=red.id)
    for entry in (old_blue, gone):
        await _backdate(adapter, entry, entry.created_at - timedelta(days=30))
    await store.withdraw_entry(gone.id, "wrong place", "bob")
    rated = await write(adapter, "rated", scope="fleet", fleet_id=red.id)
    await write(adapter, "replacement", scope="fleet", fleet_id=blue.id, supersedes=(old_blue.id,))
    now = rated.created_at + timedelta(hours=1)
    day = timedelta(days=1)
    for user, at in (("Scout", now - day), ("Rival", now - 8 * day), ("Ghost", now - day)):
        await store.record_feedback(Feedback(rated.id, user, user, Verdict.HELPFUL, None, at))

    def breathing(health: FleetHealth) -> dict[str | None, tuple[int, int, int, int]]:
        return {
            r.fleet_id: (r.writes_7d, r.verdicts_7d, r.supersessions_7d, r.withdrawals_7d)
            for r in health.by_fleet
        }

    assert breathing(await store.fleet_health(now)) == {
        blue.id: (1, 0, 1, 0),
        red.id: (1, 1, 0, 1),
    }
    assert breathing(await store.fleet_health(now + 8 * day)) == {
        blue.id: (0, 0, 0, 0),
        red.id: (0, 0, 0, 0),
    }


async def test_fleet_health_owner_level_verdicts_hand_computed(adapter: Adapter) -> None:
    """ADR 0089. Two agents of one owner (aliases differing in case and
    padding) rate each other; another owner; an agent with no alias; an
    unregistered reporter (the admin); a revoked sibling and an old verdict
    that do not count."""
    store = adapter.store
    red = await store.create_fleet("red")
    for name, alias in (
        ("a1", " Ann "),
        ("a2", "ann"),
        ("b1", "Bo"),
        ("c1", None),
        ("g1", "Ann"),
    ):
        await store.register_agent(name, alias)
    await store.activate_agent("g1", trust_level=TrustLevel.LURKER, home_fleet_id=red.id)
    await store.revoke_agent("g1")
    ea1 = await write(adapter, "ea1", author="a1")
    ea2 = await write(adapter, "ea2", author="a2")
    eb1 = await write(adapter, "eb1", author="b1")
    ec1 = await write(adapter, "ec1", author="c1")
    ex = await write(adapter, "ex", author="stranger")  # no agent record
    now = ex.created_at + timedelta(hours=1)
    day = timedelta(days=1)

    async def verdict(entry: Entry, user: str, at: datetime) -> None:
        await store.record_feedback(Feedback(entry.id, user, user, Verdict.HELPFUL, None, at))

    for entry, user in (
        (ea2, "a1"),  # sibling
        (ea1, "a1"),  # its own entry: not a sibling
        (eb1, "a1"),
        (ex, "a1"),
        (ea1, "a2"),  # sibling
        (ec1, "a2"),
        (ea1, "b1"),
        (ex, "b1"),
        (ea1, "c1"),  # no alias: never a sibling
        (ea2, "c1"),
        (eb1, "c1"),
        (ex, "c1"),
        (ea1, "admin"),  # unregistered reporter: its own owner
    ):
        await verdict(entry, user, now - day)
    await verdict(ea1, "g1", now - day)  # revoked reporter: ignored
    await verdict(ec1, "a1", now - 100 * day)  # outside the 90 days

    health = await store.fleet_health(now)
    # Reporters: a1 4, c1 4, a2 2, b1 2, admin 1. Owners: Ann (a1 + a2) 6,
    # c1 4, Bo 2, admin 1.
    assert (health.verdicts_90d, health.top3_reporter_verdicts_90d, health.reporters_90d) == (
        13,
        10,
        5,
    )
    assert health.sibling_verdicts_90d == 2
    assert (health.top3_owner_verdicts_90d, health.owners_90d) == (12, 4)
    gone = await store.fleet_health(now + 400 * day)
    assert (gone.sibling_verdicts_90d, gone.top3_owner_verdicts_90d, gone.owners_90d) == (0, 0, 0)


async def test_fleet_health_of_an_empty_pool(adapter: Adapter) -> None:
    health = await adapter.store.fleet_health(T0)
    assert health.by_fleet == () and health.correction_latencies == ()
    assert (health.helpful, health.writes_90d, health.verdicts_90d) == (0, 0, 0)


NOW = datetime(2030, 1, 1, tzinfo=UTC)


_DAY = timedelta(days=1)


async def test_fleet_health_convergence_counters(adapter: Adapter) -> None:
    """ADR 0075: rival lifetime, grounding share and the agent-label mix,
    on entries moved to known ages (``NOW`` is fixed)."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Ghost")
    await store.activate_agent("Ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Ghost")
    other = await write(adapter, "an existing entry")
    entry_ref = (Source(SourceType.OTHER, f"entry:{other.id.upper()}"),)
    url = (Source(SourceType.URL, "https://example.test/a"),)
    path = (Source(SourceType.PATH, "src/a.py"),)

    async def make(summary: str, days: int, agent: str, **kw: object) -> Entry:
        e = await write(adapter, summary, agent=agent, occurred_at=None, **kw)
        await _backdate(adapter, e, NOW - days * _DAY)
        return e

    await _backdate(adapter, other, NOW - 400 * _DAY)
    await make("r1", 10, "L1", tags=("alternative",))
    await make("r2", 20, "L1", tags=("alternative",))
    r3 = await make("r3", 5, "L2", tags=("alternative",))
    await store.withdraw_entry(r3.id, None, "alice")  # not active: not counted
    await make("Conflict: a vs b", 4, "L2")
    await make("conflict: lower case", 3, "L2")
    await make("Conflict: c vs d", 8, "L3")
    await make("Conflict: e vs f", 1, "L3")
    k1 = await make("k1", 36, "L4")
    k2 = await make("k2", 6, "L1")
    k3 = await make("k3", 6, "L1")
    k5 = await make("k5", 2, "L2")
    await make("g1", 3, "L1", kind=Kind.INSIGHT, sources=url)
    await make(
        "g2", 3, "L1", kind=Kind.INSIGHT, scope="fleet", fleet_id=fleet.id, sources=entry_ref
    )
    await make("g3", 3, "L2", kind=Kind.DECISION, sources=entry_ref + path)
    await make("g4", 3, "L2", kind=Kind.DECISION)
    await make("g5", 3, "L1", sources=url)  # a fact
    await make("g6", 3, "L5", kind=Kind.INSIGHT, scope="self", sources=url)
    await make("g7", 40, "L1", kind=Kind.INSIGHT, sources=url)  # older than 30 days
    recent = NOW - 5 * _DAY

    def v(entry: Entry, user: str, label: str, verdict: Verdict, at: datetime = recent) -> Feedback:
        return Feedback(entry.id, user, label, verdict, None, at)

    for fb in (
        v(k1, "Scout", "L1", Verdict.HELPFUL),
        v(k1, "Rival", "L2", Verdict.WRONG),
        v(k5, "Scout", "L1", Verdict.HELPFUL),
        v(k5, "Rival", "L2", Verdict.STALE),
        v(k2, "Scout", "L1", Verdict.HELPFUL),
        v(k3, "Scout", "L1", Verdict.HELPFUL),
        v(k3, "Ghost", "L3", Verdict.STALE),  # revoked reporter: k3 is not contested
        v(k2, "Mira", "L3", Verdict.HELPFUL),
        v(k2, "Nox", "L4", Verdict.HELPFUL, NOW - 40 * _DAY),  # outside 30 days
        v(k2, "Pax", "L5", Verdict.HELPFUL, NOW - _DAY),
        v(k2, "Zed", "  ", Verdict.HELPFUL),  # unlabelled: left out of the verdict mix
    ):
        await store.record_feedback(fb)

    health = await store.fleet_health(NOW)
    assert sorted(health.alternative_ages) == [10 * 86400.0, 20 * 86400.0]
    assert sorted(health.conflict_ages) == [1 * 86400.0, 4 * 86400.0, 8 * 86400.0]
    assert sorted(health.contested_ages) == [2 * 86400.0, 36 * 86400.0]
    assert (health.synthesis_writes_30d, health.grounded_30d) == (4, 2)
    # 30-day writes: the 16 non-old entries; labels L1 7, L2 6, L3 2, L5 1.
    assert (health.writes_30d, health.top3_label_writes_30d, health.write_labels_30d) == (16, 15, 4)
    # Live verdicts of 30 days: L1 4, L2 2, L3 1 (Mira), L5 1; Ghost, Nox left out.
    assert (health.verdicts_30d, health.top3_label_verdicts_30d, health.verdict_labels_30d) == (
        8,
        7,
        4,
    )


async def test_role_activity_of_scouts_and_everyone_else_hand_computed(adapter: Adapter) -> None:
    """ADR 0144: 30 days of writes, writes tagged alternative or dead-end, supersessions
    written and verdicts of non-revoked reporters, split by the agent's own role."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    for name in ("scout1", "scout2", "plain"):
        await _agent_in(adapter, name, fleet.id)
    await store.set_agent_role("scout1", AgentRole.SCOUT)
    await store.set_agent_role("scout2", AgentRole.SCOUT)
    await store.revoke_agent("scout2")  # keeps its role; its verdicts stop counting

    async def make(summary: str, days: int, author: str, **kw: object) -> Entry:
        e = await write(adapter, summary, author=author, occurred_at=None, **kw)
        await _backdate(adapter, e, NOW - days * _DAY)
        return e

    s1 = await make("s1", 3, "scout1", tags=("alternative",))
    s2 = await make("s2", 10, "scout1", tags=("dead-end", "anomaly"))
    await make("s4", 5, "scout2")
    p1 = await make("p1", 2, "plain", tags=("alternative",))
    p2 = await make("p2", 4, "plain")
    p3 = await make("p3", 6, "nobody")
    old = await make("old", 40, "plain")
    await make("n1", 1, "scout1", supersedes=(p2.id,))
    await make("n2", 2, "plain", supersedes=(old.id,))

    def verdict(entry: Entry, user: str, days: int) -> Feedback:
        return Feedback(entry.id, user, user, Verdict.HELPFUL, None, NOW - days * _DAY)

    for fb in (
        verdict(p1, "scout1", 2),
        verdict(p3, "scout1", 5),
        verdict(s1, "scout1", 10),
        verdict(s2, "scout1", 40),  # outside the 30 days
        verdict(s1, "scout2", 3),  # revoked reporter
        verdict(s1, "plain", 3),
        verdict(s1, "admin", 4),  # no agent record
    ):
        await store.record_feedback(fb)

    health = await store.fleet_health(NOW)
    assert health.scout_activity_30d == RoleActivity(
        writes=4, alternative_or_dead_end_writes=2, supersessions=1, verdicts=3
    )
    assert health.other_activity_30d == RoleActivity(
        writes=4, alternative_or_dead_end_writes=1, supersessions=1, verdicts=2
    )
    gone = await store.fleet_health(NOW + 400 * _DAY)
    assert (gone.scout_activity_30d, gone.other_activity_30d) == (RoleActivity(), RoleActivity())


async def test_fleet_convergence_echo_and_dispersion(adapter: Adapter) -> None:
    """ADR 0075. Identical vectors have cosine 1, vectors with different hot
    positions the constant ``c`` below."""
    dim = adapter.dim
    c = (2 * 0.01 + (dim - 2) * 0.0001) / (1 + (dim - 1) * 0.0001)

    async def make(summary: str, days: int, hot: int, tags: tuple[str, ...] = ()) -> Entry:
        e = await write(adapter, summary, hot=hot, tags=tags, occurred_at=None)
        await _backdate(adapter, e, NOW - days * _DAY)
        return e

    await make("e5 old", 45, 2)
    await make("e1", 10, 0)
    await make("e2", 9, 0)  # echo of e1
    await make("e3", 8, 1)  # only ~c to everything before it
    await make("e4", 7, 1)  # echo of e3
    zero = await adapter.store.create_entry(
        draft("zero norm", occurred_at=None), [0.0] * dim, "test-model"
    )  # cosine to anything is NaN in Postgres: never an echo
    await _backdate(adapter, zero, NOW - 6 * _DAY)
    got = await adapter.store.fleet_convergence(NOW)
    assert (got.echo_sampled, got.echo_hits) == (5, 2)  # e1 sees only e5 (c)
    assert got.dispersion == ()  # no tag has 5 entries in both windows

    for i in range(5):  # the first lists its tag twice: still one entry of "auth"
        await make(f"auth now {i}", 5 + i, 0, ("auth", "auth") if i == 0 else ("auth",))
    for i, hot in enumerate((0, 0, 0, 1, 1)):
        await make(f"auth before {i}", 35 + i, hot, ("auth",))
    for i in range(6):
        await make(f"ops now {i}", 3 + i, 2, ("ops",))
    for i in range(5):
        await make(f"ops before {i}", 40 + i, 2, ("ops",))
    for i in range(5):
        await make(f"db now {i}", 2 + i, 0, ("db",))
    for i in range(4):  # one short of the 5 a window needs
        await make(f"db before {i}", 50 + i, 0, ("db",))
    for window_start in (2, 35):  # self-scope entries do not feed the dispersion
        for i in range(5):
            e = await write(adapter, f"mine {i}", scope="self", tags=("mine",), occurred_at=None)
            await _backdate(adapter, e, NOW - (window_start + i) * _DAY)
    got = await adapter.store.fleet_convergence(NOW)
    by_tag = {d.tag: d for d in got.dispersion}
    assert list(by_tag) == ["ops", "auth"]  # busiest first; db is left out
    ops, auth = by_tag["ops"], by_tag["auth"]
    assert (ops.recent_entries, ops.before_entries) == (6, 5)
    assert (ops.recent_cosine, ops.before_cosine) == (pytest.approx(1.0), pytest.approx(1.0))
    assert (auth.recent_entries, auth.before_entries) == (5, 5)
    assert auth.recent_cosine == pytest.approx(1.0, abs=1e-5)
    assert auth.before_cosine == pytest.approx((4 + 6 * c) / 10, abs=1e-5)  # 4 equal pairs of 10


async def test_fleet_convergence_of_an_empty_pool(adapter: Adapter) -> None:
    got = await adapter.store.fleet_convergence(NOW)
    assert (got.echo_sampled, got.echo_hits, got.dispersion) == (0, 0, ())


async def test_usage_counts_group_every_state(adapter: Adapter) -> None:
    old = await write(adapter, "v1", author="bob")
    await write(adapter, "v2", author="bob", supersedes=(old.id,))
    rows = await adapter.store.usage_counts()
    by_active = {r.active: r.count for r in rows if r.author == "bob"}
    assert by_active == {True: 1, False: 1}


async def test_usage_counts_group_by_scope_kind_provenance_author_and_fleet(
    adapter: Adapter,
) -> None:
    fleet = await adapter.store.create_fleet("blue")
    await write(adapter, "a", author="alice")
    await write(adapter, "a2", author="alice")
    await write(adapter, "b", author="bob", kind=Kind.INSIGHT, scope="fleet", fleet_id=fleet.id)
    caller = replace(draft("c"), importance=5, importance_source=ImportanceSource.CALLER)
    withdrawn = await adapter.store.create_entry(caller, adapter.vec(0), "test-model")
    await adapter.store.withdraw_entry(withdrawn.id, "x", by_user="admin")
    got = {
        (r.scope, r.kind, r.importance_source, r.author, r.fleet_id, r.active): r.count
        for r in await adapter.store.usage_counts()
    }
    default = ImportanceSource.DEFAULT
    assert got == {
        ("org", Kind.FACT, default, "alice", None, True): 2,
        ("fleet", Kind.INSIGHT, default, "bob", fleet.id, True): 1,
        ("org", Kind.FACT, ImportanceSource.CALLER, "alice", None, False): 1,
    }
