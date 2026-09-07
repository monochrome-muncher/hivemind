"""The stdio MCP server exposing the six Hivemind tools (SPEC §5.2).

``build_server`` wires the six plain tool functions from ``app.py`` into
an ``MCPServer`` (mcp 2.x) as closures bound to a single ``McpHivemind``
app instance. ``main`` builds a self-contained dev server (in-memory
store + local embedder, per the v1 dev path) and runs the stdio
transport. A DSN-backed ``PgStore`` is a documented follow-up.

The installed ``mcp`` package is v2.x: the server class is
``MCPServer`` (not ``FastMCP``), tools are registered with
``@server.tool(...)`` / ``server.add_tool``, and the stdio transport is
started with ``await server.run_stdio_async()``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from hivemind.config import Settings
from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_list,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.mcp.local_embedder import LocalEmbedder
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
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
    "Withdraw an entry (retract without replacing). Only the author or an "
    "admin may withdraw."
)
_DESC_FEEDBACK = (
    "Report helpful|stale|wrong on an entry the agent relied on. One verdict "
    "per (entry, user, agent); the latest wins."
)


def build_server(app: McpHivemind) -> MCPServer:
    """Build an ``MCPServer`` exposing exactly the six Hivemind tools.

    Each registered tool is a closure over ``app`` so the LLM only ever
    sees the LLM-facing arguments; the acting identity and services are
    bound, not exposed as arguments.
    """
    server = MCPServer(
        name="hivemind",
        description="Shared memory pool for an organization's AI agents.",
        instructions=_INSTRUCTIONS,
    )

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
            app,
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
            app,
            query=query,
            limit=limit,
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
        return await hive_get(app, entry_id=entry_id, include_history=include_history)

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
            app,
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
        return await hive_withdraw(app, entry_id=entry_id, reason=reason)

    @server.tool(name="hive_feedback", description=_DESC_FEEDBACK)
    async def _hive_feedback(
        entry_id: str,
        verdict: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        return await hive_feedback(
            app, entry_id=entry_id, verdict=verdict, note=note
        )

    return server


def main() -> None:
    """Build a self-contained dev stdio server and run the transport."""
    settings = Settings()
    store = MemoryStore()
    embedder = LocalEmbedder(dimension=settings.embedding_dim)
    write_service = WriteService(store, embedder)
    search_service = SearchService(store, embedder, settings.search_config())
    governance_service = GovernanceService(store)
    app = McpHivemind(
        store=store,
        write_service=write_service,
        search_service=search_service,
        governance_service=governance_service,
        search_config=settings.search_config(),
        credential=Credential(user_id="dev", agent_id="hivemind-mcp"),
    )
    server = build_server(app)
    asyncio.run(server.run_stdio_async())
