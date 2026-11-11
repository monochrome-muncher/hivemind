"""Re-extract the entity facets of stored entries (ADR 0131, SPEC §13.2); console entry point
``hivemind-reextract``.

An operator's one-off backfill after the extractor prompt or model changes. It reads entries in
``(created_at, id)`` order, runs the configured extractor on each, and rewrites only ``entities``,
``entity_names`` and ``entities_model`` where the set of names and kinds changed. A dry run by
default; ``--apply`` writes. A result that would empty an entry's facets is kept back unless
``--allow-clear``. Each batch ends with a resume point, a failed extraction keeps the entry's facets, and a
second run finds nothing left to change. Redacted entries are never touched.

Usage:
    hivemind-reextract [--apply] [--fleet ID] [--created-after T] [--created-before T]
                       [--include-inactive] [--after ENTRY_ID] [--limit N] [--allow-clear]
                       [--batch-size N] [--concurrency N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NoReturn

import asyncpg

from hivemind.config import load_settings
from hivemind.domain.entry import EntryDraft, ExtractedEntity, Kind
from hivemind.domain.validation import is_valid_uuid
from hivemind.extractor import OpenAICompatExtractor
from hivemind.ports import Extractor
from hivemind.store.pgstore.rows import decode_entities, encode_entities

_READER = "hivemind-reextract"
_START = (datetime.min.replace(tzinfo=UTC), "00000000-0000-0000-0000-000000000000")

_SELECT_BATCH = """
SELECT id::text, created_at, kind, summary, body, entities
  FROM entries
 WHERE redacted_at IS NULL AND (created_at, id) > ($1, $2::uuid){scope}
 ORDER BY created_at, id
 LIMIT {limit}
"""

_SELECT_CURSOR = "SELECT created_at, id::text FROM entries WHERE id = $1::uuid"

# Only the facet columns change, and never on a memory redacted since it was read.
_UPDATE_FACETS = """
UPDATE entries SET entities = $2::jsonb, entity_names = $3, entities_model = $4
 WHERE id = $1::uuid AND redacted_at IS NULL
