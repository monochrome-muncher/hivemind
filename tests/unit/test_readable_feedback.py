"""Feedback is readable (ADR 0051): search hits carry the verdict counts,
and a read by id carries the counts plus the newest reports with their
notes, on both surfaces, to exactly the audience that can read the entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hivemind.domain.access import TrustLevel
from hivemind.domain.feedback import FEEDBACK_RECENT_LIMIT, Feedback, Verdict
from hivemind.mcp.app import hive_feedback, hive_get, hive_search, hive_write
from hivemind.memstore import MemoryStore
from tests.builders import WRITER, mcp_app
from tests.fakes import (
    agent_credential,
    make_agent_client,
    make_clock,
)

LURKER = agent_credential("lurker", "fleet-a", TrustLevel.LURKER)
OUTSIDER = agent_credential("outsider", "fleet-b", TrustLevel.CONTRIBUTOR)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


async def _flagged_entry(store: MemoryStore) -> str:
    """A fleet entry the lurker has reported stale, with a correction."""
    entry = await hive_write(mcp_app(store, WRITER), kind="fact", summary="billing runs on node 20")
    result = await hive_feedback(
        mcp_app(store, LURKER), entry["id"], verdict="stale", note="billing moved to node 22"
    )
    assert "error" not in result
    return str(entry["id"])


# --- search hits -----------------------------------------------------------------


async def test_mcp_hit_without_feedback_leaves_the_counts_out() -> None:
    store = MemoryStore(make_clock())
    await hive_write(mcp_app(store, WRITER), kind="fact", summary="billing runs on node 20")
    result = await hive_search(mcp_app(store, WRITER), query="billing node")
    [hit] = result["hits"]  # type: ignore[index]
    assert "feedback" not in hit


async def test_rest_hit_without_feedback_is_null() -> None:
    store = MemoryStore(make_clock())
    await hive_write(mcp_app(store, WRITER), kind="fact", summary="billing runs on node 20")
    async with make_agent_client(store, WRITER) as client:
        resp = await client.post("/v1/search", json={"query": "billing node"})
    [hit] = resp.json()
    assert hit["feedback"] is None


async def test_mcp_hit_carries_feedback_counts() -> None:
    store = MemoryStore(make_clock())
    await _flagged_entry(store)
    result = await hive_search(mcp_app(store, WRITER), query="billing node")
    [hit] = result["hits"]  # type: ignore[index]
    assert hit["feedback"] == {"helpful": 0, "stale": 1, "wrong": 0}


async def test_rest_hit_carries_feedback_counts() -> None:
    store = MemoryStore(make_clock())
    await _flagged_entry(store)
    async with make_agent_client(store, WRITER) as client:
        resp = await client.post("/v1/search", json={"query": "billing node"})
    [hit] = resp.json()
    assert hit["feedback"] == {"helpful": 0, "stale": 1, "wrong": 0}


# --- reads by id -----------------------------------------------------------------


async def test_mcp_get_shows_another_agents_note() -> None:
    """The case the feature exists for: a lurker cannot supersede a fleet
    entry, so it flags it; the next reader sees the correction."""
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    result = await hive_get(mcp_app(store, WRITER), entry_id)
    feedback = result["feedback"]
    assert {k: feedback[k] for k in ("helpful", "stale", "wrong")} == {  # type: ignore[index]
        "helpful": 0,
        "stale": 1,
        "wrong": 0,
    }
    [report] = feedback["recent"]  # type: ignore[index]
    assert report["verdict"] == "stale"
    assert report["note"] == "billing moved to node 22"
    assert report["reporter"] == "lurker"
    assert report["updated_at"] is not None


async def test_rest_get_shows_another_agents_note() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    async with make_agent_client(store, WRITER) as client:
        resp = await client.get(f"/v1/entries/{entry_id}")
    feedback = resp.json()["feedback"]
    assert (feedback["helpful"], feedback["stale"], feedback["wrong"]) == (0, 1, 0)
    [report] = feedback["recent"]
    assert (report["verdict"], report["note"], report["reporter"]) == (
        "stale",
        "billing moved to node 22",
        "lurker",
    )


async def test_get_without_feedback_has_empty_reports() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(mcp_app(store, WRITER), kind="fact", summary="a fact")
    result = await hive_get(mcp_app(store, WRITER), entry["id"])
    assert result["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0, "recent": []}


async def test_get_returns_the_newest_reports_only_but_counts_all() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(mcp_app(store, WRITER), kind="fact", summary="a fact")
    total = FEEDBACK_RECENT_LIMIT + 2
    for i in range(total):
        await store.record_feedback(
            Feedback(
                entry_id=entry["id"],
                user=f"agent-{i}",
                agent=f"agent-{i}",
                verdict=Verdict.HELPFUL,
                note=f"note {i}",
                updated_at=T0 + timedelta(minutes=i),
            )
        )
    result = await hive_get(mcp_app(store, WRITER), entry["id"])
    feedback = result["feedback"]
    assert feedback["helpful"] == total  # type: ignore[index]
    reporters = [r["reporter"] for r in feedback["recent"]]  # type: ignore[index]
    assert reporters == [
        f"agent-{i}" for i in reversed(range(total - FEEDBACK_RECENT_LIMIT, total))
    ]


async def test_get_keeps_a_correction_after_five_later_helpful_reports() -> None:
    """ADR 0064: bare helpful reports do not push a stale note out of `recent`."""
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    for i in range(FEEDBACK_RECENT_LIMIT):
        await hive_feedback(
            mcp_app(store, agent_credential(f"fan-{i}", "fleet-a", TrustLevel.LURKER)),
            entry_id,
            verdict="helpful",
        )
    result = await hive_get(mcp_app(store, WRITER), entry_id)
    feedback = result["feedback"]
    assert feedback["helpful"] == FEEDBACK_RECENT_LIMIT  # type: ignore[index]
    recent = feedback["recent"]  # type: ignore[index]
    assert len(recent) == FEEDBACK_RECENT_LIMIT
    assert (recent[0]["verdict"], recent[0]["note"]) == ("stale", "billing moved to node 22")


async def test_a_reporters_latest_verdict_replaces_its_earlier_one() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    await hive_feedback(mcp_app(store, LURKER), entry_id, verdict="wrong", note="never was 20")
    result = await hive_get(mcp_app(store, WRITER), entry_id)
    [report] = result["feedback"]["recent"]  # type: ignore[index]
    assert (report["verdict"], report["note"]) == ("wrong", "never was 20")


async def test_feedback_stays_with_the_entrys_audience() -> None:
    """Feedback is shown only through a read of the entry, so a reader who
    may not read the entry sees neither the entry nor its notes."""
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    result = await hive_get(mcp_app(store, OUTSIDER), entry_id)
    assert result.get("error", {}).get("code") == "not_found"  # type: ignore[union-attr]
    async with make_agent_client(store, OUTSIDER) as client:
        resp = await client.get(f"/v1/entries/{entry_id}")
    assert resp.status_code == 404


async def test_memstore_lists_feedback_newest_first_with_reporter_tiebreak() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(mcp_app(store, WRITER), kind="fact", summary="a fact")
    for user, at in (("b", T0), ("a", T0), ("c", T0 + timedelta(seconds=1))):
        await store.record_feedback(
            Feedback(
                entry_id=entry["id"], user=user, agent=user, verdict=Verdict.STALE, updated_at=at
            )
        )
    rows = await store.list_feedback(entry["id"], 10)
    assert [r.user for r in rows] == ["c", "a", "b"]
    assert [r.user for r in await store.list_feedback(entry["id"], 1)] == ["c"]
    assert await store.list_feedback("no-such-entry", 10) == []


async def test_notes_are_stored_trimmed_and_a_blank_note_is_none() -> None:
    """Postgres's ``\\S`` counts a no-break space as a character; Python's
    ``strip`` does not. Notes are trimmed before they are stored, so a
    whitespace-only note is no note on either store adapter (no
    "last confirmed" date, no empty correction)."""
    store = MemoryStore(make_clock())
    entry = await hive_write(mcp_app(store, WRITER), kind="fact", summary="billing on node 20")
    await hive_feedback(
        mcp_app(store, LURKER), entry["id"], verdict="helpful", note="\u00a0 \u0085"
    )
    peer = agent_credential("peer", "fleet-a", TrustLevel.CONTRIBUTOR)
    await hive_feedback(mcp_app(store, peer), entry["id"], verdict="helpful", note=" ok ")
    rows = {fb.agent: fb.note for fb in await store.list_feedback(str(entry["id"]), 10)}
    assert rows == {"lurker": None, "peer": "ok"}
