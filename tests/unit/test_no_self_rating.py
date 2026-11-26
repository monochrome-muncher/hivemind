"""An author cannot rate its own entry (ADR 0065): refused on both
surfaces, single and batch, with nothing recorded; readability first."""

from __future__ import annotations

from hivemind.mcp.app import hive_feedback, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from tests.builders import mcp_app
from tests.fakes import (
    agent_credential,
    make_agent_client,
    make_clock,
)

ME = agent_credential("me", "fleet-a")
PEER = agent_credential("peer", "fleet-a")
OUTSIDER = agent_credential("outsider", "fleet-b")


async def _write(store: MemoryStore, credential: Credential, summary: str) -> str:
    return str((await hive_write(mcp_app(store, credential), kind="fact", summary=summary))["id"])


async def test_mcp_single_own_entry_is_refused() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    result = await hive_feedback(mcp_app(store, ME), mine, verdict="helpful")
    assert result["error"]["code"] == "permission_denied"  # type: ignore[index]
    assert "ADR 0065" in result["error"]["message"]  # type: ignore[index]
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_mcp_someone_elses_entry_still_works() -> None:
    store = MemoryStore(make_clock())
    theirs = await _write(store, PEER, "theirs")
    result = await hive_feedback(mcp_app(store, ME), theirs, verdict="helpful")
    assert "error" not in result
    assert await store.feedback_counts(theirs) == (1, 0, 0)


async def test_mcp_batch_with_an_own_entry_records_nothing() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, PEER, "theirs")
    result = await hive_feedback(mcp_app(store, ME), verdict="wrong", entry_ids=[theirs, mine])
    assert result["error"]["code"] == "permission_denied"  # type: ignore[index]
    assert mine in result["error"]["message"]  # type: ignore[index]
    assert theirs not in result["error"]["message"]  # type: ignore[index]
    assert await store.feedback_counts(theirs) == (0, 0, 0)
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_batch_of_only_others_entries_works() -> None:
    store = MemoryStore(make_clock())
    ids = [await _write(store, PEER, f"fact {i}") for i in range(2)]
    result = await hive_feedback(mcp_app(store, ME), verdict="helpful", entry_ids=ids)
    assert "error" not in result
    for entry_id in ids:
        assert await store.feedback_counts(entry_id) == (1, 0, 0)


async def test_an_unreadable_id_is_still_not_found_beside_an_own_one() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, OUTSIDER, "elsewhere")
    result = await hive_feedback(mcp_app(store, ME), verdict="helpful", entry_ids=[mine, theirs])
    assert result["error"]["code"] == "not_found"  # type: ignore[index]
    single = await hive_feedback(mcp_app(store, ME), theirs, verdict="helpful")
    assert single["error"]["code"] == "not_found"  # type: ignore[index]


async def test_rest_single_own_entry_is_403() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    async with make_agent_client(store, ME) as client:
        resp = await client.post(f"/v1/entries/{mine}/feedback", json={"verdict": "helpful"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert "ADR 0065" in resp.json()["error"]["message"]
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_rest_someone_elses_entry_still_works() -> None:
    store = MemoryStore(make_clock())
    theirs = await _write(store, PEER, "theirs")
    async with make_agent_client(store, ME) as client:
        resp = await client.post(f"/v1/entries/{theirs}/feedback", json={"verdict": "helpful"})
    assert resp.status_code == 200


async def test_rest_batch_with_an_own_entry_is_403_and_records_nothing() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, PEER, "theirs")
    async with make_agent_client(store, ME) as client:
        resp = await client.post(
            "/v1/feedback", json={"entry_ids": [theirs, mine], "verdict": "wrong"}
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert mine in resp.json()["error"]["message"]
    assert await store.feedback_counts(theirs) == (0, 0, 0)
    assert await store.feedback_counts(mine) == (0, 0, 0)