"""


@dataclass(frozen=True, slots=True)
class Scope:
    """Which entries a run covers. Redacted entries are always left out."""

    fleet_id: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    include_inactive: bool = False

    def where(self, first_param: int) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        def add(condition: str, value: Any) -> None:
            params.append(value)
            conditions.append(condition.format(f"${first_param + len(params) - 1}"))

        if not self.include_inactive:
            conditions.append("state = 'active'")
        if self.fleet_id is not None:
            add("fleet_id = {}::uuid", self.fleet_id)
        if self.created_after is not None:
            add("created_at >= {}", self.created_after)
        if self.created_before is not None:
            add("created_at < {}", self.created_before)
        return "".join(f" AND {condition}" for condition in conditions), params


@dataclass(frozen=True, slots=True)
class Options:
    apply: bool = False
    after: str | None = None
    limit: int | None = None
    allow_clear: bool = False
    batch_size: int = 50
    concurrency: int = 4


@dataclass(slots=True)
class Tally:
    scanned: int = 0
    changed: int = 0
    kept_clear: int = 0
    failed: int = 0
    resume_after: str | None = None
    stopped_early: bool = False
    failed_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Stored:
    id: str
    created_at: datetime
    draft: EntryDraft | None
    entities: tuple[ExtractedEntity, ...]


class UnknownEntry(Exception):
    """``--after`` names no entry."""


async def reextract(
    conn: asyncpg.Connection,
    extractor: Extractor,
    scope: Scope,
    options: Options,
    report: Callable[[str], None] = print,
) -> Tally:
    """Re-extract every entry in ``scope`` after ``options.after``, batch by batch.

    A batch where every extraction fails, or whose write is refused by a lock, stops the run before
    anything of it is written, so a dead extractor does not walk the whole pool; ``resume_after``
    then names the last finished batch. An entry whose facets would become empty keeps them unless
    ``options.allow_clear``: a model answering ``[]`` to everything must not wipe the pool."""
    tally = Tally(resume_after=options.after)
    cursor = await _start_cursor(conn, options.after)
    gate = asyncio.Semaphore(options.concurrency)
    while options.limit is None or tally.scanned < options.limit:
        size = options.batch_size
        if options.limit is not None:
            size = min(size, options.limit - tally.scanned)
        batch = await _read_batch(conn, scope, cursor, size)
        if not batch:
            break
        results = await asyncio.gather(*(_extract(extractor, stored, gate) for stored in batch))
        attempted = [
            result
            for stored, result in zip(batch, results, strict=True)
            if stored.draft is not None
        ]
        if attempted and all(result is None for result in attempted):
            tally.stopped_early = True
            where = f"after {tally.resume_after}" if tally.resume_after else "at the start"
            report(f"every extraction in the batch {where} failed; stopping")
            break
        changes: list[tuple[_Stored, tuple[ExtractedEntity, ...]]] = []
        for stored, result in zip(batch, results, strict=True):
            if result is None:
                tally.failed += 1
                tally.failed_ids.append(stored.id)
                reason = "extraction failed" if stored.draft else "summary fails today's checks"
                report(f"failed {stored.id}: {reason}, facets kept")
            elif not _differs(stored.entities, result):
                continue
            elif not result and not options.allow_clear:
                tally.kept_clear += 1
                report(f"kept {stored.id}: {_names(stored.entities)} -> [] needs --allow-clear")
            else:
                changes.append((stored, result))
        if options.apply:
            try:
                await _write(conn, changes, extractor.model_name)
            except asyncpg.DeadlockDetectedError, asyncpg.LockNotAvailableError:
                tally.stopped_early = True
                where = f"after {tally.resume_after}" if tally.resume_after else "at the start"
                report(f"the batch {where} could not be written (row locks); stopping")
                break
        for stored, result in changes:
            verb = "changed" if options.apply else "would change"
            report(f"{verb} {stored.id}: {_names(stored.entities)} -> {_names(result)}")
        tally.scanned += len(batch)
        tally.changed += len(changes)
        cursor = (batch[-1].created_at, batch[-1].id)
        tally.resume_after = batch[-1].id
        report(
            f"{tally.scanned} scanned, {tally.changed} "
            f"{'changed' if options.apply else 'to change'}, {tally.kept_clear} kept rather than "
            f"cleared, {tally.failed} failed; "
            f"resume with --after {tally.resume_after}"
        )
    return tally


async def _start_cursor(conn: asyncpg.Connection, after: str | None) -> tuple[datetime, str]:
    if after is None:
        return _START
    row = await conn.fetchrow(_SELECT_CURSOR, after)
    if row is None:
        raise UnknownEntry(after)
    return row["created_at"], row["id"]


async def _read_batch(
    conn: asyncpg.Connection, scope: Scope, cursor: tuple[datetime, str], size: int
) -> list[_Stored]:
    where, params = scope.where(first_param=3)
    rows = await conn.fetch(_SELECT_BATCH.format(scope=where, limit=int(size)), *cursor, *params)
    return [
        _Stored(
            id=row["id"],
            created_at=row["created_at"],
            draft=_draft(row),
            entities=decode_entities(row["entities"]),
        )
        for row in rows
    ]


def _draft(row: asyncpg.Record) -> EntryDraft | None:
    """The text the extractor reads, as a write would hand it over; ``None`` if a row from an older
    release no longer passes today's summary checks. The extractor reads only summary and body."""
    try:
        return EntryDraft(
            kind=Kind(row["kind"]),
            summary=row["summary"],
            body=row["body"],
            author=_READER,
            agent=_READER,
        )
    except ValueError:
        return None


def _differs(stored: tuple[ExtractedEntity, ...], extracted: tuple[ExtractedEntity, ...]) -> bool:
    """Order and letter case alone are not a change: a rerun must not rewrite an entry for them."""

    def key(entities: tuple[ExtractedEntity, ...]) -> set[tuple[str, str]]:
        return {(entity.name.lower(), entity.kind.value) for entity in entities}

    return key(stored) != key(extracted)


