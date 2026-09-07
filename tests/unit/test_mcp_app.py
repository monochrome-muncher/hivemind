"""Unit tests for the six Hivemind MCP tool functions (SPEC §5.2).

Seams under test: the plain tool functions in ``hivemind.mcp.app``.
Each is exercised against a ``MemoryStore`` + ``FakeEmbedder`` (via
``tests.fakes``) so the tests sit at the service/store seam, not inside
the MCP transport. The acting identity is a stub ``Credential``.
"""

from __future__ import annotations

import pytest

from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_list,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config

ALICE = Credential(user_id="alice", agent_id="agent-1")
BOB = Credential(user_id="bob", agent_id="agent-2")
ADMIN = Credential(user_id="ops", agent_id="admin-agent", is_admin=True)


def build_app(
    store: MemoryStore, credential: Credential, clock
) -> McpHivemind:
    """Assemble a ``McpHivemind`` over a shared store (deterministic clock)."""
    embedder = make_embedder()
    config = make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        search_config=config,
        credential=credential,
    )


@pytest.fixture
def clock():
    return make_clock()


@pytest.fixture
def store(clock) -> MemoryStore:
    return MemoryStore(clock)


@pytest.fixture
def app(store, clock) -> McpHivemind:
    return build_app(store, ALICE, clock)


# --------------------------------------------------------------------------- #
# hive_write
# --------------------------------------------------------------------------- #


async def test_hive_write_persists_entry(app: McpHivemind) -> None:
    result = await hive_write(
        app, kind="fact", summary="Auth uses JWT", body="Tokens expire in 15 min"
    )
    assert "error" not in result
    assert result["kind"] == "fact"
    assert result["state"] == "active"
    assert result["author"] == "alice"  # from the acting credential
    assert result["agent"] == "agent-1"

    stored = await app.store.get_entry(result["id"])
    assert stored is not None
    assert stored.summary == "Auth uses JWT"
    assert stored.body == "Tokens expire in 15 min"
    assert stored.kind.value == "fact"


async def test_hive_write_supersedes_target(app: McpHivemind) -> None:
    first = await hive_write(app, kind="fact", summary="Token expiry 15 min")
    second = await hive_write(
        app, kind="fact", summary="Token expiry 30 min", supersedes=[first["id"]]
    )
    assert "error" not in second
    old = await app.store.get_entry(first["id"])
    assert old is not None
    assert old.state.value == "superseded"
    assert old.superseded_by == second["id"]


async def test_hive_write_invalid_kind_returns_error(app: McpHivemind) -> None:
    result = await hive_write(app, kind="bogus", summary="x")
    assert result.get("error", {}).get("code") == "invalid_input"


async def test_hive_write_requires_non_empty_summary(app: McpHivemind) -> None:
    result = await hive_write(app, kind="fact", summary="   ")
    assert result.get("error", {}).get("code") == "invalid_input"


async def test_hive_write_accepts_structured_fields(app: McpHivemind) -> None:
    result = await hive_write(
        app,
        kind="insight",
        summary="Cohort churn analysis",
        body="Weekly cohorts beat daily.",
        payload={"metric": "churn"},
        sources=[{"type": "url", "ref": "https://example.com/slice"}],
        tags=["churn", "retention"],
        importance=4,
    )
    assert "error" not in result
    assert result["payload"] == {"metric": "churn"}
    assert result["tags"] == ["churn", "retention"]
    assert result["importance"] == 4
    assert result["sources"] == [{"type": "url", "ref": "https://example.com/slice"}]


# --------------------------------------------------------------------------- #
# hive_search
# --------------------------------------------------------------------------- #


async def test_hive_search_returns_compact_hits(app: McpHivemind) -> None:
    jwt = await hive_write(app, kind="fact", summary="JWT auth uses short tokens")
    await hive_write(app, kind="insight", summary="Churn model uses weekly cohorts")

    result = await hive_search(app, query="jwt auth")
    assert "error" not in result
    hits = result["hits"]
    assert len(hits) >= 1
    assert any(h["id"] == jwt["id"] for h in hits)
    # Progressive disclosure: compact hits carry no body (SPEC §6.1).
    for hit in hits:
        assert "body" not in hit
        assert "id" in hit and "summary" in hit and "score" in hit
        assert "author" in hit and "occurred_at" in hit


async def test_hive_search_respects_kind_filter(app: McpHivemind) -> None:
    await hive_write(app, kind="fact", summary="Weekly cohorts fact", tags=["cohort"])
    decision = await hive_write(
        app, kind="decision", summary="Keep weekly cohorts", tags=["cohort"]
    )
    result = await hive_search(app, query="weekly cohorts", kind="decision")
    ids = [h["id"] for h in result["hits"]]
    assert decision["id"] in ids
    assert all(h["kind"] == "decision" for h in result["hits"])


# --------------------------------------------------------------------------- #
# hive_get
# --------------------------------------------------------------------------- #


