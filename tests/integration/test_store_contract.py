"""The ``Store`` port contract, run against both adapters.

AGENTS.md: ``MemoryStore`` "must behave like ``PgStore``", because every
service and surface unit test runs on ``MemoryStore``. These tests hold
both adapters to the behaviour ``ports.Store`` documents, case by case,
so a divergence fails here instead of making the unit suite prove the
wrong thing.

``memory`` always runs; ``pg`` needs the dev Postgres (it skips when the
database is down, and CI's ``HM_TEST_REQUIRE_PG=1`` turns that skip into
a failure). A divergence between the adapters would be marked
``xfail(strict=True)`` on the one that differs, so fixing it turns the mark
into a failure that asks for the mark to be removed; none is open today.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    FleetBusy,
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    UnknownFleet,
    Visibility,
)
from hivemind.domain.agent_cleanup import (
    AgentCleanupRefused,
    UnknownAgentCleanup,
    cleanup_reason,
)
from hivemind.domain.agent_role import AgentRole
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.entry_restore import (
    EntryNotWithdrawn,
    EntryRedacted,
    RestoreChainConflict,
    RestoreViaCleanup,
)
from hivemind.domain.feedback import (
    Correction,
    Feedback,
    FeedbackLogFilters,
    FeedbackSignals,
    FlagBacklogFilters,
    ReliedOnChange,
    Verdict,
    is_contested,
)
from hivemind.domain.fleet_merge import FleetMergeRefused, FleetMoveRefused, UnknownFleetMerge
from hivemind.domain.fleet_phase import FleetPhase, PhaseName
from hivemind.domain.health import FleetHealth, HealthCounts, RoleActivity
from hivemind.domain.offboarding import (
    OffboardAction,
    OffboardChanged,
    OffboardMode,
    UnknownOwner,
)
from hivemind.domain.pin import PinRefusal, PinWrite
from hivemind.domain.redaction import REDACTED_SUMMARY, RedactRefused
from hivemind.domain.self_move import RemovedPin, SelfMoveRefused
from hivemind.domain.succession import (
    AgentRetired,
    SuccessionRefused,
    UnknownSuccession,
)
from hivemind.domain.validation import InvalidInput
from hivemind.memstore import MemoryStore, cosine_similarity
from hivemind.ports import Store, SupersedeConflict
from hivemind.store import PgStore

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NO_SUCH_ID = "00000000-0000-4000-8000-00000000dead"


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

    await reset_tables(dsn)
    store = PgStore(dsn)
    try:
        yield Adapter("pg", store, dim)
    finally:
        await store.close()


@pytest.fixture(params=["memory", "pg"])
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[Adapter]:
    if request.param == "memory":
        yield Adapter("memory", MemoryStore(clock=TickingClock()), Settings().embedding_dim)
        return
    async for pg in _pg_adapter():
        yield pg


async def vector_ids(store: Store, *args: Any, **kwargs: Any) -> list[str]:
    """The ids of ``store.search_vector``, in order."""
    return [m.entry_id for m in await store.search_vector(*args, **kwargs)]


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
    assert got.embedding_model == "test-model"
    assert created.id
    assert created.created_at is not None
    assert created.embedding is not None and len(created.embedding) == len(adapter.vec(0))
    # The returned entry matches a re-read (a reader may or may not carry the vector).
    assert replace(got, embedding=None) == replace(created, embedding=None)


async def test_payload_round_trips_as_a_dict(adapter: Adapter) -> None:
    payload = {"n": 1, "nested": {"a": [1, 2, {"b": None}]}, "s": "x"}
    created = await adapter.store.create_entry(
        replace(draft("payload entry"), payload=payload), adapter.vec(0), "test-model"
    )
    assert created.payload == payload
    got = await adapter.store.get_entry(created.id)
    assert got is not None and got.payload == payload
    listed = await adapter.store.list_entries(EntryFilters(), limit=10, offset=0)
    assert [e.payload for e in listed] == [payload]


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


@pytest.mark.parametrize("spell", [str.upper, lambda i: i.replace("-", "")])
async def test_uuid_spellings_postgres_reads_find_the_entry(
    adapter: Adapter, spell: Callable[[str], str]
) -> None:
    """Upper case and no hyphens are spellings Postgres's ``uuid`` reads:
    both adapters find the entry, and ``get_entries`` keys it by its id."""
    entry = await write(adapter, "present")
    odd = spell(entry.id)
    found = await adapter.store.get_entry(odd)
    assert found is not None and found.id == entry.id
    assert await adapter.store.get_entries([odd]) == {entry.id: found}


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


async def test_predecessors_are_the_entries_a_write_superseded(adapter: Adapter) -> None:
    v0 = await write(adapter, "v0")
    v1 = await write(adapter, "v1", supersedes=(v0.id,))
    v2 = await write(adapter, "v2", supersedes=(v1.id,))
    v3 = await write(adapter, "v3", supersedes=(v2.id,))
    await write(adapter, "bystander")
    store = adapter.store
    assert [e.id for e in await store.list_predecessors([v2.id])] == [v1.id]
    assert {e.id for e in await store.list_predecessors([v1.id, v3.id])} == {v0.id, v2.id}
    assert await store.list_predecessors([]) == []
    assert await store.list_predecessors(["not-a-uuid", v0.id]) == []  # the tail has none
    [pred] = await store.list_predecessors([v1.id])
    assert (pred.summary, pred.state, pred.superseded_by) == ("v0", EntryState.SUPERSEDED, v1.id)
    a = await write(adapter, "ma")
    b = await write(adapter, "mb")
    merged = await write(adapter, "merged", supersedes=(a.id, b.id))
    assert {e.id for e in await store.list_predecessors([merged.id])} == {a.id, b.id}


async def test_supersede_of_an_inactive_target_writes_nothing(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    head = await write(adapter, "v2", supersedes=(old.id,))
    gone = await write(adapter, "withdrawn")
    await adapter.store.withdraw_entry(gone.id, None, by_user="alice")
    before = await adapter.store.count_entries(EntryFilters(include_inactive=True))
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "v3", supersedes=(old.id,))
    assert exc.value.ids == [old.id]
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "v3", supersedes=(gone.id,))
    assert exc.value.ids == [gone.id]
    # A valid target beside the inactive one is not flipped either.
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "v3", supersedes=(old.id, head.id))
    assert exc.value.ids == [old.id]
    untouched = await adapter.store.get_entry(head.id)
    assert untouched is not None
    assert untouched.state is EntryState.ACTIVE
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == before


async def test_supersede_of_an_unknown_target_writes_nothing(adapter: Adapter) -> None:
    # A malformed id is unknown too, not a database error.
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "orphan", supersedes=(NO_SUCH_ID, "not-a-uuid"))
    assert sorted(exc.value.ids) == sorted([NO_SUCH_ID, "not-a-uuid"])
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
    assert await adapter.store.count_entries(EntryFilters(kind=Kind.FACT)) == 1


async def test_agent_filter_matches_the_writing_agent(adapter: Adapter) -> None:
    mine = await write(adapter, "claude note", agent="claude-code")
    await write(adapter, "codex note", agent="codex")
    filters = EntryFilters(agent="claude-code")
    assert [e.id for e in await adapter.store.list_entries(filters)] == [mine.id]
    assert await adapter.store.count_entries(filters) == 1
    assert await adapter.store.search_keyword("note", filters, 10) == [mine.id]


async def test_created_range_bounds_are_inclusive(adapter: Adapter) -> None:
    first = await write(adapter, "first")
    middle = await write(adapter, "middle")
    last = await write(adapter, "last")
    assert first.created_at < middle.created_at < last.created_at

    async def created(**bounds: datetime) -> list[str]:
        filters = EntryFilters(**bounds)  # type: ignore[arg-type]
        listed = await adapter.store.list_entries(filters)
        assert await adapter.store.count_entries(filters) == len(listed)
        return [e.id for e in listed]

    assert await created(created_from=middle.created_at) == [last.id, middle.id]
    assert await created(created_to=middle.created_at) == [middle.id, first.id]
    assert await created(created_from=middle.created_at, created_to=middle.created_at) == [
        middle.id
    ]
    assert await created(created_from=last.created_at + timedelta(seconds=1)) == []
    assert await created(created_to=first.created_at - timedelta(seconds=1)) == []


async def test_scope_fleet_importance_source_and_entity_filters(adapter: Adapter) -> None:
    fleet_a, fleet_b = str(uuid.uuid4()), str(uuid.uuid4())
    postgres = (ExtractedEntity("Postgres", EntityKind.SYSTEM),)

    async def put(summary: str, **kwargs: object) -> Entry:
        made = EntryDraft(kind=Kind.FACT, summary=summary, author="alice", agent="a", **kwargs)  # type: ignore[arg-type]
        return await adapter.store.create_entry(made, adapter.vec(0), "test-model", postgres)

    caller = await put("caller rated", importance=5, importance_source=ImportanceSource.CALLER)
    in_a = await put("fleet a", scope="fleet", fleet_id=fleet_a)
    in_b = await put("fleet b", scope="fleet", fleet_id=fleet_b)
    plain = await adapter.store.create_entry(draft("no entities"), adapter.vec(0), "test-model")
    private = await put("private note", scope="self")
    both = await adapter.store.create_entry(
        draft("pool exhausted"),
        adapter.vec(0),
        "test-model",
        (
            ExtractedEntity("Postgres", EntityKind.SYSTEM),
            ExtractedEntity("auth-service", EntityKind.SERVICE),
        ),
    )

    async def listed(**kwargs: object) -> set[str]:
        filters = EntryFilters(**kwargs)  # type: ignore[arg-type]
        ids = {e.id for e in await adapter.store.list_entries(filters)}
        assert await adapter.store.count_entries(filters) == len(ids)
        return ids

    everything = {caller.id, in_a.id, in_b.id, plain.id, private.id, both.id}
    assert await listed() == everything
    assert await adapter.store.count_entries(EntryFilters()) == len(everything)
    assert await listed(scope="fleet") == {in_a.id, in_b.id}
    assert await listed(scope="org") == {caller.id, plain.id, both.id}
    assert await listed(scope="self") == {private.id}
    assert await listed(fleet_id=fleet_a) == {in_a.id}
    assert await listed(fleet_id=fleet_b) == {in_b.id}
    assert await listed(importance_source=ImportanceSource.CALLER) == {caller.id}
    assert await listed(importance_source=ImportanceSource.DEFAULT) == everything - {caller.id}
    assert await listed(entities=("postgres",)) == {
        caller.id,
        in_a.id,
        in_b.id,
        private.id,
        both.id,
    }
    assert await listed(entities=("postgres", "auth-service")) == {both.id}  # AND, not OR
    assert await listed(entities=("postgres", "etcd")) == set()
    assert await adapter.store.search_keyword("pool", EntryFilters(entities=("postgres",)), 10) == [
        both.id
    ]
    assert await adapter.store.search_keyword("pool", EntryFilters(entities=("etcd",)), 10) == []
    assert await listed(scope="fleet", entities=("POSTGRES",), fleet_id=fleet_b) == {in_b.id}


async def test_occurred_range_includes_both_bounds(adapter: Adapter) -> None:
    before = await write(adapter, "before", occurred_at=T0 - timedelta(days=1))
    at_start = await write(adapter, "at the start", occurred_at=T0)
    inside = await write(adapter, "inside", occurred_at=T0 + timedelta(days=1))
    at_end = await write(adapter, "at the end", occurred_at=T0 + timedelta(days=2))
    after = await write(adapter, "after", occurred_at=T0 + timedelta(days=3))
    filters = EntryFilters(occurred_from=T0, occurred_to=T0 + timedelta(days=2))
    assert {e.id for e in await adapter.store.list_entries(filters)} == {
        at_start.id,
        inside.id,
        at_end.id,
    }
    assert await adapter.store.count_entries(filters) == 3
    point = EntryFilters(occurred_from=T0, occurred_to=T0)  # equal bounds keep that instant only
    assert [e.id for e in await adapter.store.list_entries(point)] == [at_start.id]
    assert {before.id, after.id}.isdisjoint(e.id for e in await adapter.store.list_entries(filters))


async def test_inactive_entries_are_hidden_unless_asked(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    active = await adapter.store.list_entries(EntryFilters())
    assert [e.id for e in active] == [new.id]
    every = await adapter.store.list_entries(EntryFilters(include_inactive=True))
    assert {e.id for e in every} == {old.id, new.id}
    gone = await write(adapter, "v3")
    await adapter.store.withdraw_entry(gone.id, "no longer relevant", by_user="alice")
    assert await adapter.store.count_entries(EntryFilters()) == 1
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == 3


async def test_state_filter_matches_exactly_that_state(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    gone = await write(adapter, "v3")
    await adapter.store.withdraw_entry(gone.id, "no longer relevant", by_user="alice")

    async def ids(**kw: object) -> set[str]:
        return {e.id for e in await adapter.store.list_entries(EntryFilters(**kw))}  # type: ignore[arg-type]

    assert await ids(state=EntryState.ACTIVE) == {new.id}
    assert await ids(state=EntryState.SUPERSEDED) == {old.id}
    assert await ids(state=EntryState.WITHDRAWN) == {gone.id}
    assert await ids(state=EntryState.WITHDRAWN, include_inactive=True) == {gone.id}
    assert await adapter.store.count_entries(EntryFilters(state=EntryState.SUPERSEDED)) == 1


async def test_flagged_keeps_entries_reported_stale_or_wrong(adapter: Adapter) -> None:
    """ADR 0054: ``flagged`` keeps entries with a stale or wrong report, on
    list, count and both search streams; two reports do not duplicate a row."""
    good = await write(adapter, "billing good fact")
    stale = await write(adapter, "billing stale fact")
    wrong = await write(adapter, "billing wrong fact")
    await write(adapter, "billing untouched fact")
    await adapter.store.record_feedback(Feedback(good.id, "u", "a", Verdict.HELPFUL, updated_at=T0))
    for entry, verdict in ((stale, Verdict.STALE), (wrong, Verdict.WRONG)):
        for reporter in ("u", "v"):
            await adapter.store.record_feedback(
                Feedback(entry.id, reporter, "a", verdict, updated_at=T0)
            )
    flagged = EntryFilters(flagged=True)
    expected = {stale.id, wrong.id}
    listed = await adapter.store.list_entries(flagged)
    assert sorted(e.id for e in listed) == sorted(expected)
    assert await adapter.store.count_entries(flagged) == 2
    assert set(await adapter.store.search_keyword("billing", flagged, 10)) == expected
    assert set(await vector_ids(adapter.store, adapter.vec(0), flagged, 10)) == expected


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


async def test_relied_on_changes_say_what_changed_since_the_rating(adapter: Adapter) -> None:
    store = adapter.store
    old = await write(adapter, "old", author="ann")
    gone = await write(adapter, "gone", author="ann")
    flagged = await write(adapter, "flagged", author="ann")
    quiet = await write(adapter, "quiet", author="ann")
    for entry in (old, gone, flagged, quiet):
        await _fb(adapter, entry, ME, Verdict.HELPFUL, T0 - timedelta(days=1))
    await write(adapter, "new", author="ann", supersedes=(old.id,))
    await store.withdraw_entry(gone.id, "obsolete", by_user="ann")
    for who, verdict in ((("u-a", "a"), Verdict.STALE), (("u-b", "b"), Verdict.WRONG)):
        await _fb(adapter, flagged, who, verdict, T0)
    await _fb(adapter, flagged, ("u-c", "c"), Verdict.STALE, T0 - timedelta(days=2))  # before
    ids = [old.id, gone.id, flagged.id, quiet.id, NO_SUCH_ID]
    changes = await store.relied_on_changes(ids, ME)
    rated = T0 - timedelta(days=1)
    assert changes[old.id] == ReliedOnChange(rated, superseded=True, withdrawn=False)
    assert changes[gone.id] == ReliedOnChange(rated, superseded=False, withdrawn=True)
    assert changes[flagged.id] == ReliedOnChange(rated, False, False, stale=1, wrong=1)
    assert changes[quiet.id] == ReliedOnChange(rated, False, False)
    assert NO_SUCH_ID not in changes


async def test_relied_on_changes_skip_what_the_caller_did_not_rate_helpful(
    adapter: Adapter,
) -> None:
    entry = await write(adapter, "rated stale", author="ann")
    await _fb(adapter, entry, ME, Verdict.STALE, T0)
    other = await write(adapter, "rated by another", author="ann")
    await _fb(adapter, other, ("u-other", "other"), Verdict.HELPFUL, T0)
    assert await adapter.store.relied_on_changes([entry.id, other.id], ME) == {}
    assert await adapter.store.relied_on_changes([], ME) == {}


async def test_relied_on_changes_ignore_revoked_and_own_flags(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout")
    entry = await write(adapter, "flags", author="ann")
    await _fb(adapter, entry, ME, Verdict.HELPFUL, T0)
    await _fb(adapter, entry, ("Scout", "Scout"), Verdict.WRONG, T0 + timedelta(hours=1))
    await _fb(adapter, entry, (ME[0], "twin"), Verdict.STALE, T0 + timedelta(hours=1))
    await store.activate_agent("Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    seen = (await store.relied_on_changes([entry.id], ME))[entry.id]
    assert (seen.stale, seen.wrong) == (0, 1)  # the caller's own user's flag is not counted
    await store.revoke_agent("Scout")
    gone = (await store.relied_on_changes([entry.id], ME))[entry.id]
    assert (gone.stale, gone.wrong) == (0, 0)


async def _assert_changes_match_the_list(adapter: Adapter, ids: list[str]) -> None:
    changes = await adapter.store.relied_on_changes(ids, ME)
    changed = {i for i, c in changes.items() if c.superseded or c.withdrawn or c.stale + c.wrong}
    assert changed == set(await _relied(adapter))


async def test_relied_on_changes_ignore_set_aside_ratings(adapter: Adapter) -> None:
    """ADR 0111: a cleanup sets aside the caller's helpful row, or another user's flag."""
    store = adapter.store
    mine = await write(adapter, "my rating set aside", author="good")
    await _rate(adapter, mine.id, "bad", Verdict.HELPFUL, "worked", at=T0)
    await store.withdraw_entry(mine.id, "gone", "good")
    flagged = await write(adapter, "their flag set aside", author="good")
    await _fb(adapter, flagged, ME, Verdict.HELPFUL, T0)
    await _rate(adapter, flagged.id, "bad", Verdict.WRONG, "n", at=T0 + timedelta(hours=1))
    bad = ("bad", "a")
    before = await store.relied_on_changes([mine.id], bad)
    assert before[mine.id].withdrawn
    assert (await store.relied_on_changes([flagged.id], ME))[flagged.id].wrong == 1
    await _clean(adapter)
    assert await store.relied_on_changes([mine.id], bad) == {}
    assert (await store.relied_on_changes([flagged.id], ME))[flagged.id].wrong == 0
    await _assert_changes_match_the_list(adapter, [flagged.id])


async def test_relied_on_changes_ignore_a_supersession_that_is_not_news(adapter: Adapter) -> None:
    own = await write(adapter, "superseded by me", author="ann")
    await _fb(adapter, own, ME, Verdict.HELPFUL, T0 - timedelta(days=1))
    await write(adapter, "my successor", author=ME[0], supersedes=(own.id,))
    early = await write(adapter, "replaced before I rated", author="ann")
    await write(adapter, "their successor", author="ann", supersedes=(early.id,))
    await _fb(adapter, early, ME, Verdict.HELPFUL, datetime.now(UTC) + timedelta(days=1))
    changes = await adapter.store.relied_on_changes([own.id, early.id], ME)
    assert not changes[own.id].superseded
    assert not changes[early.id].superseded
    await _assert_changes_match_the_list(adapter, [own.id, early.id])


async def test_relied_on_changes_ignore_a_withdrawal_before_the_rating(adapter: Adapter) -> None:
    gone = await write(adapter, "gone first", author="ann")
    await adapter.store.withdraw_entry(gone.id, "obsolete", by_user="ann")
    await _fb(adapter, gone, ME, Verdict.HELPFUL, datetime.now(UTC) + timedelta(days=1))
    assert not (await adapter.store.relied_on_changes([gone.id], ME))[gone.id].withdrawn
    await _assert_changes_match_the_list(adapter, [gone.id])


async def test_malformed_fleet_filter_matches_nothing(adapter: Adapter) -> None:
    await write(adapter, "in a fleet", scope="fleet", fleet_id=str(uuid.uuid4()))
    await write(adapter, "no fleet")
    bad = EntryFilters(fleet_id="not-a-uuid")
    assert await adapter.store.list_entries(bad) == []
    assert await adapter.store.count_entries(bad) == 0
    assert await adapter.store.search_keyword("fleet", bad, 10) == []
    assert await vector_ids(adapter.store, adapter.vec(1), bad, 10) == []


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
    assert (
        await adapter.store.count_entries(
            EntryFilters(), visibility=_vis(TrustLevel.UNTRUSTED, fleet=home)
        )
        == 0
    )


async def test_own_fleet_entries_stay_visible_after_a_fleet_move(adapter: Adapter) -> None:
    old_home, new_home = str(uuid.uuid4()), str(uuid.uuid4())
    entry = await write(adapter, "written at home", scope="fleet", fleet_id=old_home, author="me")
    v = _vis(TrustLevel.CONTRIBUTOR, name="me", fleet=new_home)
    assert [e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)] == [
        entry.id
    ]


async def test_moving_an_agent_leaves_its_fleet_entries_in_the_old_fleet(
    adapter: Adapter,
) -> None:
    old = await adapter.store.create_fleet("old")
    new = await adapter.store.create_fleet("new")
    await adapter.store.register_agent("me")
    await adapter.store.activate_agent(
        "me", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=old.id
    )
    entry = await write(adapter, "written at old", scope="fleet", fleet_id=old.id, author="me")
    moved = await adapter.store.set_agent_home_fleet("me", new.id)
    assert moved.home_fleet_id == new.id

    stored = await adapter.store.get_entry(entry.id)
    assert stored is not None
    assert (stored.scope, stored.fleet_id) == ("fleet", old.id)

    async def sees(fleet: str, name: str) -> list[str]:
        v = _vis(TrustLevel.CONTRIBUTOR, name=name, fleet=fleet)
        return [e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)]

    assert await sees(old.id, "teammate") == [entry.id]
    assert await sees(new.id, "newcomer") == []


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
    await adapter.store.create_entry(draft("no vector"), None, None)  # never a vector hit
    assert await vector_ids(adapter.store, probe, EntryFilters(), 10) == [near.id, far.id]


async def test_both_search_streams_apply_visibility(adapter: Adapter) -> None:
    """Every entry matches the query on both streams, so only visibility can
    leave one out."""
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "cohort churn org", scope="org", author="w")
    mine = await write(adapter, "cohort churn mine", scope="self", author="reader")
    await write(adapter, "cohort churn theirs", scope="self", author="w")
    in_home = await write(adapter, "cohort churn home", scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "cohort churn other", scope="fleet", fleet_id=other, author="w")

    async def seen(v: Visibility) -> tuple[set[str], set[str]]:
        keyword = await adapter.store.search_keyword("cohort", EntryFilters(), 10, visibility=v)
        vector = await vector_ids(adapter.store, adapter.vec(0), EntryFilters(), 10, visibility=v)
        return set(keyword), set(vector)

    member = {org.id, mine.id, in_home.id}
    assert await seen(_vis(TrustLevel.UNTRUSTED, fleet=home)) == (set(), set())
    assert await seen(_vis(TrustLevel.LURKER, fleet=home)) == (member, member)
    assert await seen(_vis(TrustLevel.PRIVILEGED, fleet=home)) == (
        member | {in_other.id},
        member | {in_other.id},
    )


async def test_similar_entries_rank_nearest_first_with_cosine_similarity(
    adapter: Adapter,
) -> None:
    """ADR 0052: nearest active entries first, each with its cosine similarity
    (an identical vector scores 1.0), cut to ``limit``."""
    same = await write(adapter, "Auth uses JWT", hot=0)
    far = await write(adapter, "Cookies", hot=3)
    pairs = await adapter.store.similar_entries(adapter.vec(0), 10)
    ids = [eid for eid, _ in pairs]
    assert ids == [same.id, far.id]
    assert pairs[0][1] == pytest.approx(1.0, abs=1e-4)
    assert pairs[1][1] < pairs[0][1]
    assert len(await adapter.store.similar_entries(adapter.vec(0), 1)) == 1


async def test_similar_entries_score_a_zero_vector_as_zero_not_nan(adapter: Adapter) -> None:
    """Cosine to a zero vector is NaN in pgvector; both stores say 0.0, as
    ``pairwise_similarity`` does."""
    near = await write(adapter, "near", hot=0)
    zero = await adapter.store.create_entry(draft("zero norm"), [0.0] * adapter.dim, "test-model")
    pairs = await adapter.store.similar_entries(adapter.vec(0), 10)
    assert [eid for eid, _ in pairs] == [near.id, zero.id]
    assert pairs[1][1] == 0.0
    assert all(math.isfinite(sim) for _, sim in pairs)


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
    old = await write(adapter, "old", hot=3)
    new = await write(adapter, "new", hot=3, supersedes=(old.id,))
    other = await write(adapter, "other", hot=3)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5)
    assert {eid for eid, _ in pairs} == {new.id, other.id}


async def test_similar_entries_apply_the_readers_visibility(adapter: Adapter) -> None:
    """Related entries on a write are the writer's own view: another agent's
    ``self`` entries and another fleet's entries never show up as related."""
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "org note", hot=3, author="w")
    mine = await write(adapter, "my note", hot=3, scope="self", author="reader")
    theirs = await write(adapter, "their note", hot=3, scope="self", author="w")
    in_home = await write(adapter, "home fleet", hot=3, scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "other fleet", hot=3, scope="fleet", fleet_id=other, author="w")

    async def related(v: Visibility | None) -> set[str]:
        pairs = await adapter.store.similar_entries(adapter.vec(3), 10, visibility=v)
        return {eid for eid, _ in pairs}

    assert await related(None) == {org.id, mine.id, theirs.id, in_home.id, in_other.id}
    assert await related(_vis(TrustLevel.UNTRUSTED, fleet=home)) == set()
    member = {org.id, mine.id, in_home.id}
    assert await related(_vis(TrustLevel.LURKER, fleet=home)) == member
    assert await related(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == member
    assert await related(_vis(TrustLevel.PRIVILEGED, fleet=home)) == member | {in_other.id}


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
    assert await vector_ids(adapter.store, adapter.vec(5, base=0.0), EntryFilters(), 10) == [
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
    matches = await search(probe, EntryFilters(), 10, min_similarity=0.0)
    assert [m.entry_id for m in matches] == [near.id, far.id]
    assert [m.similarity for m in matches] == pytest.approx([0.8, 0.3], abs=1e-3)
    assert [m.entry_id for m in await search(probe, EntryFilters(), 10, min_similarity=0.5)] == [
        near.id
    ]
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


# -- redact (ADR 0125) ---------------------------------------------------------------


async def _secret_entry(
    adapter: Adapter, fleet_id: str | None = None, see_also: tuple[str, ...] = ()
) -> Entry:
    """A memory that carries a canary in every free-text field."""
    store = adapter.store
    return await store.create_entry(
        replace(
            draft(
                "hunter2canary summary",
                body="the password is hunter2canary",
                tags=("hunter2canary-tag",),
                sources=(Source(SourceType.URL, "https://example.test/hunter2canary"),),
                scope="fleet" if fleet_id else "org",
                fleet_id=fleet_id,
                see_also=see_also,
            ),
            payload={"token": "hunter2canary"},
        ),
        adapter.vec(3),
        "test-model",
        entities=(ExtractedEntity("hunter2canary-host", EntityKind.SYSTEM),),
        entities_model="ner-1",
    )


async def test_redact_wipes_every_text_field_and_keeps_the_rest(adapter: Adapter) -> None:
    store = adapter.store
    other = await write(adapter, "an ordinary memory", hot=3)
    secret = await _secret_entry(adapter, see_also=(other.id,))  # a link made from its text
    pointer = await store.create_entry(
        draft("points at the secret", see_also=(secret.id,)), adapter.vec(4), "m"
    )
    assert await store.entry_links(secret.id, 10) == ([other.id], [pointer.id])
    linking = await store.create_entry(
        draft("hunter2canary links out", see_also=(other.id,)), adapter.vec(3), "m"
    )
    await store.record_feedback(
        Feedback(secret.id, "bob", "bob-agent", Verdict.WRONG, "it said hunter2canary", T0)
    )
    await store.record_feedback(Feedback(secret.id, "cy", "cy-agent", Verdict.HELPFUL, "ok", T0))
    await store.record_feedback(Feedback(secret.id, "di", "di-agent", Verdict.STALE, None, T0))
    await store.withdraw_entry(secret.id, "contains hunter2canary", by_user="alice")
    await store.withdraw_entry(linking.id, None, by_user="alice")

    out = await store.redact_entry(secret.id)

    assert (out.feedback_notes, out.flag_notes) == (2, 1)  # a missing note is not counted
    for got in (out.entry, await store.get_entry(secret.id)):
        assert got is not None
        assert got.state is EntryState.WITHDRAWN and got.redacted_at is not None
        assert got.summary == REDACTED_SUMMARY and got.withdrawn_reason == REDACTED_SUMMARY
        assert (got.body, got.payload, got.sources, got.tags) == (None, None, (), ())
        assert (got.entities, got.entities_model, got.embedding_model) == ((), None, None)
        assert "hunter2canary" not in repr(got)
        # What stays.
        for field in ("id", "kind", "author", "agent", "scope", "fleet_id", "occurred_at"):
            assert getattr(got, field) == getattr(secret, field)
        assert got.created_at == secret.created_at and got.importance == secret.importance
    # Not findable by keyword or by vector, even when inactive entries are asked for.
    everything = EntryFilters(include_inactive=True)
    assert await store.search_keyword("hunter2canary", everything, 10) == [linking.id]
    assert await store.search_keyword("redacted", everything, 10) == []
    assert await store.search_keyword("tag host", everything, 10) == []
    assert secret.id not in await vector_ids(store, adapter.vec(3, base=0.0), everything, 10)
    assert (
        await store.list_entries(
            EntryFilters(entities=("hunter2canary-host",), include_inactive=True)
        )
        == []
    )
    assert (
        await store.list_entries(EntryFilters(tags=("hunter2canary-tag",), include_inactive=True))
        == []
    )
    # Its own outgoing links went; links to it stay (those memories still learn it changed).
    assert await store.entry_links(secret.id, 10) == ([], [pointer.id])
    # The feedback is untouched, notes included: it is only counted.
    notes = {fb.user: fb.note for fb in await store.list_feedback(secret.id, 10)}
    assert notes == {"bob": "it said hunter2canary", "cy": "ok", "di": None}
    # A memory that was not redacted is untouched.
    untouched = await store.get_entry(other.id)
    assert untouched is not None and untouched.summary == "an ordinary memory"
    assert untouched.redacted_at is None


async def test_redact_refuses_what_it_should(adapter: Adapter) -> None:
    store = adapter.store
    active = await write(adapter, "still active")
    with pytest.raises(RedactRefused, match="withdraw it first"):
        await store.redact_entry(active.id)
    got = await store.get_entry(active.id)
    assert got is not None and got.summary == "still active" and got.redacted_at is None

    gone = await write(adapter, "withdrawn then redacted")
    await store.withdraw_entry(gone.id, None, by_user="alice")
    await store.redact_entry(gone.id)
    with pytest.raises(RedactRefused, match="already redacted"):
        await store.redact_entry(gone.id)

    with pytest.raises(KeyError):
        await store.redact_entry(NO_SUCH_ID)
    with pytest.raises(KeyError):
        await store.redact_entry("not-a-uuid")


async def test_a_superseded_memory_can_be_redacted_and_keeps_its_chain(adapter: Adapter) -> None:
    store = adapter.store
    old = await _secret_entry(adapter)
    newer = await store.create_entry(
        draft("the corrected version", supersedes=(old.id,)), adapter.vec(0), "m"
    )
    out = await store.redact_entry(old.id)

    got = await store.get_entry(old.id)
    assert got is not None and out.entry.redacted_at == got.redacted_at
    assert got.state is EntryState.SUPERSEDED and got.superseded_by == newer.id
    assert got.summary == REDACTED_SUMMARY and got.withdrawn_reason is None
    assert (got.body, got.payload, got.sources, got.tags, got.entities) == (None, None, (), (), ())
    assert "hunter2canary" not in repr(got)
    (pred,) = await store.list_predecessors([newer.id])
    assert pred.id == old.id and pred.summary == REDACTED_SUMMARY
    everything = EntryFilters(include_inactive=True)
    assert await store.search_keyword("hunter2canary", everything, 10) == []
    assert old.id not in await vector_ids(store, adapter.vec(3, base=0.0), everything, 10)
    with pytest.raises(RedactRefused, match="already redacted"):
        await store.redact_entry(old.id)


async def test_redact_counts_the_withdrawal_reasons_in_the_audit_log(adapter: Adapter) -> None:
    store = adapter.store
    entry = await write(adapter, "withdrawn twice over")
    other = await write(adapter, "another")
    await store.withdraw_entry(entry.id, "leaked", by_user="admin")

    def withdrawal(target: str, reason: str | None) -> AuditEvent:
        return AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_WITHDRAW,
            target,
            {"author": "alice", "reason": reason},
        )

    for target, reason in (
        (entry.id, "leaked"),
        (entry.id, "  "),
        (entry.id, None),
        (other.id, "x"),
    ):
        await store.record_audit(withdrawal(target, reason))
    assert (await store.redact_entry(entry.id)).withdraw_reasons == 1
    # The audit rows are not edited.
    rows = await store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert sorted(str(r.detail["reason"]) for r in rows if r.target == entry.id) == [
        "  ",
        "None",
        "leaked",
    ]


async def test_redact_counts_the_withdrawal_reasons_a_restore_copied(adapter: Adapter) -> None:
    # A restore copies the withdrawal reason into its audit row as
    # `previous_reason` (ADR 0124), so a redaction counts those rows too.
    store = adapter.store
    entry = await write(adapter, "withdrawn, restored, withdrawn again")
    await store.withdraw_entry(entry.id, "again", by_user="admin")

    def restore_row(previous_reason: str | None) -> AuditEvent:
        return AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_RESTORE,
            entry.id,
            {"author": "alice", "previous_reason": previous_reason, "reason": "fine after all"},
        )

    await store.record_audit(restore_row("holds sk-canary"))
    await store.record_audit(restore_row(None))
    await store.record_audit(restore_row(" "))
    await store.record_audit(
        AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_WITHDRAW,
            entry.id,
            {"author": "alice", "reason": "again"},
        )
    )
    assert (await store.redact_entry(entry.id)).withdraw_reasons == 2


