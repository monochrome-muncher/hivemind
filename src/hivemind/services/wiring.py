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
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.metrics import MetricsService
from hivemind.services.search import SearchService


@dataclass(frozen=True, slots=True)
class Services:
    """The stateless services over one set of ports (the identity of the
    caller is passed per call, never held here)."""

    search_config: SearchConfig
    write_service: WriteService
    search_service: SearchService
    governance_service: GovernanceService
    access_service: AccessService
    metrics_service: MetricsService


def build_services(
    store: Store,
    embedder: Embedder,
    search_config: SearchConfig,
    *,
    extractor: Extractor | None = None,
    authenticator: Authenticator | None = None,
) -> Services:
    """Wire the services. ``extractor`` is optional (ADR 0016: ``None`` is
    extraction off); without ``authenticator`` key management is refused
    (the in-memory dev runner)."""
    return Services(
        search_config=search_config,
        write_service=WriteService(store, embedder, extractor),
        search_service=SearchService(store, embedder, search_config),
        governance_service=GovernanceService(store, search_config),
        access_service=AccessService(store, authenticator),
        metrics_service=MetricsService(store),
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
