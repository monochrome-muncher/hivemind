"""Production entry point: build the app from settings and serve it.

Store and embeddings are imported lazily, so unit tests injecting fakes never import them.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import uvicorn
from fastapi import FastAPI

from hivemind.api.deps import HivemindApp, create_app
from hivemind.config import (
    SearchConfig,
    Settings,
    bind_host,
    bind_port,
    configure_logging,
    load_settings,
)
from hivemind.ports import Authenticator, Embedder, Extractor, Store
from hivemind.services.wiring import build_services, log_startup

logger = logging.getLogger(__name__)


def create_app_for_config(
    settings: Settings,
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    authenticator: Authenticator | None = None,
    extractor: Extractor | None = None,
    search_config: SearchConfig | None = None,
) -> HivemindApp:
    """Build a HivemindApp from settings; tests pass fakes, anything unset is built (SPEC §8.2, ADR
    0007)."""
    store = store if store is not None else _build_store(settings)
    embedder = embedder if embedder is not None else _build_embedder(settings)
    authenticator = authenticator if authenticator is not None else _build_authenticator(settings)
    extractor = extractor if extractor is not None else _build_extractor(settings)
    search_config = search_config if search_config is not None else settings.search_config()
    services = build_services(
        store,
        embedder,
        search_config,
        extractor=extractor,
        authenticator=authenticator,
        metrics_entry_counts_ttl=settings.metrics_entry_counts_ttl,
        require_stored_keys=settings.require_stored_keys,
    )
    return HivemindApp(
        store=store,
        embedder=embedder,
        authenticator=authenticator,
        search_config=search_config,
        write_service=services.write_service,
        read_service=services.read_service,
        governance_service=services.governance_service,
        search_service=services.search_service,
        access_service=services.access_service,
        metrics_service=services.metrics_service,
        activity_service=services.activity_service,
        require_stored_keys=settings.require_stored_keys,
        idle_agent_days=settings.idle_agent_days,
    )


def create_app_from_settings(
    settings: Settings,
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    authenticator: Authenticator | None = None,
    extractor: Extractor | None = None,
    search_config: SearchConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app from settings (for tests and tooling)."""
    return create_app(
        create_app_for_config(
            settings,
            store=store,
            embedder=embedder,
            authenticator=authenticator,
            extractor=extractor,
            search_config=search_config,
        )
    )


def run() -> None:
    """Console entry point (``hivemind-api``): serve the REST surface."""
    settings = load_settings()
    configure_logging(settings.log_level)
    log_startup(logger, "hivemind-api", settings)
    fastapi_app = create_app_from_settings(settings)
    uvicorn.run(
        fastapi_app,
        host=bind_host(),
        port=bind_port(8000),
    )


def _lazy_module(name: str, missing_hint: str) -> Any:
    import importlib

    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"{name} is not available; {missing_hint}") from exc


def _build_store(settings: Settings) -> Store:
    module = _lazy_module("hivemind.store", "pass an explicit store")
    builder = getattr(module, "build_store", None)
    if builder is None:
        raise RuntimeError("hivemind.store does not expose build_store(settings) -> Store")
    return cast(Store, builder(settings))


def _build_embedder(settings: Settings) -> Embedder:
    module = _lazy_module("hivemind.embeddings", "pass an explicit embedder")
    builder = getattr(module, "build_embedder", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.embeddings does not expose build_embedder(settings) -> Embedder"
        )
    return cast(Embedder, builder(settings))


def _build_authenticator(settings: Settings) -> Authenticator:
    """The production Authenticator (ADR 0008)."""
    module = _lazy_module("hivemind.store", "pass an explicit authenticator")
    builder = getattr(module, "build_authenticator", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.store does not expose build_authenticator(settings) -> Authenticator"
        )
    return cast(Authenticator, builder(settings))


def _build_extractor(settings: Settings) -> Extractor | None:
    """The production Extractor, or ``None`` when its endpoint is unset (ADR 0016)."""
    module = _lazy_module("hivemind.extractor", "pass an explicit extractor")
    builder = getattr(module, "build_extractor", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.extractor does not expose build_extractor(settings) -> Extractor"
        )
    return cast("Extractor | None", builder(settings))
