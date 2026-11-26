"""Entries on both adapters: create and read, supersession, listing and filters, visibility,
withdraw, "see also" and upstream links."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.store_contract.helpers import (
    ME,
    NO_SUCH_ID,
    T0,
    Adapter,
    _clean,
    _fb,
    _rate,
    _vis,
    draft,
    vector_ids,
    write,
)

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
)
from hivemind.domain.feedback import Feedback, ReliedOnChange, Verdict
from hivemind.memstore import cosine_similarity
from hivemind.ports import EntryNotActiveConflict, SupersedeConflict, UnknownEntry

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
    with pytest.raises(UnknownEntry):
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


async def test_unverified_keeps_active_tagged_entries_without_a_standing_confirmation(
    adapter: Adapter,
) -> None:
    """``unverified`` keeps active entries tagged ``unverified`` whose last confirmation (a noted
    helpful newer than every flag) is missing, on list and count; the fleet's vital-signs count
    is the same set."""
    hour = timedelta(hours=1)

    async def tagged(summary: str) -> Entry:
        return await write(adapter, summary, tags=("unverified",))

    bare = await tagged("never rated")
    unnoted = await tagged("helpful without a note")
    confirmed = await tagged("confirmed")
    flagged_after = await tagged("flagged after the confirmation")
    reconfirmed = await tagged("confirmed after the flag")
    tie = await tagged("confirmed and flagged at once")
    old = await tagged("replaced")
    await write(adapter, "no tag")
    await write(adapter, "replacement", supersedes=(old.id,))
    note = "checked it"

    async def rate(
        entry: Entry, who: str, verdict: Verdict, at: datetime, text: str | None
    ) -> None:
        await adapter.store.record_feedback(Feedback(entry.id, who, who, verdict, text, at))

    await rate(unnoted, "u", Verdict.HELPFUL, T0, None)
    await rate(confirmed, "u", Verdict.HELPFUL, T0, note)
    await rate(flagged_after, "u", Verdict.HELPFUL, T0, note)
    await rate(flagged_after, "v", Verdict.STALE, T0 + hour, None)
    await rate(reconfirmed, "v", Verdict.WRONG, T0, None)
    await rate(reconfirmed, "u", Verdict.HELPFUL, T0 + hour, note)
    await rate(tie, "u", Verdict.HELPFUL, T0, note)
    await rate(tie, "v", Verdict.STALE, T0, None)
    fleet = await adapter.store.create_fleet("blue")
    await adapter.store.register_agent("Scout")
    await adapter.store.activate_agent(
        "Scout", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id
    )
    by_revoked = await tagged("confirmed only by a revoked agent")
    revoked_flag = await tagged("confirmed, then flagged by a revoked agent")
    await rate(by_revoked, "Scout", Verdict.HELPFUL, T0, note)
    await rate(revoked_flag, "u", Verdict.HELPFUL, T0, note)
    await rate(revoked_flag, "Scout", Verdict.WRONG, T0 + hour, None)
    await adapter.store.revoke_agent("Scout")

    expected = {bare.id, unnoted.id, flagged_after.id, tie.id, by_revoked.id}
    filters = EntryFilters(unverified=True)
    assert {e.id for e in await adapter.store.list_entries(filters)} == expected
    assert await adapter.store.count_entries(filters) == len(expected)
    health = await adapter.store.fleet_health(T0 + 2 * hour)
    assert sum(r.unverified for r in health.by_fleet) == len(expected)


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


# -- withdraw ------------------------------------------------------------------------


async def test_withdraw(adapter: Adapter) -> None:
    entry = await write(adapter, "to withdraw")
    withdrawn = await adapter.store.withdraw_entry(entry.id, "obsolete", by_user="alice")
    assert withdrawn.state is EntryState.WITHDRAWN
    assert withdrawn.withdrawn_reason == "obsolete"
    with pytest.raises(EntryNotActiveConflict):
        await adapter.store.withdraw_entry(entry.id, None, by_user="alice")
    with pytest.raises(UnknownEntry):
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
