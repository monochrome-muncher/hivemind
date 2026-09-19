"""The stdio MCP server exposing the six Hivemind tools (SPEC §5.2).

``build_server`` wires the six plain tool functions from ``app.py`` into
an ``MCPServer`` (mcp 2.x) as closures bound to a single ``McpHivemind``
app instance. ``main`` builds a self-contained dev server (in-memory
store + local embedder, per the v1 dev path) and runs the stdio
transport. ``main_pg`` (console: ``hivemind-mcp-pg``) builds the
production runner: a DSN-backed ``PgStore`` + OpenAI-compatible embedder
plus a per-agent credential resolved from ``HIVEMIND_MCP_KEY`` (ADR 0009),
so multiple agents share one pool over a unified MCP interface.
``main_http`` (console: ``hivemind-mcp-http``, see ``hivemind.mcp.http``)
is the hostable form: one long-lived streamable-HTTP process serving an
unlimited number of agents, each authenticating *per request* (ADR 0010).

The installed ``mcp`` package is v2.x: the server class is
``MCPServer`` (not ``FastMCP``), tools are registered with
``@server.tool(...)`` / ``server.add_tool``, and the stdio transport is
started with ``await server.run_stdio_async()``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from mcp.server.mcpserver import MCPServer

from hivemind.config import Settings
from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_list,
    hive_register,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.mcp.local_embedder import LocalEmbedder
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService

# The agent prompt contract from SPEC §5.2, surfaced as server instructions.
_INSTRUCTIONS = (
    "recall before you analyze; write what you learn; supersede, don't "
    "duplicate; report when something you relied on proved wrong."
)


# Tool descriptions (SPEC §5.2 / §5.3), registered as the tool docs.
_DESC_WRITE = (
    "Write a distilled entry (fact|insight|decision) into the shared pool. "
    "Provenance falls back to the acting credential when author/agent are "
    "omitted. Optional 'supersedes' names entries this one replaces."
)
_DESC_SEARCH = (
    "Hybrid (keyword + vector) search over the pool. Returns compact hits "
    "with no bodies; open a hit with hive_get. Superseded/withdrawn entries "
    "are hidden unless include_inactive."
)
_DESC_GET = (
    "Fetch a full entry including its body. include_history adds the "
    "supersession chain (successors + superseded)."
)
_DESC_LIST = (
    "List / filter entries without a query (filter only, paginated). "
    "Supports kind, tags, scope, author, agent, memory-date and ingest-date "
    "ranges."
)
_DESC_WITHDRAW = (
    "Withdraw an entry (retract without replacing). Only the author or an admin may withdraw."
)
_DESC_FEEDBACK = (
    "Report helpful|stale|wrong on an entry the caller relied on. "
    "One row per (entry, user, agent); the latest verdict wins (SPEC §4.2)."
)

_DESC_REGISTER = (
    "Register (or re-register) an agent (ADR 0012). Gated on the org or admin "
    "key; creates a 'pending' agent (level 0, no fleet). Re-registering a "
    "pending name is idempotent; an active name is a conflict (the name stays "
    "reserved — pick a new one, ADR 0012)."
)


def build_server(
    app: McpHivemind,
    *,
    credential_provider: Callable[[], Credential | None] | None = None,
) -> MCPServer:
    """Build an ``MCPServer`` exposing exactly the six Hivemind tools.

    Each registered tool is a closure over ``app`` so the LLM only ever
    sees the LLM-facing arguments; the acting identity and services are
    bound, not exposed as arguments.

    ``credential_provider`` (optional) turns this into a *shared,
    multi-agent* server: when provided, every tool dispatch resolves the
    acting credential by re-binding ``app``'s (stateless) services to
    ``credential_provider()``. The stdio / dev path passes nothing, so
    one process = one agent (the fixed ``app``). The streamable-HTTP path
    (see ``hivemind.mcp.http``) passes a per-request provider, so one
    process = many agents (ADR 0010).
    """
    server = MCPServer(
        name="hivemind",
        description="Shared memory pool for an organization's AI agents.",
        instructions=_INSTRUCTIONS,
    )

    def resolve_app() -> McpHivemind:
        """The acting ``McpHivemind`` for this dispatch: the shared,
        stateless services, re-bound to the current credential when a
        provider is set (the identity varies per request; the services
        do not)."""
        if credential_provider is None:
            return app
        credential = credential_provider()
        if credential is None or credential == app.credential:
            return app
        return replace(app, credential=credential)

    @server.tool(name="hive_write", description=_DESC_WRITE)
    async def _hive_write(
        kind: str,
        summary: str,
        body: str | None = None,
        payload: dict[str, Any] | None = None,
        sources: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
        occurred_at: str | None = None,
        importance: int = 3,
        scope: str = "org",
        supersedes: list[str] | None = None,
        author: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        return await hive_write(
            resolve_app(),
            kind=kind,
            summary=summary,
            body=body,
            payload=payload,
            sources=sources,
            tags=tags,
            occurred_at=occurred_at,
            importance=importance,
            scope=scope,
            supersedes=supersedes,
            author=author,
            agent=agent,
        )

    @server.tool(name="hive_search", description=_DESC_SEARCH)
    async def _hive_search(
        query: str,
        limit: int | None = None,
        offset: int | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        return await hive_search(
            resolve_app(),
            query=query,
            limit=limit,
            offset=offset,
            kind=kind,
            tags=tags,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
        )

    @server.tool(name="hive_get", description=_DESC_GET)
    async def _hive_get(
        entry_id: str,
        include_history: bool = False,
    ) -> dict[str, Any]:
        return await hive_get(resolve_app(), entry_id=entry_id, include_history=include_history)

    @server.tool(name="hive_list", description=_DESC_LIST)
    async def _hive_list(
        kind: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await hive_list(
            resolve_app(),
            kind=kind,
            tags=tags,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )

    @server.tool(name="hive_withdraw", description=_DESC_WITHDRAW)
    async def _hive_withdraw(
        entry_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await hive_withdraw(resolve_app(), entry_id=entry_id, reason=reason)

    @server.tool(name="hive_register", description=_DESC_REGISTER)
    async def _hive_register(
        name: str,
        owner_alias: str | None = None,
    ) -> dict[str, Any]:
        return await hive_register(resolve_app(), name=name, owner_alias=owner_alias)

    @server.tool(name="hive_feedback", description=_DESC_FEEDBACK)
    async def _hive_feedback(
        entry_id: str,
        verdict: str,
        note: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        return await hive_feedback(
            resolve_app(), entry_id=entry_id, verdict=verdict, note=note, agent=agent
        )

    return server


def main() -> None:
    """Build a self-contained dev stdio server and run the transport."""
    settings = Settings()
    store = MemoryStore()
    embedder = LocalEmbedder(dimension=settings.embedding_dim)
    write_service = WriteService(store, embedder)
    search_config = settings.search_config()
    search_service = SearchService(store, embedder, search_config)
    governance_service = GovernanceService(store, search_config)
    app = McpHivemind(
        store=store,
        write_service=write_service,
        search_service=search_service,
        governance_service=governance_service,
        access_service=AccessService(store),
        search_config=search_config,
        credential=Credential(user_id="dev", agent_id="hivemind-mcp"),
    )
    server = build_server(app)
    asyncio.run(server.run_stdio_async())


def _mcp_key_missing_hint() -> str:
    """The error text when ``HIVEMIND_MCP_KEY`` is unset."""
    return (
        "HIVEMIND_MCP_KEY is required to run hivemind-mcp-pg. Issue an "
        "agent-scoped key, then set it in the agent's MCP config:\n"
        "  uv run hivemind-keys issue --user <user> --agent <agent>\n"
        '  {"command": "uv", "args": ["run", "--directory", "<repo>", "hivemind-mcp-pg"],\n'
        '   "env": {"HIVEMIND_MCP_KEY": "hm_..."}}'
    )


def _mcp_key_unknown_hint() -> str:
    """The error text when the key is set but is not a known credential."""
    return (
        "HIVEMIND_MCP_KEY is not a known credential. Issue it first, then retry:\n"
        "  uv run hivemind-keys issue --user <user> --agent <agent>\n"
        "  (list existing credential hashes: uv run hivemind-keys list)"
    )


def main_pg() -> None:
    """Build a Postgres-backed stdio server and run the transport.

    The multi-agent production runner (ADR 0009): every agent runs its
    own ``hivemind-mcp-pg`` process with its own ``HIVEMIND_MCP_KEY``
    (a raw ``hm_...`` key issued via ``hivemind-keys``), so all agents
    read/write the same Postgres pool over a unified MCP interface while
    each write carries its agent's *verified* provenance (SPEC §8.1,
    ADR 0008). The real ``PgStore`` + OpenAI-compatible embedder are
    built from ``Settings`` (env-driven); the acting credential is
    resolved by verifying the key against the Postgres ``credentials``
    table. The store/embedder/authenticator pools are torn down on exit.
    """
    from hivemind.embeddings import build_embedder
    from hivemind.store import build_authenticator, build_store

    settings = Settings()
    raw_key = os.environ.get("HIVEMIND_MCP_KEY", "").strip()
    if not raw_key:
        raise SystemExit(_mcp_key_missing_hint())

    store = build_store(settings)
    embedder = build_embedder(settings)
    authenticator = build_authenticator(settings)

    async def _run() -> None:
        credential = await authenticator.verify(raw_key)
        if credential is None:
            raise SystemExit(_mcp_key_unknown_hint())
        search_config = settings.search_config()
        app = McpHivemind(
            store=store,
            write_service=WriteService(store, embedder),
            search_service=SearchService(store, embedder, search_config),
            governance_service=GovernanceService(store, search_config),
            access_service=AccessService(store, authenticator),
            search_config=search_config,
            credential=credential,
        )
        server = build_server(app)
        try:
            await server.run_stdio_async()
        finally:
            for closer in (
                getattr(store, "close", None),
                getattr(embedder, "aclose", None),
                getattr(authenticator, "close", None),
            ):
                if closer is not None:
                    await closer()

    asyncio.run(_run())
