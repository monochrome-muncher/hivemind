"""The ``Store`` port: every area protocol together, plus the pairs a consumer needs."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.ports.agent_cleanup import AgentCleanupPort
from hivemind.ports.agents import AgentPort
from hivemind.ports.audit import AuditPort
from hivemind.ports.entries import EntryPort
from hivemind.ports.feedback import FeedbackPort
from hivemind.ports.fleet_merge import FleetMergePort
from hivemind.ports.fleets import FleetPort
from hivemind.ports.health import HealthPort
from hivemind.ports.metrics import MetricsPort
from hivemind.ports.offboarding import OffboardingPort
from hivemind.ports.pins import PinPort
from hivemind.ports.search import SearchPort
from hivemind.ports.self_move import SelfMovePort
from hivemind.ports.successions import SuccessionPort


@runtime_checkable
class EntryFeedbackPort(EntryPort, FeedbackPort, Protocol):
    """What the entry-read path needs: entries and the feedback shown with them."""


@runtime_checkable
class Store(
    EntryPort,
    SearchPort,
    PinPort,
    FeedbackPort,
    MetricsPort,
    FleetPort,
    FleetMergePort,
    AgentCleanupPort,
    AgentPort,
    SuccessionPort,
    OffboardingPort,
    SelfMovePort,
    AuditPort,
    HealthPort,
    Protocol,
):
    """Read/write access to the memory pool."""
