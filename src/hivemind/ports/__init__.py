"""The seams tests live at: ``Store`` (the area protocols in this package, one module each),
``Embedder``, ``Extractor`` (optional, ADR 0016) and ``Authenticator``."""

from __future__ import annotations

from hivemind.ports.agent_cleanup import AgentCleanupPort
from hivemind.ports.agents import AgentPort
from hivemind.ports.audit import AuditPort
from hivemind.ports.auth import Authenticator, Credential, KeyReplacement
from hivemind.ports.embedding import Embedder, entry_embeddable_text
from hivemind.ports.entries import (
    EntryNotActiveConflict,
    EntryPort,
    SupersedeConflict,
    UnknownEntry,
)
from hivemind.ports.extraction import Extractor
from hivemind.ports.feedback import FeedbackPort
from hivemind.ports.fleet_merge import FleetMergePort
from hivemind.ports.fleets import FleetPort
from hivemind.ports.health import HealthPort
from hivemind.ports.metrics import MetricsPort
from hivemind.ports.offboarding import OffboardingPort
from hivemind.ports.pins import PinPort
from hivemind.ports.search import SearchPort, VectorMatch
from hivemind.ports.self_move import SelfMovePort
from hivemind.ports.store import EntryFeedbackPort, Store
from hivemind.ports.successions import SuccessionPort

__all__ = [
    "AgentCleanupPort",
    "AgentPort",
    "AuditPort",
    "Authenticator",
    "Credential",
    "Embedder",
    "EntryFeedbackPort",
    "EntryNotActiveConflict",
    "EntryPort",
    "Extractor",
    "FeedbackPort",
    "FleetMergePort",
    "FleetPort",
    "HealthPort",
    "KeyReplacement",
    "MetricsPort",
    "OffboardingPort",
    "PinPort",
    "SearchPort",
    "SelfMovePort",
    "Store",
    "SuccessionPort",
    "SupersedeConflict",
    "UnknownEntry",
    "VectorMatch",
    "entry_embeddable_text",
]
