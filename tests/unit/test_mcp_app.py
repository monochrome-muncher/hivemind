"""Unit tests for the six Hivemind MCP tool functions (SPEC §5.2).

Seams under test: the plain tool functions in ``hivemind.mcp.app``.
Each is exercised against a ``MemoryStore`` + ``FakeEmbedder`` (via
``tests.fakes``) so the tests sit at the service/store seam, not inside
the MCP transport. The acting identity is a stub ``Credential``.
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntityKind, EntryDraft, ExtractedEntity, Kind
from hivemind.mcp.app import (
    ERR_AGENT_UNRESOLVED,
    ERR_INVALID_INPUT,
    ERR_PERMISSION_DENIED,
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_list,
    hive_register,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config

ALICE = Credential(user_id="alice", agent_id="agent-1")
BOB = Credential(user_id="bob", agent_id="agent-2")
ADMIN = Credential(user_id="ops", agent_id="admin-agent", is_admin=True)
ORG = Credential(user_id="org", is_org=True, access_controlled=True)


def build_app(store: MemoryStore, credential: Credential, clock) -> McpHivemind:
    """Assemble a ``McpHivemind`` over a shared store (deterministic clock)."""
    embedder = make_embedder()
    config = make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
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


# --- write-scope resolution (ADR 0011): an omitted scope defaults to the
# --- highest scope the trust level permits, not a forced 'org' ---------- #


async def _l2_agent(store: MemoryStore, clock, name: str = "carol") -> tuple[Credential, str]:
    """A v2 L2 (contributor) agent with a home fleet (ADRs 0011-0012)."""
    fleet = await store.create_fleet("eng")
    await store.register_agent(name)
    await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    cred = Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet.id,
    )
    return cred, fleet.id


async def test_hive_write_omitted_scope_defaults_to_max_permitted() -> None:
    """ADR 0011: an omitted scope defaults to the highest scope the trust
    level permits (L2 -> fleet). The tool must not force 'org' — that
    rejected every L2/L1 write (dogfood finding 1)."""
    clock = make_clock()
    store = MemoryStore(clock)
    cred, fleet_id = await _l2_agent(store, clock)
    app = build_app(store, cred, clock)
    result = await hive_write(app, kind="fact", summary="omitted scope probe")
    assert "error" not in result, result
    assert result["scope"] == "fleet"
    assert result["fleet_id"] == fleet_id


async def test_hive_write_l2_explicit_org_scope_denied() -> None:
    """An *explicit* out-of-permission scope is still rejected (ADR 0011)."""
    clock = make_clock()
    store = MemoryStore(clock)
    cred, _ = await _l2_agent(store, clock)
    app = build_app(store, cred, clock)
    result = await hive_write(app, kind="fact", summary="org probe", scope="org")
    assert result["error"]["code"] == ERR_PERMISSION_DENIED


async def test_hive_write_legacy_omitted_scope_stays_org() -> None:
    """Legacy (v1) credentials keep the flat pool: omitted scope -> org."""
    clock = make_clock()
    store = MemoryStore(clock)
    app = build_app(store, ALICE, clock)
    result = await hive_write(app, kind="fact", summary="legacy probe")
    assert "error" not in result
    assert result["scope"] == "org"


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
    written = await hive_write(app, kind="insight", summary="Analysis", body="Long body text")
    fetched = await hive_get(app, written["id"])
    assert "error" not in fetched
    assert fetched["body"] == "Long body text"
    assert fetched["id"] == written["id"]


async def test_hive_get_unknown_entry_returns_error(app: McpHivemind) -> None:
    result = await hive_get(app, "no-such-id")
    assert result.get("error", {}).get("code") == "not_found"


async def test_hive_get_empty_entry_id_is_invalid_input(app: McpHivemind) -> None:
    """An empty id is a caller error, not 'unknown entry: ' (dogfood note)."""
    result = await hive_get(app, entry_id="")
    assert result["error"]["code"] == ERR_INVALID_INPUT
    assert "entry_id" in result["error"]["message"]


async def test_hive_feedback_empty_entry_id_is_invalid_input(app: McpHivemind) -> None:
    """Same guard on hive_feedback: an empty id is invalid_input, not not_found."""
    result = await hive_feedback(app, entry_id="", verdict="helpful")
    assert result["error"]["code"] == ERR_INVALID_INPUT
    assert "entry_id" in result["error"]["message"]


async def test_hive_get_include_history_surfaces_successor(
    app: McpHivemind,
) -> None:
    old = await hive_write(app, kind="fact", summary="v1: 15 min expiry")
    new = await hive_write(app, kind="fact", summary="v2: 30 min expiry", supersedes=[old["id"]])
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
    decision = await hive_write(app, kind="decision", summary="decision x", tags=["x"])
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


# --------------------------------------------------------------------------- #
# Review-fix seams (SPEC.md §5.2/§5.3/§8.1)
# --------------------------------------------------------------------------- #
async def test_hive_write_user_key_without_agent_is_rejected() -> None:
    """SPEC.md §8.1: a plain user key must self-report the agent; no
    fabricated ``unknown`` identity (n3)."""
    clock = make_clock()
    store = MemoryStore(clock)
    user_key_app = build_app(
        store, Credential(user_id="alice"), clock
    )  # plain user key: no agent_id
    result = await hive_write(user_key_app, kind="fact", summary="needs an agent")
    assert result.get("error", {}).get("code") == ERR_AGENT_UNRESOLVED


async def test_hive_write_user_key_with_self_reported_agent() -> None:
    clock = make_clock()
    store = MemoryStore(clock)
    user_key_app = build_app(store, Credential(user_id="alice"), clock)
    result = await hive_write(
        user_key_app, kind="fact", summary="self-reported", agent="claude-code"
    )
    assert "error" not in result
    assert result["agent"] == "claude-code"


async def test_hive_feedback_user_key_self_reports_agent(app: McpHivemind) -> None:
    """SPEC.md §8.1: a plain user key self-reports the agent (m5)."""
    clock = make_clock()
    store = app.store
    user_key_app = build_app(store, Credential(user_id="alice"), clock)
    entry = await hive_write(user_key_app, kind="fact", summary="a fact", agent="claude-code")
    result = await hive_feedback(user_key_app, entry["id"], verdict="helpful", agent="claude-code")
    assert "error" not in result
    assert result["feedback"]["agent"] == "claude-code"


async def test_hive_feedback_user_key_without_agent_is_rejected(app: McpHivemind) -> None:
    clock = make_clock()
    store = app.store
    user_key_app = build_app(store, Credential(user_id="alice"), clock)
    entry = await hive_write(user_key_app, kind="fact", summary="a fact", agent="claude-code")
    result = await hive_feedback(user_key_app, entry["id"], verdict="stale")
    assert result.get("error", {}).get("code") == ERR_AGENT_UNRESOLVED


async def test_hive_search_offset_paginates(app: McpHivemind) -> None:
    """SPEC.md §5.3: limit/offset on search (parity with list)."""
    for i in range(5):
        await hive_write(app, kind="fact", summary=f"cohort note number {i}")
    result = await hive_search(app, "cohort note", limit=2, offset=1)
    assert result["count"] == 2


# --------------------------------------------------------------------------- #
# hive_search / hive_list: entity facets (ADR 0016, SPEC §13) ----------------- #


async def _seed_entity_entries(app: McpHivemind) -> dict[str, str]:
    """Seed two entries carrying machine-extracted entity facets (ADR 0016)."""
    a = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Postgres connection pool exhausted",
            author="alice",
            agent="agent-1",
        ),
        entities=(
            ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),
            ExtractedEntity(name="auth-service", kind=EntityKind.SERVICE),
        ),
        entities_model="test-extractor",
    )
    b = await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Kubernetes autoscaler misconfigured",
            author="alice",
            agent="agent-1",
        ),
        entities=(ExtractedEntity(name="Kubernetes", kind=EntityKind.SYSTEM),),
        entities_model="test-extractor",
    )
    return {"a": a.id, "b": b.id}


async def test_hive_search_filters_by_entities(app: McpHivemind) -> None:
    """ADR 0016 / SPEC §13: filter by machine-extracted entity names
    (AND-semantics, case-insensitive)."""
    ids = await _seed_entity_entries(app)
    result = await hive_search(app, query="postgres pool", entities=["postgres"])
    assert [h["id"] for h in result["hits"]] == [ids["a"]]
    # Filter names are case-insensitive against the stored names.
    upper = await hive_search(app, query="postgres pool", entities=["POSTGRES"])
    assert [h["id"] for h in upper["hits"]] == [ids["a"]]
    # AND-semantics: every listed name must be on the entry.
    none = await hive_search(app, query="pool", entities=["postgres", "kubernetes"])
    assert none["hits"] == []


async def test_hive_list_filters_by_entities(app: McpHivemind) -> None:
    ids = await _seed_entity_entries(app)
    result = await hive_list(app, entities=["auth-service"])
    assert [e["id"] for e in result["entries"]] == [ids["a"]]
    missing = await hive_list(app, entities=["postgres", "kubernetes"])
    assert missing["entries"] == []


async def test_hive_get_exposes_entities_and_provenance(app: McpHivemind) -> None:
    """The MCP entry payload carries the facets + the extractor model
    that produced them (symmetric with ``embedding_model``)."""
    ids = await _seed_entity_entries(app)
    fetched = await hive_get(app, ids["a"])
    assert fetched["entities"] == [
        {"name": "Postgres", "kind": "system"},
        {"name": "auth-service", "kind": "service"},
    ]
    assert fetched["entities_model"] == "test-extractor"


async def test_hive_get_without_extraction_defaults_to_empty(app: McpHivemind) -> None:
    written = await hive_write(app, kind="fact", summary="a plain entry")
    fetched = await hive_get(app, written["id"])
    assert fetched["entities"] == []
    assert fetched["entities_model"] is None


async def test_hive_search_hits_do_not_carry_entities(app: McpHivemind) -> None:
    """Progressive disclosure (SPEC §6.1): compact hits stay slim —
    the facets live on the full entry, not the hit."""
    await _seed_entity_entries(app)
    result = await hive_search(app, query="postgres pool")
    assert result["hits"]
    for hit in result["hits"]:
        assert "entities" not in hit
        assert "entities_model" not in hit


class TestHiveRegister:
    """The hive_register tool (ADR 0012): agent registration, org/admin-gated."""

    async def test_register_with_org_key_creates_pending(self) -> None:
        clock = make_clock()
        store = MemoryStore(clock)
        app = build_app(store, ORG, clock)
        result = await hive_register(app, "alice")
        assert "error" not in result
        assert result["name"] == "alice"
        assert result["status"] == "pending"
        assert result["trust_level"] == 0

    async def test_register_with_admin_key_denied(self) -> None:
        # SPEC §5.2: the MCP hive_register verb is org-key only (the REST
        # surface also accepts an admin key; the MCP verb does not).
        clock = make_clock()
        store = MemoryStore(clock)
        app = build_app(store, ADMIN, clock)
        result = await hive_register(app, "bob")
        assert result["error"]["code"] == ERR_PERMISSION_DENIED

    async def test_register_with_plain_agent_key_denied(self) -> None:
        # A plain agent key (not org/admin) may not register (ADR 0012).
        clock = make_clock()
        store = MemoryStore(clock)
        app = build_app(store, ALICE, clock)
        result = await hive_register(app, "alice")
        assert result["error"]["code"] == ERR_PERMISSION_DENIED

    async def test_register_active_name_conflicts(self) -> None:
        clock = make_clock()
        store = MemoryStore(clock)
        await store.register_agent("alice")
        fleet = await store.create_fleet("data-eng")
        await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
        app = build_app(store, ORG, clock)
        result = await hive_register(app, "alice")
        assert result["error"]["code"] == "name_conflict"
