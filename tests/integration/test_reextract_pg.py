"""``hivemind-reextract`` against Postgres (ADR 0131, SPEC §13.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.entry import EntityKind, EntryDraft, EntryFilters, ExtractedEntity, Kind
from hivemind.store import PgStore
from hivemind.store.reextract import Options, Scope, UnknownEntry, reextract

T0 = datetime(2026, 1, 1, tzinfo=UTC)
OLD_MODEL = "old-extractor"
POSTGRES = ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM)
POSTGRESQL = ExtractedEntity(name="PostgreSQL", kind=EntityKind.SYSTEM)
K8S = ExtractedEntity(name="k8s", kind=EntityKind.SYSTEM)
KUBERNETES = ExtractedEntity(name="Kubernetes", kind=EntityKind.SYSTEM)


class ScriptedExtractor:
    """Answers by summary; a summary in ``failing`` raises."""

    model_name = "new-extractor"

    def __init__(
        self,
        answers: dict[str, tuple[ExtractedEntity, ...]],
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self.answers = answers
        self.failing = failing
        self.calls: list[str] = []

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        self.calls.append(draft.summary)
        if draft.summary in self.failing:
            raise RuntimeError("extractor down")
        return self.answers[draft.summary]


@pytest.fixture
async def pg():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await reset_tables(dsn)
    store = PgStore(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        yield store, conn
    finally:
        await conn.close()
        await store.close()


async def _entry(
    store: PgStore,
    conn: asyncpg.Connection,
    summary: str,
    entities: tuple[ExtractedEntity, ...],
    *,
    minutes: int,
    tags: tuple[str, ...] = (),
) -> str:
    draft = EntryDraft(
        kind=Kind.FACT, summary=summary, body="body text", author="alice", agent="bot", tags=tags
    )
    entry = await store.create_entry(draft, entities=entities, entities_model=OLD_MODEL)
    await conn.execute(
        "UPDATE entries SET created_at = $2 WHERE id = $1::uuid",
        entry.id,
        T0 + timedelta(minutes=minutes),
    )
    return entry.id


async def _facets(conn: asyncpg.Connection, entry_id: str) -> asyncpg.Record:
    row = await conn.fetchrow(
        "SELECT entities, entity_names, entities_model, summary, body, tags, search_tsv::text "
        "AS tsv FROM entries WHERE id = $1::uuid",
        entry_id,
    )
    assert row is not None
    return row


async def test_dry_run_reports_and_writes_nothing(pg) -> None:
    store, conn = pg
    entry_id = await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=1)
    before = await _facets(conn, entry_id)
    lines: list[str] = []

    tally = await reextract(
        conn,
        ScriptedExtractor({"pool exhausted": (POSTGRESQL,)}),
        Scope(),
        Options(),
        lines.append,
    )

    assert (tally.scanned, tally.changed, tally.failed) == (1, 1, 0)
    assert f'would change {entry_id}: ["Postgres"] -> ["PostgreSQL"]' in lines
    assert await _facets(conn, entry_id) == before


async def test_apply_rewrites_only_the_facets(pg) -> None:
    store, conn = pg
    entry_id = await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=1, tags=("db",))
    before = await _facets(conn, entry_id)

    await reextract(
        conn,
        ScriptedExtractor({"pool exhausted": (POSTGRESQL, KUBERNETES)}),
        Scope(),
        Options(apply=True),
        lambda _: None,
    )

    after = await _facets(conn, entry_id)
    assert after["entity_names"] == ["postgresql", "kubernetes"]
    assert after["entities_model"] == "new-extractor"
    for column in ("summary", "body", "tags", "tsv"):
        assert after[column] == before[column]
    found = await store.list_entries(EntryFilters(entities=("PostgreSQL",)))
    assert [entry.id for entry in found] == [entry_id]


async def test_a_second_run_changes_nothing(pg) -> None:
    store, conn = pg
    await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=1)
    await _entry(store, conn, "cluster upgrade", (KUBERNETES,), minutes=2)
    extractor = ScriptedExtractor(
        {"pool exhausted": (POSTGRESQL,), "cluster upgrade": (KUBERNETES,)}
    )

    first = await reextract(conn, extractor, Scope(), Options(apply=True), lambda _: None)
    second = await reextract(conn, extractor, Scope(), Options(apply=True), lambda _: None)

    assert (first.scanned, first.changed) == (2, 1)
    assert (second.scanned, second.changed) == (2, 0)


async def test_a_failed_extraction_keeps_the_facets(pg) -> None:
    store, conn = pg
    kept = await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=1)
    changed = await _entry(store, conn, "cluster upgrade", (K8S,), minutes=2)
    extractor = ScriptedExtractor(
        {"cluster upgrade": (KUBERNETES,)}, failing=frozenset({"pool exhausted"})
    )

    tally = await reextract(conn, extractor, Scope(), Options(apply=True), lambda _: None)

    assert (tally.changed, tally.failed, tally.failed_ids) == (1, 1, [kept])
    assert (await _facets(conn, kept))["entity_names"] == ["postgres"]
    assert (await _facets(conn, changed))["entity_names"] == ["kubernetes"]


async def test_a_batch_where_everything_fails_stops_the_run(pg) -> None:
    store, conn = pg
    first = await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=1)
    await _entry(store, conn, "cluster upgrade", (K8S,), minutes=2)
    await _entry(store, conn, "disk full", (), minutes=3)
    extractor = ScriptedExtractor(
        {"pool exhausted": (POSTGRESQL,)},
        failing=frozenset({"cluster upgrade", "disk full"}),
    )

    tally = await reextract(
        conn, extractor, Scope(), Options(apply=True, batch_size=1), lambda _: None
    )

    assert tally.stopped_early
    assert (tally.scanned, tally.changed, tally.resume_after) == (1, 1, first)
    assert extractor.calls == ["pool exhausted", "cluster upgrade"]


async def test_redacted_and_inactive_entries(pg) -> None:
    store, conn = pg
    redacted = await _entry(store, conn, "secret", (POSTGRES,), minutes=1)
    withdrawn = await _entry(store, conn, "retracted", (POSTGRES,), minutes=2)
    await conn.execute(
        "UPDATE entries SET state = 'withdrawn', redacted_at = now() WHERE id = $1::uuid", redacted
    )
    await conn.execute("UPDATE entries SET state = 'withdrawn' WHERE id = $1::uuid", withdrawn)
    extractor = ScriptedExtractor({"secret": (POSTGRESQL,), "retracted": (POSTGRESQL,)})

    active_only = await reextract(conn, extractor, Scope(), Options(apply=True), lambda _: None)
    with_inactive = await reextract(
        conn, extractor, Scope(include_inactive=True), Options(apply=True), lambda _: None
    )

    assert active_only.scanned == 0
    assert (with_inactive.scanned, with_inactive.changed) == (1, 1)
    assert (await _facets(conn, redacted))["entity_names"] == ["postgres"]
    assert (await _facets(conn, withdrawn))["entity_names"] == ["postgresql"]


async def test_scope_by_fleet_and_creation_time(pg) -> None:
    store, conn = pg
    fleet = str(uuid.uuid4())
    early = await _entry(store, conn, "early", (POSTGRES,), minutes=1)
    in_fleet = await _entry(store, conn, "in fleet", (POSTGRES,), minutes=2)
    await _entry(store, conn, "late", (POSTGRES,), minutes=10)
    await conn.execute(
        "UPDATE entries SET fleet_id = $2::uuid WHERE id = ANY($1::uuid[])",
        [early, in_fleet],
        fleet,
    )
    extractor = ScriptedExtractor(dict.fromkeys(("early", "in fleet", "late"), (POSTGRESQL,)))

    by_fleet = await reextract(conn, extractor, Scope(fleet_id=fleet), Options(), lambda _: None)
    by_time = await reextract(
        conn,
        extractor,
        Scope(created_after=T0 + timedelta(minutes=2), created_before=T0 + timedelta(minutes=10)),
        Options(),
        lambda _: None,
    )

    assert by_fleet.scanned == 2
    assert (by_time.scanned, by_time.resume_after) == (1, in_fleet)


async def test_resume_after_an_entry_and_stop_at_a_limit(pg) -> None:
    store, conn = pg
    ids = [await _entry(store, conn, f"entry {n}", (POSTGRES,), minutes=n) for n in range(5)]
    extractor = ScriptedExtractor({f"entry {n}": (POSTGRESQL,) for n in range(5)})

    first = await reextract(
        conn, extractor, Scope(), Options(apply=True, limit=3, batch_size=2), lambda _: None
    )
    rest = await reextract(
        conn,
        extractor,
        Scope(),
        Options(apply=True, after=first.resume_after),
        lambda _: None,
    )

    assert (first.scanned, first.resume_after) == (3, ids[2])
    assert (rest.scanned, rest.resume_after) == (2, ids[4])
    assert extractor.calls == [f"entry {n}" for n in range(5)]


async def test_resume_from_an_unknown_entry_is_refused(pg) -> None:
    _, conn = pg
    with pytest.raises(UnknownEntry):
        await reextract(
            conn,
            ScriptedExtractor({}),
            Scope(),
            Options(after=str(uuid.uuid4())),
            lambda _: None,
        )


async def test_an_entry_that_no_longer_passes_the_draft_checks_is_skipped(pg) -> None:
    store, conn = pg
    odd = await _entry(store, conn, "odd", (POSTGRES,), minutes=1)
    await _entry(store, conn, "pool exhausted", (POSTGRES,), minutes=2)
    await conn.execute("UPDATE entries SET summary = ' ' WHERE id = $1::uuid", odd)
    extractor = ScriptedExtractor({"pool exhausted": (POSTGRESQL,)})

    tally = await reextract(
        conn, extractor, Scope(), Options(apply=True, batch_size=1), lambda _: None
    )

    assert not tally.stopped_early
    assert (tally.scanned, tally.changed, tally.failed_ids) == (2, 1, [odd])
    assert extractor.calls == ["pool exhausted"]


async def test_an_empty_result_keeps_the_facets_unless_clearing_is_allowed(pg) -> None:
    store, conn = pg
    entry_id = await _entry(store, conn, "generic", (POSTGRES,), minutes=1)
    extractor = ScriptedExtractor({"generic": ()})
    lines: list[str] = []

    kept = await reextract(conn, extractor, Scope(), Options(apply=True), lines.append)
    assert (kept.changed, kept.kept_clear) == (0, 1)
    assert f'kept {entry_id}: ["Postgres"] -> [] needs --allow-clear' in lines
    assert (await _facets(conn, entry_id))["entity_names"] == ["postgres"]

    cleared = await reextract(
        conn, extractor, Scope(), Options(apply=True, allow_clear=True), lambda _: None
    )
    assert (cleared.changed, cleared.kept_clear) == (1, 0)
    assert (await _facets(conn, entry_id))["entity_names"] == []


async def test_order_and_letter_case_alone_are_not_a_change(pg) -> None:
    store, conn = pg
    await _entry(store, conn, "both", (POSTGRESQL, KUBERNETES), minutes=1)
    reordered = (KUBERNETES, ExtractedEntity(name="postgresql", kind=EntityKind.SYSTEM))

    tally = await reextract(
        conn, ScriptedExtractor({"both": reordered}), Scope(), Options(), lambda _: None
    )

    assert (tally.scanned, tally.changed) == (1, 0)
