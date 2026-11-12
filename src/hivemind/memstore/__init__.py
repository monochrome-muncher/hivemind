"""In-memory reference ``Store`` for tests and dev mode (SPEC §4): one mixin per area over
``MemoryState``.

Keyword score is the count of distinct query tokens in the embeddable text, vector score is cosine;
both tie-break on ``created_at`` then ``id``. ``created_at`` comes from the injected ``clock``.
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
from hivemind.memstore.offboarding import OffboardingStore
from hivemind.memstore.pins import PinStore
from hivemind.memstore.search import SearchStore

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
    OffboardingStore,
    AuditStore,
):
    """Thread-safe in-memory Store (a ``dict`` under a lock)."""
