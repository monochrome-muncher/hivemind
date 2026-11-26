from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import Entry, EntryDraft, Kind, Source, SourceType
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Store
from hivemind.store import PgStore

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


NO_SUCH_ID = "00000000-0000-4000-8000-00000000dead"


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


ME = ("u-me", "me")  # the (user, agent) a feedback row is keyed by


async def _fb(
    a: Adapter, entry: Entry, who: tuple[str, str], verdict: Verdict, at: datetime
) -> None:
    await a.store.record_feedback(Feedback(entry.id, who[0], who[1], verdict, None, at))


async def _backdate(a: Adapter, entry: Entry, created_at: datetime) -> None:
    """Move an entry's ``created_at`` (the adapters stamp their own clocks)."""
    if isinstance(a.store, MemoryStore):
        a.store._entries[entry.id] = replace(a.store._entries[entry.id], created_at=created_at)
        return
    async with a.store._connection() as conn:  # type: ignore[attr-defined]
        await conn.execute("UPDATE entries SET created_at = $1 WHERE id = $2", created_at, entry.id)


# -- fleet merge (ADR 0107) -------------------------------------------------------------


async def _two_fleets(adapter: Adapter) -> tuple[str, str]:
    return (await adapter.store.create_fleet("a")).id, (await adapter.store.create_fleet("b")).id


async def _fleet_entry(adapter: Adapter, summary: str, fleet_id: str, hot: int = 0) -> Entry:
    return await write(adapter, summary, hot=hot, scope="fleet", fleet_id=fleet_id)


# -- fleet move (ADR 0112) --------------------------------------------------------------


async def _agent_in(adapter: Adapter, name: str, fleet_id: str) -> None:
    await adapter.store.register_agent(name)
    await adapter.store.activate_agent(
        name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet_id
    )


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
