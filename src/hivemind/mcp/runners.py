"""The stdio MCP runners (ADR 0085): ``hivemind-mcp`` (in-memory dev server) and ``hivemind-mcp-pg``
(one process per agent with its own ``HIVEMIND_MCP_KEY``, ADR 0009, re-verified per call, ADR
0042)."""

from __future__ import annotations

import asyncio
import logging
import os

from hivemind.config import Settings, configure_logging, load_settings
from hivemind.domain.access import KEY_LOCKED_MESSAGE, KeyLocked, StoreUnavailable
from hivemind.extractor import build_extractor
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.local_embedder import LocalEmbedder
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Authenticator, Credential, Embedder, Extractor, Store
from hivemind.services.wiring import build_services, log_startup

logger = logging.getLogger(__name__)


def main() -> None:
    """Run the self-contained dev stdio server."""
    settings = load_settings()
    configure_logging(settings.log_level)
    store = MemoryStore()
    embedder = LocalEmbedder(
        dimension=settings.embedding_dim, prefix_tokens=settings.embedding_prefix_tokens
    )
    extractor = build_extractor(settings)
    services = build_services(
        store,
        embedder,
        settings.search_config(),
        extractor=extractor,
        require_stored_keys=settings.require_stored_keys,
    )
    app = McpHivemind.from_services(
        store,
        services,
        credential=Credential(user_id="dev", agent_id="hivemind-mcp").effective(
            settings.require_stored_keys
        ),
    )
    server = build_server(app, require_stored_keys=settings.require_stored_keys)
    asyncio.run(server.run_stdio_async())


def _mcp_key_missing_hint() -> str:
    """The text when ``HIVEMIND_MCP_KEY`` is unset."""
    return (
        "HIVEMIND_MCP_KEY is required to run hivemind-mcp-pg. Issue an "
        "agent-scoped key, then set it in the agent's MCP config:\n"
        "  uv run hivemind-keys issue-agent --name <agent>  (the agent must be registered and active)\n"
        '  {"command": "uv", "args": ["run", "--directory", "<repo>", "hivemind-mcp-pg"],\n'
        '   "env": {"HIVEMIND_MCP_KEY": "hm_..."}}'
    )


def _mcp_key_unknown_hint() -> str:
    """The text when the key is not a known credential."""
    return (
        "HIVEMIND_MCP_KEY is not a known credential. Issue it first, then retry:\n"
        "  uv run hivemind-keys issue-agent --name <agent>  (the agent must be registered and active)\n"
        "  (list existing credential hashes: uv run hivemind-keys list)"
    )


def main_pg() -> None:
    """Run the Postgres-backed stdio server, one process per agent, so every write carries verified
    provenance (SPEC §8.1, ADR 0008). Pools are closed on exit."""
    from hivemind.embeddings import build_embedder
    from hivemind.extractor import build_extractor
    from hivemind.store import build_authenticator, build_store

    settings = load_settings()
    configure_logging(settings.log_level)
    raw_key = os.environ.get("HIVEMIND_MCP_KEY", "").strip()
    if not raw_key:
        raise SystemExit(_mcp_key_missing_hint())

    log_startup(logger, "hivemind-mcp-pg", settings)
    store = build_store(settings)
    embedder = build_embedder(settings)
    authenticator = build_authenticator(settings)
    extractor = build_extractor(settings)
    asyncio.run(_serve_pg(settings, raw_key, store, embedder, authenticator, extractor))


async def _serve_pg(
    settings: Settings,
    raw_key: str,
    store: Store,
    embedder: Embedder,
    authenticator: Authenticator,
    extractor: Extractor | None,
) -> None:
    """Verify the key, then serve stdio until it ends; close the pools after."""
    credential = await _verify_startup_key(authenticator, raw_key)
    services = build_services(
        store,
        embedder,
        settings.search_config(),
        extractor=extractor,
        authenticator=authenticator,
        require_stored_keys=settings.require_stored_keys,
    )
    app = McpHivemind.from_services(store, services, credential=credential)

    async def current_credential() -> Credential | None:
        # Re-verified on EVERY call (ADR 0042): revocation, demotion or re-homing applies at once.
        return await authenticator.verify(raw_key)

    server = build_server(
        app,
        credential_provider=current_credential,
        require_stored_keys=settings.require_stored_keys,
    )
    try:
        await server.run_stdio_async()
    finally:
        await _close_all(store, embedder, authenticator, extractor)


async def _verify_startup_key(authenticator: Authenticator, raw_key: str) -> Credential:
    """The credential of ``raw_key``, or exit with the reason it is unusable."""
    try:
        credential = await authenticator.verify(raw_key)  # fail fast
    except KeyLocked:
        raise SystemExit(KEY_LOCKED_MESSAGE) from None
    except StoreUnavailable as exc:
        raise SystemExit(
            f"cannot verify HIVEMIND_MCP_KEY: the database is unreachable ({exc.cause}); "
            "check HIVEMIND_DATABASE_URL and that Postgres is up"
        ) from None
    if credential is None:
        raise SystemExit(_mcp_key_unknown_hint())
    return credential


async def _close_all(
    store: Store,
    embedder: Embedder,
    authenticator: Authenticator,
    extractor: Extractor | None,
) -> None:
    for closer in (
        getattr(store, "close", None),
        getattr(embedder, "aclose", None),
        getattr(authenticator, "close", None),
        getattr(extractor, "aclose", None),
    ):
        if closer is not None:
            await closer()
