"""The ``Store`` port contract, run against both adapters.

AGENTS.md: ``MemoryStore`` "must behave like ``PgStore``", because every
service and surface unit test runs on ``MemoryStore``. These tests hold
both adapters to the behaviour ``ports.Store`` documents, case by case,
so a divergence fails here instead of making the unit suite prove the
wrong thing.

``memory`` always runs; ``pg`` needs the dev Postgres (it skips when the
database is down, and CI's ``HM_TEST_REQUIRE_PG=1`` turns that skip into
a failure). The divergences known today are marked ``xfail(strict=True)``
on the adapter that differs, so fixing one turns the mark into a failure
that asks for the mark to be removed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel, Visibility
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.domain.entry import Entry, EntryDraft, EntryFilters, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Store, SupersedeConflict
from hivemind.store import PgStore
from hivemind.store.migrate import migrate

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NO_SUCH_ID = "00000000-0000-4000-8000-00000000dead"

_TABLES = (
    "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, "
    "search_counts, entry_links, pins"
)


class TickingClock:
    """Every reading is 1 ms later, so ``created_at`` orders writes the way
    Postgres's per-transaction ``now()`` does."""

    def __init__(self) -> None:
        self._now = T0

    def __call__(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now


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
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    try:
        has_vector = await probe.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
        )
    finally:
        await probe.close()
    if not has_vector:
        pytest.skip("'vector' extension is not available on the dev database")
    await migrate(dsn, dim)

    async def truncate() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_TABLES)
        finally:
            await conn.close()

    await truncate()
    store = PgStore(dsn)
    try:
        yield Adapter("pg", store, dim)
    finally:
        await truncate()
        await store.close()


@pytest.fixture(params=["memory", "pg"])
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[Adapter]:
    if request.param == "memory":
        yield Adapter("memory", MemoryStore(clock=TickingClock()), Settings().embedding_dim)
        return
    async for pg in _pg_adapter():
        yield pg


def diverges_on(kind: str, reason: str) -> Callable[[pytest.FixtureRequest], None]:
    """Mark the current test xfail (strict) for one adapter: a divergence
    known today, kept visible rather than hidden."""

    def apply(request: pytest.FixtureRequest) -> None:
        if request.node.callspec.params["adapter"] == kind:
            request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))

    return apply


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
) -> EntryDraft:
    return EntryDraft(
        kind=kind,
        summary=summary,
        author=author,
        agent="claude-code",
        body=body,
        tags=tags,
        scope=scope,
        fleet_id=fleet_id,
        supersedes=supersedes,
        see_also=see_also,
        occurred_at=T0,
    )


async def write(a: Adapter, summary: str, hot: int = 0, **kwargs: object) -> Entry:
    return await a.store.create_entry(draft(summary, **kwargs), a.vec(hot), "test-model")  # type: ignore[arg-type]


def _vis(level: TrustLevel, name: str = "reader", fleet: str | None = None) -> Visibility:
    return Visibility(level=level, name=name, home_fleet_id=fleet)


# -- create / read ----------------------------------------------------------------


async def test_create_entry_round_trips(adapter: Adapter) -> None:
    created = await write(adapter, "auth uses JWT", body="details", tags=("auth", "jwt"))
    assert created.state is EntryState.ACTIVE
    assert created.embedding_model == "test-model"
    got = await adapter.store.get_entry(created.id)
    assert got is not None
    for field in ("id", "kind", "summary", "body", "tags", "author", "agent", "scope"):
        assert getattr(got, field) == getattr(created, field)
    assert got.occurred_at == T0


async def test_unknown_and_malformed_ids_read_as_absent(adapter: Adapter) -> None:
    entry = await write(adapter, "present")
    assert await adapter.store.get_entry(NO_SUCH_ID) is None
    assert await adapter.store.get_entry("not-a-uuid") is None
    found = await adapter.store.get_entries([entry.id, NO_SUCH_ID, "not-a-uuid"])
    assert list(found) == [entry.id]


