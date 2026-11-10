"""The composition root: ``build_services`` wires the service graph every runner (REST, both stdio
MCP runners, HTTP MCP) shares, so a new service reaches all of them at once."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from hivemind.config import SearchConfig, Settings, redact_url
from hivemind.ports import Authenticator, Embedder, Extractor, Store
from hivemind.services.access import AccessService
from hivemind.services.activity import ActivityService
from hivemind.services.governance import GovernanceService
from hivemind.services.metrics import DEFAULT_ENTRY_COUNTS_TTL, MetricsService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService


@dataclass(frozen=True, slots=True)
class Services:
    """The stateless services over one set of ports; the caller's identity is passed per call."""

    search_config: SearchConfig
    write_service: WriteService
    read_service: ReadService
    search_service: SearchService
    governance_service: GovernanceService
    access_service: AccessService
    metrics_service: MetricsService
    activity_service: ActivityService


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
    governance_service = GovernanceService(
        store, search_config, require_stored_keys=require_stored_keys
    )
    access_service = AccessService(store, authenticator)
    metrics_service = MetricsService(store, entry_counts_ttl=metrics_entry_counts_ttl)
    activity_service = ActivityService(store, ttl=metrics_entry_counts_ttl)
    # A merge or undo moves entries between fleets: drop the cached scans.
    access_service.merges.on_change += [metrics_service.invalidate, activity_service.invalidate]
    # So does a cleanup and its undo.
    access_service.cleanups.on_change += [metrics_service.invalidate, activity_service.invalidate]
    # A corrected owner alias regroups the owner-level health figures.
    access_service.on_owner_change += [metrics_service.invalidate, activity_service.invalidate]
    return Services(
        search_config=search_config,
        write_service=WriteService(store, embedder, extractor),
        governance_service=governance_service,
        read_service=ReadService(store, governance_service, search_config),
        search_service=SearchService(store, embedder, search_config),
        access_service=access_service,
        metrics_service=metrics_service,
        activity_service=activity_service,
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
