"""Postgres-backed integration tests for the access-control Store methods.

These validate the *new* ``PgStore`` methods (fleet management, agent
registration/activation, and visibility-aware discovery) against the live
dev Postgres (ADR 0011-0012), mirroring ``test_pgstore.py``'s
hermetic-by-construction pattern (skip when the DB is unreachable;
TRUNCATE between tests). They prove the access-control SQL (the trust-level
matrix in SPEC §12.2 / ADR 0011) works in Postgres, not just in the
in-memory reference store.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    InvalidAgentStatus,
    TrustLevel,
    Visibility,
)
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

VEC_DIM = Settings().embedding_dim
VEC = [0.01] * VEC_DIM  # a fixed-dimension vector (spike not needed here)
VEC[3] = 1.0


def _dsn() -> str:
    return Settings().database_url


async def _truncate_access(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("TRUNCATE fleets, agents, entries, feedbacks, credentials")
    finally:
        await conn.close()


@pytest.fixture
async def pg():
    """A ready ``PgStore`` on a clean dev DB (skips when Postgres is down)."""
    dsn = _dsn()
    dim = Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, dim)
    await _truncate_access(dsn)
    store = PgStore(dsn)
    try:
        yield store
    finally:
        await _truncate_access(dsn)
        await store.close()


async def _seed(
    store: PgStore,
    summary: str,
    *,
    author: str = "alice",
    agent: str = "alice",
    scope: str = "org",
    fleet_id: str | None = None,
) -> None:
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=author,
        agent=agent,
        scope=scope,
        fleet_id=fleet_id,
    )
    await store.create_entry(draft, VEC, embedding_model="fake")


# --- fleet management -------------------------------------------------------


async def test_create_and_list_fleets(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    fb = await pg.create_fleet("ml")
    assert fa.name == "data-eng" and fb.name == "ml"
    assert fa.id != fb.id
    names = {f.name for f in await pg.list_fleets()}
    assert names == {"data-eng", "ml"}
    got = await pg.get_fleet(fa.id)
    assert got is not None and got.name == "data-eng"
    assert await pg.get_fleet("nonexistent") is None


async def test_create_fleet_duplicate_name_raises(pg) -> None:
    await pg.create_fleet("data-eng")
    with pytest.raises(ValueError):
        await pg.create_fleet("data-eng")


# --- agent registration / activation ----------------------------------------


async def test_register_agent_pending_and_idempotent(pg) -> None:
    agent = await pg.register_agent("alice", owner_alias="alice@example.com")
    assert agent.status is AgentStatus.PENDING
    assert agent.trust_level is TrustLevel.UNTRUSTED
    assert agent.owner_alias == "alice@example.com"
    # Idempotent: re-registering returns the same pending record.
    again = await pg.register_agent("alice")
    assert again.name == "alice" and again.status is AgentStatus.PENDING
    assert len(await pg.list_agents()) == 1


async def test_activate_agent_sets_level_and_fleet(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    await pg.register_agent("alice")
    active = await pg.activate_agent(
        "alice", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fa.id
    )
    assert active.status is AgentStatus.ACTIVE
    assert active.trust_level is TrustLevel.CONTRIBUTOR
    assert active.home_fleet_id == fa.id
    assert active.activated_at is not None
    stored = await pg.get_agent("alice")
    assert stored is not None and stored.status is AgentStatus.ACTIVE


async def test_activate_unknown_agent_raises(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    with pytest.raises(KeyError):
        await pg.activate_agent("ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id)


async def test_lifecycle_guards_in_postgres(pg) -> None:
    """ADR 0028: the status guard is in the UPDATE itself."""
    fa = await pg.create_fleet("data-eng")
    await pg.register_agent("alice")
    await pg.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id)
    with pytest.raises(InvalidAgentStatus):
        await pg.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fa.id)
    revoked = await pg.revoke_agent("alice")
    assert revoked.status is AgentStatus.REVOKED
    with pytest.raises(InvalidAgentStatus):
        await pg.revoke_agent("alice")
    with pytest.raises(KeyError):
        await pg.revoke_agent("ghost")
    again = await pg.activate_agent("alice", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fa.id)
    assert again.status is AgentStatus.ACTIVE
    assert again.trust_level is TrustLevel.PRIVILEGED
    await pg.register_agent("bob")
    assert (await pg.revoke_agent("bob")).status is AgentStatus.REVOKED


async def test_demote_and_move_fleet(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    fb = await pg.create_fleet("ml")
    await pg.register_agent("alice")
    await pg.activate_agent("alice", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fa.id)
    demoted = await pg.set_agent_trust_level("alice", TrustLevel.UNTRUSTED)
    assert demoted.trust_level is TrustLevel.UNTRUSTED
    assert demoted.status is AgentStatus.ACTIVE  # dormant, not revoked
    moved = await pg.set_agent_home_fleet("alice", fb.id)
    assert moved.home_fleet_id == fb.id


# --- visibility-aware discovery (the SQL trust matrix, ADR 0011) -----------


async def test_list_entries_visibility_in_postgres(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    fb = await pg.create_fleet("ml")
    await _seed(pg, "weekly cohort churn", author="alice", scope="fleet", fleet_id=fa.id)
    await _seed(pg, "cohort retention", author="bob", scope="fleet", fleet_id=fa.id)
    await _seed(pg, "ml feature store", author="carol", scope="fleet", fleet_id=fb.id)
    await _seed(pg, "bob private note", author="bob", scope="self")
    await _seed(pg, "org-wide launch", author="dave", scope="org")
    await _seed(pg, "alice private note", author="alice", scope="self")

    # Lurker alice (home=fleet-a): own + home fleet + legacy org, NOT other
    # fleets' fleet entries or bob's self.
    lurker = Visibility(level=TrustLevel.LURKER, name="alice", home_fleet_id=fa.id)
    hits = await pg.list_entries(EntryFilters(), visibility=lurker)
    authors_scopes = {(e.author, e.scope) for e in hits}
    assert ("alice", "fleet") in authors_scopes
    assert ("bob", "fleet") in authors_scopes  # same home fleet
    assert ("dave", "org") in authors_scopes
    assert ("alice", "self") in authors_scopes
    assert ("carol", "fleet") not in authors_scopes  # other fleet
    assert ("bob", "self") not in authors_scopes  # private self

    # Privileged alice (level 3): reads every fleet, but NOT bob's self.
    priv = Visibility(level=TrustLevel.PRIVILEGED, name="alice", home_fleet_id=fa.id)
    hits = await pg.list_entries(EntryFilters(), visibility=priv)
    authors = {e.author for e in hits}
    assert {"alice", "bob", "carol", "dave"} <= authors
    self_authors = {e.author for e in hits if e.scope == "self"}
    assert self_authors == {"alice"}

    # Untrusted sees nothing.
    untrusted = Visibility(level=TrustLevel.UNTRUSTED, name="alice", home_fleet_id=fa.id)
    assert await pg.list_entries(EntryFilters(), visibility=untrusted) == []


async def test_search_visibility_in_postgres(pg) -> None:
    fa = await pg.create_fleet("data-eng")
    fb = await pg.create_fleet("ml")
    await _seed(pg, "cohort churn model", author="alice", scope="fleet", fleet_id=fa.id)
    await _seed(pg, "cohort retention", author="bob", scope="fleet", fleet_id=fa.id)
    await _seed(pg, "ml cohort features", author="carol", scope="fleet", fleet_id=fb.id)

    lurker = Visibility(level=TrustLevel.LURKER, name="alice", home_fleet_id=fa.id)
    priv = Visibility(level=TrustLevel.PRIVILEGED, name="alice", home_fleet_id=fa.id)
    lurker_hits = await pg.search_keyword("cohort", EntryFilters(), 10, visibility=lurker)
    priv_hits = await pg.search_keyword("cohort", EntryFilters(), 10, visibility=priv)
    # A privileged reader sees a superset (all fleets) of a lurker's (home fleet).
    assert set(lurker_hits) <= set(priv_hits)
    # The lurker cannot see carol's (other-fleet) entry.
    carol_entry = await _find_by_author(pg, "carol")
    assert carol_entry is None or carol_entry not in lurker_hits
    # But the privileged reader can.
    assert carol_entry is not None and carol_entry in priv_hits


async def _find_by_author(pg, author: str) -> str | None:
    """The single entry id authored by ``author`` (or None)."""
    entries = await pg.list_entries(EntryFilters(), limit=100)
    match = [e.id for e in entries if e.author == author]
    return match[0] if match else None
