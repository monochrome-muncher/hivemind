"""The stdio MCP server exposing the Hivemind tools (SPEC §5.2).

``build_server`` binds the ``app.py`` tool functions into an mcp 2.x
``MCPServer`` (not ``FastMCP``). ``main`` is the in-memory dev server;
``main_pg`` (``hivemind-mcp-pg``) is the per-agent Postgres runner keyed
by ``HIVEMIND_MCP_KEY`` (ADR 0009). The hostable streamable-HTTP runner
lives in ``hivemind.mcp.http`` (ADR 0010).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import Tool as MCPTool

from hivemind.mcp import contract
from hivemind.mcp.app import (
    ERR_STORE_UNAVAILABLE,
    ERR_UNAUTHENTICATED,
    STORE_UNAVAILABLE_MESSAGE,
    McpHivemind,
    _error,
    hive_feedback,
    hive_get,
    hive_list,
    hive_pin,
    hive_pinned,
    hive_register,
    hive_search,
    hive_whoami,
    hive_withdraw,
    hive_write,
)
from hivemind.ports import Credential

logger = logging.getLogger(__name__)


def compact_schema(schema: Any) -> Any:
    """A JSON schema without what pydantic adds for nothing: every
    ``title`` (the property name again, or ``_hive_writeArguments``) and the
    ``anyOf [T, null]`` / ``default: null`` of an optional argument, which
    becomes plain ``T`` (it is not in ``required``, so it can be left out).
    Clients load these schemas into every session's context; the server
    still validates arguments with its own model, so a client that sends
    ``null`` keeps working."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: compact_schema(v) for k, v in schema.items() if k not in ("title", "properties")}
    if isinstance(schema.get("properties"), dict):
        # Keys here are argument names, not keywords: an argument may be "title".
        out["properties"] = {k: compact_schema(v) for k, v in schema["properties"].items()}
    variants = out.get("anyOf")
    if isinstance(variants, list) and {"type": "null"} in variants:
        rest = [v for v in variants if v != {"type": "null"}]
        if len(rest) == 1 and out.get("default", ...) is None:
            del out["anyOf"], out["default"]
            out.update(rest[0])
    return out


class _CompactSchemaServer(MCPServer):
    """``MCPServer`` whose ``tools/list`` answer carries compact schemas."""

    async def list_tools(self) -> list[MCPTool]:
        return [
            tool.model_copy(
                update={
                    "input_schema": compact_schema(tool.input_schema),
                    "output_schema": compact_schema(tool.output_schema),
                }
            )
            for tool in await super().list_tools()
        ]


