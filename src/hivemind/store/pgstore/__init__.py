"""The Postgres adapter for the ``Store`` port (asyncpg + pgvector, ADR 0007).

Mirrors ``MemoryStore`` behavior-for-behavior (SPEC.md §4, §6). One module per
area holds its SQL and store methods; ``PgStore`` composes them on ``connection.py``.
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
    """A ``Store`` backed by PostgreSQL + pgvector. The pool opens on first use, so synchronous
    builders can hand the store to a running loop; ``close()`` on shutdown."""