@pytest.mark.parametrize("spelling", ["{%s}", "urn:uuid:%s", "-----%s"])
async def test_uuid_spellings_postgres_rejects_read_as_absent(
    adapter: Adapter, spelling: str
) -> None:
    """``uuid.UUID`` parses these, asyncpg's codec does not: an id-taking
    call must answer "no such entry", never raise a ``DataError`` (a 500)."""
    entry = await write(adapter, "present")
    odd = spelling % entry.id
    store = adapter.store
    assert await store.get_entry(odd) is None
    assert await store.get_entries([odd]) == {}
    assert await store.list_predecessors([odd]) == []
    assert await store.entry_links(odd, 5) == ([], [])
    assert await store.feedback_counts(odd) == (0, 0, 0)
    assert await store.list_feedback(odd, 5) == []
    assert await store.get_fleet(odd) is None
    with pytest.raises(KeyError):
        await store.withdraw_entry(odd, None, by_user="alice")


# -- supersession (ADR 0034) --------------------------------------------------------


async def test_supersede_flips_the_target(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    flipped = await adapter.store.get_entry(old.id)
    assert flipped is not None
    assert flipped.state is EntryState.SUPERSEDED
    assert flipped.superseded_by == new.id
    preds = await adapter.store.list_predecessors([new.id])
    assert [p.id for p in preds] == [old.id]


async def test_supersede_of_an_inactive_target_writes_nothing(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    await write(adapter, "v2", supersedes=(old.id,))
    before = await adapter.store.count_entries(EntryFilters(include_inactive=True))
    with pytest.raises(SupersedeConflict) as exc:
        await write(adapter, "v3", supersedes=(old.id,))
    assert exc.value.ids == [old.id]
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == before


async def test_supersede_of_an_unknown_target_writes_nothing(adapter: Adapter) -> None:
    with pytest.raises(SupersedeConflict):
        await write(adapter, "orphan", supersedes=(NO_SUCH_ID,))
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == 0


# -- listing and filters (SPEC §5.3) ------------------------------------------------


async def test_list_is_newest_first_and_paginates(adapter: Adapter) -> None:
    ids = [(await write(adapter, f"entry {i}")).id for i in range(4)]
    listed = await adapter.store.list_entries(EntryFilters(), limit=2, offset=1)
    assert [e.id for e in listed] == [ids[2], ids[1]]


async def test_filters_combine_with_and(adapter: Adapter) -> None:
    hit = await write(adapter, "a", kind=Kind.DECISION, tags=("x", "y"), author="bob")
    await write(adapter, "b", kind=Kind.DECISION, tags=("x",), author="bob")
    await write(adapter, "c", kind=Kind.FACT, tags=("x", "y"), author="bob")
    await write(adapter, "d", kind=Kind.DECISION, tags=("x", "y"), author="carol")
    filters = EntryFilters(kind=Kind.DECISION, tags=("x", "y"), author="bob")
    assert [e.id for e in await adapter.store.list_entries(filters)] == [hit.id]
    assert await adapter.store.count_entries(filters) == 1


async def test_inactive_entries_are_hidden_unless_asked(adapter: Adapter) -> None:
    old = await write(adapter, "v1")
    new = await write(adapter, "v2", supersedes=(old.id,))
    active = await adapter.store.list_entries(EntryFilters())
    assert [e.id for e in active] == [new.id]
    every = await adapter.store.list_entries(EntryFilters(include_inactive=True))
    assert {e.id for e in every} == {old.id, new.id}


async def test_flagged_keeps_entries_reported_stale_or_wrong(adapter: Adapter) -> None:
    good = await write(adapter, "good")
    stale = await write(adapter, "stale")
    for entry, verdict in ((good, Verdict.HELPFUL), (stale, Verdict.STALE)):
        await adapter.store.record_feedback(Feedback(entry.id, "u", "a", verdict, updated_at=T0))
    flagged = await adapter.store.list_entries(EntryFilters(flagged=True))
    assert [e.id for e in flagged] == [stale.id]


# -- visibility (ADR 0011, SPEC §12.2) ----------------------------------------------


async def test_visibility_matrix(adapter: Adapter) -> None:
    home, other = str(uuid.uuid4()), str(uuid.uuid4())
    org = await write(adapter, "org note", scope="org", author="w")
    mine = await write(adapter, "my note", scope="self", author="reader")
    theirs = await write(adapter, "their note", scope="self", author="w")
    in_home = await write(adapter, "home fleet", scope="fleet", fleet_id=home, author="w")
    in_other = await write(adapter, "other fleet", scope="fleet", fleet_id=other, author="w")

    async def visible(v: Visibility | None) -> set[str]:
        return {e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)}

    everything = {org.id, mine.id, theirs.id, in_home.id, in_other.id}
    assert await visible(None) == everything
    assert await visible(Visibility(TrustLevel.PRIVILEGED, "x", is_admin=True)) == everything
    assert await visible(_vis(TrustLevel.UNTRUSTED, fleet=home)) == set()
    member = {org.id, mine.id, in_home.id}
    assert await visible(_vis(TrustLevel.LURKER, fleet=home)) == member
    assert await visible(_vis(TrustLevel.CONTRIBUTOR, fleet=home)) == member
    assert await visible(_vis(TrustLevel.PRIVILEGED, fleet=home)) == member | {in_other.id}
    count = await adapter.store.count_entries(
        EntryFilters(), visibility=_vis(TrustLevel.LURKER, fleet=home)
    )
    assert count == len(member)


async def test_own_fleet_entries_stay_visible_after_a_fleet_move(adapter: Adapter) -> None:
    old_home, new_home = str(uuid.uuid4()), str(uuid.uuid4())
    entry = await write(adapter, "written at home", scope="fleet", fleet_id=old_home, author="me")
    v = _vis(TrustLevel.CONTRIBUTOR, name="me", fleet=new_home)
    assert [e.id for e in await adapter.store.list_entries(EntryFilters(), visibility=v)] == [
        entry.id
    ]


# -- search streams (SPEC §6.2) -----------------------------------------------------


async def test_keyword_search_matches_any_term_and_respects_visibility(adapter: Adapter) -> None:
    jwt = await write(adapter, "tokens are signed with JWT", author="w")
    await write(adapter, "unrelated cooking recipe", author="w")
    private = await write(adapter, "JWT rotation notes", scope="self", author="w")
    ids = await adapter.store.search_keyword("JWT expiry", EntryFilters(), 10)
    assert set(ids) == {jwt.id, private.id}
    seen = await adapter.store.search_keyword(
        "JWT", EntryFilters(), 10, visibility=_vis(TrustLevel.CONTRIBUTOR)
    )
    assert seen == [jwt.id]


async def test_vector_search_ranks_nearest_first(adapter: Adapter) -> None:
    near = await write(adapter, "near", hot=1)
    far = await write(adapter, "far", hot=2)
    probe = adapter.vec(1)
    probe[2] = 0.5
    assert await adapter.store.search_vector(probe, EntryFilters(), 10) == [near.id, far.id]


async def test_similar_entries_exclude_the_entry_and_inactive_ones(adapter: Adapter) -> None:
    old = await write(adapter, "v1", hot=3)
    new = await write(adapter, "v2", hot=3, supersedes=(old.id,))
    other = await write(adapter, "other", hot=4)
    pairs = await adapter.store.similar_entries(adapter.vec(3), 5, exclude_id=new.id)
    assert [eid for eid, _ in pairs] == [other.id]
    assert -1.0 <= pairs[0][1] <= 1.0


async def test_keyword_ties_break_newest_first(
    adapter: Adapter, request: pytest.FixtureRequest
) -> None:
    diverges_on("memory", "MemoryStore breaks keyword ties oldest first")(request)
    first = await write(adapter, "shared word alpha")
    second = await write(adapter, "shared word alpha")
    assert await adapter.store.search_keyword("alpha", EntryFilters(), 10) == [
        second.id,
        first.id,
    ]


async def test_vector_search_keeps_dissimilar_entries(
    adapter: Adapter, request: pytest.FixtureRequest
) -> None:
    diverges_on("memory", "MemoryStore drops entries with cosine similarity <= 0")(request)
    opposite = [0.0] * adapter.dim
    opposite[5] = -1.0
    entry = await adapter.store.create_entry(draft("opposite"), opposite, "test-model")
    assert await adapter.store.search_vector(adapter.vec(5, base=0.0), EntryFilters(), 10) == [
        entry.id
    ]


# -- withdraw ------------------------------------------------------------------------


async def test_withdraw(adapter: Adapter) -> None:
    entry = await write(adapter, "to withdraw")
    withdrawn = await adapter.store.withdraw_entry(entry.id, "obsolete", by_user="alice")
    assert withdrawn.state is EntryState.WITHDRAWN
    assert withdrawn.withdrawn_reason == "obsolete"
    with pytest.raises(ValueError):
        await adapter.store.withdraw_entry(entry.id, None, by_user="alice")
    with pytest.raises(KeyError):
        await adapter.store.withdraw_entry(NO_SUCH_ID, None, by_user="alice")


# -- "see also" links (ADR 0057, 0059) ------------------------------------------------


async def test_links_read_from_both_ends(adapter: Adapter) -> None:
    a = await write(adapter, "a")
    b = await write(adapter, "b")
    c = await write(adapter, "c", see_also=(a.id, b.id, NO_SUCH_ID))
    d = await write(adapter, "d", see_also=(a.id,))
    outgoing, incoming = await adapter.store.entry_links(c.id, 10)
    assert (sorted(outgoing), incoming) == (sorted([a.id, b.id]), [])
    assert await adapter.store.entry_links(a.id, 10) == ([], [d.id, c.id])
    assert await adapter.store.entry_links(a.id, 1) == ([], [d.id])
    many = await adapter.store.entry_links_many([a.id, c.id, NO_SUCH_ID], 10)
    assert many[a.id] == ([], [d.id, c.id])
    assert sorted(many[c.id][0]) == sorted([a.id, b.id]) and many[c.id][1] == []
    assert many[NO_SUCH_ID] == ([], [])
    assert list(many) == [a.id, c.id, NO_SUCH_ID]


async def test_links_of_one_write_read_in_id_order(
    adapter: Adapter, request: pytest.FixtureRequest
) -> None:
    diverges_on("memory", "MemoryStore keeps one write's links in see_also order")(request)
    a = await write(adapter, "a")
    b = await write(adapter, "b")
    c = await write(adapter, "c", see_also=(max(a.id, b.id), min(a.id, b.id)))
    outgoing, _ = await adapter.store.entry_links(c.id, 10)
    assert outgoing == sorted([a.id, b.id])


# -- pins (ADR 0058) -----------------------------------------------------------------


async def test_pins_are_idempotent_capped_and_newest_first(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    c = await write(adapter, "c", scope="fleet", fleet_id=fleet)
    first = await adapter.store.pin_entry(fleet, a.id, "boss", limit=2)
    assert first is not None and first.entry_id == a.id and first.pinned_by == "boss"
    assert await adapter.store.pin_entry(fleet, a.id, "other", limit=2) == first
    assert await adapter.store.pin_entry(fleet, b.id, "boss", limit=2) is not None
    assert await adapter.store.pin_entry(fleet, c.id, "boss", limit=2) is None
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [b.id, a.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is True
    assert await adapter.store.unpin_entry(fleet, a.id) is False
    assert await adapter.store.list_pins(str(uuid.uuid4())) == []


# -- feedback (SPEC §4.2, ADRs 0051, 0059) ---------------------------------------------


async def test_feedback_upserts_per_reporter_and_lists_newest_first(adapter: Adapter) -> None:
    entry = await write(adapter, "rated")
    other = await write(adapter, "unrated")
    store = adapter.store
    await store.record_feedback(Feedback(entry.id, "u1", "a", Verdict.HELPFUL, "ok", T0))
    await store.record_feedback(
        Feedback(entry.id, "u1", "a", Verdict.STALE, "now stale", T0 + timedelta(minutes=1))
    )
    await store.record_feedback(Feedback(entry.id, "u2", "a", Verdict.WRONG, None, T0))
    assert await store.feedback_counts(entry.id) == (0, 1, 1)
    rows = await store.list_feedback(entry.id, 10)
    assert [(r.user, r.verdict, r.note) for r in rows] == [
        ("u1", Verdict.STALE, "now stale"),
        ("u2", Verdict.WRONG, None),
    ]
    assert len(await store.list_feedback(entry.id, 1)) == 1
    many = await store.list_feedback_many([entry.id, other.id], 10)
    assert [r.user for r in many[entry.id]] == ["u1", "u2"]
    assert many[other.id] == []
    counts = await store.quality_counts([entry.id, other.id])
    assert counts == {entry.id: (0, 1, 1), other.id: (0, 0, 0)}


async def test_quality_counts_cover_every_well_formed_id(adapter: Adapter) -> None:
    counts = await adapter.store.quality_counts([NO_SUCH_ID, "not-a-uuid"])
    assert counts == {NO_SUCH_ID: (0, 0, 0)}


# -- counters (ROADMAP §3.3, ADR 0056) --------------------------------------------------


async def test_search_counters_accumulate_per_fleet(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    await adapter.store.record_search(fleet, empty=True)
    await adapter.store.record_search(fleet, empty=False)
    await adapter.store.record_search(None, empty=False)
    counts = {c.fleet_id: (c.searches, c.empty) for c in await adapter.store.search_counts()}
    assert counts == {fleet: (2, 1), None: (1, 0)}


async def test_usage_counts_group_every_state(adapter: Adapter) -> None:
    old = await write(adapter, "v1", author="bob")
    await write(adapter, "v2", author="bob", supersedes=(old.id,))
    rows = await adapter.store.usage_counts()
    by_active = {r.active: r.count for r in rows if r.author == "bob"}
    assert by_active == {True: 1, False: 1}


# -- fleets and agents (ADRs 0011, 0012, 0028, 0045) -------------------------------------


async def test_fleets(adapter: Adapter) -> None:
    fleet = await adapter.store.create_fleet("blue")
    with pytest.raises(ValueError):
        await adapter.store.create_fleet("blue")
    assert await adapter.store.get_fleet(fleet.id) == fleet
    assert await adapter.store.get_fleet(NO_SUCH_ID) is None
    assert [f.name for f in await adapter.store.list_fleets()] == ["blue"]


async def test_agent_lifecycle(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    agent = await store.register_agent("Scout", "owner")
    assert (agent.status, agent.trust_level, agent.home_fleet_id) == (
        AgentStatus.PENDING,
        TrustLevel.UNTRUSTED,
        None,
    )
    assert await store.register_agent("Scout", "someone else") == agent  # never overwritten
    assert (await store.register_agent("scout")).name == "Scout"  # case variant (ADR 0045)
    active = await store.activate_agent(
        "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
    )
    assert active.status is AgentStatus.ACTIVE and active.home_fleet_id == fleet.id
    with pytest.raises(InvalidAgentStatus):
        await store.activate_agent(
            "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
    promoted = await store.set_agent_trust_level("Scout", TrustLevel.PRIVILEGED)
    assert promoted.trust_level is TrustLevel.PRIVILEGED
    revoked = await store.revoke_agent("Scout")
    assert revoked.status is AgentStatus.REVOKED
    with pytest.raises(InvalidAgentStatus):
        await store.revoke_agent("Scout")
    for call in (
        store.revoke_agent("ghost"),
        store.set_agent_trust_level("ghost", TrustLevel.LURKER),
        store.set_agent_home_fleet("ghost", fleet.id),
        store.activate_agent("ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id),
    ):
        with pytest.raises(KeyError):
            await call
    assert await store.get_agent("ghost") is None


async def test_agents_list_by_name(adapter: Adapter, request: pytest.FixtureRequest) -> None:
    diverges_on("memory", "MemoryStore lists agents in registration order")(request)
    for name in ("zed", "amy"):
        await adapter.store.register_agent(name)
    assert [a.name for a in await adapter.store.list_agents()] == ["amy", "zed"]


# -- audit log (ADR 0027) ---------------------------------------------------------------


async def test_audit_log_is_newest_first_and_filters(adapter: Adapter) -> None:
    store = adapter.store

    def event(actor: str, target: str) -> AuditEvent:
        return AuditEvent(ActorKind.ADMIN_KEY, actor, AuditAction.FLEET_CREATE, target, {"n": 1})

    first = await store.record_audit(event("admin:a", "f1"))
    second = await store.record_audit(event("admin:b", "f2"))
    third = await store.record_audit(event("admin:a", "f3"))
    assert second.detail == {"n": 1}
    assert [r.id for r in await store.list_audit(AuditFilters(), 10)] == [
        third.id,
        second.id,
        first.id,
    ]
    by_actor = await store.list_audit(AuditFilters(actor="admin:a"), 10)
    assert [r.target for r in by_actor] == ["f3", "f1"]
    older = await store.list_audit(AuditFilters(before=third.id), 10)
    assert [r.id for r in older] == [second.id, first.id]
    assert len(await store.list_audit(AuditFilters(), 1)) == 1
