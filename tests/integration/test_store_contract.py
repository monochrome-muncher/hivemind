"""The ``Store`` port contract, run against both adapters.

AGENTS.md: ``MemoryStore`` "must behave like ``PgStore``", because every
service and surface unit test runs on ``MemoryStore``. These tests hold
both adapters to the behaviour ``ports.Store`` documents, case by case,
so a divergence fails here instead of making the unit suite prove the
wrong thing.

``memory`` always runs; ``pg`` needs the dev Postgres (it skips when the
database is down, and CI's ``HM_TEST_REQUIRE_PG=1`` turns that skip into
a failure). The divergences known today are marked ``xfail(strict=True)``
on the adapter that differs, so fixing one turns the mark into a failure
that asks for the mark to be removed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    FleetExists,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    Visibility,
)
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Correction, Feedback, FeedbackSignals, Verdict, is_contested
from hivemind.domain.health import HealthCounts
from hivemind.memstore import MemoryStore
from hivemind.ports import Store, SupersedeConflict
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NO_SUCH_ID = "00000000-0000-4000-8000-00000000dead"

_TABLES = (
    "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, "
    "search_counts, entry_links, pins"
)


class TickingClock:
    """Every reading is 1 ms later, so ``created_at`` orders writes the way
    Postgres's per-transaction ``now()`` does."""

    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now


@dataclass
class Adapter:
    kind: str
    store: Store
    dim: int

    def vec(self, hot: int, base: float = 0.01) -> list[float]:
        vector = [base] * self.dim
        vector[hot] = 1.0
        return vector


async def _pg_adapter() -> AsyncIterator[Adapter]:
    settings = Settings()
    dsn, dim = settings.database_url, settings.embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    try:
        has_vector = await probe.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
        )
    finally:
        await probe.close()
    if not has_vector:
        pytest.skip("'vector' extension is not available on the dev database")
    await migrate(dsn, dim)

    async def truncate() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_TABLES)
        finally:
            await conn.close()

    await truncate()
    store = PgStore(dsn)
    try:
        yield Adapter("pg", store, dim)
    finally:
        await truncate()
        await store.close()


@pytest.fixture(params=["memory", "pg"])
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[Adapter]:
    if request.param == "memory":
        yield Adapter("memory", MemoryStore(clock=TickingClock()), Settings().embedding_dim)
        return
    async for pg in _pg_adapter():
        yield pg


def diverges_on(kind: str, reason: str) -> Callable[[pytest.FixtureRequest], None]:
    """Mark the current test xfail (strict) for one adapter: a divergence
    known today, kept visible rather than hidden."""

    def apply(request: pytest.FixtureRequest) -> None:
        if request.node.callspec.params["adapter"] == kind:
            request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))

    return apply


def draft(
    summary: str,
    *,
    author: str = "alice",
    kind: Kind = Kind.FACT,
    body: str | None = None,
    tags: tuple[str, ...] = (),
    scope: str = "org",
    fleet_id: str | None = None,
    supersedes: tuple[str, ...] = (),
    see_also: tuple[str, ...] = (),
    sources: int | tuple[Source, ...] = 0,
    agent: str = "claude-code",
    occurred_at: datetime | None = T0,
) -> EntryDraft:
    return EntryDraft(
        kind=kind,
        summary=summary,
        author=author,
        agent=agent,
        body=body,
        tags=tags,
        scope=scope,
        fleet_id=fleet_id,
        supersedes=supersedes,
        see_also=see_also,
        sources=(
            sources
            if isinstance(sources, tuple)
            else tuple(Source(SourceType.URL, f"https://example.test/{i}") for i in range(sources))
        ),
        occurred_at=occurred_at,
    )


async def write(a: Adapter, summary: str, hot: int = 0, **kwargs: object) -> Entry:
    return await a.store.create_entry(draft(summary, **kwargs), a.vec(hot), "test-model")  # type: ignore[arg-type]


def _vis(level: TrustLevel, name: str = "reader", fleet: str | None = None) -> Visibility:
    return Visibility(level=level, name=name, home_fleet_id=fleet)


# -- create / read ----------------------------------------------------------------


async def test_create_entry_round_trips(adapter: Adapter) -> None:
    created = await write(adapter, "auth uses JWT", body="details", tags=("auth", "jwt"))
    assert created.state is EntryState.ACTIVE
    assert created.embedding_model == "test-model"
    got = await adapter.store.get_entry(created.id)
    assert got is not None
    for field in ("id", "kind", "summary", "body", "tags", "author", "agent", "scope"):
        assert getattr(got, field) == getattr(created, field)
    assert got.occurred_at == T0


