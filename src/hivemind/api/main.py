"""Production entry point: build the app from settings and serve it.

The store and embeddings modules are imported lazily, so the unit suite
(which injects fakes via ``create_app_for_config``) never imports them.
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

import uvicorn
from fastapi import FastAPI

from hivemind.api.deps import HivemindApp, create_app
from hivemind.config import SearchConfig, Settings, configure_logging, load_settings
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
    """Build a HivemindApp from settings, with optional fake overrides.

    Tests pass fakes; anything left unset is built from settings
    (SPEC.md §8.2, ADR 0007). The built extractor is ``None`` when its
    endpoint is unset (ADR 0016).
    """
    store = store if store is not None else _build_store(settings)
    embedder = embedder if embedder is not None else _build_embedder(settings)
    authenticator = authenticator if authenticator is not None else _build_authenticator(settings)
    extractor = extractor if extractor is not None else _build_extractor(settings)
    search_config = search_config if search_config is not None else settings.search_config()
    services = build_services(
        store, embedder, search_config, extractor=extractor, authenticator=authenticator
    )
    return HivemindApp(
        store=store,
        embedder=embedder,
        authenticator=authenticator,
        search_config=search_config,
        write_service=services.write_service,
        governance_service=services.governance_service,
        search_service=services.search_service,
        access_service=services.access_service,
        metrics_service=services.metrics_service,
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
        host=os.environ.get("HIVEMIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("HIVEMIND_PORT", "8000")),
    )


def _lazy_module(name: str, missing_hint: str) -> Any:
    """Import a sibling lane's module at call time (lazy by design)."""
    import importlib

    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"{name} is not available; {missing_hint}") from exc


def _build_store(settings: Settings) -> Store:
    """Build the production Store (the store lane, ADR 0007)."""
    module = _lazy_module("hivemind.store", "pass an explicit store")
    builder = getattr(module, "build_store", None)
    if builder is None:
        raise RuntimeError("hivemind.store does not expose build_store(settings) -> Store")
    return cast(Store, builder(settings))


def _build_embedder(settings: Settings) -> Embedder:
    """Build the production Embedder (the embeddings lane, ADR 0005)."""
    module = _lazy_module("hivemind.embeddings", "pass an explicit embedder")
    builder = getattr(module, "build_embedder", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.embeddings does not expose build_embedder(settings) -> Embedder"
        )
    return cast(Embedder, builder(settings))


def _build_authenticator(settings: Settings) -> Authenticator:
    """Build the production Authenticator (ADR 0008: keys live in the
    store lane's credentials table)."""
    module = _lazy_module("hivemind.store", "pass an explicit authenticator")
    builder = getattr(module, "build_authenticator", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.store does not expose build_authenticator(settings) -> Authenticator"
        )
    return cast(Authenticator, builder(settings))


def _build_extractor(settings: Settings) -> Extractor | None:
    """Build the production Extractor (the extractor lane, ADR 0016, SPEC §13) —
    or ``None`` when the extractor endpoint is unset (extraction off:
    zero LLM-extraction cost, ADR 0016's optional stance)."""
    module = _lazy_module("hivemind.extractor", "pass an explicit extractor")
    builder = getattr(module, "build_extractor", None)
    if builder is None:
        raise RuntimeError(
            "hivemind.extractor does not expose build_extractor(settings) -> Extractor"
        )
    return cast("Extractor | None", builder(settings))
