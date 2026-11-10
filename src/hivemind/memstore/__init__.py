"""In-memory Store implementation (SPEC.md §4).

The dependency-free reference ``Store``, for tests and local dev mode.

One module per area, each a mixin over the shared ``MemoryState`` (``state.py``):
``entries``, ``search``, ``pins``, ``feedback``, ``metrics``, ``fleets``,
``fleet_merge``, ``agent_cleanup``, ``agents``, ``successions`` and ``audit``;
``helpers`` holds the pure functions they share. ``MemoryStore`` composes them.

Determinism notes (for test stability):
* Keyword scoring: number of *distinct* query tokens present in the
  entry's embeddable text, tie-broken by ``created_at`` then ``id``.
* Vector scoring: cosine similarity, tie-broken by ``created_at`` then
  ``id``.
* ``created_at`` is caller-supplied via ``clock`` (defaults to now).
"""

from __future__ import annotations

from hivemind.memstore.agent_cleanup import AgentCleanupStore
from hivemind.memstore.agents import AgentStore
from hivemind.memstore.audit import AuditStore
from hivemind.memstore.entries import EntryStore
from hivemind.memstore.feedback import FeedbackStore
from hivemind.memstore.fleet_merge import FleetMergeStore
from hivemind.memstore.fleets import FleetStore
from hivemind.memstore.helpers import cosine_similarity
from hivemind.memstore.metrics import MetricsStore
from hivemind.memstore.pins import PinStore
from hivemind.memstore.search import SearchStore
from hivemind.memstore.successions import SuccessionStore

__all__ = ["MemoryStore", "cosine_similarity"]


class MemoryStore(
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
):
    """Thread-safe in-memory Store (a ``dict`` under a lock)."""