async def test_unknown_and_malformed_ids_read_as_absent(adapter: Adapter) -> None:
    entry = await write(adapter, "present")
    assert await adapter.store.get_entry(NO_SUCH_ID) is None
    assert await adapter.store.get_entry("not-a-uuid") is None
    found = await adapter.store.get_entries([entry.id, NO_SUCH_ID, "not-a-uuid"])
    assert list(found) == [entry.id]


@pytest.mark.parametrize("spelling", ["{%s}", "urn:uuid:%s", "-----%s"])
async def test_uuid_spellings_postgres_rejects_read_as_absent(
    adapter: Adapter, spelling: str
) -> None:
    """``uuid.UUID`` parses these, asyncpg's codec does not: an id-taking
    call must answer "no such entry", never raise a ``DataError`` (a 500)."""
    entry = await write(adapter, "present")
    odd = spelling % entry.id
    store = adapter.store
    assert await store.get_entry(odd) is None
    assert await store.get_entries([odd]) == {}
    assert await store.list_predecessors([odd]) == []
    assert await store.entry_links(odd, 5) == ([], [])
    assert await store.feedback_counts(odd) == (0, 0, 0)
    assert await store.list_feedback(odd, 5) == []
    assert await store.get_fleet(odd) is None
    with pytest.raises(KeyError):
        await store.withdraw_entry(odd, None, by_user="alice")


# -- supersession (ADR 0034) --------------------------------------------------------


async def test_supersede_flips_the_target(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    flipped = await adapter.store.get_entry(old.id)
    assert flipped is not None
    assert flipped.state is EntryState.SUPERSEDED
    assert flipped.superseded_by == new.id
    preds = await adapter.store.list_predecessors([new.id])
    assert [p.id for p in preds] == [old.id]


async def test_supersede_of_an_inactive_target_writes_nothing(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    await write(adapter, "v2", supersedes=(old.id,))
    before = await adapter.store.count_entries(EntryFilters(include_inactive=True))
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "v3", supersedes=(old.id,))
    assert exc.value.ids == [old.id]
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == before


async def test_supersede_of_an_unknown_target_writes_nothing(adapter: Adapter) -> None:
    with pytest.raises(SupersedeConflict):
        await write(adapter, "orphan", supersedes=(NO_SUCH_ID,))
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == 0


# -- listing and filters (SPEC §5.3) ------------------------------------------------


async def test_list_is_newest_first_and_paginates(adapter: Adapter) -> None:
    ids = [(await write(adapter, f"entry {i}")).id for i in range(4)]
    listed = await adapter.store.list_entries(EntryFilters(), limit=2, offset=1)
    assert [e.id for e in listed] == [ids[2], ids[1]]


async def test_filters_combine_with_and(adapter: Adapter) -> None:
    hit = await write(adapter, "a", kind=Kind.DECISION, tags=("x", "y"), author="bob")
    await write(adapter, "b", kind=Kind.DECISION, tags=("x",), author="bob")
    await write(adapter, "c", kind=Kind.FACT, tags=("x", "y"), author="bob")
    await write(adapter, "d", kind=Kind.DECISION, tags=("x", "y"), author="carol")
    filters = EntryFilters(kind=Kind.DECISION, tags=("x", "y"), author="bob")
    assert [e.id for e in await adapter.store.list_entries(filters)] == [hit.id]
    assert await adapter.store.count_entries(filters) == 1


async def test_inactive_entries_are_hidden_unless_asked(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    active = await adapter.store.list_entries(EntryFilters())
    assert [e.id for e in active] == [new.id]
    every = await adapter.store.list_entries(EntryFilters(include_inactive=True))
    assert {e.id for e in every} == {old.id, new.id}


async def test_flagged_keeps_entries_reported_stale_or_wrong(adapter: Adapter) -> None:
    good = await write(adapter, "good")
    stale = await write(adapter, "stale")
    for entry, verdict in ((good, Verdict.HELPFUL), (stale, Verdict.STALE)):
        await adapter.store.record_feedback(Feedback(entry.id, "u", "a", verdict, updated_at=T0))
    flagged = await adapter.store.list_entries(EntryFilters(flagged=True))
    assert [e.id for e in flagged] == [stale.id]


# -- visibility (ADR 0011, SPEC §12.2) ----------------------------------------------


async def test_visibility_matrix(adapter: Adapter) -> None:
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "org note", scope="org", author="w")
    mine = await write(adapter, "my note", scope="self", author="reader")
    theirs = await write(adapter, "their note", scope="self", author="w")
    in_home = await write(adapter, "home fleet", scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "other fleet", scope="fleet", fleet_id=other, author="w")

    async def visible(v: Visibility | None) -> set[str]:
        return {e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)}

    everything = {org.id, mine.id, theirs.id, in_home.id, in_other.id}
    assert await visible(None) == everything
    assert await visible(Visibility(TrustLevel.PRIVILEGED, "x", is_admin=True)) == everything
    assert await visible(_vis(TrustLevel.UNTRUSTED, fleet=home)) == set()
    member = {org.id, mine.id, in_home.id}
    assert await visible(_vis(TrustLevel.LURKER, fleet=home)) == member
    assert await visible(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == member
    assert await visible(_vis(TrustLevel.PRIVILEGED, fleet=home)) == member | {in_other.id}
    count = await adapter.store.count_entries(
        EntryFilters(), visibility=_vis(TrustLevel.LURKER, fleet=home)
    )
    assert count == len(member)


async def test_own_fleet_entries_stay_visible_after_a_fleet_move(adapter: Adapter) -> None:
    old_home, new_home = str(uuid.uuid4()), str(uuid.uuid4())
    entry = await write(adapter, "written at home", scope="fleet", fleet_id=old_home, author="me")
    v = _vis(TrustLevel.CONTRIBUTOR, name="me", fleet=new_home)
    assert [e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)] == [
        entry.id
    ]