def build_server(
    app: McpHivemind,
    *,
    credential_provider: Callable[[], Credential | Awaitable[Credential | None] | None]
    | None = None,
    require_stored_keys: bool = False,
) -> MCPServer:
    """Build an ``MCPServer`` exposing exactly the ten Hivemind tools.

    Tools are closures over ``app``, so identity and services are bound,
    never exposed as arguments.

    With ``credential_provider`` (sync or async), each dispatch re-binds
    ``app``'s stateless services to the provider's credential (per
    request over HTTP, ADR 0010; re-verified per call for
    ``hivemind-mcp-pg``). A provider yielding ``None`` fails closed with
    ``unauthenticated``, never falling back to ``app.credential`` (ADR 0042).
    """
    server = _CompactSchemaServer(
        name="hivemind",
        description="Shared memory pool for an organization's AI agents.",
        instructions=contract.INSTRUCTIONS,
    )

    async def resolve_app() -> McpHivemind | None:
        """The acting ``McpHivemind`` for this dispatch, or ``None`` when
        the provider yields no credential (fail closed, ADR 0042)."""
        if credential_provider is None:
            return app
        resolved = credential_provider()
        credential = await resolved if isinstance(resolved, Awaitable) else resolved
        if credential is None:
            return None
        # ADR 0082: a dev-mode credential sits at trust level 0 when the
        # deployment requires stored keys.
        credential = credential.effective(require_stored_keys)
        if credential == app.credential:
            return app
        return replace(app, credential=credential)

    async def dispatch(
        verb: Callable[..., Awaitable[dict[str, object]]], **kwargs: Any
    ) -> dict[str, object]:
        try:
            acting = await resolve_app()
            if acting is None:
                return _error(
                    ERR_UNAUTHENTICATED,
                    "this key is no longer valid (revoked or unknown); ask your admin",
                )
            return await verb(acting, **kwargs)
        except TimeoutError:
            # A pool acquire or statement timed out: the database is busy,
            # not the call wrong (ROADMAP 3.13; the REST surface answers 503).
            logger.warning("store call timed out during an MCP tool call")
            return _error(ERR_STORE_UNAVAILABLE, STORE_UNAVAILABLE_MESSAGE)

    @server.tool(name="hive_write", description=contract.DESC_WRITE)
    async def _hive_write(
        kind: str,
        summary: str,
        body: str | None = None,
        payload: dict[str, Any] | None = None,
        sources: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
        occurred_at: str | None = None,
        importance: int | None = None,
        scope: str | None = None,
        supersedes: list[str] | None = None,
        agent: str | None = None,
        see_also: list[str] | None = None,
        check: bool = False,
    ) -> dict[str, Any]:
        return await dispatch(
            hive_write,
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
            agent=agent,
            see_also=see_also,
            check=check,
        )

    @server.tool(name="hive_search", description=contract.DESC_SEARCH)
    async def _hive_search(
        query: str,
        limit: int | None = None,
        offset: int | None = None,
        kind: str | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool = False,
        flagged: bool = False,
        explore: bool = False,
        recency: str | None = None,
    ) -> dict[str, Any]:
        return await dispatch(
            hive_search,
            query=query,
            limit=limit,
            offset=offset,
            kind=kind,
            tags=tags,
            entities=entities,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
            flagged=flagged,
            explore=explore,
            recency=recency,
        )

    @server.tool(name="hive_get", description=contract.DESC_GET)
    async def _hive_get(
        entry_id: str = "",
        include_history: bool = False,
        entry_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return await dispatch(
            hive_get, entry_id=entry_id, include_history=include_history, entry_ids=entry_ids
        )

    @server.tool(name="hive_list", description=contract.DESC_LIST)
    async def _hive_list(
        kind: str | None = None,
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool = False,
        flagged: bool = False,
        limit: int | None = None,
        offset: int = 0,
        include_body: bool = False,
    ) -> dict[str, Any]:
        return await dispatch(
            hive_list,
            kind=kind,
            tags=tags,
            entities=entities,
            scope=scope,
            author=author,
            agent=agent,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            created_from=created_from,
            created_to=created_to,
            include_inactive=include_inactive,
            flagged=flagged,
            limit=limit,
            offset=offset,
            include_body=include_body,
        )

    @server.tool(name="hive_withdraw", description=contract.DESC_WITHDRAW)
    async def _hive_withdraw(
        entry_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await dispatch(hive_withdraw, entry_id=entry_id, reason=reason)

    @server.tool(name="hive_pinned", description=contract.DESC_PINNED)
    async def _hive_pinned(fleet_id: str | None = None) -> dict[str, Any]:
        return await dispatch(hive_pinned, fleet_id=fleet_id)

    @server.tool(name="hive_pin", description=contract.DESC_PIN)
    async def _hive_pin(entry_id: str, unpin: bool = False) -> dict[str, Any]:
        return await dispatch(hive_pin, entry_id=entry_id, unpin=unpin)

    @server.tool(name="hive_register", description=contract.DESC_REGISTER)
    async def _hive_register(
        name: str,
        owner_alias: str | None = None,
    ) -> dict[str, Any]:
        return await dispatch(hive_register, name=name, owner_alias=owner_alias)

    @server.tool(name="hive_feedback", description=contract.DESC_FEEDBACK)
    async def _hive_feedback(
        verdict: str,
        entry_id: str = "",
        note: str | None = None,
        agent: str | None = None,
        entry_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return await dispatch(
            hive_feedback,
            entry_id=entry_id,
            verdict=verdict,
            note=note,
            agent=agent,
            entry_ids=entry_ids,
        )

    @server.tool(name="hive_whoami", description=contract.DESC_WHOAMI)
    async def _hive_whoami() -> dict[str, Any]:
        return await dispatch(hive_whoami)

    return server
