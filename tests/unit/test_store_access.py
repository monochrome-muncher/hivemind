"""Unit tests for the access-control additions to the Store seam (ADR 0011-0012).

These pin the *new* Store behaviors so the Postgres adapter can be tested
against the same expectations (TDD at the seam):

* fleet management: ``create_fleet`` / ``list_fleets`` / ``get_fleet``
* agent registration / activation: ``register_agent`` / ``get_agent`` /
  ``activate_agent`` / ``set_agent_trust_level`` / ``set_agent_home_fleet``
* visibility-aware discovery reads: ``list_entries`` / ``search_keyword`` /
  ``search_vector`` accept an optional ``Visibility`` (the trust-level matrix
  in SPEC §12.2 / ADR 0011).

The in-memory store is the reference implementation of the ``Store`` port;
Postgres implements the same contract.
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import (
    AgentStatus,
    TrustLevel,
    Visibility,
)
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.memstore import MemoryStore
from tests.fakes import make_clock

FL_A = "fleet-a"
FL_B = "fleet-b"

VEC = [0.1, 0.2, 0.3]


def make_draft(
    summary: str,
    *,
    author: str = "alice",
    agent: str = "alice",
    scope: str = "org",
    fleet_id: str | None = None,
) -> EntryDraft:
    """A write request with an explicit scope + fleet (the access model)."""
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=author,
        agent=agent,
        scope=scope,
        fleet_id=fleet_id,
    )


def visibility(
    level: int, name: str, home: str | None = None, is_admin: bool = False
) -> Visibility:
    """A reader's ``Visibility`` from its trust level + home fleet."""
    return Visibility(level=TrustLevel(level), name=name, home_fleet_id=home, is_admin=is_admin)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(make_clock())


async def seed(
    store: MemoryStore,
    summary: str,
    *,
    author: str = "alice",
    agent: str = "alice",
    scope: str = "org",
    fleet_id: str | None = None,
) -> str:
    """Seed an entry (with an embedding so both search streams can see it)."""
    entry = await store.create_entry(
        make_draft(summary, author=author, agent=agent, scope=scope, fleet_id=fleet_id),
        VEC,
        embedding_model="fake",
    )
    return entry.id


async def _seed_matrix(store: MemoryStore) -> None:
    """The six-entry matrix the visibility tests assert against."""
    await seed(store, "weekly cohort churn", author="alice", agent="alice", scope="fleet", fleet_id=FL_A)
    await seed(store, "cohort retention", author="bob", agent="bob", scope="fleet", fleet_id=FL_A)
    await seed(store, "ml feature store", author="carol", agent="carol", scope="fleet", fleet_id=FL_B)
    await seed(store, "bob private note", author="bob", agent="bob", scope="self")
    await seed(store, "org-wide launch", author="dave", agent="dave", scope="org")
    await seed(store, "alice private note", author="alice", agent="alice", scope="self")


# --- fleet management -------------------------------------------------------


async def test_create_fleet_returns_record(store: MemoryStore) -> None:
    fleet = await store.create_fleet("data-eng")
    assert fleet.name == "data-eng"
    assert fleet.id  # a stable identifier is assigned

    fetched = await store.get_fleet(fleet.id)
    assert fetched is not None and fetched.name == "data-eng"


async def test_create_fleet_rejects_duplicate_name(store: MemoryStore) -> None:
    await store.create_fleet("data-eng")
    with pytest.raises(ValueError):
        await store.create_fleet("data-eng")


