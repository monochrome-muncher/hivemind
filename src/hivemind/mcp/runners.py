"""The stdio MCP runners (ADR 0085): the console-script entry points.

``hivemind-mcp`` (main) — the self-contained in-memory dev server.
``hivemind-mcp-pg`` (main_pg) — one process per agent, each with its own
``HIVEMIND_MCP_KEY`` (ADR 0009), re-verified per call (ADR 0042).
"""

from __future__ import annotations

import asyncio
import logging
import os

from hivemind.config import configure_logging, load_settings
from hivemind.domain.access import KEY_LOCKED_MESSAGE, KeyLocked
from hivemind.extractor import build_extractor
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.local_embedder import LocalEmbedder
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.wiring import build_services, log_startup

logger = logging.getLogger(__name__)


def main() -> None:
    """Build a self-contained dev stdio server and run the transport."""
    settings = load_settings()
    configure_logging(settings.log_level)
    store = MemoryStore()
    embedder = LocalEmbedder(
        dimension=settings.embedding_dim, prefix_tokens=settings.embedding_prefix_tokens
    )
    extractor = build_extractor(settings)  # optional (ADR 0016): None when the endpoint is unset
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
    """The error text when ``HIVEMIND_MCP_KEY`` is unset."""
    return (
        "HIVEMIND_MCP_KEY is required to run hivemind-mcp-pg. Issue an "
        "agent-scoped key, then set it in the agent's MCP config:\n"
        "  uv run hivemind-keys issue-agent --name <agent>  (the agent must be registered and active)\n"
        '  {"command": "uv", "args": ["run", "--directory", "<repo>", "hivemind-mcp-pg"],\n'
        '   "env": {"HIVEMIND_MCP_KEY": "hm_..."}}'
    )


def _mcp_key_unknown_hint() -> str:
    """The error text when the key is set but is not a known credential."""
    return (
        "HIVEMIND_MCP_KEY is not a known credential. Issue it first, then retry:\n"
        "  uv run hivemind-keys issue-agent --name <agent>  (the agent must be registered and active)\n"
        "  (list existing credential hashes: uv run hivemind-keys list)"
    )


def main_pg() -> None:
    """Build a Postgres-backed stdio server and run the transport.

    One process per agent, each with its own ``HIVEMIND_MCP_KEY``
    (ADR 0009), so every write carries verified provenance (SPEC §8.1,
    ADR 0008). Pools are closed on exit.
    """
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
    extractor = build_extractor(settings)  # optional (ADR 0016): None when the endpoint is unset

    async def _run() -> None:
        try:
            credential = await authenticator.verify(
                raw_key
            )  # fail fast; re-verified per call below
        except KeyLocked:
            raise SystemExit(KEY_LOCKED_MESSAGE) from None
        if credential is None:
            raise SystemExit(_mcp_key_unknown_hint())
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
            # Re-verified on EVERY call (ADR 0042) so revocation, demotion
            # or re-homing applies at once, not at the next restart.
            return await authenticator.verify(raw_key)

        server = build_server(
            app,
            credential_provider=current_credential,
            require_stored_keys=settings.require_stored_keys,
        )
        try:
            await server.run_stdio_async()
        finally:
            for closer in (
                getattr(store, "close", None),
                getattr(embedder, "aclose", None),
                getattr(authenticator, "close", None),
                getattr(extractor, "aclose", None),  # the extractor's HTTP client (when enabled)
            ):
                if closer is not None:
                    await closer()

    asyncio.run(_run())