async def test_hive_get_returns_full_entry(app: McpHivemind) -> None:
    written = await hive_write(
        app, kind="insight", summary="Analysis", body="Long body text"
    )
    fetched = await hive_get(app, written["id"])
    assert "error" not in fetched
    assert fetched["body"] == "Long body text"
    assert fetched["id"] == written["id"]


async def test_hive_get_unknown_entry_returns_error(app: McpHivemind) -> None:
    result = await hive_get(app, "no-such-id")
    assert result.get("error", {}).get("code") == "not_found"


async def test_hive_get_include_history_surfaces_successor(
    app: McpHivemind,
) -> None:
    old = await hive_write(app, kind="fact", summary="v1: 15 min expiry")
    new = await hive_write(
        app, kind="fact", summary="v2: 30 min expiry", supersedes=[old["id"]]
    )
    # The successor lists the entry it superseded.
    with_history = await hive_get(app, new["id"], include_history=True)
    assert "error" not in with_history
    assert [e["id"] for e in with_history["superseded"]] == [old["id"]]
    # The older entry lists its successor.
    old_history = await hive_get(app, old["id"], include_history=True)
    assert [e["id"] for e in old_history["successors"]] == [new["id"]]


# --------------------------------------------------------------------------- #
# hive_list
# --------------------------------------------------------------------------- #


async def test_hive_list_filters_by_kind_and_tags(app: McpHivemind) -> None:
    await hive_write(app, kind="fact", summary="fact x", tags=["x"])
    decision = await hive_write(
        app, kind="decision", summary="decision x", tags=["x"]
    )
    result = await hive_list(app, kind="fact", tags=["x"])
    assert result["count"] == 1
    assert result["entries"][0]["id"] is not None
    assert all(e["kind"] == "fact" for e in result["entries"])
    assert "decision" not in {e["kind"] for e in result["entries"]}
    # The decision entry is still present in the pool, just filtered out.
    assert decision["id"] not in [e["id"] for e in result["entries"]]


async def test_hive_list_is_paginated(app: McpHivemind) -> None:
    for i in range(4):
        await hive_write(app, kind="fact", summary=f"entry {i}")
    page = await hive_list(app, limit=2, offset=0)
    assert page["count"] == 2
    second = await hive_list(app, limit=2, offset=2)
    assert second["count"] == 2
    ids = [e["id"] for e in page["entries"]] + [e["id"] for e in second["entries"]]
    assert len(set(ids)) == 4


# --------------------------------------------------------------------------- #
# hive_withdraw
# --------------------------------------------------------------------------- #


async def test_hive_withdraw_enforces_author_rule(store: MemoryStore, clock) -> None:
    alice_app = build_app(store, ALICE, clock)
    bob_app = build_app(store, BOB, clock)
    admin_app = build_app(store, ADMIN, clock)

    mine = await hive_write(alice_app, kind="fact", summary="alice's entry")
    # The author may withdraw their own entry.
    ok = await hive_withdraw(alice_app, mine["id"], reason="no longer valid")
    assert "error" not in ok
    assert ok["state"] == "withdrawn"
    assert ok["withdrawn_reason"] == "no longer valid"

    # A non-author, non-admin may not.
    other = await hive_write(alice_app, kind="fact", summary="alice's other entry")
    denied = await hive_withdraw(bob_app, other["id"])
    assert denied.get("error", {}).get("code") == "permission_denied"

    # An admin may withdraw any entry.
    admin_target = await hive_write(alice_app, kind="fact", summary="third")
    allowed = await hive_withdraw(admin_app, admin_target["id"])
    assert "error" not in allowed
    assert allowed["state"] == "withdrawn"


async def test_hive_withdraw_unknown_entry_returns_error(app: McpHivemind) -> None:
    result = await hive_withdraw(app, "no-such-id")
    assert result.get("error", {}).get("code") == "not_found"


# --------------------------------------------------------------------------- #
# hive_feedback
# --------------------------------------------------------------------------- #


async def test_hive_feedback_records_verdict(app: McpHivemind) -> None:
    entry = await hive_write(app, kind="fact", summary="a fact")
    result = await hive_feedback(app, entry["id"], verdict="helpful", note="useful")
    assert "error" not in result
    assert result["verdict"] == "helpful"
    assert "quality" in result
    # (helpful, stale, wrong)
    assert await app.store.feedback_counts(entry["id"]) == (1, 0, 0)


async def test_hive_feedback_rejects_unknown_verdict(app: McpHivemind) -> None:
    entry = await hive_write(app, kind="fact", summary="a fact")
    result = await hive_feedback(app, entry["id"], verdict="maybe")
    assert result.get("error", {}).get("code") == "invalid_verdict"


async def test_hive_feedback_unknown_entry_returns_error(app: McpHivemind) -> None:
    result = await hive_feedback(app, "no-such-id", verdict="helpful")
    assert result.get("error", {}).get("code") == "not_found"