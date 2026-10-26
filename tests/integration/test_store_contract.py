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
from tests.pg import ensure_migrated

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
from hivemind.domain.feedback import (
    Correction,
    Feedback,
    FeedbackLogFilters,
    FeedbackSignals,
    FlagBacklogFilters,
    Verdict,
    is_contested,
)
from hivemind.domain.health import HealthCounts
from hivemind.domain.pin import PinRefusal, PinWrite
from hivemind.memstore import MemoryStore, cosine_similarity
from hivemind.ports import Store, SupersedeConflict
from hivemind.store import PgStore

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
    await ensure_migrated(dsn, dim)

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


async def test_pairwise_similarity_reads_stored_vectors(adapter: Adapter) -> None:
    """ADR 0074: cosine between stored vectors, each unordered pair once under
    sorted string keys; entries with no vector, unknown and malformed ids
    drop out; no embedder involved."""
    same = await write(adapter, "pool is 20", hot=3)
    near = await adapter.store.create_entry(
        draft("pool raised to 90"), adapter.vec(3, base=0.02), "test-model"
    )
    far = await write(adapter, "unrelated", hot=7)
    bare = await adapter.store.create_entry(draft("no vector"), None, None)
    ids = [far.id, same.id, near.id, bare.id, NO_SUCH_ID, "not-a-uuid", same.id]
    got = await adapter.store.pairwise_similarity(ids)

    def key(a: Entry, b: Entry) -> tuple[str, str]:
        x, y = sorted((a.id, b.id))
        return (x, y)

    assert set(got) == {key(same, near), key(same, far), key(near, far)}
    expected = cosine_similarity(adapter.vec(3), adapter.vec(3, base=0.02))  # any dim
    assert got[key(same, near)] == pytest.approx(expected, abs=1e-3)
    assert got[key(same, near)] > got[key(same, far)]
    assert await adapter.store.pairwise_similarity([same.id]) == {}
    assert await adapter.store.pairwise_similarity([]) == {}


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


ME = ("u-me", "me")  # the (user, agent) a feedback row is keyed by


async def _fb(
    a: Adapter, entry: Entry, who: tuple[str, str], verdict: Verdict, at: datetime
) -> None:
    await a.store.record_feedback(Feedback(entry.id, who[0], who[1], verdict, None, at))


async def _relied(a: Adapter, who: tuple[str, str] | None = ME, **kw: object) -> list[str]:
    filters = EntryFilters(relied_on_changed=True, reporter=who, **kw)  # type: ignore[arg-type]
    return [e.id for e in await a.store.list_entries(filters)]


async def test_relied_on_changed_lists_a_superseded_entry_the_caller_rated_helpful(
    adapter: Adapter,
) -> None:
    old = await write(adapter, "old", author="ann")
    await _fb(adapter, old, ME, Verdict.HELPFUL, T0 - timedelta(days=1))
    new = await write(adapter, "new", author="ann", supersedes=(old.id,))
    assert await _relied(adapter) == [old.id]  # inactive entries are included
    listed = await adapter.store.list_entries(EntryFilters(relied_on_changed=True, reporter=ME))
    assert listed[0].state is EntryState.SUPERSEDED and listed[0].superseded_by == new.id
    default = await adapter.store.list_entries(EntryFilters(reporter=ME))
    assert [e.id for e in default] == [new.id]  # the filter is off by default


async def test_relied_on_changed_ignores_a_successor_older_than_the_rating(
    adapter: Adapter,
) -> None:
    old = await write(adapter, "old", author="ann")
    await write(adapter, "new", author="ann", supersedes=(old.id,))
    await _fb(adapter, old, ME, Verdict.HELPFUL, datetime.now(UTC) + timedelta(days=1))
    assert await _relied(adapter) == []  # rated after it was already superseded


async def test_relied_on_changed_lists_a_withdrawn_entry(adapter: Adapter) -> None:
    gone = await write(adapter, "gone", author="ann")
    await _fb(adapter, gone, ME, Verdict.HELPFUL, T0)
    await adapter.store.withdraw_entry(gone.id, "obsolete", by_user="ann")
    assert await _relied(adapter) == [gone.id]