async def test_list_fleets(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    fb = await store.create_fleet("ml")
    names = {f.name for f in await store.list_fleets()}
    assert names == {"data-eng", "ml"}
    assert fa.id != fb.id


async def test_get_fleet_missing_returns_none(store: MemoryStore) -> None:
    assert await store.get_fleet("nope") is None


# --- agent registration / activation ----------------------------------------


async def test_register_agent_creates_pending(store: MemoryStore) -> None:
    agent = await store.register_agent("alice", owner_alias="alice@example.com")
    assert agent.name == "alice"
    assert agent.status is AgentStatus.PENDING
    assert agent.trust_level is TrustLevel.UNTRUSTED
    assert agent.home_fleet_id is None
    assert agent.owner_alias == "alice@example.com"


async def test_register_agent_idempotent_for_pending(store: MemoryStore) -> None:
    first = await store.register_agent("alice", owner_alias="alice@example.com")
    again = await store.register_agent("alice")
    assert again.name == first.name
    assert again.status is AgentStatus.PENDING
    # idempotent: still exactly one agent named "alice"
    assert len(await store.list_agents()) == 1


async def test_register_agent_returns_existing_active(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    await store.register_agent("alice")
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id)
    # Re-registering an active name returns the existing active record.
    again = await store.register_agent("alice")
    assert again.status is AgentStatus.ACTIVE
    assert again.trust_level is TrustLevel.LURKER


async def test_activate_agent_sets_level_and_fleet(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    await store.register_agent("alice")
    active = await store.activate_agent(
        "alice", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fa.id
    )
    assert active.status is AgentStatus.ACTIVE
    assert active.trust_level is TrustLevel.CONTRIBUTOR
    assert active.home_fleet_id == fa.id
    assert active.activated_at is not None
    # The stored record reflects the activation.
    stored = await store.get_agent("alice")
    assert stored is not None and stored.status is AgentStatus.ACTIVE


async def test_activate_agent_unknown_raises(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    with pytest.raises(KeyError):
        await store.activate_agent(
            "ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id
        )


async def test_set_agent_trust_level_demotes(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    await store.register_agent("alice")
    await store.activate_agent("alice", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fa.id)
    demoted = await store.set_agent_trust_level("alice", TrustLevel.UNTRUSTED)
    assert demoted.trust_level is TrustLevel.UNTRUSTED
    # Still ACTIVE (dormant), just level 0 (SPEC §12.3: demotion != revocation).
    assert demoted.status is AgentStatus.ACTIVE


async def test_set_agent_home_fleet(store: MemoryStore) -> None:
    fa = await store.create_fleet("data-eng")
    fb = await store.create_fleet("ml")
    await store.register_agent("alice")
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id)
    moved = await store.set_agent_home_fleet("alice", fb.id)
    assert moved.home_fleet_id == fb.id


# --- visibility-aware discovery -----------------------------------------------


async def test_list_entries_visibility_lurker(store: MemoryStore) -> None:
    await _seed_matrix(store)
    hits = await store.list_entries(EntryFilters(), visibility=visibility(1, "alice", home=FL_A))
    authors_scopes = {(e.author, e.scope) for e in hits}
    # Lurker alice (home=fA): own fleet (alice/fA), home-fleet teammate
    # (bob/fA), legacy org (dave/org), own self (alice/self). NOT:
    # other-fleet (carol/fB) or bob's self.
    assert ("alice", "fleet") in authors_scopes
    assert ("bob", "fleet") in authors_scopes
    assert ("dave", "org") in authors_scopes
    assert ("alice", "self") in authors_scopes
    assert ("carol", "fleet") not in authors_scopes
    assert ("bob", "self") not in authors_scopes


async def test_list_entries_visibility_untrusted_sees_nothing(store: MemoryStore) -> None:
    await _seed_matrix(store)
    hits = await store.list_entries(EntryFilters(), visibility=visibility(0, "alice", home=FL_A))
    assert hits == []


async def test_list_entries_visibility_privileged_reads_all_fleets(store: MemoryStore) -> None:
    await _seed_matrix(store)
    hits = await store.list_entries(EntryFilters(), visibility=visibility(3, "alice", home=FL_A))
    authors = {e.author for e in hits}
    # L3 reads every fleet's fleet-scoped entries + org, but NOT others' `self`.
    assert {"alice", "bob", "carol", "dave"} <= authors
    self_authors = {e.author for e in hits if e.scope == "self"}
    assert self_authors == {"alice"}  # only alice's own self (bob's self is private)


async def test_list_entries_visibility_admin_sees_all(store: MemoryStore) -> None:
    await _seed_matrix(store)
    hits = await store.list_entries(EntryFilters(), visibility=visibility(0, "root", is_admin=True))
    assert len(hits) == 6  # everything, including bob's private self


async def test_list_entries_without_visibility_is_unfiltered(store: MemoryStore) -> None:
    await _seed_matrix(store)
    hits = await store.list_entries(EntryFilters())  # v1 behavior: the flat pool
    assert len(hits) == 6


async def test_search_keyword_respects_visibility(store: MemoryStore) -> None:
    await _seed_matrix(store)
    lurker = await store.search_keyword(
        "cohort", EntryFilters(), 10, visibility=visibility(1, "alice", home=FL_A)
    )
    # Only fleet-A / org / own-self matches surface (no fleet-B, no bob-self).
    lurker_entries = await store.get_entries(lurker)
    summaries = " ".join(e.summary for e in lurker_entries.values())
    assert "cohort" in summaries
    # A privileged reader sees strictly at least as many.
    priv = await store.search_keyword(
        "cohort", EntryFilters(), 10, visibility=visibility(3, "alice", home=FL_A)
    )
    assert len(priv) >= len(lurker)


async def test_search_vector_respects_visibility(store: MemoryStore) -> None:
    await _seed_matrix(store)
    lurker = await store.search_vector(
        VEC, EntryFilters(), 10, visibility=visibility(1, "alice", home=FL_A)
    )
    priv = await store.search_vector(
        VEC, EntryFilters(), 10, visibility=visibility(3, "alice", home=FL_A)
    )
    # Privileged (all fleets) sees a superset of the lurker's (home fleet only).
    assert set(lurker) <= set(priv)