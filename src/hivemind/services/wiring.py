"""The composition root: the service graph every runner shares.

The REST app, both stdio MCP runners and the streamable-HTTP MCP runner
build the same services from the same ports. ``build_services`` is that
wiring, in one place, so a new service or constructor argument reaches
every runner at once.
"""

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
    """The stateless services over one set of ports (the identity of the
    caller is passed per call, never held here)."""

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
    """Wire the services. ``extractor`` is optional (ADR 0016: ``None`` is
    extraction off); without ``authenticator`` key management is refused
    (the in-memory dev runner). ``metrics_entry_counts_ttl`` is
    ``Settings.metrics_entry_counts_ttl`` (ADR 0069). ``require_stored_keys``
    is ``Settings.require_stored_keys`` (ADR 0082): when true, feedback and
    pins downgrade a dev-mode credential to trust level 0 here. Every other
    service takes it where each surface resolves its credential (the REST
    dependency and ``build_server``), so a script calling those services
    directly must apply ``Credential.effective`` itself."""
    governance_service = GovernanceService(
        store, search_config, require_stored_keys=require_stored_keys
    )
    access_service = AccessService(store, authenticator)
    metrics_service = MetricsService(store, entry_counts_ttl=metrics_entry_counts_ttl)
    activity_service = ActivityService(store, ttl=metrics_entry_counts_ttl)
    # A merge or undo moves entries between fleets: drop the cached scans (ADR 0107).
    access_service.merges.on_change += [metrics_service.invalidate, activity_service.invalidate]
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
    """The one startup line each server runner logs. The embedding endpoint
    is redacted: no userinfo ever reaches a log."""
    logger.info(
        "starting %s: embedding_endpoint=%s embedding_dim=%d extraction=%s pool_max_size=%d",
        runner,
        redact_url(settings.embedding_endpoint),
        settings.embedding_dim,
        "on" if settings.extractor_endpoint else "off",
        settings.pool_max_size,
    )
