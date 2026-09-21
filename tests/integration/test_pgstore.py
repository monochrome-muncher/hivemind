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
  matching how the service migrates on start (ADR 0007).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import (
    EntryDraft,
    EntryFilters,
    EntryState,
    Kind,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash
from hivemind.store.migrate import migrate

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
        await conn.execute("TRUNCATE credentials, feedbacks, entries")
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
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    try:
        vector_ok = await _vector_extension_available(dsn)
    except Exception:
        pytest.skip("could not verify the 'vector' extension on the dev database")
    if not vector_ok:
        pytest.skip("'vector' extension is not available on the dev database")

    await migrate(dsn, dim)
    await _truncate(dsn)

    store = PgStore(dsn)
    auth = PgAuthenticator(dsn)
    try:
        yield store, auth, dim
    finally:
        await _truncate(dsn)
        await store.close()
        await auth.close()


async def _vector_extension_available(dsn: str) -> bool:
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        return count > 0
    finally:
        await conn.close()


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


async def test_create_entry_with_supersedes_flips_targets(pg) -> None:
    store, _auth, _dim = pg
    old = await store.create_entry(draft("Token lifetime is 15 minutes"))
    new = await store.create_entry(draft("Token lifetime is 30 minutes", supersedes=(old.id,)))
    reloaded_old = await store.get_entry(old.id)
    reloaded_new = await store.get_entry(new.id)
    assert reloaded_old is not None
    assert reloaded_old.state is EntryState.SUPERSEDED
    assert reloaded_old.superseded_by == new.id
    assert reloaded_new is not None
    assert reloaded_new.state is EntryState.ACTIVE


async def test_supersede_unknown_target_is_ignored(pg) -> None:
    store, _, _ = pg
    entry = await store.create_entry(draft("Something", supersedes=("no-such-id",)))
    assert entry.state is EntryState.ACTIVE


async def test_withdraw_entry_sets_state_and_reason(pg) -> None:
    store, _, _ = pg
    entry = await store.create_entry(draft("Retract this"))
    withdrawn = await store.withdraw_entry(entry.id, "turned out to be wrong", by_user="alice")
    assert withdrawn.state is EntryState.WITHDRAWN
    assert withdrawn.withdrawn_reason == "turned out to be wrong"
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.state is EntryState.WITHDRAWN


async def test_withdraw_inactive_entry_raises(pg) -> None:
    store, _, _ = pg
    entry = await store.create_entry(draft("Once active"))
    await store.withdraw_entry(entry.id, None, by_user="alice")
    with pytest.raises(ValueError):
        await store.withdraw_entry(entry.id, "again", by_user="alice")


async def test_withdraw_unknown_entry_raises(pg) -> None:
    store, _, _ = pg
    with pytest.raises(KeyError):
        await store.withdraw_entry("nope", None, by_user="alice")


# --- feedback -------------------------------------------------------------------


async def test_feedback_counts_aggregate_verdicts(pg) -> None:
    store, _, _ = pg
    entry = await store.create_entry(draft("A fact"))
    await store.record_feedback(
        Feedback(entry_id=entry.id, user="bob", agent="agent-1", verdict=Verdict.HELPFUL)
    )
    await store.record_feedback(
        Feedback(entry_id=entry.id, user="bob", agent="agent-2", verdict=Verdict.STALE)
    )
    await store.record_feedback(
        Feedback(entry_id=entry.id, user="bob", agent="agent-3", verdict=Verdict.WRONG)
    )
    assert await store.feedback_counts(entry.id) == (1, 1, 1)


async def test_feedback_upsert_same_reporter_overrides(pg) -> None:
    store, _, _ = pg
    entry = await store.create_entry(draft("A fact"))
    await store.record_feedback(
        Feedback(entry_id=entry.id, user="bob", agent="agent-1", verdict=Verdict.HELPFUL)
    )
    await store.record_feedback(
        Feedback(entry_id=entry.id, user="bob", agent="agent-1", verdict=Verdict.WRONG)
    )
    assert await store.feedback_counts(entry.id) == (0, 0, 1)


async def test_quality_counts_is_batched(pg) -> None:
    store, _, _ = pg
    a = await store.create_entry(draft("fact a"))
    b = await store.create_entry(draft("fact b"))
    await store.record_feedback(
        Feedback(entry_id=a.id, user="bob", agent="a1", verdict=Verdict.HELPFUL)
    )
    counts = await store.quality_counts([a.id, b.id, "missing"])
    assert counts[a.id] == (1, 0, 0)
    assert counts[b.id] == (0, 0, 0)
    assert "missing" not in counts


# --- read path ------------------------------------------------------------------


async def test_get_entries_returns_only_known_ids(pg) -> None:
    store, _, _ = pg
    e = await store.create_entry(draft("one"))
    got = await store.get_entries([e.id, "missing"])
    assert list(got) == [e.id]


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


async def test_vector_search_ranks_by_cosine_similarity(pg) -> None:
    store, _, dim = pg
    close = await store.create_entry(draft("Auth uses JWT"), make_vec(dim, 0))
    far = await store.create_entry(draft("Cookies"), make_vec(dim, 3))
    unembedded = await store.create_entry(draft("No vector"))

    query = [0.95] * dim
    query[0] = 1.0
    results = await store.search_vector(query, EntryFilters(), limit=10)
    assert close.id in results
    assert unembedded.id not in results
    assert far.id in results
    assert results.index(close.id) < results.index(far.id)
    assert results[0] == close.id


# --- list -------------------------------------------------------------------------


async def test_list_entries_filters_and_pagination(pg) -> None:
    store, _, _ = pg
    alice = await store.create_entry(draft("alice's fact", author="alice", tags=("x",)))
    bob = await store.create_entry(draft("bob's decision", author="bob", kind=Kind.DECISION))
    bob2 = await store.create_entry(draft("bob's fact", author="bob", tags=("x", "y")))

    all_x = await store.list_entries(EntryFilters(tags=("x",)), limit=10)
    assert {e.id for e in all_x} == {alice.id, bob2.id}

    bob_only = await store.list_entries(EntryFilters(author="bob", kind=Kind.DECISION), limit=10)
    assert [e.id for e in bob_only] == [bob.id]

    page = await store.list_entries(EntryFilters(), limit=1, offset=1)
    assert len(page) == 1


async def test_list_entries_hides_inactive_by_default(pg) -> None:
    store, _, _ = pg
    old = await store.create_entry(draft("v1"))
    new = await store.create_entry(draft("v2", supersedes=(old.id,)))
    visible = await store.list_entries(EntryFilters(), limit=10)
    assert [e.id for e in visible] == [new.id]


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
            "INSERT INTO credentials (key_hash, kind, user_id, agent_id) "
            "VALUES ($1, 'agent', 'alice', 'alice-cli-1')",
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


async def test_migrate_is_idempotent(pg) -> None:
    dsn = _dsn()
    dim = _dim()
    # Running the migration twice must be a no-op (all DDL is guarded).
    await migrate(dsn, dim)
    await migrate(dsn, dim)
    # The schema must still be usable after the double migrate.
    store, _auth, _ = pg
    entry = await store.create_entry(draft("still works"))
    assert entry.state is EntryState.ACTIVE


async def test_create_entry_records_embedding_model(pg) -> None:
    """SPEC.md §7: the store records the embedding model per entry."""
    store, _, dim = pg
    entry = await store.create_entry(
        draft("Embedded fact"), make_vec(dim, 1), embedding_model="test-model"
    )
    assert entry.embedding_model == "test-model"
    reloaded = await store.get_entry(entry.id)
    assert reloaded is not None
    assert reloaded.embedding_model == "test-model"


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
