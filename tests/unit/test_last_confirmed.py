"""Last confirmed (SPEC §4.2): search hits and reads by id carry the date of
an entry's newest noted ``helpful`` report with no flag since, on both
surfaces, and its date never changes a score (whether it is newer than every
flag does, ADR 0106: ``tests/unit/test_confirmation_halves_flags.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hivemind.api.schemas import FeedbackSummaryOut
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import (
    Feedback,
    FeedbackSignals,
    FeedbackSummary,
    Verdict,
    last_confirmed,
)
from hivemind.mcp.app import hive_get, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder, make_search_config
from tests.unit.test_readable_feedback import WRITER, _rest_client, _surface

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _min(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


async def _fact(store: MemoryStore) -> str:
    entry = await hive_write(
        _surface(store, WRITER), kind="fact", summary="billing runs on node 20"
    )
    return str(entry["id"])


async def _report(
    store: MemoryStore, entry_id: str, user: str, verdict: Verdict, note: str | None, minute: int
) -> None:
    await store.record_feedback(Feedback(entry_id, user, user, verdict, note, _min(minute)))


def test_the_rule_is_dates_only() -> None:
    assert last_confirmed(None, None) is None
    assert last_confirmed(None, _min(1)) is None
    assert last_confirmed(_min(2), None) == _min(2)
    assert last_confirmed(_min(2), _min(1)) == _min(2)
    assert last_confirmed(_min(2), _min(2)) is None  # a tie is no confirmation
    assert last_confirmed(_min(2), _min(3)) is None
    assert FeedbackSignals(2, 1, 0, _min(2)).counts == (2, 1, 0)


# --- search hits -------------------------------------------------------------------


async def test_mcp_hit_carries_last_confirmed_and_omits_it_when_absent() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    [hit] = (await hive_search(_surface(store, WRITER), query="billing node"))["hits"]  # type: ignore[index]
    assert "last_confirmed_at" not in hit

    await _report(store, entry_id, "lurker", Verdict.HELPFUL, None, 1)  # no note
    [hit] = (await hive_search(_surface(store, WRITER), query="billing node"))["hits"]  # type: ignore[index]
    assert "last_confirmed_at" not in hit

    await _report(store, entry_id, "lurker", Verdict.HELPFUL, "ran the deploy", 2)
    [hit] = (await hive_search(_surface(store, WRITER), query="billing node"))["hits"]  # type: ignore[index]
    assert hit["last_confirmed_at"] == _min(2).isoformat()

    await _report(store, entry_id, "other", Verdict.STALE, "moved", 3)
    [hit] = (await hive_search(_surface(store, WRITER), query="billing node"))["hits"]  # type: ignore[index]
    assert "last_confirmed_at" not in hit


async def test_rest_hit_carries_last_confirmed() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    async with _rest_client(store, WRITER) as client:
        [hit] = (await client.post("/v1/search", json={"query": "billing node"})).json()
        assert hit["last_confirmed_at"] is None
        await _report(store, entry_id, "lurker", Verdict.HELPFUL, "ran it", 2)
        [hit] = (await client.post("/v1/search", json={"query": "billing node"})).json()
    assert datetime.fromisoformat(hit["last_confirmed_at"]) == _min(2)


# --- reads by id -------------------------------------------------------------------


async def test_mcp_get_and_batch_get_carry_last_confirmed() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    bare = await hive_get(_surface(store, WRITER), entry_id)
    assert "last_confirmed_at" not in bare["feedback"]  # type: ignore[operator]

    await _report(store, entry_id, "lurker", Verdict.HELPFUL, "ran it", 2)
    one = await hive_get(_surface(store, WRITER), entry_id)
    assert one["feedback"]["last_confirmed_at"] == _min(2).isoformat()  # type: ignore[index]
    [batched] = (await hive_get(_surface(store, WRITER), entry_ids=[entry_id]))["entries"]  # type: ignore[index]
    assert batched["feedback"] == one["feedback"]

    await _report(store, entry_id, "other", Verdict.WRONG, None, 3)
    after = await hive_get(_surface(store, WRITER), entry_id)
    assert "last_confirmed_at" not in after["feedback"]  # type: ignore[operator]


async def test_rest_get_and_batch_get_carry_last_confirmed() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    await _report(store, entry_id, "lurker", Verdict.HELPFUL, "ran it", 2)
    async with _rest_client(store, WRITER) as client:
        one = (await client.get(f"/v1/entries/{entry_id}")).json()
        many = (await client.post("/v1/entries/get", json={"entry_ids": [entry_id]})).json()
    assert datetime.fromisoformat(one["feedback"]["last_confirmed_at"]) == _min(2)
    assert many["entries"][0]["feedback"] == one["feedback"]


def test_rest_feedback_summary_carries_the_date() -> None:
    out = FeedbackSummaryOut.from_summary(FeedbackSummary(1, 0, 0, (), _min(2)))
    assert out.last_confirmed_at == _min(2)
    assert FeedbackSummaryOut.from_summary(FeedbackSummary(0, 0, 0)).last_confirmed_at is None


# --- display only ------------------------------------------------------------------


async def test_a_confirmation_date_never_changes_a_score() -> None:
    """Same helpful count, one with a note and one without, no flags: equal scores."""

    async def score_of(note: str | None) -> tuple[float, datetime | None]:
        store = MemoryStore(make_clock())
        entry = await store.create_entry(
            EntryDraft(kind=Kind.FACT, summary="billing runs on node 20", author="a", agent="a"),
            [0.1, 0.1, 0.1, 0.1],
        )
        await _report(store, entry.id, "lurker", Verdict.HELPFUL, note, 1)
        service = SearchService(
            store, make_embedder(), make_search_config(), now_fn=lambda: FIXED_NOW
        )
        [hit] = await service.search("billing node")
        return hit.score, hit.last_confirmed_at

    plain, plain_date = await score_of(None)
    noted, noted_date = await score_of("ran it")
    assert plain_date is None and noted_date == _min(1)
    assert plain == noted