async def test_relied_on_changed_lists_a_later_flag_by_someone_else(adapter: Adapter) -> None:
    wrong = await write(adapter, "wrong later", author="ann")
    stale = await write(adapter, "stale later", author="ann")
    for entry, verdict in ((wrong, Verdict.WRONG), (stale, Verdict.STALE)):
        await _fb(adapter, entry, ME, Verdict.HELPFUL, T0)
        await _fb(adapter, entry, ("u-other", "other"), verdict, T0 + timedelta(hours=1))
    assert set(await _relied(adapter)) == {wrong.id, stale.id}


async def test_relied_on_changed_skips_an_older_flag_a_helpful_report_and_unrated_entries(
    adapter: Adapter,
) -> None:
    older = await write(adapter, "flagged before I rated", author="ann")
    await _fb(adapter, older, ("u-other", "other"), Verdict.WRONG, T0 - timedelta(hours=1))
    await _fb(adapter, older, ME, Verdict.HELPFUL, T0)
    quiet = await write(adapter, "nothing happened", author="ann")
    await _fb(adapter, quiet, ME, Verdict.HELPFUL, T0)
    await _fb(adapter, quiet, ("u-other", "other"), Verdict.HELPFUL, T0 + timedelta(hours=1))
    unrated = await write(adapter, "flagged but never rated", author="ann")
    await _fb(adapter, unrated, ("u-other", "other"), Verdict.WRONG, T0 + timedelta(hours=1))
    assert await _relied(adapter) == []


async def test_relied_on_changed_ignores_the_callers_own_flags_and_other_callers_ratings(
    adapter: Adapter,
) -> None:
    mine = await write(adapter, "I changed my mind", author="ann")
    await _fb(adapter, mine, ME, Verdict.HELPFUL, T0)
    await _fb(adapter, mine, ME, Verdict.WRONG, T0 + timedelta(hours=1))  # my verdict moved
    theirs = await write(adapter, "rated by another", author="ann")
    await _fb(adapter, theirs, ("u-other", "other"), Verdict.HELPFUL, T0)
    await _fb(adapter, theirs, ("u-third", "third"), Verdict.WRONG, T0 + timedelta(hours=1))
    assert await _relied(adapter) == []
    assert await _relied(adapter, ("u-other", "other")) == [theirs.id]
    assert await _relied(adapter, ("u-me", "other")) == []  # same user, another agent
    assert await _relied(adapter, None) == []  # no identity, no match


