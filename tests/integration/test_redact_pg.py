"""Redacting a withdrawn memory on real Postgres (ADR 0125): the text is gone
from every table, the embedding and the keyword column are NULL, the memory is
not found by keyword or vector search, and no undo path brings it back.
Skips when Postgres is down, like the rest of the suite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.agent_cleanup import cleanup_reason
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import (
    EntityKind,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.redaction import RedactRefused
from hivemind.ports import Credential
from hivemind.services.governance import GovernanceService
from hivemind.store import PgStore

SINCE = datetime(2020, 1, 1, tzinfo=UTC)
UNTIL = datetime(2100, 1, 1, tzinfo=UTC)
ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
SECRET = "hunter2canary"


@pytest.fixture
async def pool():
    dsn, dim = Settings().database_url, Settings().embedding_dim
    await ensure_migrated(dsn, dim)
    await reset_tables(dsn)
    store = PgStore(dsn)
    try:
        yield dsn, dim, store
    finally:
        await store.close()


def _vec(dim: int, hot: int) -> list[float]:
    vector = [0.01] * dim
    vector[hot] = 1.0
    return vector


async def _leaky(store: PgStore, dim: int, author: str = "bad", see_also=()):  # type: ignore[no-untyped-def]
    return await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary=f"the {SECRET} password",
            body=f"login with {SECRET}",
            author=author,
            agent=author,
            tags=(SECRET, "ops"),
            sources=(Source(SourceType.URL, f"https://example.test/{SECRET}"),),
            see_also=see_also,
        ),
        _vec(dim, 7),
        "test-model",
        entities=(ExtractedEntity(f"{SECRET}-host", EntityKind.SYSTEM),),
        entities_model="ner-1",
    )


async def _everywhere(dsn: str) -> dict[str, str]:
    """Every row of every table in the schema as text, by table."""
    conn = await asyncpg.connect(dsn)
    try:
        tables = [
            r["tablename"]
            for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ]
        return {
            t: "\n".join(r["row"] for r in await conn.fetch(f'SELECT x::text AS row FROM "{t}" x'))
            for t in tables
        }
    finally:
        await conn.close()


async def test_the_text_is_gone_from_every_table_and_column(pool) -> None:
    dsn, dim, store = pool
    other = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="ordinary", author="good", agent="good"), None
    )
    entry = await _leaky(store, dim, see_also=(other.id,))
    pointer = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="points back", author="good", agent="good", see_also=(entry.id,)
        ),
        None,
    )
    # A feedback note quoting the text is the one place it may remain: it is counted, not edited.
    await store.record_feedback(Feedback(entry.id, "bob", "b", Verdict.WRONG, f"says {SECRET}"))
    await store.withdraw_entry(entry.id, f"leaked {SECRET}", by_user="admin")

    before = await _everywhere(dsn)
    assert SECRET in before["entries"] and "entry_links" in before

    service = GovernanceService(store)
    out = await service.redact(ADMIN, entry.id, "a credential leaked", entry.id)
    assert (out.feedback_notes, out.flag_notes) == (1, 1)

    after = await _everywhere(dsn)
    for table, text in after.items():
        if table == "feedbacks":
            assert f"says {SECRET}" in text  # untouched
            continue
        assert SECRET not in text, table
    assert "points back" in after["entries"] and "ordinary" in after["entries"]

    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("SELECT * FROM entries WHERE id = $1", entry.id)
        assert row is not None
        assert row["embedding"] is None and row["search_tsv"] is None
        assert row["embedding_model"] is None and row["entities_model"] is None
        assert row["summary"] == "[redacted]" and row["withdrawn_reason"] == "[redacted]"
        assert row["body"] is None and row["payload"] is None
        assert row["sources"] == "[]" or row["sources"] == []
        assert list(row["tags"]) == [] and list(row["entity_names"]) == []
        assert row["entities"] in ("[]", [])
        assert row["state"] == "withdrawn" and row["redacted_at"] is not None
        assert row["author"] == "bad" and row["kind"] == "fact" and row["scope"] == "org"
        # Outgoing links gone, the link pointing at it kept.
        links = await conn.fetch("SELECT from_id, to_id FROM entry_links")
        assert [(str(r["from_id"]), str(r["to_id"])) for r in links] == [(pointer.id, entry.id)]
        # The tsvector trigger did not refill the keyword column.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM entries WHERE search_tsv @@ plainto_tsquery('english', 'redacted')"
            )
            == 0
        )
    finally:
        await conn.close()

    # Not found by keyword or vector search, with inactive memories included.
    everything = EntryFilters(include_inactive=True)
    for query in (SECRET, "password", "login", "redacted", "ops"):
        assert entry.id not in await store.search_keyword(query, everything, 10)
    assert entry.id not in await store.search_vector(_vec(dim, 7), everything, 10)
    assert entry.id not in [sim_id for sim_id, _ in await store.similar_entries(_vec(dim, 7), 10)]
    # The audit row has the facts but never the text.
    (audit,) = await store.list_audit(AuditFilters(action=AuditAction.ENTRY_REDACT), 10)
    assert dict(audit.detail) == {
        "author": "bad",
        "reason": "a credential leaked",
        "feedback_notes": 1,
        "withdraw_reasons": 0,
    }


async def test_a_superseded_copy_loses_its_text_everywhere(pool) -> None:
    dsn, dim, store = pool
    old = await _leaky(store, dim, author="good")
    newer = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="rotated", author="other", agent="other", supersedes=(old.id,)
        ),
        _vec(dim, 2),
        "m",
    )
    await store.record_feedback(Feedback(old.id, "bob", "b", Verdict.STALE, f"old {SECRET}"))

    out = await GovernanceService(store).redact(ADMIN, old.id, "a credential leaked", old.id)
    assert (out.feedback_notes, out.flag_notes) == (1, 1)

    after = await _everywhere(dsn)
    for table, text in after.items():
        if table != "feedbacks":
            assert SECRET not in text, table
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("SELECT * FROM entries WHERE id = $1", old.id)
        assert row is not None
        assert (row["state"], str(row["superseded_by"])) == ("superseded", newer.id)
        assert row["embedding"] is None and row["search_tsv"] is None
        assert row["summary"] == "[redacted]" and row["withdrawn_reason"] is None
        assert row["redacted_at"] is not None and row["body"] is None
    finally:
        await conn.close()
    everything = EntryFilters(include_inactive=True)
    for query in (SECRET, "password", "login", "redacted"):
        assert old.id not in await store.search_keyword(query, everything, 10)
    assert old.id not in await store.search_vector(_vec(dim, 7), everything, 10)
    (pred,) = await store.list_predecessors([newer.id])
    assert pred.id == old.id and pred.summary == "[redacted]"


async def test_a_failed_redaction_changes_nothing(pool, monkeypatch) -> None:
    """All in one transaction: a failure after the wipe rolls the wipe back."""
    dsn, dim, store = pool
    entry = await _leaky(store, dim)
    await store.withdraw_entry(entry.id, None, by_user="admin")
    from hivemind.store import pgstore as module

    monkeypatch.setattr(module, "REDACT_DROP_LINKS", "DELETE FROM no_such_table")
    with pytest.raises(asyncpg.PostgresError):
        await store.redact_entry(entry.id)
    monkeypatch.undo()
    got = await store.get_entry(entry.id)
    assert got is not None and SECRET in got.summary and got.redacted_at is None
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT embedding IS NOT NULL FROM entries") is True
        assert await conn.fetchval("SELECT search_tsv IS NOT NULL FROM entries") is True
    finally:
        await conn.close()
    await store.redact_entry(entry.id)  # and the redaction still works afterwards


async def test_the_cleanup_undo_refuses_a_redacted_memory_even_with_a_matching_reason(pool) -> None:
    dsn, dim, store = pool
    entry = await _leaky(store, dim)
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    assert out.record is not None
    await store.redact_entry(entry.id)
    # The stub reason already stops the match; force the reason back to be sure
    # the redaction marker alone keeps the memory withdrawn.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE entries SET withdrawn_reason = $2 WHERE id = $1",
            entry.id,
            cleanup_reason(out.record.id),
        )
    finally:
        await conn.close()
    undone = await store.undo_agent_cleanup(out.record.id)
    assert (undone.reactivated, undone.reactivate_skipped) == (0, 1)
    got = await store.get_entry(entry.id)
    assert got is not None and got.state is EntryState.WITHDRAWN and got.redacted_at is not None


async def test_redacting_races_with_a_cleanup_undo_without_a_resurrection(pool) -> None:
    _dsn, dim, store = pool
    for round_ in range(8):
        entry = await _leaky(store, dim, author=f"bad{round_}")
        out = await store.cleanup_agent(f"bad{round_}", SINCE, UNTIL, cleaned_by="admin:1")
        assert out.record is not None
        results = await asyncio.gather(
            store.redact_entry(entry.id),
            store.undo_agent_cleanup(out.record.id),
            return_exceptions=True,
        )
        got = await store.get_entry(entry.id)
        assert got is not None
        if isinstance(results[0], RedactRefused):
            # The undo won: the memory was active again, so it cannot be redacted.
            assert got.state is EntryState.ACTIVE and got.redacted_at is None
        else:
            assert not isinstance(results[0], BaseException), results[0]
            assert got.state is EntryState.WITHDRAWN and got.redacted_at is not None
            assert SECRET not in got.summary


async def test_only_one_of_two_simultaneous_redactions_succeeds(pool) -> None:
    _, dim, store = pool
    entry = await _leaky(store, dim)
    await store.withdraw_entry(entry.id, None, by_user="admin")
    results = await asyncio.gather(
        store.redact_entry(entry.id), store.redact_entry(entry.id), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["RedactOutcome", "RedactRefused"]
