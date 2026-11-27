"""The Hivemind client: one MCP session as one agent."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Literal, cast, overload

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation, TextContent

from hivemind_memory import text
from hivemind_memory._version import __version__
from hivemind_memory.conversation import Conversation
from hivemind_memory.errors import AUTH_CODES, AuthenticationError, HivemindError
from hivemind_memory.formats import (
    ToolDefinitions,
    ToolSpecs,
    anthropic_tools,
    openai_responses_tools,
    openai_tools,
)
from hivemind_memory.replies import (
    BatchFeedbackReply,
    CheckReply,
    Entries,
    Entry,
    EntryDetail,
    EntryKind,
    FeedbackReply,
    ListReply,
    PinnedReply,
    PinReply,
    Recency,
    RegisterReply,
    SearchReply,
    Verdict,
    WhoAmI,
    WriteReply,
)

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup

EntryIds = list[str] | tuple[str, ...]

URL_ENV = "HIVEMIND_MCP_URL"
KEY_ENV = "HIVEMIND_API_KEY"

_TIMEOUT = httpx2.Timeout(30.0, read=300.0)
_REJECTED = "the server rejected the API key (HTTP 401)"


def _setting(value: str | None, env_name: str, what: str) -> str:
    resolved = value or os.environ.get(env_name)
    if not resolved:
        raise ValueError(f"no {what}: pass it as an argument or set {env_name}")
    return resolved


def _present(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if value is not None}


def _entry_arguments(entry: str | Sequence[str]) -> dict[str, Any]:
    if isinstance(entry, str):
        return {"entry_id": entry}
    return {"entry_ids": list(entry)}


def _error_text(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


def _model_arguments(arguments: Mapping[str, Any] | str) -> dict[str, Any]:
    if not isinstance(arguments, str):
        return dict(arguments)
    if not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the arguments are not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("the arguments must be a JSON object")
    return parsed


def _parsed(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _refusal(reply: Any) -> HivemindError | None:
    """The error behind a reply that is the ``{"error": {"code", "message"}}`` envelope."""
    if not isinstance(reply, dict) or set(reply) != {"error"}:
        return None
    error = reply["error"]
    if not isinstance(error, dict):
        return None
    code, message = str(error.get("code") or ""), str(error.get("message") or "")
    return (AuthenticationError if code in AUTH_CODES else HivemindError)(code, message)


def _single_leaf(exc: BaseException) -> BaseException | None:
    while isinstance(exc, BaseExceptionGroup):
        if len(exc.exceptions) != 1:
            return None
        exc = exc.exceptions[0]
    return exc


@asynccontextmanager
async def _without_task_group_wrapping() -> AsyncIterator[None]:
    """The MCP client runs on a task group, which wraps any error in an exception group."""
    try:
        yield
    except BaseExceptionGroup as group:
        leaf = _single_leaf(group)
        if leaf is None:
            raise
        raise leaf from None


class _Rejection:
    """Remembers why the server answered 401: the MCP SDK reports it only as a generic error.

    The body is ``{"error": "key_locked" | "unauthorized", "detail": ...}``.
    """

    def __init__(self) -> None:
        self.code: str | None = None
        self.message = ""

    async def observe(self, response: httpx2.Response) -> None:
        if response.status_code != 401:
            return
        try:
            body = _parsed((await response.aread()).decode(errors="replace"))
        except httpx2.HTTPError:
            body = None
        body = body if isinstance(body, dict) else {}
        detail = body.get("detail")
        self.code = "key_locked" if body.get("error") == "key_locked" else "unauthenticated"
        self.message = detail if isinstance(detail, str) and detail else _REJECTED

    def raise_if_seen(self) -> None:
        if self.code is not None:
            raise AuthenticationError(self.code, self.message)

    def forget(self) -> None:
        self.code = None


class Hivemind:
    """A connected session. Build one with :meth:`connect` or :meth:`open`."""

    def __init__(self, client: Client, rejection: _Rejection) -> None:
        self._client = client
        self._rejection = rejection
        self._stack: AsyncExitStack | None = None
        self.whoami = cast(WhoAmI, {})
        self.tools: ToolDefinitions = []

    @classmethod
    def connect(
        cls, url: str | None = None, api_key: str | None = None
    ) -> AbstractAsyncContextManager[Hivemind]:
        """Open a session: ``async with Hivemind.connect() as hive``.

        Arguments win over the ``HIVEMIND_MCP_URL`` and ``HIVEMIND_API_KEY`` variables.
        """
        return cls._open_http(
            _setting(url, URL_ENV, "server URL"), _setting(api_key, KEY_ENV, "API key")
        )

    @classmethod
    async def open(cls, url: str | None = None, api_key: str | None = None) -> Hivemind:
        """Like :meth:`connect` for code that cannot use ``async with``; end it with :meth:`close`."""
        stack = AsyncExitStack()
        hive = await stack.enter_async_context(cls.connect(url, api_key))
        hive._stack = stack
        return hive

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            await stack.aclose()

    @classmethod
    @asynccontextmanager
    async def _open_http(cls, url: str, api_key: str) -> AsyncIterator[Hivemind]:
        rejection = _Rejection()
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT,
            event_hooks={"response": [rejection.observe]},
        ) as http:
            transport = streamable_http_client(url, http_client=http)
            async with cls._open_target(transport, rejection) as hive:
                yield hive

    @classmethod
    @asynccontextmanager
    async def _open_target(
        cls, target: Any, rejection: _Rejection | None = None
    ) -> AsyncIterator[Hivemind]:
        """Connect to anything the MCP ``Client`` accepts; tests pass an in-process server."""
        rejection = rejection or _Rejection()
        info = Implementation(name="hivemind-memory", version=__version__)
        async with _without_task_group_wrapping(), AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(Client(target, client_info=info))
                hive = cls(client, rejection)
                await hive._handshake()
            except Exception:
                rejection.raise_if_seen()
                raise
            yield hive

    async def _handshake(self) -> None:
        cursor: str | None = None
        while True:
            page = await self._client.list_tools(cursor=cursor)
            self.tools.extend(
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": dict(tool.input_schema),
                }
                for tool in page.tools
            )
            cursor = page.next_cursor
            if cursor is None:
                break
        await self.refresh_whoami()

    async def _call_text(self, name: str, arguments: Mapping[str, Any]) -> str:
        """The reply text; a failed call that is not already the error envelope is wrapped in one."""
        try:
            result = await self._client.call_tool(name, dict(arguments))
        except Exception:
            self._rejection.raise_if_seen()
            raise
        self._rejection.forget()
        reply = "".join(b.text for b in result.content if isinstance(b, TextContent))
        if result.is_error and _refusal(_parsed(reply)) is None:
            return _error_text("tool_error", reply or f"{name} failed")
        return reply

    async def _tool(self, tool: str, /, **arguments: Any) -> dict[str, Any]:
        return await self.call(tool, _present(arguments))

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool and return its JSON reply; a refusal raises :class:`HivemindError`."""
        reply_text = await self._call_text(name, arguments or {})
        reply = _parsed(reply_text)
        if (error := _refusal(reply)) is not None:
            raise error
        if not isinstance(reply, dict):
            raise HivemindError("invalid_reply", reply_text)
        return reply

    async def run_tool(self, name: str, arguments: Mapping[str, Any] | str) -> str:
        """Run a tool for a model: its arguments, as the model wrote them, in; the reply text out.

        A refusal comes back as the reply text, never as an exception, so the model sees its code.
        """
        if name not in {tool["name"] for tool in self.tools}:
            return _error_text("invalid_input", f"unknown tool {name!r}")
        try:
            parsed = _model_arguments(arguments)
        except ValueError as exc:
            return _error_text("invalid_input", str(exc))
        return await self._call_text(name, parsed)

    def instructions(self) -> str:
        """The system prompt for an agent: the server's instructions, then the ``hivemind`` skill."""
        return text.INSTRUCTIONS

    def reminder(self) -> str:
        """The end-of-task text. :meth:`Conversation.reminder_due` says when to send it."""
        return text.REMINDER

    def after_compaction(self) -> str:
        """The line to add after the app compacts or summarizes a conversation's context."""
        return text.COMPACTED

    def conversation(self, *, min_tool_calls: int | None = None) -> Conversation:
        """Start counting a new conversation's tool calls, for the end-of-task reminder."""
        return Conversation(self, min_tool_calls)

    def anthropic_tools(self) -> ToolSpecs:
        return anthropic_tools(self.tools)

    def openai_tools(self) -> ToolSpecs:
        """The tools for OpenAI Chat Completions."""
        return openai_tools(self.tools)

    def openai_responses_tools(self) -> ToolSpecs:
        """The tools for the OpenAI Responses API."""
        return openai_responses_tools(self.tools)

    async def refresh_whoami(self) -> WhoAmI:
        """Ask ``hive_whoami`` again, for example after an admin changed the key's trust."""
        self.whoami = cast(WhoAmI, await self.call("hive_whoami"))
        return self.whoami

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        kind: EntryKind | None = None,
        tags: Sequence[str] | None = None,
        entities: Sequence[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool | None = None,
        flagged: bool | None = None,
        explore: bool | None = None,
        recency: Recency | None = None,
    ) -> SearchReply:
        """Hybrid search over the pool: compact hits, no bodies."""
        reply = await self._tool(
            "hive_search",
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
        return cast(SearchReply, reply)

    @overload
    async def get(self, entry: str, *, include_history: bool | None = None) -> EntryDetail: ...
    @overload
    async def get(self, entry: EntryIds) -> Entries: ...
    async def get(
        self, entry: str | Sequence[str], *, include_history: bool | None = None
    ) -> EntryDetail | Entries:
        """One entry in full by id, or up to 10 from a list of ids."""
        reply = await self._tool(
            "hive_get", **_entry_arguments(entry), include_history=include_history
        )
        return cast(EntryDetail | Entries, reply)

    async def list(
        self,
        *,
        kind: EntryKind | None = None,
        tags: Sequence[str] | None = None,
        entities: Sequence[str] | None = None,
        scope: str | None = None,
        author: str | None = None,
        agent: str | None = None,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        include_inactive: bool | None = None,
        flagged: bool | None = None,
        relied_on_changed: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include_body: bool | None = None,
    ) -> ListReply:
        """Filter entries without a query."""
        reply = await self._tool(
            "hive_list",
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
            relied_on_changed=relied_on_changed,
            limit=limit,
            offset=offset,
            include_body=include_body,
        )
        return cast(ListReply, reply)

    async def pinned(self, *, fleet_id: str | None = None) -> PinnedReply:
        """A fleet's pinned entries."""
        return cast(PinnedReply, await self._tool("hive_pinned", fleet_id=fleet_id))

    @overload
    async def write(
        self,
        kind: EntryKind,
        summary: str,
        *,
        body: str | None = None,
        payload: Mapping[str, Any] | None = None,
        sources: Sequence[Mapping[str, str]] | None = None,
        tags: Sequence[str] | None = None,
        occurred_at: str | None = None,
        importance: int | None = None,
        scope: str | None = None,
        supersedes: Sequence[str] | None = None,
        agent: str | None = None,
        see_also: Sequence[str] | None = None,
        check: Literal[False] | None = None,
    ) -> WriteReply: ...
    @overload
    async def write(
        self,
        kind: EntryKind,
        summary: str,
        *,
        body: str | None = None,
        payload: Mapping[str, Any] | None = None,
        sources: Sequence[Mapping[str, str]] | None = None,
        tags: Sequence[str] | None = None,
        occurred_at: str | None = None,
        importance: int | None = None,
        scope: str | None = None,
        supersedes: Sequence[str] | None = None,
        agent: str | None = None,
        see_also: Sequence[str] | None = None,
        check: Literal[True],
    ) -> CheckReply: ...
    async def write(
        self,
        kind: EntryKind,
        summary: str,
        *,
        body: str | None = None,
        payload: Mapping[str, Any] | None = None,
        sources: Sequence[Mapping[str, str]] | None = None,
        tags: Sequence[str] | None = None,
        occurred_at: str | None = None,
        importance: int | None = None,
        scope: str | None = None,
        supersedes: Sequence[str] | None = None,
        agent: str | None = None,
        see_also: Sequence[str] | None = None,
        check: bool | None = None,
    ) -> WriteReply | CheckReply:
        """Write an entry (``fact``, ``insight`` or ``decision``); ``check=True`` stores nothing."""
        reply = await self._tool(
            "hive_write",
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
        return cast(WriteReply | CheckReply, reply)

    @overload
    async def feedback(
        self, entry: str, verdict: Verdict, *, note: str | None = None, agent: str | None = None
    ) -> FeedbackReply: ...
    @overload
    async def feedback(
        self,
        entry: EntryIds,
        verdict: Verdict,
        *,
        note: str | None = None,
        agent: str | None = None,
    ) -> BatchFeedbackReply: ...
    async def feedback(
        self,
        entry: str | Sequence[str],
        verdict: Verdict,
        *,
        note: str | None = None,
        agent: str | None = None,
    ) -> FeedbackReply | BatchFeedbackReply:
        """Rate an entry by id, or up to 16 from a list, ``helpful``, ``stale`` or ``wrong``."""
        reply = await self._tool(
            "hive_feedback", **_entry_arguments(entry), verdict=verdict, note=note, agent=agent
        )
        return cast(FeedbackReply | BatchFeedbackReply, reply)

    async def withdraw(self, entry_id: str, reason: str | None = None) -> Entry:
        """Retract an entry without replacing it; the reply is the withdrawn entry."""
        return cast(Entry, await self._tool("hive_withdraw", entry_id=entry_id, reason=reason))

    async def pin(self, entry_id: str, *, unpin: bool | None = None) -> PinReply:
        """Pin a fleet entry to its fleet's briefing, or unpin it."""
        return cast(PinReply, await self._tool("hive_pin", entry_id=entry_id, unpin=unpin))

    async def register(self, name: str, *, owner_alias: str | None = None) -> RegisterReply:
        """Register an agent (an org key only)."""
        reply = await self._tool("hive_register", name=name, owner_alias=owner_alias)
        return cast(RegisterReply, reply)
