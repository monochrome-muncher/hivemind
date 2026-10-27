"""Postgres-backed Store integration tests (SPEC.md §4, §6).

These run against the dev Postgres (docker, ``pgvector/pgvector:pg16``)
from ``docker-compose.yaml``: the DSN is ``HIVEMIND_DATABASE_URL``
(default ``postgresql://hivemind:hivemind@localhost:5432/hivemind``).

Hermetic-by-construction:
- The module skips cleanly (``pytest.skip``) when the DB is
  unreachable or the ``vector`` extension is unavailable — so
  ``uv run pytest`` stays green on a machine without Postgres.
- Each test starts from a TRUNCATEd table set (re-runnable, no
  leftover state between runs or between the unit and integration
  suites).
- The schema is applied idempotently (``migrate``) before each test,
  matching how the service migrates on start (ADR 0018).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.fakes import FakeEmbedder
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import (
    EntryDraft,
    EntryFilters,
    EntryState,
    ImportanceSource,
    Kind,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.pin import PinRefusal, PinWrite
from hivemind.ports import Credential, SupersedeConflict
from hivemind.services.chain import get_visible_entries
from hivemind.services.pins import PinService
from hivemind.services.write import WriteService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash

# --- helpers -------------------------------------------------------------------


def _dsn() -> str:
    return Settings().database_url


def _dim() -> int:
    return Settings().embedding_dim


def make_vec(dim: int, hot: int, base: float = 0.01) -> list[float]:
    """A deterministic ``dim``-dimensional vector with a 1.0 spike at ``hot``."""
    vec = [base] * dim
    vec[hot] = 1.0
    return vec


def draft(
    summary: str,
    *,
    author: str = "alice",
    agent: str = "claude-code",
    kind: Kind = Kind.FACT,
    body: str | None = None,
    tags: tuple[str, ...] = (),
    supersedes: tuple[str, ...] = (),
    occurred_at: datetime | None = None,
    scope: str = "org",
) -> EntryDraft:
    return EntryDraft(
        kind=kind,
        summary=summary,
        author=author,
        agent=agent,
        body=body,
        tags=tags,
        supersedes=supersedes,
        occurred_at=occurred_at,
        scope=scope,
    )


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "TRUNCATE credentials, feedbacks, entries, search_counts, entry_links, pins, "
            "agents, fleets, audit_log"
        )
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """A ready ``PgStore`` + ``PgAuthenticator`` on a clean dev database.

    Skips the whole module (via ``pytest.skip`` inside each test's
    fixture setup) when Postgres is unreachable, keeping the suite green
    on machines without a database.
    """
    dsn = _dsn()
    dim = _dim()
    await ensure_migrated(dsn, dim)
    await _truncate(dsn)

    store = PgStore(dsn)
    auth = PgAuthenticator(dsn)
    try:
        yield store, auth, dim
    finally:
        await _truncate(dsn)
        await store.close()
        await auth.close()


# --- write path -----------------------------------------------------------------


async def test_create_entry_assigns_id_and_created_at(pg) -> None:
    store, _, dim = pg
    entry = await store.create_entry(draft("Auth service uses JWT"), make_vec(dim, 0))
    assert entry.id
    assert entry.created_at is not None
    assert entry.state is EntryState.ACTIVE
    assert entry.embedding is not None and len(entry.embedding) == dim  # embedding round-trips
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.id == entry.id
    assert reloaded.summary == "Auth service uses JWT"
    assert reloaded == replace(entry, embedding=None)  # RETURNING matches a re-read


async def test_reads_do_not_fetch_the_stored_vector(pg) -> None:
    """No reader uses the stored embedding (search ranks it in SQL), so
    reads leave it out — and the vector stream still ranks by it."""
    store, _, dim = pg
    entry = await store.create_entry(draft("vector stays in Postgres"), make_vec(dim, 3))
    assert (await store.get_entry(entry.id)).embedding is None
    assert all(e.embedding is None for e in (await store.get_entries([entry.id])).values())
    listed = await store.list_entries(EntryFilters())
    assert listed and all(e.embedding is None for e in listed)
    assert await store.search_vector(make_vec(dim, 3), EntryFilters(), 5) == [entry.id]


async def test_concurrent_supersedes_of_one_head_exactly_one_wins(pg) -> None:
    """ADR 0034 atomicity: N writers superseding one head -> one wins, the
    rest roll back (no orphaned active successors)."""
    store, _, _ = pg
    head = await store.create_entry(draft("head"))
    results = await asyncio.gather(
        *(store.create_entry(draft(f"v{i}", supersedes=(head.id,))) for i in range(8)),
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, BaseException)]
    assert len(wins) == 1
    assert all(isinstance(r, SupersedeConflict) for r in results if r not in wins)
    active = await store.list_entries(EntryFilters())
    assert [e.id for e in active] == [wins[0].id]
    reloaded = await store.get_entry(head.id)
    assert reloaded is not None
    assert reloaded.superseded_by == wins[0].id


async def test_supersede_accepts_a_non_canonical_uuid_spelling(pg) -> None:
    """Postgres accepts upper-case / hyphen-less UUIDs; the atomic flip's
    "was every target flipped?" check must compare canonical forms, or a
    valid supersession is falsely denied and rolled back."""
    store, _, _ = pg
    head = await store.create_entry(draft("head"))
    spelled = head.id.upper().replace("-", "")
    successor = await store.create_entry(draft("v2", supersedes=(spelled,)))
    reloaded = await store.get_entry(head.id)
    assert reloaded is not None
    assert reloaded.state is EntryState.SUPERSEDED
    assert reloaded.superseded_by == successor.id


async def test_write_service_supersedes_by_a_non_canonical_uuid_spelling(pg) -> None:
    """The write path's reach check must find a target spelled upper-case
    or without hyphens, as the store's own flip does (Postgres answers with
    the canonical id)."""
    store, _, dim = pg
    service = WriteService(store, FakeEmbedder(dimension=dim))
    admin = Visibility(TrustLevel.PRIVILEGED, "admin", is_admin=True)
    head = await store.create_entry(draft("head"))
    spelled = head.id.upper().replace("-", "")
    successor = await service.write(draft("v2", supersedes=(spelled,)), writer=admin)
    reloaded = await store.get_entry(head.id)
    assert reloaded is not None
    assert reloaded.superseded_by == successor.id


async def test_write_service_warns_when_a_write_may_answer_an_open_question(pg) -> None:
    """ADR 0099: the warning is computed from ``related`` on a real write and
    on a check, and naming the question in ``see_also`` silences it."""
    store, _, dim = pg
    service = WriteService(store, FakeEmbedder(dimension=dim))
    admin = Visibility(TrustLevel.PRIVILEGED, "admin", is_admin=True)
    text = "why does billing in the eu region stall on node 20 behind the shared proxy"
    question = await service.write(
        draft(text, kind=Kind.INSIGHT, tags=("open-question",)), writer=admin
    )
    answer = text.replace("why does", "because").replace("stall", "stalls")

    def codes(warnings) -> list[str]:
        return [w.code for w in warnings]

    checked = await service.check(draft(answer), writer=admin)
    assert "answers_open_question" in codes(checked.warnings)
    assert any(question.id in w.message for w in checked.warnings)
    linked = replace(draft(answer), see_also=(question.id,))
    assert "answers_open_question" not in codes(
        (await service.check(linked, writer=admin)).warnings
    )
    written = await service.write_and_relate(draft(answer), writer=admin)
    assert "answers_open_question" in codes(written.warnings)
    superseding = await service.write_and_relate(
        draft(answer, supersedes=(question.id,)), writer=admin
    )
    assert "answers_open_question" not in codes(superseding.warnings)


async def test_concurrent_withdraw_raises_typed_error_not_assertion(pg) -> None:
    """Two concurrent withdrawals of one active entry: exactly one wins;
    the loser gets the port-contract ``ValueError`` (a 409 upstream),
    never a raw ``AssertionError`` — the guarded UPDATE used to be
    followed by a bare assert on its 0-row result."""
    store, _, _ = pg
    entry = await store.create_entry(draft("Race me"))
    results = await asyncio.gather(
        store.withdraw_entry(entry.id, "first", by_user="alice"),
        store.withdraw_entry(entry.id, "second", by_user="bob"),
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Exception)]
    assert len(wins) == 1 and len(losses) == 1
    assert isinstance(losses[0], ValueError)
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None and reloaded.state is EntryState.WITHDRAWN


async def test_concurrent_registration_of_same_name_is_idempotent(pg) -> None:
    """Two concurrent registrations of a new name both return the same
    pending record (SPEC §12.3 idempotent no-op) — the conflict-guarded
    insert used to surface a raw ``UniqueViolation`` as a 500."""
    store, _, _ = pg
    a, b = await asyncio.gather(store.register_agent("racer"), store.register_agent("racer"))
    assert a.name == b.name == "racer"
    assert a.status.value == "pending" and b.status.value == "pending"
    same = [a for a in await store.list_agents() if a.name == "racer"]
    assert len(same) == 1


async def test_concurrent_fleet_create_raises_typed_error(pg) -> None:
    """Two concurrent creates of the same fleet name: one wins, the other
    gets the port-contract ``ValueError`` (a 409 upstream) — not a raw
    ``UniqueViolation``."""
    store, _, _ = pg
    results = await asyncio.gather(
        store.create_fleet("race-fleet"),
        store.create_fleet("race-fleet"),
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Exception)]
    assert len(wins) == 1 and len(losses) == 1
    assert isinstance(losses[0], ValueError)


# --- feedback -------------------------------------------------------------------


async def test_list_feedback_newest_first_with_notes(pg) -> None:
    """ADR 0051: an entry's feedback rows come back newest first (ties on
    the reporter), notes included, at most ``limit`` of them."""
    store, _, _ = pg
    entry = await store.create_entry(draft("A fact"))
    other = await store.create_entry(draft("Another fact"))
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    for user, at, note in (
        ("b", t0, "old b"),
        ("a", t0, "old a"),
        ("c", t0 + timedelta(seconds=1), "newest"),
    ):
        await store.record_feedback(
            Feedback(
                entry_id=entry.id,
                user=user,
                agent=user,
                verdict=Verdict.STALE,
                note=note,
                updated_at=at,
            )
        )
    await store.record_feedback(
        Feedback(entry_id=other.id, user="d", agent="d", verdict=Verdict.WRONG, updated_at=t0)
    )
    rows = await store.list_feedback(entry.id, 10)
    assert [(r.user, r.note, r.verdict) for r in rows] == [
        ("c", "newest", Verdict.STALE),
        ("a", "old a", Verdict.STALE),
        ("b", "old b", Verdict.STALE),
    ]
    assert rows[0].entry_id == entry.id
    assert rows[0].updated_at == t0 + timedelta(seconds=1)
    assert [r.user for r in await store.list_feedback(entry.id, 1)] == ["c"]
    assert await store.list_feedback("not-a-uuid", 10) == []
    # ADR 0059: the batched read answers the same, per requested id.
    many = await store.list_feedback_many([entry.id, other.id, "not-a-uuid"], 2)
    assert list(many) == [entry.id, other.id, "not-a-uuid"]
    assert many[entry.id] == (await store.list_feedback(entry.id, 10))[:2]
    assert [r.user for r in many[other.id]] == ["d"]
    assert many["not-a-uuid"] == []


async def test_flagged_filter_keeps_stale_or_wrong_entries(pg) -> None:
    """ADR 0054: ``flagged`` keeps entries with a stale or wrong report, on
    list, count and both search streams."""
    store, _, dim = pg
    stale = await store.create_entry(draft("billing stale fact"), make_vec(dim, 0))
    wrong = await store.create_entry(draft("billing wrong fact"), make_vec(dim, 0))
    helpful = await store.create_entry(draft("billing helpful fact"), make_vec(dim, 0))
    await store.create_entry(draft("billing untouched fact"), make_vec(dim, 0))
    for entry, verdict in ((stale, Verdict.STALE), (wrong, Verdict.WRONG)):
        for reporter in ("a", "b"):  # two reports must not duplicate a row
            await store.record_feedback(
                Feedback(entry_id=entry.id, user=reporter, agent=reporter, verdict=verdict)
            )
    await store.record_feedback(
        Feedback(entry_id=helpful.id, user="a", agent="a", verdict=Verdict.HELPFUL)
    )
    flagged = EntryFilters(flagged=True)
    expected = {stale.id, wrong.id}
    listed = await store.list_entries(flagged, limit=10)
    assert sorted(e.id for e in listed) == sorted(expected)
    assert await store.count_entries(flagged) == 2
    assert set(await store.search_keyword("billing", flagged, 10)) == expected
    assert set(await store.search_vector(make_vec(dim, 0), flagged, 10)) == expected


# --- read path ------------------------------------------------------------------


# --- search ---------------------------------------------------------------------


async def test_keyword_search_ranks_by_relevance(pg) -> None:
    store, _, _ = pg
    jwt = await store.create_entry(draft("JWT tokens expire after 15 minutes", tags=("auth",)))
    other = await store.create_entry(draft("Cookies are HTTP only"))
    await store.create_entry(draft("Unrelated churn model"))

    results = await store.search_keyword("jwt token", EntryFilters(), limit=10)
    assert jwt.id in results
    assert other.id not in results
    assert results[0] == jwt.id  # the only entry with both tokens ranks first


async def test_keyword_search_matches_any_term(pg) -> None:
    """ADR 0047: an entry matching one query term is found; an entry
    matching more terms ranks above it."""
    store, _, _ = pg
    one = await store.create_entry(draft("Rotate the signing key every quarter"))
    both = await store.create_entry(draft("Rotate the JWT signing key"))
    none = await store.create_entry(draft("Cookies are HTTP only"))

    results = await store.search_keyword("jwt rotate", EntryFilters(), limit=10)
    assert results == [both.id, one.id]
    assert none.id not in results


async def test_keyword_search_matches_only_the_first_distinct_terms(pg) -> None:
    """ADR 0049: the keyword stream matches on the first
    ``MAX_KEYWORD_TERMS`` distinct query terms; later ones are ignored,
    and a repeated term takes one slot, not one per repetition."""
    from hivemind.store.pgstore import MAX_KEYWORD_TERMS

    store, _, _ = pg
    terms = [f"term{i:02d}" for i in range(MAX_KEYWORD_TERMS + 1)]
    first = await store.create_entry(draft(f"Notes on {terms[0]}"))
    last_cap = await store.create_entry(draft(f"Notes on {terms[MAX_KEYWORD_TERMS - 1]}"))
    past_cap = await store.create_entry(draft(f"Notes on {terms[MAX_KEYWORD_TERMS]}"))

    results = await store.search_keyword(" ".join(terms), EntryFilters(), limit=10)
    assert set(results) == {first.id, last_cap.id}
    assert past_cap.id not in results

    repeated = " ".join([terms[0]] * (MAX_KEYWORD_TERMS * 2) + [terms[MAX_KEYWORD_TERMS]])
    results = await store.search_keyword(repeated, EntryFilters(), limit=10)
    assert set(results) == {first.id, past_cap.id}


async def test_keyword_search_of_only_stop_words_matches_nothing(pg) -> None:
    store, _, _ = pg
    await store.create_entry(draft("The state of the art"))
    assert await store.search_keyword("the of and", EntryFilters(), limit=10) == []


@pytest.mark.parametrize(
    ("query", "reads_as_terms"),
    [
        ("jwt & !cookie", True),
        ("jwt | (cookie", True),
        ("jwt:* <-> cookie", True),
        ("' & '", False),
        ("it's o'neil", False),
        ("a\\b", False),
    ],
)
async def test_keyword_search_never_reads_tsquery_syntax(
    pg, query: str, reads_as_terms: bool
) -> None:
    """The query is parsed by ``plainto_tsquery`` before the any-term
    rewrite, so operator characters are plain text, never syntax: ``jwt &
    !cookie`` is the two words ``jwt`` and ``cookie`` (an entry with either
    matches), not "jwt and not cookie", and a malformed one is no error."""
    store, _, _ = pg
    jwt = await store.create_entry(draft("JWT tokens expire"))
    cookie = await store.create_entry(draft("Cookie jar settings"))
    results = await store.search_keyword(query, EntryFilters(), limit=10)
    assert set(results) == ({jwt.id, cookie.id} if reads_as_terms else set())


async def test_keyword_stream_finds_every_golden_entry(pg) -> None:
    """ADR 0047's recall measurement, kept as a gate: on the golden set
    (``tests/eval/golden.py``) the keyword stream alone puts the relevant
    entry in its top 5 for every query. The old every-term rule found 1
    of 8, because natural-language queries carry words the entry lacks."""
    from tests.eval.golden import golden_corpus, golden_queries

    store, _, _ = pg
    ids = []
    for kind, summary, tags in golden_corpus():
        entry = await store.create_entry(draft(summary, kind=Kind(kind), tags=tuple(tags)))
        ids.append(entry.id)

    for query, relevant in golden_queries():
        results = await store.search_keyword(query, EntryFilters(), limit=5)
        assert {ids[i] for i in relevant} & set(results), query


async def test_keyword_search_respects_filters(pg) -> None:
    store, _, _ = pg
    decision = await store.create_entry(
        draft("We decided to keep weekly cohorts", kind=Kind.DECISION)
    )
    fact = await store.create_entry(draft("Weekly cohorts are used"))
    results = await store.search_keyword(
        "weekly cohorts", EntryFilters(kind=Kind.DECISION), limit=10
    )
    assert decision.id in results
    assert fact.id not in results


async def test_similar_entries_returns_nearest_active_with_similarity(pg) -> None:
    """ADR 0052: nearest active entries with their cosine similarity,
    leaving out the excluded id and inactive entries."""
    store, _, dim = pg
    same = await store.create_entry(draft("Auth uses JWT"), make_vec(dim, 0))
    far = await store.create_entry(draft("Cookies"), make_vec(dim, 3))
    old = await store.create_entry(draft("Auth used sessions"), make_vec(dim, 0))
    await store.create_entry(draft("Auth uses JWT now", supersedes=(old.id,)), make_vec(dim, 1))
    new = await store.create_entry(draft("Auth uses JWT tokens"), make_vec(dim, 0))

    pairs = await store.similar_entries(make_vec(dim, 0), 10, exclude_id=new.id)
    ids = [eid for eid, _ in pairs]
    assert new.id not in ids
    assert old.id not in ids  # superseded
    assert ids[0] == same.id
    assert pairs[0][1] == pytest.approx(1.0, abs=1e-4)
    assert far.id in ids
    assert pairs[ids.index(far.id)][1] < pairs[0][1]
    assert len(await store.similar_entries(make_vec(dim, 0), 1, exclude_id=new.id)) == 1


# --- list -------------------------------------------------------------------------


async def test_list_entries_occurred_range(pg) -> None:
    store, _, _ = pg
    base = datetime(2026, 6, 1, tzinfo=UTC)
    early = await store.create_entry(draft("early", occurred_at=base))
    late = await store.create_entry(draft("late", occurred_at=base + timedelta(days=1)))
    got = await store.list_entries(EntryFilters(occurred_from=base, occurred_to=base), limit=10)
    assert {e.id for e in got} == {early.id}
    assert late.id not in {e.id for e in got}


# --- authenticator -----------------------------------------------------------------


async def test_authenticator_resolves_key_to_credential(pg) -> None:
    _store, auth, _dim = pg
    dsn = _dsn()
    # Insert an agent-scoped credential directly, then verify via the port.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO agents (name, status, trust_level) VALUES ('alice', 'active', 2)"
        )
        await conn.execute(
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
            "VALUES ($1, 'agent', 'alice', 'alice-cli-1', 'alice')",
            key_hash("raw-secret"),
        )
    finally:
        await conn.close()

    cred = await auth.verify("raw-secret")
    assert isinstance(cred, Credential)
    assert cred.user_id == "alice"
    assert cred.agent_id == "alice-cli-1"
    assert cred.is_admin is False

    unknown = await auth.verify("never-issued")
    assert unknown is None


# --- migration idempotency ---------------------------------------------------------


async def test_create_entry_records_importance_source(pg) -> None:
    """ROADMAP §4.5: ``importance_source`` round-trips through Postgres."""
    store, _, dim = pg
    defaulted = await store.create_entry(draft("Rode the default"), make_vec(dim, 2))
    assert defaulted.importance_source is ImportanceSource.DEFAULT
    reloaded_default = await store.get_entry(defaulted.id)
    assert reloaded_default is not None
    assert reloaded_default.importance_source is ImportanceSource.DEFAULT

    caller_draft = EntryDraft(
        kind=Kind.FACT,
        summary="Caller set it",
        author="alice",
        agent="claude-code",
        importance=5,
        importance_source=ImportanceSource.CALLER,
    )
    supplied = await store.create_entry(caller_draft, make_vec(dim, 3))
    assert supplied.importance == 5
    assert supplied.importance_source is ImportanceSource.CALLER
    reloaded_caller = await store.get_entry(supplied.id)
    assert reloaded_caller is not None
    assert reloaded_caller.importance_source is ImportanceSource.CALLER


# --- orchestrator probes (ADR 0019) -------------------------------------------


async def test_health_check_is_true_against_a_live_pool(pg) -> None:
    store, _auth, _dim = pg
    assert await store.health_check() is True


async def test_health_check_is_false_on_an_unreachable_pool() -> None:
    """Hermetic (no DB needed): a store pointed at a dead DSN reports
    unhealthy instead of raising — the 503 path of the probe endpoint
    (ADR 0019)."""
    store = PgStore("postgresql://hivemind:hivemind@localhost:59999/hivemind")
    try:
        assert await store.health_check() is False
    finally:
        await store.close()


async def test_get_visible_entries_keys_by_the_id_as_asked(pg) -> None:
    """ADR 0055: a batch read finds an entry whatever the case of its uuid,
    and an id that is not a uuid is simply absent."""
    store, _, _ = pg
    entry = await store.create_entry(draft("A fact"))
    upper = entry.id.upper()
    found = await get_visible_entries(
        store, [upper, "nope"], Visibility(TrustLevel.PRIVILEGED, "admin", is_admin=True)
    )
    assert list(found) == [upper]
    assert found[upper].id == entry.id


async def test_the_fleet_cap_holds_under_concurrent_pinners(pg) -> None:
    """ADR 0058: the per-fleet cap holds when the pins arrive together."""
    store, _, _ = pg
    entries = [await store.create_entry(draft(f"Pin {i}")) for i in range(4)]
    writes = await asyncio.gather(*(store.pin_entry("fleet-x", e.id, "lead", 3) for e in entries))
    kept = [w for w in writes if isinstance(w, PinWrite)]
    assert len(kept) == 3 and all(w is PinRefusal.FLEET for w in writes if w not in kept)
    assert len(await store.list_pins("fleet-x")) == 3
    # MemoryStore does not refuse a malformed entry id, so this is not in the contract.
    assert await store.pin_entry("fleet-x", "not-a-uuid", "lead", 3) is PinRefusal.FLEET


async def test_the_agent_cap_holds_under_concurrent_pinners(pg) -> None:
    """ADR 0096: the per-agent cap is checked under the fleet lock, so several
    simultaneous pins by one agent store exactly the cap."""
    store, _, _ = pg
    entries = [await store.create_entry(draft(f"Agent pin {i}")) for i in range(6)]
    writes = await asyncio.gather(
        *(store.pin_entry("fleet-x", e.id, "dev", 10, agent_limit=3) for e in entries)
    )
    assert sum(isinstance(w, PinWrite) for w in writes) == 3
    assert [w for w in writes if not isinstance(w, PinWrite)] == [PinRefusal.AGENT] * 3
    assert len(await store.list_pins("fleet-x")) == 3


async def test_pinned_listing_marks_superseded_and_flagged_pins(pg) -> None:
    """ADR 0076 over Postgres: a pin whose entry was superseded, and a pin
    whose entry got a stale verdict after the pin, report why they changed;
    a verdict from before the pin, or a helpful one, does not."""
    store, _, _ = pg
    fleet = str(uuid.uuid4())
    lead = Credential(
        user_id="lead",
        agent_name="lead",
        access_controlled=True,
        trust_level=TrustLevel.PRIVILEGED,
        home_fleet_id=fleet,
    )

    def fleet_draft(summary: str, **kw) -> EntryDraft:
        return replace(draft(summary, **kw), scope="fleet", fleet_id=fleet)

    old = await store.create_entry(fleet_draft("pool A"))
    flagged = await store.create_entry(fleet_draft("freeze Fridays"))
    calm = await store.create_entry(fleet_draft("use the VPN"))
    before = Feedback(calm.id, "ops", "ops", Verdict.WRONG, updated_at=datetime.now(UTC))
    await store.record_feedback(before)  # older than the pin
    service = PinService(store)
    for entry in (old, flagged, calm):
        await service.pin(lead, entry.id)
    later = datetime.now(UTC)  # after the pins, before the re-pin below
    await store.record_feedback(Feedback(flagged.id, "ops", "ops", Verdict.STALE, updated_at=later))
    await store.record_feedback(Feedback(calm.id, "dev", "dev", Verdict.HELPFUL, updated_at=later))
    new = await store.create_entry(fleet_draft("pool B", supersedes=(old.id,)))

    by_pinned = {p.pin.entry_id: p for p in await service.pinned(lead)}
    assert (by_pinned[old.id].entry.id, by_pinned[old.id].change_reason) == (new.id, "superseded")
    assert by_pinned[flagged.id].change_reason == "flagged"
    assert by_pinned[calm.id].change_reason is None

    await asyncio.sleep(0.05)
    await service.pin(lead, flagged.id)  # re-pinning acknowledges it
    assert {p.pin.entry_id: p.change_reason for p in await service.pinned(lead)}[flagged.id] is None