# -- search streams (SPEC §6.2) -----------------------------------------------------


async def test_keyword_search_matches_any_term_and_respects_visibility(adapter: Adapter) -> None:
    jwt = await write(adapter, "tokens are signed with JWT", author="w")
    await write(adapter, "unrelated cooking recipe", author="w")
    private = await write(adapter, "JWT rotation notes", scope="self", author="w")
    ids = await adapter.store.search_keyword("JWT expiry", EntryFilters(), 10)
    assert set(ids) == {jwt.id, private.id}
    seen = await adapter.store.search_keyword(
        "JWT", EntryFilters(), 10, visibility=_vis(TrustLevel.CONTRIBUTOR)
    )
    assert seen == [jwt.id]


async def test_vector_search_ranks_nearest_first(adapter: Adapter) -> None:
    near = await write(adapter, "near", hot=1)
    far = await write(adapter, "far", hot=2)
    probe = adapter.vec(1)
    probe[2] = 0.5
    assert await adapter.store.search_vector(probe, EntryFilters(), 10) == [near.id, far.id]


async def test_similar_entries_exclude_the_entry_and_inactive_ones(adapter: Adapter) -> None:
    old = await write(adapter, "v1", hot=3)
    new = await write(adapter, "v2", hot=3, supersedes=(old.id,))
    other = await write(adapter, "other", hot=4)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5, exclude_id=new.id)
    assert [eid for eid, _ in pairs] == [other.id]
    assert -1.0 <= pairs[0][1] <= 1.0


async def test_similar_entries_without_an_excluded_id_returns_every_active_match(
    adapter: Adapter,
) -> None:
    first = await write(adapter, "first", hot=3)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5)
    assert [eid for eid, _ in pairs] == [first.id]


async def test_keyword_ties_break_newest_first(adapter: Adapter) -> None:
    first = await write(adapter, "shared word alpha")
    second = await write(adapter, "shared word alpha")
    assert await adapter.store.search_keyword("alpha", EntryFilters(), 10) == [
        second.id,
        first.id,
    ]


async def test_vector_search_leaves_out_entries_with_no_positive_similarity(
    adapter: Adapter,
) -> None:
    near = await adapter.store.create_entry(draft("near"), adapter.vec(5), "test-model")
    opposite = [0.0] * adapter.dim
    opposite[5] = -1.0
    await adapter.store.create_entry(draft("opposite"), opposite, "test-model")
    orthogonal = [0.0] * adapter.dim
    orthogonal[6] = 1.0
    await adapter.store.create_entry(draft("orthogonal"), orthogonal, "test-model")
    assert await adapter.store.search_vector(adapter.vec(5, base=0.0), EntryFilters(), 10) == [
        near.id
    ]


