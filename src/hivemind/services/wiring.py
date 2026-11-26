"""The composition root: ``build_services`` wires the service graph every runner (REST, both stdio
MCP runners, HTTP MCP) shares, so a new service reaches all of them at once."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from hivemind.config import SearchConfig, Settings, redact_url
from hivemind.ports import Authenticator, Embedder, Extractor, Store
from hivemind.services.access import AccessService
from hivemind.services.activity import ActivityService
from hivemind.services.agent_cleanup import AgentCleanupService
from hivemind.services.agent_history import AgentHistoryService
from hivemind.services.feedback import FeedbackService
from hivemind.services.fleet_activity import FleetActivityService
from hivemind.services.fleet_merge import FleetMergeService
from hivemind.services.governance import GovernanceService
from hivemind.services.metrics import DEFAULT_ENTRY_COUNTS_TTL, MetricsService
from hivemind.services.offboarding import OffboardingService
from hivemind.services.pins import PinService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.successions import SuccessionService
from hivemind.services.write import WriteService


@dataclass(frozen=True, slots=True)
class Services:
    """The stateless services over one set of ports; the caller's identity is passed per call."""

    search_config: SearchConfig
    write_service: WriteService
    read_service: ReadService
    search_service: SearchService
    feedback_service: FeedbackService
    pin_service: PinService
    governance_service: GovernanceService
    access_service: AccessService
    merge_service: FleetMergeService
    succession_service: SuccessionService
    offboarding_service: OffboardingService
    cleanup_service: AgentCleanupService
    history_service: AgentHistoryService
    metrics_service: MetricsService
    activity_service: ActivityService
    fleet_activity_service: FleetActivityService


def build_services(
    store: Store,
    embedder: Embedder,
    search_config: SearchConfig,
    *,
    extractor: Extractor | None = None,
    authenticator: Authenticator | None = None,
    metrics_entry_counts_ttl: float = DEFAULT_ENTRY_COUNTS_TTL,
    require_stored_keys: bool = False,
) -> Services:
    """Wire the services. ``extractor=None`` is extraction off (ADR 0016); without
    ``authenticator`` key management is refused. ``require_stored_keys`` (ADR 0082) makes only
    feedback and pins downgrade a dev-mode credential here; every other service gets it where the
    surface resolves its credential, so a script calling them directly must apply
    ``Credential.effective`` itself."""
    feedback_service = FeedbackService(
        store, search_config, require_stored_keys=require_stored_keys
    )
    pin_service = PinService(store, require_stored_keys=require_stored_keys)
    access_service = AccessService(store, authenticator)
    merge_service = FleetMergeService(store)
    cleanup_service = AgentCleanupService(
        store, revoke=access_service.revoke, revocable=access_service.revocable
    )
    metrics_service = MetricsService(store, entry_counts_ttl=metrics_entry_counts_ttl)
    activity_service = ActivityService(store, ttl=metrics_entry_counts_ttl)
    fleet_activity_service = FleetActivityService(store, ttl=metrics_entry_counts_ttl)
    drop_cached = [
        metrics_service.invalidate,
        activity_service.invalidate,
        fleet_activity_service.invalidate,
    ]
    # A merge or undo moves entries between fleets: drop the cached scans.
    merge_service.on_change += drop_cached
    # So does a cleanup and its undo.
    cleanup_service.on_change += drop_cached
    # A corrected owner alias regroups the owner-level health figures.
    access_service.on_owner_change += drop_cached
    # A renamed agent changes the names in the cached per-agent figures.
    access_service.on_agent_rename += drop_cached
    return Services(
        search_config=search_config,
        write_service=WriteService(store, embedder, extractor),
        read_service=ReadService(store, feedback_service, search_config),
        search_service=SearchService(store, embedder, search_config),
        feedback_service=feedback_service,
        pin_service=pin_service,
        governance_service=GovernanceService(store, pin_service),
        access_service=access_service,
        merge_service=merge_service,
        succession_service=SuccessionService(store, access_service.require_authenticator),
        offboarding_service=OffboardingService(store, access_service.require_authenticator),
        cleanup_service=cleanup_service,
        history_service=AgentHistoryService(store),
        metrics_service=metrics_service,
        activity_service=activity_service,
        fleet_activity_service=fleet_activity_service,
    )


def log_startup(logger: logging.Logger, runner: str, settings: Settings) -> None:
    """The one startup line each runner logs; the embedding endpoint is redacted, no userinfo."""
    logger.info(
        "starting %s: embedding_endpoint=%s embedding_dim=%d extraction=%s pool_max_size=%d",
        runner,
        redact_url(settings.embedding_endpoint),
        settings.embedding_dim,
        "on" if settings.extractor_endpoint else "off",
        settings.pool_max_size,
    )