async def test_relied_on_changed_ignores_a_revoked_reporters_flag(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout")
    entry = await write(adapter, "flagged by Scout", author="ann")
    await _fb(adapter, entry, ME, Verdict.HELPFUL, T0)
    await _fb(adapter, entry, ("Scout", "Scout"), Verdict.WRONG, T0 + timedelta(hours=1))
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    assert await _relied(adapter) == [entry.id]
    await store.revoke_agent("Scout")
    assert await _relied(adapter) == []


async def test_relied_on_changed_applies_visibility_and_the_other_filters(
    adapter: Adapter,
) -> None:
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    readable = await write(
        adapter, "readable", scope="fleet", fleet_id=home, author="w", tags=("a",)
    )
    foreign = await write(
        adapter, "foreign", scope="fleet", fleet_id=other, author="w", tags=("a",)
    )
    secret = await write(adapter, "secret", scope="self", author="w", tags=("b",))
    for entry in (readable, foreign, secret):
        await _fb(adapter, entry, ME, Verdict.HELPFUL, T0)
        await _fb(adapter, entry, ("u-other", "other"), Verdict.WRONG, T0 + timedelta(hours=1))
    filters = EntryFilters(relied_on_changed=True, reporter=ME)
    vis = _vis(TrustLevel.CONTRIBUTOR, name="me", fleet=home)
    listed = await adapter.store.list_entries(filters, visibility=vis)
    assert [e.id for e in listed] == [readable.id]
    assert await adapter.store.count_entries(filters, visibility=vis) == 1
    assert len(await adapter.store.list_entries(filters)) == 3  # no visibility: the flat pool
    tagged = EntryFilters(relied_on_changed=True, reporter=ME, tags=("b",))
    assert [e.id for e in await adapter.store.list_entries(tagged)] == [secret.id]


async def test_rating_helpful_again_clears_a_changed_entry(adapter: Adapter) -> None:
    """ADR 0098: a rating made after the change is the agent's "I re-checked"."""
    after = datetime.now(UTC) + timedelta(days=1)
    superseded = await write(adapter, "superseded", author="ann")
    flagged = await write(adapter, "flagged", author="ann")
    withdrawn = await write(adapter, "withdrawn", author="ann")
    for entry in (superseded, flagged, withdrawn):
        await _fb(adapter, entry, ME, Verdict.HELPFUL, T0 - timedelta(days=1))
    await write(adapter, "new", author="ann", supersedes=(superseded.id,))
    await _fb(adapter, flagged, ("u-other", "other"), Verdict.WRONG, T0)
    await adapter.store.withdraw_entry(withdrawn.id, "obsolete", by_user="ann")
    assert set(await _relied(adapter)) == {superseded.id, flagged.id, withdrawn.id}
    for entry in (superseded, flagged, withdrawn):
        await _fb(adapter, entry, ME, Verdict.HELPFUL, after)
    assert await _relied(adapter) == []


async def test_relied_on_changed_ignores_a_withdrawal_before_the_rating(adapter: Adapter) -> None:
    gone = await write(adapter, "gone first", author="ann")
    await adapter.store.withdraw_entry(gone.id, "obsolete", by_user="ann")
    await _fb(adapter, gone, ME, Verdict.HELPFUL, datetime.now(UTC) + timedelta(days=1))
    assert await _relied(adapter) == []


async def test_relied_on_changed_ignores_the_callers_own_changes(adapter: Adapter) -> None:
    """A supersession by the caller, or a verdict from another agent
    instance under the caller's own user, is not "changed under you"."""
    mine = await write(adapter, "superseded by me", author="ann")
    await _fb(adapter, mine, ME, Verdict.HELPFUL, T0 - timedelta(days=1))
    await write(adapter, "my successor", author=ME[0], supersedes=(mine.id,))
    flagged = await write(adapter, "flagged by my other instance", author="ann")
    await _fb(adapter, flagged, ME, Verdict.HELPFUL, T0)
    await _fb(adapter, flagged, (ME[0], "other-instance"), Verdict.STALE, T0 + timedelta(hours=1))
    assert await _relied(adapter) == []


async def test_malformed_fleet_filter_matches_nothing(adapter: Adapter) -> None:
    await write(adapter, "in a fleet", scope="fleet", fleet_id=str(uuid.uuid4()))
    await write(adapter, "no fleet")
    bad = EntryFilters(fleet_id="not-a-uuid")
    assert await adapter.store.list_entries(bad) == []
    assert await adapter.store.count_entries(bad) == 0
    assert await adapter.store.search_keyword("fleet", bad, 10) == []
    assert await adapter.store.search_vector(adapter.vec(1), bad, 10) == []


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


# -- upstream links (ADR 0081) --------------------------------------------------------


async def test_upstream_links_report_what_happened_to_each_target(adapter: Adapter) -> None:
    store = adapter.store
    plain = await write(adapter, "plain", author="ann")
    flagged = await write(adapter, "flagged", author="ann")
    old = await write(adapter, "old", author="ann")
    gone = await write(adapter, "gone", author="ann")
    built = await write(
        adapter,
        "built",
        kind=Kind.INSIGHT,
        author="bob",
        see_also=(plain.id, flagged.id, old.id, gone.id),
    )
    linked = (await store.upstream_links([built.id]))[built.id]
    assert sorted(link.to_id for link in linked) == sorted([plain.id, flagged.id, old.id, gone.id])
    linked_at = linked[0].linked_at
    assert {link.linked_at for link in linked} == {linked_at}
    soon = linked_at + timedelta(minutes=5)

    new = await write(adapter, "new", author="cat", supersedes=(old.id,))
    await store.withdraw_entry(gone.id, "wrong", by_user="ann")
    await store.record_feedback(Feedback(flagged.id, "u1", "a", Verdict.WRONG, None, soon))
    await store.record_feedback(Feedback(flagged.id, "u2", "a", Verdict.HELPFUL, "ok", soon))
    await store.record_feedback(
        Feedback(flagged.id, "u3", "a", Verdict.HELPFUL, " ", soon + timedelta(minutes=1))
    )

    by_target = {link.to_id: link for link in (await store.upstream_links([built.id]))[built.id]}
    assert by_target[plain.id].state is EntryState.ACTIVE
    assert by_target[plain.id].newest_flag_at is None
    assert by_target[old.id].state is EntryState.SUPERSEDED
    assert by_target[old.id].superseded_author == "cat"
    assert by_target[old.id].superseded_at == new.created_at
    assert by_target[old.id].superseded_at > linked_at  # type: ignore[operator]
    assert by_target[gone.id].state is EntryState.WITHDRAWN
    assert by_target[flagged.id].newest_flag_at == soon
    assert by_target[flagged.id].newest_noted_helpful_at == soon
    assert all(link.from_id == built.id for link in by_target.values())


async def test_upstream_links_are_one_read_for_many_and_skip_the_unlinked(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a = await write(adapter, "a")
    b = await write(adapter, "b")
    c = await write(adapter, "c", see_also=(a.id,))
    d = await write(adapter, "d", see_also=(b.id, a.id))
    got = await store.upstream_links([c.id, d.id, a.id, NO_SUCH_ID, "not-a-uuid"])
    assert set(got) == {c.id, d.id}
    assert [link.to_id for link in got[c.id]] == [a.id]
    assert sorted(link.to_id for link in got[d.id]) == sorted([a.id, b.id])
    assert await store.upstream_links([]) == {}


async def test_upstream_links_ignore_a_revoked_reporters_flag(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout")
    base = await write(adapter, "base", author="ann")
    built = await write(adapter, "built", author="bob", see_also=(base.id,))
    [link] = (await store.upstream_links([built.id]))[built.id]
    later = link.linked_at + timedelta(minutes=5)
    await store.record_feedback(Feedback(base.id, "Scout", "Scout", Verdict.WRONG, None, later))
    [link] = (await store.upstream_links([built.id]))[built.id]
    assert link.newest_flag_at == later
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.revoke_agent("Scout")
    [link] = (await store.upstream_links([built.id]))[built.id]
    assert link.newest_flag_at is None


async def test_upstream_links_leave_out_targets_the_reader_cannot_read(adapter: Adapter) -> None:
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    store = adapter.store
    org = await write(adapter, "org", scope="org", author="w")
    mine = await write(adapter, "mine", scope="fleet", fleet_id=home, author="w")
    foreign = await write(adapter, "foreign", scope="fleet", fleet_id=other, author="w")
    secret = await write(adapter, "secret", scope="self", author="w")
    built = await write(
        adapter,
        "built",
        kind=Kind.INSIGHT,
        scope="self",
        author="w",
        see_also=(org.id, mine.id, foreign.id, secret.id),
    )

    async def seen(v: Visibility | None) -> set[str]:
        links = (await store.upstream_links([built.id], visibility=v)).get(built.id, [])
        return {link.to_id for link in links}

    assert await seen(None) == {org.id, mine.id, foreign.id, secret.id}
    assert await seen(Visibility(TrustLevel.PRIVILEGED, "x", is_admin=True)) == await seen(None)
    assert await seen(_vis(TrustLevel.UNTRUSTED, fleet=home)) == set()
    assert await seen(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == {org.id, mine.id}
    assert await seen(_vis(TrustLevel.PRIVILEGED, fleet=home)) == {org.id, mine.id, foreign.id}
    assert await seen(_vis(TrustLevel.LURKER, name="w", fleet=other)) == {
        org.id,
        mine.id,  # its own entry, whatever the fleet
        foreign.id,
        secret.id,
    }


async def test_upstream_links_show_a_supersession_or_flag_before_the_link_as_older(
    adapter: Adapter,
) -> None:
    """Facts only: the successor and the flag predate the link, so the rule
    (``upstream_changed``) reports neither."""
    store = adapter.store
    old = await write(adapter, "old", author="ann")
    await write(adapter, "new", author="cat", supersedes=(old.id,))
    flagged = await write(adapter, "flagged", author="ann")
    await store.record_feedback(
        Feedback(flagged.id, "u1", "a", Verdict.WRONG, None, T0 - timedelta(days=1))
    )
    built = await write(
        adapter, "built", kind=Kind.INSIGHT, author="bob", see_also=(old.id, flagged.id)
    )
    by_target = {link.to_id: link for link in (await store.upstream_links([built.id]))[built.id]}
    assert by_target[old.id].superseded_at is not None
    assert by_target[old.id].superseded_at < by_target[old.id].linked_at
    flag_at = by_target[flagged.id].newest_flag_at
    assert flag_at is not None and flag_at < by_target[flagged.id].linked_at
    from hivemind.domain.upstream import upstream_changed

    assert upstream_changed(Kind.INSIGHT, "bob", list(by_target.values())) == ()


# -- pins (ADR 0058) -----------------------------------------------------------------


async def test_pins_are_refreshed_capped_and_newest_first(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    c = await write(adapter, "c", scope="fleet", fleet_id=fleet)
    first = await adapter.store.pin_entry(fleet, a.id, "boss", limit=2)
    assert first is not None and first.created and first.replaced == ()
    assert first.pin.entry_id == a.id and first.pin.pinned_by == "boss"
    assert await adapter.store.pin_entry(fleet, b.id, "boss", limit=2) is not None
    assert await adapter.store.pin_entry(fleet, c.id, "boss", limit=2) is PinRefusal.FLEET
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [b.id, a.id]
    # Pinning a pinned entry refreshes it (ADR 0076): same row, new pinner,
    # back to the front, and a full fleet still accepts it.
    again = await adapter.store.pin_entry(fleet, a.id, "other", limit=2)
    assert again is not None and not again.created and again.pin.pinned_by == "other"
    assert again.pin.pinned_at >= first.pin.pinned_at
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [a.id, b.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is True
    assert await adapter.store.unpin_entry(fleet, a.id) is False
    assert await adapter.store.list_pins(str(uuid.uuid4())) == []


async def test_a_pin_replaces_the_older_pins_it_names(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    new = await write(adapter, "ab", scope="fleet", fleet_id=fleet)
    gone = str(uuid.uuid4())
    for entry in (a, b):
        assert await adapter.store.pin_entry(fleet, entry.id, "boss", limit=2) is not None
    # A full fleet may still swap pins: the count does not grow.
    moved = await adapter.store.pin_entry(
        fleet, new.id, "lead", limit=2, replaces=[a.id, b.id, gone, new.id]
    )
    assert moved is not None and moved.created and set(moved.replaced) == {a.id, b.id}
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [new.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is False


async def test_a_pin_is_capped_per_agent_and_a_refusal_writes_nothing(adapter: Adapter) -> None:
    """ADR 0096: an agent holds at most ``agent_limit`` of the fleet's pins;
    a refresh restamps the pinner so it counts, a move onto a new version
    does not raise the count, and ``None`` (the admin) is exempt."""
    fleet = str(uuid.uuid4())
    store = adapter.store
    es = [await write(adapter, f"e{i}", scope="fleet", fleet_id=fleet) for i in range(5)]
    for e in es[:2]:
        assert isinstance(await store.pin_entry(fleet, e.id, "ann", 10, agent_limit=2), PinWrite)
    # A third pin by ann breaks her cap; the fleet cap (10) is not the one hit.
    refused = await store.pin_entry(fleet, es[2].id, "ann", 10, agent_limit=2)
    assert refused is PinRefusal.AGENT
    assert [p.entry_id for p in await store.list_pins(fleet)] == [es[1].id, es[0].id]
    # Bob has his own count; pinning an entry ann holds is a refresh that takes
    # it over to him, which frees her slot.
    assert isinstance(await store.pin_entry(fleet, es[2].id, "bob", 10, agent_limit=2), PinWrite)
    assert isinstance(await store.pin_entry(fleet, es[0].id, "bob", 10, agent_limit=2), PinWrite)
    # Bob now holds two; a refresh of ann's other pin would be his third.
    assert await store.pin_entry(fleet, es[1].id, "bob", 10, agent_limit=2) is PinRefusal.AGENT
    assert {p.entry_id: p.pinned_by for p in await store.list_pins(fleet)} == {
        es[0].id: "bob",
        es[1].id: "ann",
        es[2].id: "bob",
    }
    # Refreshing one's own pin at the cap is fine.
    assert isinstance(await store.pin_entry(fleet, es[0].id, "bob", 10, agent_limit=2), PinWrite)
    # A move onto a new version removes the caller's pin and adds one: allowed
    # at the cap; but a move that does not free one of the caller's slots is not.
    moved = await store.pin_entry(fleet, es[3].id, "bob", 10, replaces=[es[2].id], agent_limit=2)
    assert isinstance(moved, PinWrite) and moved.replaced == (es[2].id,)
    refused = await store.pin_entry(fleet, es[4].id, "bob", 10, replaces=[es[1].id], agent_limit=2)
    assert refused is PinRefusal.AGENT  # es[1] is ann's: bob would hold three
    assert {p.entry_id for p in await store.list_pins(fleet)} == {es[0].id, es[1].id, es[3].id}
    # No agent limit (the admin): exempt, though the fleet cap still applies.
    assert await store.pin_entry(fleet, es[4].id, "admin", 3) is PinRefusal.FLEET
    assert isinstance(await store.pin_entry(fleet, es[4].id, "admin", 4), PinWrite)


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
