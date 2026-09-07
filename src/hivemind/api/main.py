"""Production entry point: build the app from settings and serve it.

The pgstore and embeddings lanes are imported lazily (inside the
factory functions) so this module stays importable before those lanes
exist; the unit suite never triggers those imports (it injects fakes
via ``create_app_for_config``).
"""

from __future__ import annotations

import os
from typing import Any, cast

import uvicorn
from fastapi import FastAPI

from hivemind.api.deps import HivemindApp, create_app
from hivemind.config import SearchConfig, Settings
from hivemind.ports import Authenticator, Embedder, Store
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService


def create_app_for_config(
    settings: Settings,
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    authenticator: Authenticator | None = None,
    search_config: SearchConfig | None = None,
) -> HivemindApp:
    """Build a HivemindApp from settings, with optional fake overrides.

    Tests pass fakes for store/embedder/authenticator (the unit suite
    must never depend on the pgstore or embeddings lanes); production
    leaves them unset, and the real implementations are built lazily
    from settings (SPEC.md §8.2, ADR 0007).
    """
    store = store if store is not None else _build_store(settings)
    embedder = embedder if embedder is not None else _build_embedder(settings)
    authenticator = authenticator if authenticator is not None else _build_authenticator(settings)
    search_config = search_config if search_config is not None else settings.search_config()
    return HivemindApp(
        store=store,
        embedder=embedder,
        authenticator=authenticator,
        search_config=search_config,
        write_service=WriteService(store, embedder),
        governance_service=GovernanceService(store),
        search_service=SearchService(store, embedder, search_config),
    )


def create_app_from_settings(
    settings: Settings,
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    authenticator: Authenticator | None = None,
    search_config: SearchConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app from settings (for tests and tooling)."""
    return create_app(
        create_app_for_config(
            settings,
            store=store,
            embedder=embedder,
            authenticator=authenticator,
            search_config=search_config,
        )
    )


def run() -> None:
    """Console entry point (``hivemind-api``): serve the REST surface."""
    settings = Settings()
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
