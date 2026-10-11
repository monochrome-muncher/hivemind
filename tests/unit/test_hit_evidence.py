"""Evidence on a search hit (SPEC §6.1): ``contested``, ``correction``,
``supersedes`` and ``state``, on both surfaces, each omitted (MCP) / null
(REST) when empty, and none of it changes a score."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hivemind.api.schemas import HitOut
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import (
    CORRECTION_NOTE_CHARS,
    Correction,
    Feedback,
    Verdict,
    shown_correction,
    truncate_note,
)
from hivemind.mcp.app import hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.services.search import SearchService
from tests.fakes import FIXED_NOW, make_clock, make_embedder, make_search_config
from tests.unit.test_readable_feedback import WRITER, _rest_client, _surface

T0 = datetime(2026, 9, 1, tzinfo=UTC)
EVIDENCE = ("contested", "correction", "supersedes", "state")


def _min(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


async def _fact(store: MemoryStore, summary: str = "billing runs on node 20", **kw: object) -> str:
    entry = await hive_write(_surface(store, WRITER), kind="fact", summary=summary, **kw)  # type: ignore[arg-type]
    return str(entry["id"])


async def _report(
    store: MemoryStore, entry_id: str, user: str, verdict: Verdict, note: str | None, minute: int
) -> None:
    await store.record_feedback(Feedback(entry_id, user, user, verdict, note, _min(minute)))


async def _hits(store: MemoryStore, **kw: object) -> list[dict[str, object]]:
    reply = await hive_search(_surface(store, WRITER), query="billing node", **kw)  # type: ignore[arg-type]
    return reply["hits"]  # type: ignore[return-value,index]


# --- pure rules ---------------------------------------------------------------------


def test_truncate_note_cuts_on_characters_and_marks_the_cut() -> None:
    assert truncate_note("  short  ") == "short"
    assert truncate_note("x" * CORRECTION_NOTE_CHARS) == "x" * CORRECTION_NOTE_CHARS
    assert truncate_note("é" * 161) == "é" * 160 + "…"
    assert truncate_note("😀" * 200) == "😀" * 160 + "…"
    assert truncate_note("x" * 159 + " " + "y" * 9) == "x" * 159 + "…"


def test_a_correction_shows_only_when_newer_than_the_confirmation() -> None:
    corr = Correction(Verdict.STALE, "moved", "u", _min(5))
    assert shown_correction(None, None) is None
    assert shown_correction(corr, None) == corr
    assert shown_correction(corr, _min(4)) == corr
    assert shown_correction(corr, _min(5)) is None
    assert shown_correction(corr, _min(6)) is None


# --- MCP ----------------------------------------------------------------------------


async def test_a_plain_hit_carries_no_evidence_fields() -> None:
    store = MemoryStore(make_clock())
    await _fact(store)
    [hit] = await _hits(store)
    assert not any(k in hit for k in EVIDENCE)


async def test_contested_needs_a_helpful_and_a_stale_or_wrong_report() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    await _report(store, entry_id, "a", Verdict.HELPFUL, None, 1)
    assert "contested" not in (await _hits(store))[0]
    await _report(store, entry_id, "b", Verdict.STALE, None, 2)
    assert (await _hits(store))[0]["contested"] is True

    other = MemoryStore(make_clock())
    only_flags = await _fact(other)
    await _report(other, only_flags, "a", Verdict.WRONG, None, 1)
    assert "contested" not in (await _hits(other))[0]


async def test_correction_is_the_newest_noted_flag_and_shown_only_if_not_confirmed_since() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    await _report(store, entry_id, "old", Verdict.STALE, "was node 18", 1)
    await _report(store, entry_id, "new", Verdict.WRONG, "n" * 200, 2)
    await _report(store, entry_id, "bare", Verdict.WRONG, None, 3)
    [hit] = await _hits(store)
    assert hit["correction"] == {
        "verdict": "wrong",
        "note": "n" * 160 + "…",
        "reporter": "new",
        "updated_at": _min(2).isoformat(),
    }
    # A confirmation newer than the flags is not possible while a bare flag is
    # newer; once the bare flag is overtaken by a noted helpful, the correction goes.
    await _report(store, entry_id, "fan", Verdict.HELPFUL, "ran it on node 20", 4)
    [hit] = await _hits(store)
    assert "correction" not in hit
    assert hit["last_confirmed_at"] == _min(4).isoformat()
    # A flag after the confirmation brings a correction back.
    await _report(store, entry_id, "late", Verdict.STALE, "node 22 now", 5)
    [hit] = await _hits(store)
    assert hit["correction"]["note"] == "node 22 now"  # type: ignore[index]


async def test_supersedes_counts_only_the_predecessors_the_reader_may_read() -> None:
    store = MemoryStore(make_clock())
    home = "fleet-home"
    mine = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="billing node v1", author="w", agent="a", scope="org"),
        [0.1] * 4,
    )
    secret = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT, summary="billing node secret", author="zed", agent="a", scope="self"
        ),
        [0.1] * 4,
    )
    await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="billing node v2",
            author="zed",
            agent="a",
            scope="org",
            supersedes=(mine.id, secret.id),
        ),
        [0.1] * 4,
    )
    service = SearchService(store, make_embedder(), make_search_config(), now_fn=lambda: FIXED_NOW)
    reader = Visibility(TrustLevel.CONTRIBUTOR, "reader", home_fleet_id=home)
    [hit] = await service.search("billing node", visibility=reader)
    assert hit.supersedes == 1  # the other predecessor is the author's private note
    [hit] = await service.search(
        "billing node", visibility=Visibility(TrustLevel.CONTRIBUTOR, "zed")
    )
    assert hit.supersedes == 2


async def test_state_shows_only_under_include_inactive() -> None:
    store = MemoryStore(make_clock())
    old = await _fact(store)
    await _fact(store, "billing runs on node 22", supersedes=[old])
    assert all("state" not in h for h in await _hits(store))
    hits = await _hits(store, include_inactive=True)
    assert sorted(str(h["state"]) for h in hits) == ["active", "superseded"]
    assert [h.get("supersedes") for h in hits if h["state"] == "active"] == [1]


# --- REST ---------------------------------------------------------------------------


async def test_rest_hit_has_null_evidence_when_empty_and_the_fields_when_present() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _fact(store)
    async with _rest_client(store, WRITER) as client:
        [hit] = (await client.post("/v1/search", json={"query": "billing node"})).json()
        assert all(hit[k] is None for k in EVIDENCE)
        await _report(store, entry_id, "a", Verdict.HELPFUL, None, 1)
        await _report(store, entry_id, "b", Verdict.STALE, "outdated", 2)
        [hit] = (await client.post("/v1/search", json={"query": "billing node"})).json()
        assert hit["contested"] is True
        assert hit["correction"]["reporter"] == "b" and hit["correction"]["verdict"] == "stale"
        assert hit["correction"]["note"] == "outdated"
        assert hit["supersedes"] is None and hit["state"] is None
        [hit] = (
            await client.post(
                "/v1/search", json={"query": "billing node", "include_inactive": True}
            )
        ).json()
        assert hit["state"] == "active"


def test_rest_schema_defaults_are_null() -> None:
    for name in EVIDENCE:
        assert HitOut.model_fields[name].default is None


# --- display only -------------------------------------------------------------------


async def test_the_evidence_never_changes_a_score() -> None:
    """Two entries with identical counts: one flag carries a note (so it
    becomes a correction), the other is bare. Scores are equal."""

    async def score_of(note: str | None) -> float:
        store = MemoryStore(make_clock())
        entry = await store.create_entry(
            EntryDraft(kind=Kind.FACT, summary="billing runs on node 20", author="a", agent="a"),
            [0.1, 0.1, 0.1, 0.1],
        )
        await _report(store, entry.id, "u1", Verdict.HELPFUL, None, 1)
        await _report(store, entry.id, "u2", Verdict.WRONG, note, 2)
        service = SearchService(
            store, make_embedder(), make_search_config(), now_fn=lambda: FIXED_NOW
        )
        [hit] = await service.search("billing node", EntryFilters())
        assert hit.contested and (hit.correction is not None) == (note is not None)
        assert hit.state is None or hit.state is EntryState.ACTIVE
        return hit.score

    assert await score_of(None) == await score_of("wrong since v3")