async def test_cleanup_does_not_offer_a_redacted_superseded_memory_for_restore(
    adapter: Adapter,
) -> None:
    store = adapter.store
    good = await write(adapter, "good fact", author="good")
    junk = await _junk(adapter, "junk over good", supersedes=(good.id,))
    await store.redact_entry(good.id)
    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert scan.restorable == () and scan.restorable_total == 0
    out = await _clean(adapter)
    assert out.restored == 0
    assert (await _state(adapter, junk.id)).state is EntryState.WITHDRAWN
    stub = await _state(adapter, good.id)
    assert stub.state is EntryState.SUPERSEDED and stub.redacted_at is not None


async def test_restore_refuses_a_redacted_memory_and_keeps_it_withdrawn(adapter: Adapter) -> None:
    store = adapter.store
    secret = await _secret_entry(adapter)
    await store.withdraw_entry(secret.id, "leak", by_user="alice")
    await store.redact_entry(secret.id)
    with pytest.raises(EntryRedacted, match="redacted"):
        await store.restore_entry(secret.id)
    got = await store.get_entry(secret.id)
    assert got is not None and got.state is EntryState.WITHDRAWN
    assert got.summary == REDACTED_SUMMARY and got.redacted_at is not None
    # Also a superseded one: the redaction is reported, not "not withdrawn".
    old = await write(adapter, "old version")
    await store.create_entry(draft("new version", supersedes=(old.id,)), adapter.vec(0), "m")
    await store.redact_entry(old.id)
    with pytest.raises(EntryRedacted):
        await store.restore_entry(old.id)
    # A memory that was not redacted still restores.
    plain = await write(adapter, "plain")
    await store.withdraw_entry(plain.id, None, by_user="alice")
    assert (await store.restore_entry(plain.id)).entry.state is EntryState.ACTIVE


