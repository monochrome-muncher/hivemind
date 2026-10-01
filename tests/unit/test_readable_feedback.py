"""Feedback is readable (ADR 0051): search hits carry the verdict counts,
and a read by id carries the counts plus the newest reports with their
notes, on both surfaces, to exactly the audience that can read the entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.feedback import FEEDBACK_RECENT_LIMIT, Feedback, Verdict
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_get, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, fleet: str, level: TrustLevel) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


WRITER = _agent("writer", "fleet-a", TrustLevel.CONTRIBUTOR)
LURKER = _agent("lurker", "fleet-a", TrustLevel.LURKER)
OUTSIDER = _agent("outsider", "fleet-b", TrustLevel.CONTRIBUTOR)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


def _rest_client(store: MemoryStore, credential: Credential) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": credential}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    return httpx.AsyncClient(transport=transport, base_url="http://t", headers={"X-API-Key": "k"})


async def _flagged_entry(store: MemoryStore) -> str:
    """A fleet entry the lurker has reported stale, with a correction."""
    entry = await hive_write(
        _surface(store, WRITER), kind="fact", summary="billing runs on node 20"
    )
    result = await hive_feedback(
        _surface(store, LURKER), entry["id"], verdict="stale", note="billing moved to node 22"
    )
    assert "error" not in result
    return str(entry["id"])


# --- search hits -----------------------------------------------------------------


async def test_mcp_hit_without_feedback_has_zero_counts() -> None:
    store = MemoryStore(make_clock())
    await hive_write(_surface(store, WRITER), kind="fact", summary="billing runs on node 20")
    result = await hive_search(_surface(store, WRITER), query="billing node")
    [hit] = result["hits"]  # type: ignore[index]
    assert hit["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0}


async def test_mcp_hit_carries_feedback_counts() -> None:
    store = MemoryStore(make_clock())
    await _flagged_entry(store)
    result = await hive_search(_surface(store, WRITER), query="billing node")
    [hit] = result["hits"]  # type: ignore[index]
    assert hit["feedback"] == {"helpful": 0, "stale": 1, "wrong": 0}


async def test_rest_hit_carries_feedback_counts() -> None:
    store = MemoryStore(make_clock())
    await _flagged_entry(store)
    async with _rest_client(store, WRITER) as client:
        resp = await client.post("/v1/search", json={"query": "billing node"})
    [hit] = resp.json()
    assert hit["feedback"] == {"helpful": 0, "stale": 1, "wrong": 0}


# --- reads by id -----------------------------------------------------------------


async def test_mcp_get_shows_another_agents_note() -> None:
    """The case the feature exists for: a lurker cannot supersede a fleet
    entry, so it flags it; the next reader sees the correction."""
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    result = await hive_get(_surface(store, WRITER), entry_id)
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
    async with _rest_client(store, WRITER) as client:
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
    entry = await hive_write(_surface(store, WRITER), kind="fact", summary="a fact")
    result = await hive_get(_surface(store, WRITER), entry["id"])
    assert result["feedback"] == {"helpful": 0, "stale": 0, "wrong": 0, "recent": []}


async def test_get_returns_the_newest_reports_only_but_counts_all() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(_surface(store, WRITER), kind="fact", summary="a fact")
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
    result = await hive_get(_surface(store, WRITER), entry["id"])
    feedback = result["feedback"]
    assert feedback["helpful"] == total  # type: ignore[index]
    reporters = [r["reporter"] for r in feedback["recent"]]  # type: ignore[index]
    assert reporters == [f"agent-{i}" for i in reversed(range(2, total))]


async def test_a_reporters_latest_verdict_replaces_its_earlier_one() -> None:
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    await hive_feedback(_surface(store, LURKER), entry_id, verdict="wrong", note="never was 20")
    result = await hive_get(_surface(store, WRITER), entry_id)
    [report] = result["feedback"]["recent"]  # type: ignore[index]
    assert (report["verdict"], report["note"]) == ("wrong", "never was 20")


async def test_feedback_stays_with_the_entrys_audience() -> None:
    """Feedback is shown only through a read of the entry, so a reader who
    may not read the entry sees neither the entry nor its notes."""
    store = MemoryStore(make_clock())
    entry_id = await _flagged_entry(store)
    result = await hive_get(_surface(store, OUTSIDER), entry_id)
    assert result.get("error", {}).get("code") == "not_found"  # type: ignore[union-attr]
    async with _rest_client(store, OUTSIDER) as client:
        resp = await client.get(f"/v1/entries/{entry_id}")
    assert resp.status_code == 404


async def test_memstore_lists_feedback_newest_first_with_reporter_tiebreak() -> None:
    store = MemoryStore(make_clock())
    entry = await hive_write(_surface(store, WRITER), kind="fact", summary="a fact")
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
