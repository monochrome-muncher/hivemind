"""The stdio MCP server exposing the Hivemind tools (SPEC §5.2).

``build_server`` binds the ``app.py`` tool functions into an mcp 2.x
``MCPServer`` (not ``FastMCP``). ``main`` is the in-memory dev server;
``main_pg`` (``hivemind-mcp-pg``) is the per-agent Postgres runner keyed
by ``HIVEMIND_MCP_KEY`` (ADR 0009). The hostable streamable-HTTP runner
lives in ``hivemind.mcp.http`` (ADR 0010).
"""

from __future__ import annotations

import inspect
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
    ``title`` (the property name again, or ``hive_writeArguments``) and the
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


def register_tool(
    server: MCPServer,
    name: str,
    description: str,
    verb: Callable[..., Awaitable[dict[str, object]]],
    dispatch: Callable[..., Awaitable[dict[str, object]]],
) -> None:
    """Bind one app tool to the server (ADR 0085). The closure carries
    ``verb``'s signature — the MCP schema is generated from it — and every
    call resolves the acting app first; the identity never leaves the
    server."""

    async def _tool(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await dispatch(verb, **kwargs)

    # The MCP schema is generated from the closure's signature at decoration
    # time: the verb's, minus its ``app`` parameter (bound here, never
    # exposed). Set before the decorator snapshots it.
    _tool.__name__ = verb.__name__
    _tool.__doc__ = verb.__doc__
    setattr(_tool, "__wrapped__", verb)  # noqa: B010 (mypy: dynamic attr)
    parameters = list(inspect.signature(verb).parameters.values())[1:]
    setattr(_tool, "__signature__", inspect.signature(verb).replace(parameters=parameters))  # noqa: B010 (mypy: dynamic attr)
    server.tool(name=name, description=description)(_tool)


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

    # The ten tools, registered explicitly (ADR 0085): one line each; the
    # helper binds the signature and the per-call app resolution.
    register_tool(server, "hive_write", contract.DESC_WRITE, hive_write, dispatch)
    register_tool(server, "hive_search", contract.DESC_SEARCH, hive_search, dispatch)
    register_tool(server, "hive_get", contract.DESC_GET, hive_get, dispatch)
    register_tool(server, "hive_list", contract.DESC_LIST, hive_list, dispatch)
    register_tool(server, "hive_withdraw", contract.DESC_WITHDRAW, hive_withdraw, dispatch)
    register_tool(server, "hive_pinned", contract.DESC_PINNED, hive_pinned, dispatch)
    register_tool(server, "hive_pin", contract.DESC_PIN, hive_pin, dispatch)
    register_tool(server, "hive_register", contract.DESC_REGISTER, hive_register, dispatch)
    register_tool(server, "hive_feedback", contract.DESC_FEEDBACK, hive_feedback, dispatch)
    register_tool(server, "hive_whoami", contract.DESC_WHOAMI, hive_whoami, dispatch)
    return server