async def test_a_redacted_memory_cannot_be_withdrawn_or_superseded_back(adapter: Adapter) -> None:
    store = adapter.store
    secret = await write(adapter, "to wipe")
    await store.withdraw_entry(secret.id, "leak", by_user="alice")
    await store.redact_entry(secret.id)
    with pytest.raises(ValueError):
        await store.withdraw_entry(secret.id, None, by_user="alice")
    with pytest.raises(SupersedeConflict):
        await store.create_entry(draft("replacement", supersedes=(secret.id,)), adapter.vec(0), "m")
    got = await store.get_entry(secret.id)
    assert got is not None and got.state is EntryState.WITHDRAWN


async def test_a_redacted_memory_keeps_its_place_in_the_supersession_chain(
    adapter: Adapter,
) -> None:
    store = adapter.store
    old = await write(adapter, "first version")
    head = await store.create_entry(
        draft("second version", supersedes=(old.id,)), adapter.vec(0), "m"
    )
    await store.withdraw_entry(head.id, "leak", by_user="alice")
    await store.redact_entry(head.id)
    (pred,) = await store.list_predecessors([head.id])
    assert pred.id == old.id and pred.superseded_by == head.id
    got = await store.get_entry(head.id)
    assert got is not None and got.summary == REDACTED_SUMMARY


async def test_agent_cleanup_undo_never_brings_a_redacted_memory_back(adapter: Adapter) -> None:
    store = adapter.store
    leaked = await _junk(adapter, "junk with a secret", body="hunter2canary")
    other = await _junk(adapter, "ordinary junk")
    out = await _clean(adapter)
    assert out.withdrawn == 2
    await store.redact_entry(leaked.id)

    undone = await store.undo_agent_cleanup(out.record.id)

    assert undone.reactivated == 1 and undone.reactivate_skipped == 1
    assert (await _state(adapter, other.id)).state is EntryState.ACTIVE
    stub = await _state(adapter, leaked.id)
    assert stub.state is EntryState.WITHDRAWN and stub.redacted_at is not None
    assert stub.summary == REDACTED_SUMMARY and stub.body is None


