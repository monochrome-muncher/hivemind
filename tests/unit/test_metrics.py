"""Unit tests for the minimal usage-counters surface (ROADMAP §3.3).

Seams under test: ``Store.count_entries`` (the cheap ``COUNT`` behind the
metrics service, mirrored in MemoryStore + PgStore) and the
``MetricsService`` (the usage report: entries / fleets / agents
counters). The report is the §12 usage counters that make the SPEC §10
usage-based triggers measurable (pair with the §1.1 eval harness).
"""

from __future__ import annotations

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, ImportanceSource, Kind
from hivemind.memstore import MemoryStore
from hivemind.services.metrics import MetricsService
from tests.fakes import make_clock


def _make_store(clock) -> MemoryStore:
    return MemoryStore(clock)


async def _seed_entry(store: MemoryStore, **kw) -> str:
    draft = EntryDraft(
        kind=Kind(kw.get("kind", "fact")),
        summary=kw.get("summary", "a note"),
        author=kw.get("author", "alice"),
        agent=kw.get("agent", "agent-1"),
        scope=kw.get("scope", "org"),
        fleet_id=kw.get("fleet_id"),
        importance_source=kw.get("importance_source", ImportanceSource.DEFAULT),
    )
    entry = await store.create_entry(draft, [0.1, 0.1, 0.1, 0.1])
    return entry.id