async def test_vector_search_leaves_out_entries_at_or_below_min_similarity(
    adapter: Adapter,
) -> None:
    """The search similarity threshold (ADR 0062): ``min_similarity``
    raises ADR 0061's floor of 0, the same way on both adapters."""
    close = [0.0] * adapter.dim
    close[5], close[6] = 0.8, 0.6  # similarity 0.8 to the probe
    loose = [0.0] * adapter.dim
    loose[5], loose[6] = 0.3, 0.9539392  # similarity 0.3
    near = await adapter.store.create_entry(draft("close"), close, "test-model")
    far = await adapter.store.create_entry(draft("loose"), loose, "test-model")
    probe = adapter.vec(5, base=0.0)
    search = adapter.store.search_vector
    assert await search(probe, EntryFilters(), 10, min_similarity=0.0) == [near.id, far.id]
    assert await search(probe, EntryFilters(), 10, min_similarity=0.5) == [near.id]
    assert await search(probe, EntryFilters(), 10, min_similarity=0.9) == []


# -- withdraw ------------------------------------------------------------------------


async def test_withdraw(adapter: Adapter) -> None:
    entry = await write(adapter, "to withdraw")
    withdrawn = await adapter.store.withdraw_entry(entry.id, "obsolete", by_user="alice")
    assert withdrawn.state is EntryState.WITHDRAWN
    assert withdrawn.withdrawn_reason == "obsolete"
    with pytest.raises(ValueError):
        await adapter.store.withdraw_entry(entry.id, None, by_user="alice")
    with pytest.raises(KeyError):
        await adapter.store.withdraw_entry(NO_SUCH_ID, None, by_user="alice")


# -- "see also" links (ADR 0057, 0059) ------------------------------------------------


async def test_links_read_from_both_ends(adapter: Adapter) -> None:
    a = await write(adapter, "a")
    b = await write(adapter, "b")
    c = await write(adapter, "c", see_also=(a.id, b.id, NO_SUCH_ID))
    d = await write(adapter, "d", see_also=(a.id,))
    outgoing, incoming = await adapter.store.entry_links(c.id, 10)
    assert (sorted(outgoing), incoming) == (sorted([a.id, b.id]), [])
    assert await adapter.store.entry_links(a.id, 10) == ([], [d.id, c.id])
    assert await adapter.store.entry_links(a.id, 1) == ([], [d.id])
    many = await adapter.store.entry_links_many([a.id, c.id, NO_SUCH_ID], 10)
    assert many[a.id] == ([], [d.id, c.id])
    assert sorted(many[c.id][0]) == sorted([a.id, b.id]) and many[c.id][1] == []
    assert many[NO_SUCH_ID] == ([], [])
    assert list(many) == [a.id, c.id, NO_SUCH_ID]


async def test_links_of_one_write_read_in_id_order(adapter: Adapter) -> None:
    a = await write(adapter, "a")
    b = await write(adapter, "b")
    c = await write(adapter, "c", see_also=(max(a.id, b.id), min(a.id, b.id)))
    outgoing, _ = await adapter.store.entry_links(c.id, 10)
    assert outgoing == sorted([a.id, b.id])


# -- pins (ADR 0058) -----------------------------------------------------------------