async def test_a_fleet_merge_and_its_undo_leave_a_redacted_memory_withdrawn(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    secret = await _secret_entry(adapter, fleet_id=a)
    await store.withdraw_entry(secret.id, None, by_user="alice")
    await store.redact_entry(secret.id)

    merged = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    moved = await store.get_entry(secret.id)
    assert moved is not None and moved.fleet_id == b and moved.redacted_at is not None
    await store.unmerge_fleet(merged.record.id)

    back = await store.get_entry(secret.id)
    assert back is not None and back.fleet_id == a
    assert back.state is EntryState.WITHDRAWN and back.summary == REDACTED_SUMMARY
    assert back.body is None and back.redacted_at == moved.redacted_at


async def test_supersede_and_withdraw_leave_the_content_fields_unchanged(
    adapter: Adapter,
) -> None:
    """Entries are immutable: only the state fields move."""

    async def put(summary: str) -> Entry:
        made = EntryDraft(
            kind=Kind.DECISION,
            summary=summary,
            author="alice",
            agent="claude-code",
            body="the long form",
            payload={"k": [1, 2], "nested": {"a": "b"}},
            sources=(Source(SourceType.URL, "https://example.test/doc"),),
            tags=("auth", "jwt"),
            occurred_at=T0 - timedelta(days=3),
            importance=5,
            importance_source=ImportanceSource.CALLER,
            scope="self",
        )
        entities = (ExtractedEntity("JWT", EntityKind.CONCEPT),)
        return await adapter.store.create_entry(made, adapter.vec(1), "test-model", entities)

    async def snapshot(entry: Entry) -> Entry:
        got = await adapter.store.get_entry(entry.id)
        assert got is not None
        return got

    old, doomed = await put("to supersede"), await put("to withdraw")
    old_before, doomed_before = await snapshot(old), await snapshot(doomed)
    assert old_before.entities and old_before.payload and old_before.sources

    new = await write(adapter, "replacement", supersedes=(old.id,))
    await adapter.store.withdraw_entry(doomed.id, "obsolete", by_user="alice")

    old_after, doomed_after = await snapshot(old), await snapshot(doomed)
    assert (old_after.state, old_after.superseded_by) == (EntryState.SUPERSEDED, new.id)
    assert doomed_after.state is EntryState.WITHDRAWN
    moved = {
        "state": EntryState.ACTIVE,
        "superseded_by": None,
        "withdrawn_reason": None,
        "withdrawn_at": None,
    }
    assert replace(old_after, **moved) == old_before
    assert replace(doomed_after, **moved) == doomed_before


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
    many = await adapter.store.entry_links_many([a.id, c.id, NO_SUCH_ID, "not-a-uuid"], 10)
    assert many[a.id] == ([], [d.id, c.id])
    assert sorted(many[c.id][0]) == sorted([a.id, b.id]) and many[c.id][1] == []
    assert many[NO_SUCH_ID] == ([], [])
    assert many["not-a-uuid"] == ([], [])
    assert list(many) == [a.id, c.id, NO_SUCH_ID, "not-a-uuid"]


async def test_incoming_links_cap_counts_only_active_readable_sources(adapter: Adapter) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    red = (await store.create_fleet("red")).id
    target = await write(adapter, "target", author="t", scope="org")
    mine = await write(
        adapter, "mine", author="me", scope="fleet", fleet_id=blue, see_also=(target.id,)
    )
    gone = await write(adapter, "gone", author="me", scope="org", see_also=(target.id,))
    await store.withdraw_entry(gone.id, None, by_user="me")
    # Newer links from another fleet and from someone's self entries.
    for i in range(3):
        await write(
            adapter, f"red {i}", author="r", scope="fleet", fleet_id=red, see_also=(target.id,)
        )
        await write(adapter, f"self {i}", author="r", scope="self", see_also=(target.id,))
    reader = _vis(TrustLevel.CONTRIBUTOR, "me", blue)
    assert await store.entry_links(target.id, 2, visibility=reader) == ([], [mine.id])
    many = await store.entry_links_many([target.id], 2, visibility=reader)
    assert many[target.id] == ([], [mine.id])
    # Without a visibility only the state filter applies.
    _, unrestricted = await store.entry_links(target.id, 100)
    assert gone.id not in unrestricted and len(unrestricted) == 7


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
    assert await adapter.store.pin_entry(fleet, "not-a-uuid", "boss", limit=9) is PinRefusal.FLEET
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


# -- fleets and agents (ADRs 0011, 0012, 0028, 0045) -------------------------------------


async def test_fleets(adapter: Adapter) -> None:
    fleet = await adapter.store.create_fleet("blue")
    with pytest.raises(FleetExists, match="fleet already exists: 'blue'"):
        await adapter.store.create_fleet("blue")
    assert await adapter.store.get_fleet(fleet.id) == fleet
    assert await adapter.store.get_fleet(NO_SUCH_ID) is None
    assert [f.name for f in await adapter.store.list_fleets()] == ["blue"]


async def test_rename_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, _ = await _two_fleets(adapter)
    entry = await _fleet_entry(adapter, "kept", a)
    before, after = await store.rename_fleet(a, "alpha")
    assert (before.name, after.name, after.id) == ("a", "alpha", a)
    assert (after.created_at, after.merged_into) == (before.created_at, None)
    assert await store.get_fleet(a) == after
    assert [f.name for f in await store.list_fleets()] == ["alpha", "b"]
    got = await store.get_entry(entry.id)  # the id never changes, so memories stay put
    assert got is not None and got.fleet_id == a
    # The current name again is a no-op; a taken name (exact match) is refused.
    assert await store.rename_fleet(a, "alpha") == (after, after)
    with pytest.raises(FleetExists, match="fleet already exists: 'b'"):
        await store.rename_fleet(a, "b")
    assert (await store.get_fleet(a)) == after
    await store.rename_fleet(a, "B")  # names are case-sensitive, as create is
    with pytest.raises(UnknownFleet):
        await store.rename_fleet(NO_SUCH_ID, "x")
    with pytest.raises(UnknownFleet):
        await store.rename_fleet("not-a-uuid", "x")


async def test_rename_a_merged_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    _, after = await store.rename_fleet(a, "a (merged)")
    assert (after.name, after.merged_into) == ("a (merged)", b)
    assert await store.get_fleet(a) == after


async def test_set_and_clear_a_fleet_phase(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    until = datetime.now(UTC) + timedelta(days=5)
    explore = FleetPhase(PhaseName.EXPLORE, until)
    before, after = await store.set_fleet_phase(a, explore)
    assert before.phase is None
    assert after.phase == explore
    assert await store.get_fleet(a) == after
    assert (await store.get_fleet(b)).phase is None  # type: ignore[union-attr]
    was, replaced = await store.set_fleet_phase(a, FleetPhase(PhaseName.CONSOLIDATE, until))
    assert was.phase == explore
    assert replaced.phase == FleetPhase(PhaseName.CONSOLIDATE, until)
    assert [f.phase for f in await store.list_fleets()] == [replaced.phase, None]
    was, cleared = await store.set_fleet_phase(a, None)
    assert was.phase == replaced.phase
    assert cleared.phase is None
    assert await store.get_fleet(a) == cleared
    with pytest.raises(UnknownFleet):
        await store.set_fleet_phase(NO_SUCH_ID, explore)
    with pytest.raises(UnknownFleet):
        await store.set_fleet_phase("not-a-uuid", explore)


async def test_a_merge_clears_the_source_phase_and_an_undo_does_not_bring_it_back(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    until = datetime.now(UTC) + timedelta(days=5)
    await store.set_fleet_phase(a, FleetPhase(PhaseName.EXPLORE, until))
    target_phase = FleetPhase(PhaseName.CONSOLIDATE, until)
    await store.set_fleet_phase(b, target_phase)
    merged = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    assert (await store.get_fleet(a)).phase is None  # type: ignore[union-attr]
    await store.unmerge_fleet(merged.record.id)
    restored = await store.get_fleet(a)
    assert restored is not None and restored.merged_into is None and restored.phase is None
    assert (await store.get_fleet(b)).phase == target_phase  # type: ignore[union-attr]


async def test_a_merged_fleet_takes_no_phase(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    phase = FleetPhase(PhaseName.EXPLORE, datetime.now(UTC) + timedelta(days=5))
    with pytest.raises(FleetMerged) as exc:
        await store.set_fleet_phase(a, phase)
    assert exc.value.target_id == b
    merged = await store.get_fleet(a)
    assert merged is not None and merged.phase is None


async def test_set_and_clear_an_agent_role(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("alice")
    pending_before, pending_after = await store.set_agent_role("alice", AgentRole.SCOUT)
    assert (pending_before.role, pending_after.role) == (None, AgentRole.SCOUT)
    await _agent_in(adapter, "bob", fleet.id)
    assert (await store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    assert (await store.get_agent("bob")).role is None  # type: ignore[union-attr]
    assert [a.role for a in await store.list_agents()] == [AgentRole.SCOUT, None]
    same_before, same_after = await store.set_agent_role("alice", AgentRole.SCOUT)
    assert same_before == same_after
    cleared_before, cleared_after = await store.set_agent_role("alice", None)
    assert (cleared_before.role, cleared_after.role) == (AgentRole.SCOUT, None)
    none_before, none_after = await store.set_agent_role("alice", None)
    assert none_before == none_after
    with pytest.raises(UnknownAgent):
        await store.set_agent_role("nobody", AgentRole.SCOUT)


async def test_a_revoked_agent_takes_no_role_and_a_role_it_had_can_be_cleared(
    adapter: Adapter,
) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await _agent_in(adapter, "alice", fleet.id)
    await _agent_in(adapter, "bob", fleet.id)
    await store.set_agent_role("alice", AgentRole.SCOUT)
    await store.revoke_agent("alice")
    await store.revoke_agent("bob")
    assert (await store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    with pytest.raises(InvalidAgentStatus):
        await store.set_agent_role("bob", AgentRole.SCOUT)
    assert (await store.get_agent("bob")).role is None  # type: ignore[union-attr]
    _, cleared = await store.set_agent_role("alice", None)
    assert cleared.role is None


async def test_agent_lifecycle(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    agent = await store.register_agent("Scout", "owner")
    assert (agent.status, agent.trust_level, agent.home_fleet_id) == (
        AgentStatus.PENDING,
        TrustLevel.UNTRUSTED,
        None,
    )
    assert agent.owner_alias == "owner"
    assert await store.register_agent("Scout", "someone else") == agent  # never overwritten
    assert (await store.register_agent("scout")).name == "Scout"  # case variant (ADR 0045)
    assert [a.name for a in await store.list_agents()] == ["Scout"]
    await store.register_agent("Bare")
    assert (await store.register_agent("Bare", "mallory")).owner_alias is None  # no back-fill
    active = await store.activate_agent(
        "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
    )
    assert active.status is AgentStatus.ACTIVE and active.home_fleet_id == fleet.id
    assert active.activated_at is not None
    assert await store.get_agent("Scout") == active
    with pytest.raises(InvalidAgentStatus):
        await store.activate_agent(
            "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
    promoted = await store.set_agent_trust_level("Scout", TrustLevel.PRIVILEGED)
    assert promoted.trust_level is TrustLevel.PRIVILEGED
    demoted = await store.set_agent_trust_level("Scout", TrustLevel.UNTRUSTED)
    assert (demoted.trust_level, demoted.status) == (TrustLevel.UNTRUSTED, AgentStatus.ACTIVE)
    other = await store.create_fleet("red")
    assert (await store.set_agent_home_fleet("Scout", other.id)).home_fleet_id == other.id
    revoked = await store.revoke_agent("Scout")
    assert revoked.status is AgentStatus.REVOKED
    with pytest.raises(InvalidAgentStatus):
        await store.revoke_agent("Scout")
    again = await store.activate_agent(
        "Scout", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id
    )
    assert (again.status, again.trust_level) == (AgentStatus.ACTIVE, TrustLevel.PRIVILEGED)
    assert (await store.revoke_agent("Bare")).status is AgentStatus.REVOKED  # even while pending
    for call in (
        store.revoke_agent("ghost"),
        store.set_agent_trust_level("ghost", TrustLevel.LURKER),
        store.set_agent_home_fleet("ghost", fleet.id),
        store.activate_agent("ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id),
    ):
        with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
            await call
    assert await store.get_agent("ghost") is None


async def test_set_agent_owner_alias_returns_before_and_after(adapter: Adapter) -> None:
    """ADR 0122: any status; (before, after) is read under one lock; the alias
    lives on the agent row only, and nothing else about the agent moves."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout", "jane.doe")
    await store.register_agent("Bare")
    pending_before, pending_after = await store.set_agent_owner_alias("Scout", "jane.d")
    assert (pending_before.owner_alias, pending_after.owner_alias) == ("jane.doe", "jane.d")
    assert pending_after.status is AgentStatus.PENDING
    assert await store.get_agent("Scout") == pending_after
    await store.activate_agent("Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    active_before, active_after = await store.set_agent_owner_alias("Scout", "Jane.D")  # case only
    assert (active_before.owner_alias, active_after.owner_alias) == ("jane.d", "Jane.D")
    assert (active_after.status, active_after.trust_level, active_after.home_fleet_id) == (
        AgentStatus.ACTIVE,
        TrustLevel.CONTRIBUTOR,
        fleet.id,
    )
    same_before, same_after = await store.set_agent_owner_alias("Scout", "Jane.D")
    assert same_before == same_after
    await store.revoke_agent("Scout")  # a revoked agent can be corrected too
    assert (await store.set_agent_owner_alias("Scout", "jane"))[1].owner_alias == "jane"
    assert (await store.set_agent_owner_alias("Bare", "mallory"))[0].owner_alias is None
    assert [(a.name, a.owner_alias) for a in await store.list_agents()] == [
        ("Bare", "mallory"),
        ("Scout", "jane"),
    ]
    with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
        await store.set_agent_owner_alias("ghost", "x")


async def test_agents_list_by_name(adapter: Adapter) -> None:
    for name in ("zed", "amy"):
        await adapter.store.register_agent(name)
    assert [a.name for a in await adapter.store.list_agents()] == ["amy", "zed"]


async def test_agents_list_shows_no_key_use_for_an_agent_that_never_authenticated(
    adapter: Adapter,
) -> None:
    """ADR 0110: the agent list carries ``last_used_at``; the key check (the
    authenticator) is what stamps it, so a fresh or keyless agent shows none."""
    await adapter.store.register_agent("amy")
    assert [a.last_used_at for a in await adapter.store.list_agents()] == [None]
    got = await adapter.store.get_agent("amy")
    assert got is not None and got.last_used_at is None  # only the list fills it
    assert await adapter.store.agent_key_last_used("amy") is None
    assert await adapter.store.agent_key_last_used("ghost") is None


async def test_agents_list_shows_no_key_lock_for_an_agent_whose_key_was_never_locked(
    adapter: Adapter,
) -> None:
    """ADR 0117: the agent list carries ``key_locked_at`` (the key lock itself
    is the authenticator's); a fresh or keyless agent shows none, and only the
    list fills it."""
    await adapter.store.register_agent("amy")
    assert [a.key_locked_at for a in await adapter.store.list_agents()] == [None]
    got = await adapter.store.get_agent("amy")
    assert got is not None and got.key_locked_at is None


# -- audit log (ADR 0027) ---------------------------------------------------------------


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


# -- fleet merge (ADR 0107) -------------------------------------------------------------


async def _two_fleets(adapter: Adapter) -> tuple[str, str]:
    return (await adapter.store.create_fleet("a")).id, (await adapter.store.create_fleet("b")).id


async def _fleet_entry(adapter: Adapter, summary: str, fleet_id: str, hot: int = 0) -> Entry:
    return await write(adapter, summary, hot=hot, scope="fleet", fleet_id=fleet_id)


async def _pinned(
    adapter: Adapter, summary: str, fleet_id: str, hot: int = 0, by: str | None = None
) -> str:
    """Pin a new entry, each by its own agent unless ``by`` names one."""
    entry = await _fleet_entry(adapter, summary, fleet_id, hot)
    written = await adapter.store.pin_entry(fleet_id, entry.id, by or f"by-{summary}", limit=99)
    assert isinstance(written, PinWrite)
    return entry.id


async def test_fleet_facts_count_agents_and_entries_by_state(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _fleet_entry(adapter, "live", a)
    gone = await _fleet_entry(adapter, "gone", a)
    await store.withdraw_entry(gone.id, "x", by_user="alice")
    await _fleet_entry(adapter, "theirs", b)
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    facts = await store.fleet_facts(a)
    assert (facts.agents, dict(facts.entries)) == (1, {"active": 1, "withdrawn": 1})
    empty = await store.fleet_facts(NO_SUCH_ID)
    assert (empty.agents, dict(empty.entries)) == (0, {})
    assert (await store.fleet_facts("not-a-uuid")).agents == 0


async def test_duplicate_pairs_pair_each_entry_with_its_nearest_in_the_other_fleet(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = await _fleet_entry(adapter, "mine", a, hot=1)
    await _fleet_entry(adapter, "unmatched", a, hot=5)
    twin = await _fleet_entry(adapter, "twin", b, hot=1)
    await _fleet_entry(adapter, "other", b, hot=9)
    gone = await _fleet_entry(adapter, "withdrawn twin", b, hot=5)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    scan = await store.duplicate_pairs(a, b, 0.9, 100)
    assert [(p.entry_id, p.match_id) for p in scan.pairs] == [(mine.id, twin.id)]
    assert scan.pairs[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert (scan.looked_up, scan.truncated) == (2, False)
    capped = await store.duplicate_pairs(a, b, 0.9, 1)
    assert (capped.looked_up, capped.truncated) == (1, True)
    assert (await store.duplicate_pairs(a, NO_SUCH_ID, 0.9, 10)).pairs == ()
    assert (await store.duplicate_pairs("not-a-uuid", b, 0.9, 10)).pairs == ()


async def test_merge_fleets_moves_what_the_design_says(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    live = await _fleet_entry(adapter, "live", a)
    old = await _fleet_entry(adapter, "old", a)
    newer = await store.create_entry(
        draft("newer", scope="fleet", fleet_id=a, supersedes=(old.id,)), adapter.vec(0), "m"
    )
    gone = await _fleet_entry(adapter, "gone", a)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    theirs = await _fleet_entry(adapter, "theirs", b)
    org = await write(adapter, "org entry")
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=a)
    pin_a = await _pinned(adapter, "pinned a", a)
    pin_b = await _pinned(adapter, "pinned b", b)
    await store.record_search(a, empty=True)
    await store.record_search(a, empty=False, explore=True)
    await store.record_search(b, empty=False)

    out = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")

    assert (out.entries_moved, out.agents_moved, out.target_created) == (5, 1, False)
    assert out.target.id == b and out.record.source_fleet_id == a
    assert (out.record.merged_by, out.record.moved_agents) == ("admin:1", ("amy",))
    for e in (live, old, newer, gone, theirs):
        got = await store.get_entry(e.id)
        assert got is not None and got.fleet_id == b
    assert (await store.get_entry(org.id)).fleet_id is None  # type: ignore[union-attr]
    amy = await store.get_agent("amy")
    assert amy is not None and amy.home_fleet_id == b and amy.trust_level is TrustLevel.PRIVILEGED
    assert {p.entry_id for p in await store.list_pins(b)} == {pin_a, pin_b}
    assert await store.list_pins(a) == []
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into == b and source.merged_at is not None
    target = await store.get_fleet(b)
    assert target is not None and target.merged_into is None
    assert {f.id: f.merged_into for f in await store.list_fleets()} == {a: b, b: None}
    counts = {c.fleet_id: (c.searches, c.empty, c.explore) for c in await store.search_counts()}
    assert counts == {b: (3, 1, 1)}
    assert await store.get_fleet_merge(out.record.id) == out.record
    assert [m.id for m in await store.list_fleet_merges(10)] == [out.record.id]


async def test_merge_into_a_new_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a = (await store.create_fleet("a")).id
    e = await _fleet_entry(adapter, "e", a)
    out = await store.merge_fleets(a, new_fleet_name="c", merged_by="admin:1")
    assert out.target_created and out.target.name == "c" and out.entries_moved == 1
    got = await store.get_entry(e.id)
    assert got is not None and got.fleet_id == out.target.id
    assert {f.name for f in await store.list_fleets()} == {"a", "c"}
    with pytest.raises(FleetExists):
        await store.merge_fleets(out.target.id, new_fleet_name="a", merged_by="admin:1")
    assert (await store.get_fleet(out.target.id)).merged_into is None  # type: ignore[union-attr]


async def test_merge_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    e = await _fleet_entry(adapter, "e", a)
    with pytest.raises(FleetMergeRefused):
        await store.merge_fleets(a, target_fleet_id=a, merged_by="x")
    for source, target in ((NO_SUCH_ID, a), (a, NO_SUCH_ID), ("not-a-uuid", a), (a, "not-a-uuid")):
        with pytest.raises(UnknownFleet):
            await store.merge_fleets(source, target_fleet_id=target, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, new_fleet_name="n", merged_by="x")
    pins = [await _pinned(adapter, f"p{i}", a) for i in range(11)]
    with pytest.raises(InvalidInput):  # over the cap with no choice
        await store.merge_fleets(a, target_fleet_id=b, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=pins, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=[NO_SUCH_ID], merged_by="x")
    got = await store.get_entry(e.id)
    assert got is not None and got.fleet_id == a
    assert len(await store.list_pins(a)) == 11
    assert await store.list_fleet_merges(10) == []
    await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=pins[1:], merged_by="x")
    with pytest.raises(FleetMerged) as gone:
        await store.merge_fleets(a, target_fleet_id=c, merged_by="x")
    assert (gone.value.target_id, gone.value.target_name) == (b, "b")
    with pytest.raises(FleetMerged):
        await store.merge_fleets(c, target_fleet_id=a, merged_by="x")


async def test_a_merged_fleet_refuses_entries_pins_and_members(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    e = await _fleet_entry(adapter, "e", a)
    await store.register_agent("amy")
    await store.register_agent("bob")
    await store.activate_agent("bob", trust_level=TrustLevel.LURKER, home_fleet_id=b)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="x")
    with pytest.raises(FleetMerged, match="'b'"):
        await store.create_entry(
            draft("late", scope="fleet", fleet_id=a), adapter.vec(0), "test-model"
        )
    with pytest.raises(FleetMerged):
        await store.pin_entry(a, e.id, "lead", limit=10)
    with pytest.raises(FleetMerged):
        await store.activate_agent("amy", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    with pytest.raises(FleetMerged):
        await store.set_agent_home_fleet("bob", a)
    assert (await store.get_agent("amy")).status is AgentStatus.PENDING  # type: ignore[union-attr]
    assert (await store.get_agent("bob")).home_fleet_id == b  # type: ignore[union-attr]
    # Org entries, and the target, are unaffected.
    await write(adapter, "org")
    await write(adapter, "ok", scope="fleet", fleet_id=b)


async def test_unmerge_restores_what_moved_and_leaves_what_came_after(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = await _fleet_entry(adapter, "mine", a)
    theirs = await _fleet_entry(adapter, "theirs", b)
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)
    await store.register_agent("ann")
    await store.activate_agent("ann", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    pin_a = await _pinned(adapter, "pa", a)
    pins_b = [await _pinned(adapter, f"pb{i}", b) for i in range(10)]
    out = await store.merge_fleets(
        a, target_fleet_id=b, keep_pin_ids=[pin_a, *pins_b[1:]], merged_by="x"
    )
    assert [p.entry_id for p in out.pins_dropped] == [pins_b[0]]
    late = await _fleet_entry(adapter, "late", b)
    other = (await store.create_fleet("c")).id
    await store.set_agent_home_fleet("ann", other)

    undone = await store.unmerge_fleet(out.record.id)

    assert undone.entries_restored == 2  # mine and pa's entry
    for e, home in ((mine.id, a), (pin_a, a), (theirs.id, b), (late.id, b)):
        got = await store.get_entry(e)
        assert got is not None and got.fleet_id == home
    assert undone.agents_restored == ("amy",)
    assert (await store.get_agent("amy")).home_fleet_id == a  # type: ignore[union-attr]
    assert (await store.get_agent("ann")).home_fleet_id == other  # type: ignore[union-attr]
    assert undone.pins_returned == 1
    assert [p.entry_id for p in await store.list_pins(a)] == [pin_a]
    assert {p.entry_id for p in await store.list_pins(b)} == set(pins_b)
    assert [p.entry_id for p in undone.pins_restored] == [pins_b[0]]
    assert undone.pins_skipped == () and undone.record.undone_at is not None
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into is None and source.merged_at is None
    stored = await store.get_fleet_merge(out.record.id)
    assert stored is not None and stored.undone_at is not None
    # The source takes members and entries again.
    await store.set_agent_home_fleet("ann", a)
    await _fleet_entry(adapter, "again", a)


async def test_unmerge_refusals(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    first = await store.merge_fleets(a, target_fleet_id=c, merged_by="x")
    second = await store.merge_fleets(b, target_fleet_id=c, merged_by="x")
    assert [m.id for m in await store.list_fleet_merges(10)] == [second.record.id, first.record.id]
    with pytest.raises(FleetMergeRefused, match="newer"):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(second.record.id)
    with pytest.raises(FleetMergeRefused, match="already undone"):
        await store.unmerge_fleet(second.record.id)
    chained = await store.merge_fleets(c, target_fleet_id=b, merged_by="x")  # c -> b
    with pytest.raises(FleetMergeRefused):
        await store.unmerge_fleet(first.record.id)  # its target c is merged now
    await store.unmerge_fleet(chained.record.id)
    await store.unmerge_fleet(first.record.id)
    for bad in (NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(UnknownFleetMerge):
            await store.unmerge_fleet(bad)
    assert await store.get_fleet_merge(NO_SUCH_ID) is None
    assert await store.get_fleet_merge("not-a-uuid") is None


async def test_unmerge_skips_the_oldest_dropped_pins_that_do_not_fit(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = [await _pinned(adapter, f"a{i}", a) for i in range(2)]
    theirs = [await _pinned(adapter, f"b{i}", b) for i in range(10)]
    out = await store.merge_fleets(
        a, target_fleet_id=b, keep_pin_ids=[*mine, *theirs[2:]], merged_by="x"
    )
    assert {p.entry_id for p in out.pins_dropped} == set(theirs[:2])
    for entry_id in mine:
        assert await store.unpin_entry(b, entry_id)
    fresh = await _pinned(adapter, "fresh", b)
    undone = await store.unmerge_fleet(out.record.id)
    assert [p.entry_id for p in undone.pins_restored] == [theirs[1]]
    assert [p.entry_id for p in undone.pins_skipped] == [theirs[0]]
    assert {p.entry_id for p in await store.list_pins(b)} == {*theirs[1:], fresh}


async def test_merge_is_refused_when_the_pins_changed_since_the_preview(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    seen = [await _pinned(adapter, "seen", a)]
    late = await _pinned(adapter, "late", b)  # pinned after the preview was taken
    with pytest.raises(FleetMergeRefused, match="preview again"):
        await store.merge_fleets(a, target_fleet_id=b, expected_pin_ids=seen, merged_by="x")
    with pytest.raises(FleetMergeRefused, match="preview again"):
        await store.merge_fleets(
            a, target_fleet_id=b, expected_pin_ids=[*seen, late, NO_SUCH_ID], merged_by="x"
        )
    assert await store.list_fleet_merges(10) == []
    assert (await store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]
    out = await store.merge_fleets(
        a, target_fleet_id=b, expected_pin_ids=[late, *seen], merged_by="x"
    )
    assert (len(out.pins_kept), out.pins_dropped) == (2, ())


async def test_merge_reads_ids_the_same_on_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    with pytest.raises(UnknownFleet):  # an unknown source wins over a taken new name
        await store.merge_fleets(NO_SUCH_ID, new_fleet_name="a", merged_by="x")
    with pytest.raises(FleetExists):
        await store.merge_fleets(a, new_fleet_name="b", merged_by="x")
    out = await store.merge_fleets(a.upper(), target_fleet_id=b.upper(), merged_by="x")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    with pytest.raises(UnknownFleetMerge):
        await store.unmerge_fleet("not-a-uuid")
    undone = await store.unmerge_fleet(out.record.id.upper())
    assert undone.record.id == out.record.id


async def test_unmerge_keeps_to_three_restored_pins_per_agent(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    pins = [await _pinned(adapter, f"p{i}", a, by="lead") for i in range(4)]
    out = await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=[pins[3]], merged_by="x")
    assert {p.entry_id for p in out.pins_dropped} == set(pins[:3])
    undone = await store.unmerge_fleet(out.record.id)
    # pins[3] came back with its entry; "lead" may hold two more, so the oldest is skipped.
    assert [p.entry_id for p in undone.pins_restored] == [pins[2], pins[1]]
    assert [p.entry_id for p in undone.pins_skipped] == [pins[0]]
    assert {p.entry_id for p in await store.list_pins(a)} == set(pins[1:])


# -- fleet move (ADR 0112) --------------------------------------------------------------


async def _agent_in(adapter: Adapter, name: str, fleet_id: str) -> None:
    await adapter.store.register_agent(name)
    await adapter.store.activate_agent(
        name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet_id
    )


async def _authored(
    adapter: Adapter, summary: str, fleet_id: str, author: str, supersedes: tuple[str, ...] = ()
) -> Entry:
    return await write(
        adapter, summary, scope="fleet", fleet_id=fleet_id, author=author, supersedes=supersedes
    )


async def test_move_agents_moves_the_agents_and_their_fleet_memories(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    for name in ("amy", "ann", "bob"):
        await _agent_in(adapter, name, a)
    live = await _authored(adapter, "live", a, "amy")
    gone = await _authored(adapter, "gone", a, "ann")
    await store.withdraw_entry(gone.id, "x", by_user="alice")
    bobs = await _authored(adapter, "bobs", a, "bob")
    own = await write(adapter, "self note", author="amy", scope="self")
    org = await write(adapter, "org note", author="amy", scope="org")
    theirs = await _authored(adapter, "theirs", b, "bea")
    await store.record_search(a, empty=True)

    plan = await store.plan_fleet_move(a, ["amy", "ann", "amy"])
    assert (set(plan.entry_ids), plan.held_back) == ({live.id, gone.id}, 0)
    assert dict(plan.states or {}) == {"active": 1, "withdrawn": 1}

    out = await store.move_agents(a, ["amy", "ann"], target_fleet_id=b, moved_by="admin:1")

    assert (out.entries_moved, out.entries_held_back, out.agents_moved) == (2, 0, 2)
    assert out.record.kind == "move" and out.record.moved_agents == ("amy", "ann")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    assert out.record.merged_by == "admin:1" and not out.target_created
    found = await store.get_entries([live.id, gone.id, bobs.id, own.id, org.id, theirs.id])
    fleets = {e.id: e.fleet_id for e in found.values()}
    assert fleets == {
        live.id: b,
        gone.id: b,
        bobs.id: a,
        own.id: None,
        org.id: None,
        theirs.id: b,
    }
    homes = {n: (await store.get_agent(n)).home_fleet_id for n in ("amy", "ann", "bob")}  # type: ignore[union-attr]
    assert homes == {"amy": b, "ann": b, "bob": a}
    assert {f.id: f.merged_into for f in await store.list_fleets()} == {a: None, b: None}
    assert {c.fleet_id: c.searches for c in await store.search_counts()} == {a: 1}
    assert await store.get_fleet_merge(out.record.id) == out.record
    assert [m.kind for m in await store.list_fleet_merges(10)] == ["move"]


async def test_move_agents_into_a_new_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, _ = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = await _authored(adapter, "mine", a, "amy")
    out = await store.move_agents(a, ["amy"], new_fleet_name="c", moved_by="admin:1")
    assert out.target_created and out.target.name == "c" and out.entries_moved == 1
    assert (await store.get_entry(mine.id)).fleet_id == out.target.id  # type: ignore[union-attr]
    with pytest.raises(FleetExists):
        await store.move_agents(a, ["amy"], new_fleet_name="a", moved_by="x")
    await store.move_agents(out.target.id, ["amy"], target_fleet_id=a, moved_by="x")


async def test_move_follows_the_chain_rule(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    for name in ("amy", "bob"):
        await _agent_in(adapter, name, a)
    v1 = await _authored(adapter, "v1", a, "amy")
    v2 = await _authored(adapter, "v2", a, "bob", (v1.id,))  # a stayer's head: v1 stays
    c1 = await _authored(adapter, "c1", a, "amy")
    c2 = await _authored(adapter, "c2", a, "bob", (c1.id,))
    c3 = await _authored(adapter, "c3", a, "amy", (c2.id,))  # amy's head: c1 moves, c2 stays
    d1 = await _authored(adapter, "d1", a, "amy")
    d2 = await _authored(adapter, "d2", a, "amy", (d1.id,))
    plan = await store.plan_fleet_move(a, ["amy"])
    assert (set(plan.entry_ids), plan.held_back) == ({c1.id, c3.id, d1.id, d2.id}, 1)
    out = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    assert (out.entries_moved, out.entries_held_back) == (4, 1)
    now = {
        e.id: e.fleet_id
        for e in (await store.get_entries([x.id for x in (v1, v2, c1, c2, c3, d1, d2)])).values()
    }
    assert now == {v1.id: a, v2.id: a, c1.id: b, c2.id: a, c3.id: b, d1.id: b, d2.id: b}


async def test_move_pins_follow_and_the_picker_applies(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = []
    for i in range(6):
        e = await _authored(adapter, f"m{i}", a, "amy")
        assert isinstance(await store.pin_entry(a, e.id, f"p{i}", limit=99), PinWrite)
        mine.append(e.id)
    stays = await _pinned(adapter, "stays", a)  # an author who is not moving
    theirs = [await _pinned(adapter, f"t{i}", b) for i in range(6)]
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")  # 12 > 10
    shown = [*mine, *theirs]
    with pytest.raises(FleetMoveRefused, match="preview again"):
        await store.move_agents(
            a, ["amy"], target_fleet_id=b, expected_pin_ids=shown[1:], moved_by="x"
        )
    assert await store.list_fleet_merges(10) == []
    keep = [*mine[1:], *theirs[1:]]
    out = await store.move_agents(
        a, ["amy"], target_fleet_id=b, keep_pin_ids=keep, expected_pin_ids=shown, moved_by="x"
    )
    assert {p.entry_id for p in out.pins_dropped} == {mine[0], theirs[0]}
    assert {p.entry_id for p in await store.list_pins(b)} == set(keep)
    assert {p.entry_id for p in await store.list_pins(a)} == {stays}

    undone = await store.unmerge_fleet(out.record.id)
    assert undone.record.kind == "move" and undone.agents_restored == ("amy",)
    assert undone.pins_returned == 5
    assert {p.entry_id for p in await store.list_pins(a)} == {stays, *mine[1:], mine[0]}
    assert {p.entry_id for p in await store.list_pins(b)} == set(theirs)


async def test_move_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    await _agent_in(adapter, "amy", a)
    await _agent_in(adapter, "cy", c)
    mine = await _authored(adapter, "mine", a, "amy")
    with pytest.raises(FleetMoveRefused, match="cy, ghost"):
        await store.move_agents(a, ["amy", "ghost", "cy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(FleetMoveRefused):
        await store.move_agents(a, ["amy"], target_fleet_id=a, moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, [], target_fleet_id=b, moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], target_fleet_id=b, new_fleet_name="n", moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents(NO_SUCH_ID, ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents(a, ["amy"], target_fleet_id=NO_SUCH_ID, moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents("not-a-uuid", ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(UnknownFleet):  # an unknown source wins over a taken new name
        await store.move_agents(NO_SUCH_ID, ["amy"], new_fleet_name="a", moved_by="x")
    with pytest.raises(FleetExists):
        await store.move_agents(a, ["amy"], new_fleet_name="b", moved_by="x")
    await store.merge_fleets(c, target_fleet_id=b, merged_by="x")
    with pytest.raises(FleetMerged):
        await store.move_agents(a, ["amy"], target_fleet_id=c, moved_by="x")
    with pytest.raises(FleetMerged):
        await store.move_agents(c, ["cy"], target_fleet_id=a, moved_by="x")
    assert (await store.get_agent("amy")).home_fleet_id == a  # type: ignore[union-attr]
    assert (await store.get_entry(mine.id)).fleet_id == a  # type: ignore[union-attr]
    assert [m.kind for m in await store.list_fleet_merges(10)] == ["merge"]
    assert {f.name for f in await store.list_fleets()} == {"a", "b", "c"}  # no stray new fleet


async def test_move_ids_are_read_the_same_on_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    out = await store.move_agents(a.upper(), ["amy"], target_fleet_id=b.upper(), moved_by="x")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    assert (await store.plan_fleet_move("not-a-uuid", ["amy"])).entry_ids == ()


async def test_a_write_by_a_moved_author_for_the_old_fleet_is_refused_as_busy(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(FleetBusy, match="home fleet of amy changed"):
        await _authored(adapter, "late", a, "amy")
    assert (await _authored(adapter, "now", b, "amy")).fleet_id == b


async def test_undo_of_a_move_follows_the_newest_standing_rule(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    for name in ("amy", "ann"):
        await _agent_in(adapter, name, a)
    mine = await _authored(adapter, "mine", a, "amy")
    first = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    second = await store.move_agents(a, ["ann"], target_fleet_id=c, moved_by="x")  # same source
    with pytest.raises(FleetMoveRefused, match="newer"):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(second.record.id)
    merged = await store.merge_fleets(b, target_fleet_id=c, merged_by="x")  # the target merged away
    with pytest.raises(FleetMoveRefused):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(merged.record.id)
    undone = await store.unmerge_fleet(first.record.id)
    assert (undone.entries_restored, undone.agents_restored) == (1, ("amy",))
    assert (await store.get_entry(mine.id)).fleet_id == a  # type: ignore[union-attr]
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into is None
    with pytest.raises(FleetMoveRefused, match="already undone"):
        await store.unmerge_fleet(first.record.id)
    # A merge refusal stays a plain merge refusal.
    older = await store.merge_fleets(c, new_fleet_name="d", merged_by="x")
    await store.move_agents(a, ["amy"], target_fleet_id=older.target.id, moved_by="x")
    with pytest.raises(FleetMergeRefused, match="newer") as caught:
        await store.unmerge_fleet(older.record.id)
    assert not isinstance(caught.value, FleetMoveRefused)


async def test_undo_of_a_move_applies_the_source_pin_cap_to_returning_pins(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = []
    for i in range(4):
        e = await _authored(adapter, f"m{i}", a, "amy")
        assert isinstance(await store.pin_entry(a, e.id, f"p{i}", limit=99), PinWrite)
        mine.append(e.id)
    out = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    for i in range(8):
        await _pinned(adapter, f"s{i}", a)
    undone = await store.unmerge_fleet(out.record.id)
    assert undone.pins_returned == 2
    assert {p.entry_id for p in undone.pins_unpinned} == set(mine[:2])
    assert {p.entry_id for p in undone.pins_skipped} == set(mine[:2])
    assert len(await store.list_pins(a)) == 10
    assert await store.list_pins(b) == []
    assert (await store.get_entry(mine[0])).fleet_id == a  # type: ignore[union-attr]


async def test_the_home_fleet_guard_ignores_unregistered_authors_and_private_notes(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    assert (await _authored(adapter, "unregistered", a, "ghost")).fleet_id == a
    note = await write(adapter, "private", author="amy", scope="self")
    assert note.fleet_id is None
    stale_pin = await _authored(adapter, "to pin", a, "ghost")
    with pytest.raises(FleetBusy):  # a pin by the moved agent for the old fleet is refused
        await store.pin_entry(a, stale_pin.id, "amy", limit=10)
    assert isinstance(await store.pin_entry(a, stale_pin.id, "ghost", limit=10), PinWrite)


# -- agent cleanup (ADR 0111) -----------------------------------------------------------------

WIDE_SINCE = datetime(2020, 1, 1, tzinfo=UTC)
WIDE_UNTIL = datetime(2100, 1, 1, tzinfo=UTC)
CLEAN = {"cleaned_by": "admin:1"}


async def _junk(adapter: Adapter, summary: str, **kwargs: object) -> Entry:
    return await write(adapter, summary, author="bad", **kwargs)


async def _rate(
    adapter: Adapter,
    entry_id: str,
    user: str = "bad",
    verdict: Verdict = Verdict.WRONG,
    note: str | None = "n",
    at: datetime | None = T0,
) -> None:
    await adapter.store.record_feedback(Feedback(entry_id, user, "a", verdict, note, updated_at=at))


async def _clean(adapter: Adapter, **kwargs: object):  # type: ignore[no-untyped-def]
    return await adapter.store.cleanup_agent("bad", WIDE_SINCE, WIDE_UNTIL, **CLEAN, **kwargs)  # type: ignore[arg-type]


async def _state(adapter: Adapter, entry_id: str) -> Entry:
    entry = await adapter.store.get_entry(entry_id)
    assert entry is not None
    return entry


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


# -- successions (ADR 0113) -----------------------------------------------------------


async def _active(a: Adapter, name: str, fleet: str, level: TrustLevel = TrustLevel.CONTRIBUTOR):
    await a.store.register_agent(name)
    await a.store.activate_agent(name, trust_level=level, home_fleet_id=fleet)


async def _people(a: Adapter) -> tuple[str, str]:
    red = (await a.store.create_fleet("red")).id
    blue = (await a.store.create_fleet("blue")).id
    await _active(a, "old", red)
    await _active(a, "heir", blue)
    return red, blue


async def test_retire_agent_records_the_succession_and_revokes_the_agent(adapter: Adapter) -> None:
    store = adapter.store
    await _people(adapter)
    succession = await store.retire_agent("old", "heir", "admin:1")
    assert (succession.retired_agent, succession.successor, succession.by) == (
        "old",
        "heir",
        "admin:1",
    )
    assert succession.at is not None
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert await store.get_succession("old") == succession
    assert await store.list_successions() == [succession]
    assert await store.inherited_authors("heir") == ("old",)
    assert await store.inherited_authors("old") == ()
    assert await store.inherited_authors("nobody") == ()


async def test_retire_agent_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.register_agent("waiting")
    with pytest.raises(InvalidInput):
        await store.retire_agent("old", "old", "x")
    with pytest.raises(UnknownAgent):
        await store.retire_agent("ghost", "heir", "x")
    with pytest.raises(UnknownAgent):
        await store.retire_agent("old", "ghost", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.retire_agent("old", "waiting", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.retire_agent("waiting", "heir", "x")
    assert await store.list_successions() == []
    assert (await store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    await store.retire_agent("old", "heir", "x")
    with pytest.raises(SuccessionRefused, match="already retired"):
        await store.retire_agent("old", "heir", "x")
    await _active(adapter, "third", red)
    with pytest.raises(InvalidAgentStatus):  # a revoked successor
        await store.retire_agent("third", "old", "x")
    assert [s.retired_agent for s in await store.list_successions()] == ["old"]


async def test_a_revoked_agent_may_be_retired_and_chains_resolve_transitively(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await _active(adapter, "c", red)
    await store.revoke_agent("old")
    await store.retire_agent("old", "heir", "x")  # already revoked: still hands down
    await store.retire_agent("heir", "c", "x")
    assert await store.inherited_authors("c") == ("heir", "old")
    assert await store.inherited_authors("heir") == ("old",)
    await store.undo_succession("heir")
    assert await store.inherited_authors("c") == ()
    assert await store.inherited_authors("heir") == ("old",)


async def test_a_cycle_is_refused_by_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    # Re-activate behind the guard (an older pod could): old would then succeed the agent
    # that hands down to it.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["old"] = replace(agents["old"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'old'")
        finally:
            await conn.close()
    with pytest.raises(SuccessionRefused, match="cycle"):
        await store.retire_agent("heir", "old", "x")
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert [s.retired_agent for s in await store.list_successions()] == ["old"]


async def test_undo_succession_deletes_it_and_leaves_the_agent_revoked(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    undone = await store.undo_succession("old")
    assert undone.successor == "heir"
    assert await store.list_successions() == [] and await store.get_succession("old") is None
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    with pytest.raises(UnknownSuccession):
        await store.undo_succession("old")
    await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)
    await store.retire_agent("old", "heir", "again")  # free to retire once more
    assert (await store.get_succession("old")).by == "again"  # type: ignore[union-attr]


async def test_a_retired_agent_cannot_be_activated_until_undone(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    with pytest.raises(AgentRetired, match="undo the retirement") as refused:
        await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)
    assert refused.value.successor == "heir"
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    await store.undo_succession("old")
    await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)


async def test_list_successions_is_newest_first(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await _active(adapter, "p", red)
    await store.retire_agent("old", "heir", "x")
    await store.retire_agent("p", "heir", "x")
    assert [s.retired_agent for s in await store.list_successions()] == ["p", "old"]


async def test_handed_down_counts_active_notes_fleet_memories_and_pins(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    await write(adapter, "n1", author="old", scope="self")
    await write(adapter, "n2", author="old", scope="self", hot=1)
    gone = await write(adapter, "gone", author="old", scope="self", hot=2)
    await store.withdraw_entry(gone.id, "x", by_user="old")
    # A fleet write must be in the author's home fleet (ADR 0112): old wrote in
    # blue while blue was its home, then moved to red.
    await store.set_agent_home_fleet("old", blue)
    await write(adapter, "in blue", author="old", scope="fleet", fleet_id=blue, hot=4)
    await store.set_agent_home_fleet("old", red)
    pinned = await write(adapter, "in red", author="old", scope="fleet", fleet_id=red, hot=3)
    await write(adapter, "other's", author="heir", scope="self", hot=5)
    await store.pin_entry(red, pinned.id, "old", 10)
    facts = await store.handed_down(("old",), blue)
    assert (facts.self_notes, facts.fleet_memories, facts.fleet_memories_elsewhere, facts.pins) == (
        2,
        2,
        1,
        1,
    )
    assert facts.authors == ("old",)
    both = await store.handed_down(("old", "heir"), blue)
    assert both.self_notes == 3
    nobody = await store.handed_down(("nobody",), None)
    assert (nobody.self_notes, nobody.fleet_memories, nobody.pins) == (0, 0, 0)


async def test_inherited_authors_read_like_own_in_list_and_search(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    note = await write(adapter, "old kaniko note", author="old", scope="self")
    elsewhere = await write(
        adapter, "old red kaniko fact", author="old", scope="fleet", fleet_id=red, hot=1
    )
    stranger = await write(adapter, "stranger kaniko note", author="zed", scope="self", hot=2)
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue, inherited=("old",))
    plain = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue)
    listed = await store.list_entries(EntryFilters(), limit=10, offset=0, visibility=heir)
    assert {e.id for e in listed} == {note.id, elsewhere.id}
    assert await store.list_entries(EntryFilters(), limit=10, offset=0, visibility=plain) == []
    widened = await store.list_entries(
        EntryFilters(authors=("heir", "old")), limit=10, offset=0, visibility=heir
    )
    assert {e.id for e in widened} == {note.id, elsewhere.id}
    assert stranger.id not in {e.id for e in listed}
    hits = await store.search_keyword("kaniko", EntryFilters(), 10, visibility=heir)
    assert set(hits) == {note.id, elsewhere.id}
    assert await store.search_keyword("kaniko", EntryFilters(), 10, visibility=plain) == []


async def test_inherited_authors_filter_the_feedback_log(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    mine = await write(adapter, "old red", author="old", scope="fleet", fleet_id=red)
    await write(adapter, "zed red", author="zed", scope="fleet", fleet_id=red, hot=1)
    await store.record_feedback(Feedback(mine.id, "peer", "a", Verdict.WRONG, None, T0))
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue, inherited=("old",))
    items = await store.list_feedback_log(
        FeedbackLogFilters(authors=("heir", "old")), 10, 0, visibility=heir
    )
    assert [i.entry_author for i in items] == ["old"]
    none = await store.list_feedback_log(FeedbackLogFilters(author="heir"), 10, 0, visibility=heir)
    assert none == []


async def test_inherited_pins_count_toward_the_agent_cap(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.set_agent_home_fleet("heir", red)  # a pin is in the pinner's home fleet (ADR 0112)
    es = [
        await write(adapter, f"p{i}", author="old", scope="fleet", fleet_id=red, hot=i)
        for i in range(4)
    ]
    for e in es[:3]:
        assert isinstance(await store.pin_entry(red, e.id, "old", 10, agent_limit=3), PinWrite)
    refused = await store.pin_entry(
        red, es[3].id, "heir", 10, agent_limit=3, also_owned_by=("old",)
    )
    assert refused is PinRefusal.AGENT
    assert isinstance(await store.pin_entry(red, es[3].id, "heir", 10, agent_limit=3), PinWrite)


# -- any spelling of an entry id, and tie order (MemoryStore matches Postgres) --------


def _spellings(entry_id: str) -> list[str]:
    """Upper case and hyphen-less: both name the entry, as a ``uuid`` column reads them."""
    return [entry_id.upper(), entry_id.replace("-", ""), entry_id.upper().replace("-", "")]


async def test_a_write_names_its_targets_in_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    old = await write(adapter, "old")
    see = await write(adapter, "see")
    new = await write(adapter, "new", supersedes=(old.id.upper(),), see_also=(see.id.upper(),))
    flipped = await store.get_entry(old.id)
    assert flipped is not None
    assert flipped.state is EntryState.SUPERSEDED and flipped.superseded_by == new.id
    assert await store.entry_links(new.id, 10) == ([see.id], [])
    bare = await write(adapter, "next")
    again = await write(adapter, "again", supersedes=(bare.id.replace("-", ""),))
    bare_after = await store.get_entry(bare.id)
    assert bare_after is not None and bare_after.superseded_by == again.id
    # Two spellings of one target are one target.
    one = await write(adapter, "one")
    both = await write(adapter, "both", supersedes=(one.id, one.id.upper()))
    one_after = await store.get_entry(one.id)
    assert one_after is not None and one_after.superseded_by == both.id
    # A malformed id names no entry: the write is refused and nothing lands.
    live = await write(adapter, "live")
    with pytest.raises(SupersedeConflict):
        await write(adapter, "bad", supersedes=(live.id, "not-a-uuid"))
    live_after = await store.get_entry(live.id)
    assert live_after is not None and live_after.state is EntryState.ACTIVE


async def test_reads_by_entry_id_accept_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a", hot=0)
    b = await write(adapter, "b", hot=1)
    c = await write(adapter, "c", hot=2, supersedes=(b.id,), see_also=(a.id,))
    await _fb(adapter, a, ME, Verdict.WRONG, T0)
    for spelled in _spellings(a.id):
        assert await store.feedback_counts(spelled) == (0, 0, 1)
        assert [f.entry_id for f in await store.list_feedback(spelled, 10)] == [a.id]
        assert (await store.quality_counts([spelled]))[a.id] == (0, 0, 1)
        assert (await store.feedback_signals([spelled]))[a.id].wrong == 1
        assert await store.entry_links(spelled, 10) == ([], [c.id])
        assert (await store.entry_links_many([spelled], 10))[spelled] == ([], [c.id])
        assert (await store.list_feedback_many([spelled], 10))[spelled][0].entry_id == a.id
    pair = tuple(sorted((a.id, c.id)))
    for spelled in _spellings(a.id):
        sims = await store.pairwise_similarity([spelled, c.id.upper()])
        assert list(sims) == [pair]
    for spelled in _spellings(c.id):
        near = await store.similar_entries(adapter.vec(2), 10, exclude_id=spelled)
        assert c.id not in [eid for eid, _ in near]
    for spelled in _spellings(c.id):
        assert [e.id for e in await store.list_predecessors([spelled])] == [b.id]
    signals = await store.feedback_signals([c.id], count_supersedes=True)
    assert signals[c.id].supersedes == 1
    # Malformed ids are simply not found.
    assert await store.feedback_counts("not-a-uuid") == (0, 0, 0)
    assert await store.list_feedback("not-a-uuid", 10) == []
    assert await store.entry_links("not-a-uuid", 10) == ([], [])


async def test_feedback_recorded_under_any_spelling_lands_on_the_entry(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a")
    await store.record_feedback(Feedback(a.id.upper(), "u", "x", Verdict.STALE, None, T0))
    await store.record_feedback(Feedback(a.id, "u", "x", Verdict.WRONG, None, T0))  # same row
    assert await store.feedback_counts(a.id) == (0, 0, 1)
    assert [f.entry_id for f in await store.list_feedback(a.id, 10)] == [a.id]


async def test_withdraw_accepts_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a")
    withdrawn = await store.withdraw_entry(a.id.upper(), "gone", "boss")
    assert withdrawn.id == a.id and withdrawn.state is EntryState.WITHDRAWN
    with pytest.raises(ValueError):
        await store.withdraw_entry(a.id.replace("-", ""), None, "boss")
    with pytest.raises(KeyError):
        await store.withdraw_entry("not-a-uuid", None, "boss")


async def test_pins_accept_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    first = await store.pin_entry(fleet, a.id.upper(), "boss", limit=9)
    assert isinstance(first, PinWrite) and first.created and first.pin.entry_id == a.id
    again = await store.pin_entry(fleet, a.id.replace("-", ""), "boss", limit=9)
    assert isinstance(again, PinWrite) and not again.created
    swapped = await store.pin_entry(fleet, b.id, "boss", limit=9, replaces=[a.id.upper()])
    assert isinstance(swapped, PinWrite) and swapped.replaced == (a.id.upper(),)  # echoed as asked
    assert [p.entry_id for p in await store.list_pins(fleet)] == [b.id]
    assert await store.unpin_entry(fleet, b.id.upper()) is True
    assert await store.unpin_entry(fleet, "not-a-uuid") is False


async def _same_instant(adapter: Adapter, table: str, at: datetime) -> None:
    """Give every pin (or link) one timestamp, so only the tie order is left."""
    store = adapter.store
    if isinstance(store, MemoryStore):
        if table == "pins":
            store._pins = {k: replace(p, pinned_at=at) for k, p in store._pins.items()}
        else:
            store._links = dict.fromkeys(store._links, at)
        return
    column = "pinned_at" if table == "pins" else "created_at"
    async with store._connection() as conn:  # type: ignore[attr-defined]
        await conn.execute(f"UPDATE {table} SET {column} = $1", at)


async def test_pins_with_one_timestamp_list_by_entry_id(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    entries = [await write(adapter, f"p{i}", scope="fleet", fleet_id=fleet) for i in range(4)]
    # Pin in id order, so the oldest-first order of insertion is not the tie order.
    for e in sorted(entries, key=lambda e: e.id):
        assert isinstance(await adapter.store.pin_entry(fleet, e.id, "boss", limit=9), PinWrite)
    await _same_instant(adapter, "pins", T0)
    got = [p.entry_id for p in await adapter.store.list_pins(fleet)]
    assert got == sorted(e.id for e in entries)


async def test_links_with_one_timestamp_list_by_the_other_end_id(adapter: Adapter) -> None:
    target = await write(adapter, "target")
    other = await write(adapter, "other")
    sources = [await write(adapter, f"s{i}", see_also=(target.id, other.id)) for i in range(4)]
    await _same_instant(adapter, "entry_links", T0)
    ids = sorted(s.id for s in sources)
    assert await adapter.store.entry_links(target.id, 10) == ([], ids)
    assert await adapter.store.entry_links(target.id, 2) == ([], ids[:2])
    out, _ = await adapter.store.entry_links(sources[0].id, 10)
    assert out == sorted([target.id, other.id])


async def test_supersede_of_a_fleet_target_from_another_fleet_is_refused(
    adapter: Adapter,
) -> None:
    # The target's fleet changed after the service's may_supersede check.
    old_fleet, other_fleet = str(uuid.uuid4()), str(uuid.uuid4())
    target = await write(adapter, "fleet fact", scope="fleet", fleet_id=old_fleet)
    before = await adapter.store.count_entries(EntryFilters(include_inactive=True))
    for scope, fleet in (("fleet", other_fleet), ("fleet", None), ("self", None)):
        with pytest.raises(SupersedeConflict) as exc:
            await write(adapter, "successor", scope=scope, fleet_id=fleet, supersedes=(target.id,))
        assert exc.value.ids == [target.id]
    untouched = await adapter.store.get_entry(target.id)
    assert untouched is not None and untouched.state is EntryState.ACTIVE
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == before
    # Its own fleet, and an org successor (the admin's), still supersede.
    same = await write(adapter, "v2", scope="fleet", fleet_id=old_fleet, supersedes=(target.id,))
    assert same.id
    other = await write(adapter, "other", scope="fleet", fleet_id=other_fleet)
    await write(adapter, "org v2", scope="org", supersedes=(other.id,))
    flipped = await adapter.store.get_entry(other.id)
    assert flipped is not None and flipped.state is EntryState.SUPERSEDED


async def test_supersede_of_self_and_org_targets_ignores_the_successor_fleet(
    adapter: Adapter,
) -> None:
    mine = await write(adapter, "note", scope="self")
    org = await write(adapter, "org fact", scope="org")
    await write(
        adapter,
        "v2",
        scope="fleet",
        fleet_id=str(uuid.uuid4()),
        supersedes=(mine.id, org.id),
    )
    for e in (mine, org):
        got = await adapter.store.get_entry(e.id)
        assert got is not None and got.state is EntryState.SUPERSEDED


# -- restoring one withdrawn memory (ADR 0124) ---------------------------------------


async def test_restore_puts_a_withdrawn_memory_back_and_search_finds_it(
    adapter: Adapter,
) -> None:
    store = adapter.store
    entry = await write(adapter, "kaniko cache is on", hot=4)
    other = await write(adapter, "unrelated thing", hot=9)
    await store.withdraw_entry(entry.id, "mistaken", by_user="alice")
    gone = await _state(adapter, entry.id)
    assert gone.state is EntryState.WITHDRAWN and gone.withdrawn_at is not None
    assert await store.search_keyword("kaniko", EntryFilters(), 10) == []
    assert await vector_ids(store, adapter.vec(4), EntryFilters(), 10) == [other.id]

    restoration = await store.restore_entry(entry.id)

    assert restoration.previous_reason == "mistaken"
    back = restoration.entry
    assert back.state is EntryState.ACTIVE
    assert back.withdrawn_reason is None and back.withdrawn_at is None
    assert back.superseded_by is None
    assert replace(await _state(adapter, entry.id), embedding=None) == replace(back, embedding=None)
    # The embedding and the text index survived the withdrawal.
    assert await store.search_keyword("kaniko", EntryFilters(), 10) == [entry.id]
    assert (await vector_ids(store, adapter.vec(4), EntryFilters(), 10))[0] == entry.id


async def test_restore_refuses_what_is_not_withdrawn_and_what_is_unknown(
    adapter: Adapter,
) -> None:
    store = adapter.store
    active = await write(adapter, "active one")
    old = await write(adapter, "old")
    await write(adapter, "new", supersedes=(old.id,))
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(active.id)
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(old.id)
    for bad in (NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(KeyError):
            await store.restore_entry(bad)
    assert (await _state(adapter, active.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, old.id)).state is EntryState.SUPERSEDED


async def test_restore_is_not_repeatable_and_a_second_withdrawal_works(
    adapter: Adapter,
) -> None:
    store = adapter.store
    entry = await write(adapter, "flip")
    await store.withdraw_entry(entry.id, "first", by_user="alice")
    await store.restore_entry(entry.id)
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(entry.id)
    await store.withdraw_entry(entry.id, "second", by_user="alice")
    assert (await store.restore_entry(entry.id)).previous_reason == "second"


async def test_restoring_a_withdrawn_head_keeps_the_chain_below_it(adapter: Adapter) -> None:
    """P is superseded by H, H is withdrawn (P stays superseded, nothing is
    current). Restoring H makes it the one head again."""
    store = adapter.store
    p = await write(adapter, "v1")
    h = await write(adapter, "v2", supersedes=(p.id,))
    await store.withdraw_entry(h.id, "wrong", by_user="alice")
    with pytest.raises(SupersedeConflict):  # a withdrawn memory cannot be superseded
        await write(adapter, "v3", supersedes=(h.id,))

    await store.restore_entry(h.id)

    assert (await _state(adapter, h.id)).state is EntryState.ACTIVE
    below = await _state(adapter, p.id)
    assert below.state is EntryState.SUPERSEDED and below.superseded_by == h.id
    await write(adapter, "v3", supersedes=(h.id,))  # and it is supersedable again


async def test_restore_refuses_a_memory_a_standing_cleanup_withdrew(adapter: Adapter) -> None:
    store = adapter.store
    junk = await _junk(adapter, "junk")
    out = await _clean(adapter)
    assert out.record is not None
    with pytest.raises(RestoreViaCleanup) as exc:
        await store.restore_entry(junk.id)
    assert out.record.id in str(exc.value)
    assert (await _state(adapter, junk.id)).state is EntryState.WITHDRAWN

    await store.undo_agent_cleanup(out.record.id)  # the undo is the way back
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(junk.id)


async def test_restore_of_a_hand_typed_cleanup_reason_is_allowed(adapter: Adapter) -> None:
    """Only a cleanup record that lists the memory makes it a cleanup's."""
    entry = await write(adapter, "typed")
    await adapter.store.withdraw_entry(entry.id, cleanup_reason(NO_SUCH_ID), by_user="alice")
    assert (await adapter.store.restore_entry(entry.id)).entry.state is EntryState.ACTIVE


async def _cleaned_chain(adapter: Adapter) -> tuple[Entry, Entry, str]:
    """`good` was superseded by the bad agent's `junk`; a cleanup withdrew
    `junk` and put `good` back (the link between them is gone)."""
    good = await write(adapter, "good", author="good")
    junk = await _junk(adapter, "junk", supersedes=(good.id,))
    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None and out.restored == 1
    return good, junk, out.record.id


async def _hand_withdrawn_and_cleanup_undone(adapter: Adapter) -> tuple[Entry, Entry]:
    """good was superseded by junk; cleanup C withdrew junk and put good back;
    good was withdrawn by hand; C was undone. junk is active, good withdrawn."""
    store = adapter.store
    good, junk, cleanup_id = await _cleaned_chain(adapter)
    await store.withdraw_entry(good.id, "by hand", by_user="alice")
    undone = await store.undo_agent_cleanup(cleanup_id)
    assert (undone.reactivated, undone.resupersede_skipped) == (1, 1)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    return good, junk


async def test_restore_refuses_a_memory_a_cleanup_once_put_back(adapter: Adapter) -> None:
    """Whatever the successor's state now, the cut link means a restore could
    leave two heads once the successor comes back, so the answer is: write a
    new memory. This also holds while the cleanup still stands."""
    store = adapter.store
    good, junk, cleanup_id = await _cleaned_chain(adapter)
    await store.withdraw_entry(good.id, "by hand", by_user="alice")
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    undone = await store.undo_agent_cleanup(cleanup_id)
    assert undone.reactivated == 1
    with pytest.raises(RestoreChainConflict) as exc:
        await store.restore_entry(good.id)
    assert "new memory" in str(exc.value)
    assert (await _state(adapter, good.id)).state is EntryState.WITHDRAWN
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE


async def test_restore_scenario_a_cannot_end_with_two_active_heads(adapter: Adapter) -> None:
    """The successor is withdrawn by hand, then good is restored, then the
    successor: neither step may leave both active."""
    store = adapter.store
    good, junk = await _hand_withdrawn_and_cleanup_undone(adapter)
    await store.withdraw_entry(junk.id, "by hand", by_user="alice")
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    # junk is a plain withdrawn head with no link: restoring it is the only way back.
    await store.restore_entry(junk.id)
    states = {(await _state(adapter, e.id)).state for e in (good, junk)}
    assert states == {EntryState.WITHDRAWN, EntryState.ACTIVE}


async def test_restore_scenario_b_a_newer_cleanup_then_its_undo_cannot_make_two_heads(
    adapter: Adapter,
) -> None:
    store = adapter.store
    good, junk = await _hand_withdrawn_and_cleanup_undone(adapter)
    newer = await _clean(adapter)  # C3 withdraws junk again
    assert newer.record is not None
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    await store.undo_agent_cleanup(newer.record.id)
    states = {(await _state(adapter, e.id)).state for e in (good, junk)}
    assert states == {EntryState.WITHDRAWN, EntryState.ACTIVE}


# -- offboarding an owner (ADR 0135) --------------------------------------------------


async def _leaver(a: Adapter, name: str, alias: str, *, active: bool = True) -> None:
    await a.store.register_agent(name, alias)
    if active:
        red = (await a.store.list_fleets())[0].id
        await a.store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=red)


async def _offboarding_world(a: Adapter) -> None:
    await _people(a)  # old (red) and heir (blue), no owner alias
    await a.store.set_agent_owner_alias("old", "jane")
    await _leaver(a, "jane-2", "jane")
    await _leaver(a, "jane-wait", "jane", active=False)
    await _leaver(a, "jane-gone", "jane")
    await a.store.revoke_agent("jane-gone")
    await _leaver(a, "jane-prior", "jane")
    await a.store.retire_agent("jane-prior", "heir", "x")


async def _states(a: Adapter, *names: str) -> dict[str, AgentStatus]:
    return {n: (await a.store.get_agent(n)).status for n in names}  # type: ignore[union-attr]


async def test_offboard_owner_retires_every_agent_in_one_step(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "admin:1")
    assert [(s.name, s.action) for s in steps] == [
        ("jane-2", OffboardAction.RETIRE),
        ("jane-gone", OffboardAction.RETIRE),
        ("jane-prior", OffboardAction.SKIP),
        ("jane-wait", OffboardAction.REJECT),
        ("old", OffboardAction.RETIRE),
    ]
    assert steps[0].was is AgentStatus.ACTIVE and steps[1].was is AgentStatus.REVOKED
    assert steps[2].reason is not None and "already retired" in steps[2].reason
    assert sorted(await store.inherited_authors("heir")) == [
        "jane-2",
        "jane-gone",
        "jane-prior",
        "old",
    ]
    assert set((await _states(adapter, "jane-2", "jane-wait", "old")).values()) == {
        AgentStatus.REVOKED
    }
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert {s.by for s in await store.list_successions()} == {"admin:1", "x"}


async def test_offboard_owner_revoke_revokes_active_and_rejects_pending(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    steps = await store.offboard_owner("jane", OffboardMode.REVOKE, None, "admin:1")
    assert {s.name: s.action for s in steps} == {
        "jane-2": OffboardAction.REVOKE,
        "old": OffboardAction.REVOKE,
        "jane-wait": OffboardAction.REJECT,
        "jane-gone": OffboardAction.SKIP,
        "jane-prior": OffboardAction.SKIP,
    }
    states = await _states(adapter, "jane-2", "old", "jane-wait")
    assert set(states.values()) == {AgentStatus.REVOKED}
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]


async def test_offboard_owner_is_case_sensitive_and_needs_an_owner(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    with pytest.raises(UnknownOwner):
        await store.offboard_owner("Jane", OffboardMode.REVOKE, None, "x")
    with pytest.raises(UnknownOwner):
        await store.offboard_owner("nobody", OffboardMode.REVOKE, None, "x")


async def test_offboard_owner_successor_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    with pytest.raises(UnknownAgent):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "ghost", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "jane-wait", "x")
    with pytest.raises(SuccessionRefused, match="another owner"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "jane-2", "x")
    with pytest.raises(InvalidInput):
        await store.offboard_owner("jane", OffboardMode.RETIRE, None, "x")
    with pytest.raises(InvalidInput):
        await store.offboard_owner("jane", OffboardMode.REVOKE, "heir", "x")
    states = await _states(adapter, "jane-2", "old", "jane-wait", "jane-gone")
    assert states["jane-2"] is AgentStatus.ACTIVE and states["jane-wait"] is AgentStatus.PENDING
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]


async def test_offboard_owner_is_all_or_nothing_when_one_agent_would_cycle(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    await store.retire_agent("heir", "old", "x")  # heir hands down to old, an agent of jane
    # Re-activate behind the guard (an older pod could): heir would then succeed what it inherits.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["heir"] = replace(agents["heir"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'heir'")
        finally:
            await conn.close()
    with pytest.raises(SuccessionRefused, match="cycle"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x")
    states = await _states(adapter, "old", "jane-2", "jane-wait", "jane-gone")
    assert states["old"] is AgentStatus.ACTIVE and states["jane-2"] is AgentStatus.ACTIVE
    assert states["jane-wait"] is AgentStatus.PENDING and states["jane-gone"] is AgentStatus.REVOKED
    assert {s.retired_agent for s in await store.list_successions()} == {"jane-prior", "heir"}


async def test_offboard_owner_refuses_a_plan_that_differs_from_the_expected_one(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    wrong = {"old": "retire", "jane-2": "retire", "jane-gone": "revoke"}
    with pytest.raises(OffboardChanged, match="jane-gone"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x", wrong)
    assert (await store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]
    shown = {
        "jane-2": "retire",
        "jane-gone": "retire",
        "jane-prior": "skip",
        "jane-wait": "reject",
        "old": "retire",
    }
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x", shown)
    assert {s.name: s.action.value for s in steps} == shown


async def test_offboard_owner_revokes_an_active_agent_that_is_also_retired(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    # An older pod re-activated jane-prior behind the guard: its key works, its succession stands.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["jane-prior"] = replace(agents["jane-prior"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'jane-prior'")
        finally:
            await conn.close()
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "later")
    prior = next(s for s in steps if s.name == "jane-prior")
    assert prior.action is OffboardAction.REVOKE and prior.was is AgentStatus.ACTIVE
    assert (await store.get_agent("jane-prior")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert (await store.get_succession("jane-prior")).by == "x"  # type: ignore[union-attr]
    # In revoke mode the same agent is revoked too.
    await _leaver(adapter, "jane-4", "jane")
    await store.retire_agent("jane-4", "heir", "x")
    if adapter.kind == "memory":
        agents["jane-4"] = replace(agents["jane-4"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'jane-4'")
        finally:
            await conn.close()
    steps = await store.offboard_owner("jane", OffboardMode.REVOKE, None, "later")
    assert {s.name: s.action for s in steps}["jane-4"] is OffboardAction.REVOKE


async def test_offboard_owner_refuses_revoke_with_a_successor_before_any_lookup(
    adapter: Adapter,
) -> None:
    with pytest.raises(InvalidInput):
        await adapter.store.offboard_owner("nobody", OffboardMode.REVOKE, "ghost", "x")


# -- move to self (ADR 0145) ---------------------------------------------------------


async def test_move_to_self_takes_the_authors_earlier_versions_and_their_pins(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    first = await write(adapter, "v1", scope="fleet", fleet_id=blue)
    head = await store.create_entry(
        draft("v2", scope="fleet", fleet_id=blue, supersedes=(first.id,)), adapter.vec(0), "m"
    )
    stays = await write(adapter, "someone else's", scope="fleet", fleet_id=blue, author="bob")
    await store.pin_entry(blue, first.id, "boss", limit=10)
    await store.pin_entry(blue, stays.id, "boss", limit=10)

    move = await store.move_entry_to_self(head.id)

    assert move.entry.id == head.id and move.entry.scope == "self"
    assert move.entry.state is EntryState.ACTIVE and move.entry.fleet_id == blue
    assert move.earlier_versions == (first.id,)
    assert move.unpinned == (RemovedPin(blue, first.id),)
    moved_first = await store.get_entry(first.id)
    assert moved_first is not None and moved_first.scope == "self"
    assert moved_first.state is EntryState.SUPERSEDED and moved_first.superseded_by == head.id
    assert [p.entry_id for p in await store.list_pins(blue)] == [stays.id]
    fleet_reader = _vis(TrustLevel.CONTRIBUTOR, "carol", blue)
    visible = await store.list_entries(EntryFilters(include_inactive=True), visibility=fleet_reader)
    assert [e.id for e in visible] == [stays.id]


async def test_move_to_self_refuses_what_is_not_an_active_fleet_memory(adapter: Adapter) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    private = await write(adapter, "mine", scope="self")
    gone = await write(adapter, "gone", scope="fleet", fleet_id=blue)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    legacy = await write(adapter, "legacy")
    for entry_id in (private.id, gone.id, legacy.id):
        with pytest.raises(SelfMoveRefused):
            await store.move_entry_to_self(entry_id)
    with pytest.raises(KeyError):
        await store.move_entry_to_self(str(uuid.uuid4()))
    with pytest.raises(KeyError):
        await store.move_entry_to_self("not-a-uuid")


async def test_move_to_self_refuses_when_a_version_replaced_another_agents_memory(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    bobs = await write(adapter, "bob v1", scope="fleet", fleet_id=blue, author="bob")
    mine = await store.create_entry(
        draft("alice v2", scope="fleet", fleet_id=blue, supersedes=(bobs.id,)), adapter.vec(0), "m"
    )
    head = await store.create_entry(
        draft("alice v3", scope="fleet", fleet_id=blue, supersedes=(mine.id,)), adapter.vec(0), "m"
    )
    with pytest.raises(SelfMoveRefused, match=bobs.id):
        await store.move_entry_to_self(head.id)
    for entry_id in (bobs.id, mine.id, head.id):
        unchanged = await store.get_entry(entry_id)
        assert unchanged is not None and unchanged.scope == "fleet"


async def _old_and_heir_in_one_fleet(a: Adapter) -> str:
    red = (await a.store.create_fleet("red")).id
    await _active(a, "old", red)
    await _active(a, "heir", red)
    return red


async def test_move_to_self_takes_the_versions_an_heir_inherited(adapter: Adapter) -> None:
    store = adapter.store
    red = await _old_and_heir_in_one_fleet(adapter)
    await _active(adapter, "third", red)
    v1 = await write(adapter, "old v1", scope="fleet", fleet_id=red, author="old")
    await store.pin_entry(red, v1.id, "boss", limit=10)
    await store.retire_agent("old", "heir", "admin:1")
    v2 = await store.create_entry(
        draft("heir v2", scope="fleet", fleet_id=red, author="heir", supersedes=(v1.id,)),
        adapter.vec(0),
        "m",
    )
    await store.retire_agent("heir", "third", "admin:1")
    head = await store.create_entry(
        draft("third v3", scope="fleet", fleet_id=red, author="third", supersedes=(v2.id,)),
        adapter.vec(0),
        "m",
    )

    move = await store.move_entry_to_self(head.id)

    assert move.earlier_versions == tuple(sorted((v1.id, v2.id)))
    assert move.unpinned == (RemovedPin(red, v1.id),)
    for entry_id in (v1.id, v2.id, head.id):
        moved = await store.get_entry(entry_id)
        assert moved is not None and moved.scope == "self"


async def test_move_to_self_still_refuses_a_predecessors_line_that_replaced_someone_else(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red = await _old_and_heir_in_one_fleet(adapter)
    bobs = await write(adapter, "bob v1", scope="fleet", fleet_id=red, author="bob")
    olds = await store.create_entry(
        draft("old v2", scope="fleet", fleet_id=red, author="old", supersedes=(bobs.id,)),
        adapter.vec(0),
        "m",
    )
    await store.retire_agent("old", "heir", "admin:1")
    head = await store.create_entry(
        draft("heir v3", scope="fleet", fleet_id=red, author="heir", supersedes=(olds.id,)),
        adapter.vec(0),
        "m",
    )
    with pytest.raises(SelfMoveRefused, match=bobs.id):
        await store.move_entry_to_self(head.id)
    for entry_id in (bobs.id, olds.id, head.id):
        unchanged = await store.get_entry(entry_id)
        assert unchanged is not None and unchanged.scope == "fleet"


async def test_a_private_memory_is_superseded_only_by_its_owner_or_an_org_successor(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    note = await write(adapter, "old's note", scope="self", author="old")
    with pytest.raises(SupersedeConflict):
        await store.create_entry(
            draft("bob's", scope="fleet", fleet_id=red, author="bob", supersedes=(note.id,)),
            adapter.vec(0),
            "m",
        )
    await store.retire_agent("old", "heir", "admin:1")
    by_heir = await store.create_entry(
        draft("heir's", scope="fleet", fleet_id=blue, author="heir", supersedes=(note.id,)),
        adapter.vec(0),
        "m",
    )
    flipped = await store.get_entry(note.id)
    assert flipped is not None and flipped.superseded_by == by_heir.id
    other = await write(adapter, "other note", scope="self", author="bob")
    await store.create_entry(
        draft("org", scope="org", author="admin", supersedes=(other.id,)), adapter.vec(0), "m"
    )


async def test_a_cleanup_undo_never_replaces_a_memory_moved_to_self_since(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    good = await write(adapter, "alice's fleet memory", scope="fleet", fleet_id=blue)
    junk = await store.create_entry(
        draft("bad over it", author="bad", scope="fleet", fleet_id=blue, supersedes=(good.id,)),
        adapter.vec(0),
        "m",
    )
    await store.pin_entry(blue, good.id, "alice", limit=10)
    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None
    await store.pin_entry(blue, good.id, "alice", limit=10)
    moved = await store.move_entry_to_self(good.id)
    assert moved.unpinned == (RemovedPin(blue, good.id),)

    undone = await store.undo_agent_cleanup(out.record.id)

    assert (undone.resuperseded, undone.resupersede_skipped) == (0, 1)
    kept = await _state(adapter, good.id)
    assert (kept.state, kept.scope, kept.superseded_by) == (EntryState.ACTIVE, "self", None)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    assert await store.list_pins(blue) == []