class TestCountEntries:
    """Store.count_entries: the cheap COUNT behind the metrics service."""

    async def test_counts_all_when_no_filter(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        for i in range(3):
            await _seed_entry(store, summary=f"note {i}")
        assert await store.count_entries(EntryFilters()) == 3

    async def test_counts_by_scope(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, scope="org")
        await _seed_entry(store, scope="fleet")
        await _seed_entry(store, scope="self")
        assert await store.count_entries(EntryFilters(scope="org")) == 1
        assert await store.count_entries(EntryFilters(scope="fleet")) == 1
        assert await store.count_entries(EntryFilters(scope="self")) == 1

    async def test_counts_by_kind(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, kind="fact")
        await _seed_entry(store, kind="insight")
        await _seed_entry(store, kind="insight")
        assert await store.count_entries(EntryFilters(kind=Kind.INSIGHT)) == 2
        assert await store.count_entries(EntryFilters(kind=Kind.FACT)) == 1

    async def test_counts_by_importance_source(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, importance_source=ImportanceSource.DEFAULT)
        await _seed_entry(store, importance_source=ImportanceSource.CALLER)
        await _seed_entry(store, importance_source=ImportanceSource.CALLER)
        assert (
            await store.count_entries(EntryFilters(importance_source=ImportanceSource.CALLER)) == 2
        )
        assert (
            await store.count_entries(EntryFilters(importance_source=ImportanceSource.DEFAULT)) == 1
        )

    async def test_default_counts_active_only(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        eid = await _seed_entry(store, summary="to withdraw")
        await store.withdraw_entry(eid, "no longer relevant", "alice")
        # The withdrawn entry is inactive: the default (active-only) count is 0.
        assert await store.count_entries(EntryFilters()) == 0
        # include_inactive lifts the state restriction.
        assert await store.count_entries(EntryFilters(include_inactive=True)) == 1


class TestMetricsService:
    """MetricsService.usage_report: the §12 usage counters."""

    async def test_entries_report_counts(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, scope="org", kind="fact")
        await _seed_entry(store, scope="fleet", kind="insight", fleet_id="fl1")
        await _seed_entry(store, scope="self", kind="decision")
        service = MetricsService(store)
        report = await service.usage_report()
        entries = report["entries"]
        assert entries["total"] == 3
        assert entries["active"] == 3
        assert entries["inactive"] == 0
        assert entries["by_scope"] == {"org": 1, "fleet": 1, "self": 1}
        assert entries["by_kind"] == {"fact": 1, "insight": 1, "decision": 1}
        # All three entries above rode the default (no explicit
        # importance_source), so `caller` is omitted (ROADMAP §4.5).
        assert entries["by_importance_source"] == {"default": 3}

    async def test_entries_report_by_importance_source(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, importance_source=ImportanceSource.DEFAULT)
        await _seed_entry(store, importance_source=ImportanceSource.CALLER)
        await _seed_entry(store, importance_source=ImportanceSource.CALLER)
        service = MetricsService(store)
        report = await service.usage_report()
        assert report["entries"]["by_importance_source"] == {"default": 1, "caller": 2}

    async def test_entries_report_by_author_kind(self) -> None:
        """ROADMAP §4.5's follow-on: are agents using the three kinds
        consistently? One row per registered agent, zero counts omitted."""
        clock = make_clock()
        store = _make_store(clock)
        await store.register_agent("alice")
        await store.register_agent("bob")
        await _seed_entry(store, author="alice", kind="fact")
        await _seed_entry(store, author="alice", kind="fact")
        await _seed_entry(store, author="alice", kind="decision")
        await _seed_entry(store, author="bob", kind="insight")
        service = MetricsService(store)
        report = await service.usage_report()
        assert report["entries"]["by_author_kind"] == {
            # `insight` is omitted for alice and `fact`/`decision` for bob:
            # zero counts are dropped, exactly like by_scope / by_kind.
            "alice": {"fact": 2, "decision": 1},
            "bob": {"insight": 1},
        }

    async def test_entries_report_keeps_authors_with_no_entries(self) -> None:
        """The breakdown is keyed by the registered agent roster, not by a
        DISTINCT over `entries.author` — so "registered but never written"
        is a visible state (an empty mapping), not an absent one. That is
        the whole point of asking whether agents use the kinds at all."""
        clock = make_clock()
        store = _make_store(clock)
        await store.register_agent("silent")
        service = MetricsService(store)
        report = await service.usage_report()
        assert report["entries"]["by_author_kind"] == {"silent": {}}

    async def test_entries_report_ignores_unregistered_authors(self) -> None:
        """An entry whose author is not on the roster is still counted in
        the totals and in `by_kind`, but has no row of its own: the roster
        is what bounds the query count (SPEC §8.2 scale target ~50 agents
        x 3 kinds), and `author` is server-verified from the agent key
        anyway (ADR 0012), so this is a legacy/edge row, not a norm."""
        clock = make_clock()
        store = _make_store(clock)
        await _seed_entry(store, author="ghost", kind="fact")
        service = MetricsService(store)
        report = await service.usage_report()
        assert report["entries"]["by_kind"] == {"fact": 1}
        assert report["entries"]["by_author_kind"] == {}

    async def test_entries_report_by_author_kind_counts_inactive_entries(self) -> None:
        """Consistent with every other entries counter: the question is
        what an author *wrote*, which a later supersession does not undo."""
        clock = make_clock()
        store = _make_store(clock)
        await store.register_agent("alice")
        entry_id = await _seed_entry(store, author="alice", kind="fact")
        await store.withdraw_entry(entry_id, "no longer relevant", "alice")
        service = MetricsService(store)
        report = await service.usage_report()
        assert report["entries"]["active"] == 0
        assert report["entries"]["by_author_kind"] == {"alice": {"fact": 1}}

    async def test_fleets_report_writes_per_fleet(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        f1 = await store.create_fleet("data-eng")
        f2 = await store.create_fleet("ml")
        await _seed_entry(store, fleet_id=f1.id, scope="fleet")
        await _seed_entry(store, fleet_id=f1.id, scope="fleet")
        await _seed_entry(store, fleet_id=f2.id, scope="fleet")
        service = MetricsService(store)
        report = await service.usage_report()
        fleets = report["fleets"]
        assert fleets["total"] == 2
        assert fleets["writes_by_fleet"] == {"data-eng": 2, "ml": 1}

    async def test_agents_report_trust_levels_and_pending(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        fleet = await store.create_fleet("data-eng")
        # Register, then activate: one pending (level 0), one active
        # contributor (level 2), one active privileged (level 3).
        await store.register_agent("lurker")  # stays pending, level 0
        await store.register_agent("contrib")
        await store.activate_agent(
            "contrib", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
        await store.register_agent("priv")
        await store.activate_agent(
            "priv", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id
        )
        service = MetricsService(store)
        report = await service.usage_report()
        agents = report["agents"]
        assert agents["total"] == 3
        assert agents["pending"] == 1
        assert agents["active"] == 2
        # Trust-level distribution (the §12 counter, ADR 0011).
        assert agents["by_trust_level"] == {
            "untrusted": 1,
            "lurker": 0,
            "contributor": 1,
            "privileged": 1,
        }

    async def test_usage_report_shape(self) -> None:
        clock = make_clock()
        store = _make_store(clock)
        service = MetricsService(store)
        report = await service.usage_report()
        assert set(report) == {"entries", "fleets", "agents"}