async def test_pins_are_idempotent_capped_and_newest_first(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    c = await write(adapter, "c", scope="fleet", fleet_id=fleet)
    first = await adapter.store.pin_entry(fleet, a.id, "boss", limit=2)
    assert first is not None and first.entry_id == a.id and first.pinned_by == "boss"
    assert await adapter.store.pin_entry(fleet, a.id, "other", limit=2) == first
    assert await adapter.store.pin_entry(fleet, b.id, "boss", limit=2) is not None
    assert await adapter.store.pin_entry(fleet, c.id, "boss", limit=2) is None
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [b.id, a.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is True
    assert await adapter.store.unpin_entry(fleet, a.id) is False
    assert await adapter.store.list_pins(str(uuid.uuid4())) == []


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


# -- counters (ROADMAP §3.3, ADR 0056) --------------------------------------------------


async def test_search_counters_accumulate_per_fleet(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    await adapter.store.record_search(fleet, empty=True)
    await adapter.store.record_search(fleet, empty=False)
    await adapter.store.record_search(None, empty=False)
    counts = {c.fleet_id: (c.searches, c.empty) for c in await adapter.store.search_counts()}
    assert counts == {fleet: (2, 1), None: (1, 0)}


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
                 gap_open_question=1),
        blue_id: hc(blue_id, sourceable=3, sourced=1, insights=1, synthesizing=1, gap_anomaly=1,
                    gap_dead_end=1),
        red_id: hc(red_id, sourceable=2, sourced=1, flagged_backlog_14d=1, orphaned_flagged=1),
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


async def test_fleet_health_of_an_empty_pool(adapter: Adapter) -> None:
    health = await adapter.store.fleet_health(T0)
    assert health.by_fleet == () and health.correction_latencies == ()
    assert (health.helpful, health.writes_90d, health.verdicts_90d) == (0, 0, 0)


async def _backdate(a: Adapter, entry: Entry, created_at: datetime) -> None:
    """Move an entry's ``created_at`` (the adapters stamp their own clocks)."""
    if isinstance(a.store, MemoryStore):
        a.store._entries[entry.id] = replace(a.store._entries[entry.id], created_at=created_at)
        return
    async with a.store._connection() as conn:  # type: ignore[attr-defined]
        await conn.execute("UPDATE entries SET created_at = $1 WHERE id = $2", created_at, entry.id)


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


# -- fleets and agents (ADRs 0011, 0012, 0028, 0045) -------------------------------------


async def test_fleets(adapter: Adapter) -> None:
    fleet = await adapter.store.create_fleet("blue")
    with pytest.raises(FleetExists, match="fleet already exists: 'blue'"):
        await adapter.store.create_fleet("blue")
    assert await adapter.store.get_fleet(fleet.id) == fleet
    assert await adapter.store.get_fleet(NO_SUCH_ID) is None
    assert [f.name for f in await adapter.store.list_fleets()] == ["blue"]


async def test_agent_lifecycle(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    agent = await store.register_agent("Scout", "owner")
    assert (agent.status, agent.trust_level, agent.home_fleet_id) == (
        AgentStatus.PENDING,
        TrustLevel.UNTRUSTED,
        None,
    )
    assert await store.register_agent("Scout", "someone else") == agent  # never overwritten
    assert (await store.register_agent("scout")).name == "Scout"  # case variant (ADR 0045)
    active = await store.activate_agent(
        "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
    )
    assert active.status is AgentStatus.ACTIVE and active.home_fleet_id == fleet.id
    with pytest.raises(InvalidAgentStatus):
        await store.activate_agent(
            "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
    promoted = await store.set_agent_trust_level("Scout", TrustLevel.PRIVILEGED)
    assert promoted.trust_level is TrustLevel.PRIVILEGED
    revoked = await store.revoke_agent("Scout")
    assert revoked.status is AgentStatus.REVOKED
    with pytest.raises(InvalidAgentStatus):
        await store.revoke_agent("Scout")
    for call in (
        store.revoke_agent("ghost"),
        store.set_agent_trust_level("ghost", TrustLevel.LURKER),
        store.set_agent_home_fleet("ghost", fleet.id),
        store.activate_agent("ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id),
    ):
        with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
            await call
    assert await store.get_agent("ghost") is None


async def test_agents_list_by_name(adapter: Adapter) -> None:
    for name in ("zed", "amy"):
        await adapter.store.register_agent(name)
    assert [a.name for a in await adapter.store.list_agents()] == ["amy", "zed"]


# -- audit log (ADR 0027) ---------------------------------------------------------------


async def test_audit_log_is_newest_first_and_filters(adapter: Adapter) -> None:
    store = adapter.store

    def event(actor: str, target: str) -> AuditEvent:
        return AuditEvent(ActorKind.ADMIN_KEY, actor, AuditAction.FLEET_CREATE, target, {"n": 1})

    first = await store.record_audit(event("admin:a", "f1"))
    second = await store.record_audit(event("admin:b", "f2"))
    third = await store.record_audit(event("admin:a", "f3"))
    assert second.detail == {"n": 1}
    assert [r.id for r in await store.list_audit(AuditFilters(), 10)] == [
        third.id,
        second.id,
        first.id,
    ]
    by_actor = await store.list_audit(AuditFilters(actor="admin:a"), 10)
    assert [r.target for r in by_actor] == ["f3", "f1"]
    older = await store.list_audit(AuditFilters(before=third.id), 10)
    assert [r.id for r in older] == [second.id, first.id]
    assert len(await store.list_audit(AuditFilters(), 1)) == 1


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