async def _extract(
    extractor: Extractor, stored: _Stored, gate: asyncio.Semaphore
) -> tuple[ExtractedEntity, ...] | None:
    if stored.draft is None:
        return None
    async with gate:
        try:
            return await extractor.extract_entry(stored.draft)
        except Exception:
            return None


async def _write(
    conn: asyncpg.Connection,
    changes: Sequence[tuple[_Stored, tuple[ExtractedEntity, ...]]],
    model_name: str,
) -> None:
    if not changes:
        return
    async with conn.transaction():
        await conn.executemany(
            _UPDATE_FACETS,
            [
                (
                    stored.id,
                    json.dumps(encode_entities(entities)),
                    [entity.name.lower() for entity in entities],
                    model_name,
                )
                for stored, entities in changes
            ],
        )


def _names(entities: tuple[ExtractedEntity, ...]) -> str:
    return json.dumps([entity.name for entity in entities], ensure_ascii=True)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 date or time: {value!r}") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _entry_id(value: str) -> str:
    if not is_valid_uuid(value):
        raise argparse.ArgumentTypeError(f"not an entry id: {value!r}")
    return value


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1: {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hivemind-reextract",
        description=(
            "Re-extract the entity facets of stored entries with the configured extractor "
            "(ADR 0131). A dry run unless --apply is given."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report)")
    parser.add_argument("--fleet", type=_entry_id, help="only entries in this fleet (fleet id)")
    parser.add_argument(
        "--created-after", type=_timestamp, help="only entries created at or after this time"
    )
    parser.add_argument(
        "--created-before", type=_timestamp, help="only entries created before this time"
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="also superseded and withdrawn entries (default: active only)",
    )
    parser.add_argument(
        "--after", type=_entry_id, help="resume after this entry (the id a batch line prints)"
    )
    parser.add_argument(
        "--allow-clear",
        action="store_true",
        help="also write an empty result over existing entities (default: keep them)",
    )
    parser.add_argument("--limit", type=_positive, help="stop after this many entries")
    parser.add_argument(
        "--batch-size", type=_positive, default=50, help="entries per batch (default 50)"
    )
    parser.add_argument(
        "--concurrency",
        type=_positive,
        default=4,
        help="extractor calls in flight at once (default 4)",
    )
    return parser


def _fail(message: str) -> NoReturn:
    print(f"hivemind-reextract: {message}", file=sys.stderr)
    sys.exit(1)


async def _run(args: argparse.Namespace) -> Tally:
    settings = load_settings()
    if not settings.extractor_endpoint:
        _fail("extraction is off; set HIVEMIND_EXTRACTOR_ENDPOINT and HIVEMIND_EXTRACTOR_MODEL")
    scope = Scope(
        fleet_id=args.fleet,
        created_after=args.created_after,
        created_before=args.created_before,
        include_inactive=args.include_inactive,
    )
    options = Options(
        apply=args.apply,
        after=args.after,
        limit=args.limit,
        allow_clear=args.allow_clear,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
    )
    extractor = OpenAICompatExtractor.from_settings(settings)
    try:
        conn = await asyncpg.connect(
            settings.database_url,
            statement_cache_size=0,
            server_settings={"application_name": _READER, "lock_timeout": "5s"},
        )
        try:
            return await reextract(conn, extractor, scope, options)
        finally:
            await conn.close()
    finally:
        await extractor.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point (``hivemind-reextract``)."""
    args = build_parser().parse_args(argv)
    if (
        args.created_after is not None
        and args.created_before is not None
        and args.created_after >= args.created_before
    ):
        _fail("--created-after must be earlier than --created-before")
    try:
        tally = asyncio.run(_run(args))
    except UnknownEntry as exc:
        _fail(f"--after names no entry: {exc}")
    mode = "written" if args.apply else "dry run, nothing written; add --apply to write"
    print(
        f"done ({mode}): {tally.scanned} scanned, {tally.changed} "
        f"{'changed' if args.apply else 'to change'}, {tally.kept_clear} kept rather than "
        f"cleared, {tally.failed} failed"
    )
    if tally.stopped_early:
        sys.exit(2)


if __name__ == "__main__":
    main()
