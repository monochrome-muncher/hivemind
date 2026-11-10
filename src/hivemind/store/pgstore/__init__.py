"""The Postgres adapter for the ``Store`` port (asyncpg + pgvector).

Mirrors ``hivemind.memstore.MemoryStore`` behavior-for-behavior
(SPEC.md §4, §6). The pool opens lazily on first use, so synchronous
builders can hand the adapter to a running loop (ADR 0007). One module per
area, each holding that area's SQL constants, its store methods and its
private helpers; ``PgStore`` is those area classes composed on the
``connection.py`` base. Shared SQL fragments live in ``sql_common.py``,
``pin_common.py`` and ``fleet_guards.py``, row mappers in ``rows.py``.
"""

from __future__ import annotations

from hivemind.store.pgstore.agent_cleanup import AgentCleanupStore
from hivemind.store.pgstore.agents import AgentStore
from hivemind.store.pgstore.audit import AuditStore
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.entries import EntryStore
from hivemind.store.pgstore.feedback import FeedbackStore
from hivemind.store.pgstore.fleet_merge import FleetMergeStore
from hivemind.store.pgstore.fleets import FleetStore
from hivemind.store.pgstore.metrics import MetricsStore
from hivemind.store.pgstore.pins import PinStore
from hivemind.store.pgstore.search import SearchStore
from hivemind.store.pgstore.successions import SuccessionStore

__all__ = ["PgStore"]


class PgStore(
    EntryStore,
    SearchStore,
    PinStore,
    FeedbackStore,
    MetricsStore,
    FleetStore,
    FleetMergeStore,
    AgentCleanupStore,
    AgentStore,
    SuccessionStore,
    AuditStore,
    PgConnections,
):
    """A ``Store`` backed by PostgreSQL + pgvector (ADR 0007).

    The pool opens lazily on first use; ``close()`` on shutdown. Pool
    sizes come from ``Settings.pool_min_size`` / ``pool_max_size``.
    """
