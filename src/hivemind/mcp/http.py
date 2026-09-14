"""The hostable, multi-agent streamable-HTTP MCP runner (ADR 0010).

One long-lived process serves an *unlimited* number of agents over a
single streamable-HTTP endpoint. Each agent authenticates **per
request** with its own agent-scoped key (ADR 0008); all agents share one
Postgres pool and one embedder. Every write carries that agent's
*verified* provenance (never self-reported).

The seam: the acting credential is resolved per request by an ASGI
auth middleware (:class:`BearerAuthMiddleware`) that verifies the
request's API key against the ``Authenticator`` port (the Postgres
``credentials`` table) and stashes the resolved ``Credential`` in a
per-task context var. ``build_server`` (``server.py``) re-binds the
shared, *stateless* services to that credential on every tool dispatch,
so one process = many agents. Contrast the per-agent stdio runner
(``main_pg``, ADR 0009): here the credential is per *request*, so
revocation is immediate (no restart required).

``main_http`` (console: ``hivemind-mcp-http``) is the entry point: one
``PgStore`` + one OpenAI-compatible embedder + one ``Authenticator``
pool, served over ``uvicorn``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from hivemind.config import Settings
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.server import build_server
from hivemind.ports import Authenticator, Credential, Embedder, Store
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService

# The acting credential for the *current* request. Set by the auth
# middleware, read by ``build_server``'s ``credential_provider`` on each
# tool dispatch. Per-task: an ASGI request is one asyncio task, so the
# value is visible to the tool dispatch within that request.
_current_credential: ContextVar[Credential | None] = ContextVar(
    "hivemind_mcp_credential", default=None
)


def current_credential() -> Credential | None:
    """The acting credential for the current request (or ``None``)."""
    return _current_credential.get()


def make_credential_provider() -> Callable[[], Credential | None]:
    """A zero-arg callable for ``build_server``'s ``credential_provider``.

    It reads the per-request credential set by :class:`BearerAuthMiddleware`,
    so each tool dispatch acts as the key that authenticated that request.
    """
    return _current_credential.get


def _extract_key(scope: Scope) -> str | None:
    """The request's API key: ``Authorization: Bearer <key>`` or ``X-Api-Key``."""
    raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    headers = {k.lower(): v for k, v in raw_headers}
    authorization = headers.get(b"authorization")
    if authorization:
        parts = authorization.decode("latin-1").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    api_key = headers.get(b"x-api-key")
    return api_key.decode("latin-1").strip() if api_key else None


async def _send_unauthorized(send: Send) -> None:
    """Send a minimal 401 JSON response (no body needed on the MCP path)."""
    body = json.dumps(
        {
            "error": "unauthorized",
            "detail": "a valid API key is required (Authorization: Bearer <key> or X-Api-Key)",
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """A thin ASGI auth middleware: verify the request's API key against
    the ``Authenticator`` port; on success stash the credential in the
    per-task context var and dispatch, on failure send a 401.

    Non-HTTP scopes (e.g. ``lifespan``) pass straight through so the
    transport's session management is unaffected.
    """

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        key = _extract_key(scope)
        credential = await self._authenticator.verify(key) if key else None
        if credential is None:
            await _send_unauthorized(send)
            return
        token = _current_credential.set(credential)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_credential.reset(token)


def build_http_app(
    settings: Settings,
    store: Store,
    embedder: Embedder,
    authenticator: Authenticator,
) -> ASGIApp:
    """One shared pool, many agents: build the streamable-HTTP ASGI app.

    The ``store`` / ``embedder`` / services are *shared and stateless*
    across all requests; the acting credential is resolved per request by
    :class:`BearerAuthMiddleware` (the ``Authenticator`` port) and
    re-bound to the services on every tool dispatch. The result is a
    single ASGI app (the MCP ``streamable_http_app`` wrapped in the auth
    middleware) that serves an unlimited number of agents, each with its
    own verified provenance (ADR 0010).
    """
    search_config = settings.search_config()
    template = McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, search_config),
        governance_service=GovernanceService(store, search_config),
        search_config=search_config,
        credential=Credential(user_id="shared", agent_id="hivemind-mcp-http"),
    )
    server = build_server(template, credential_provider=make_credential_provider())
    host = os.environ.get("HIVEMIND_HOST", "0.0.0.0")
    mcp_app = server.streamable_http_app(stateless_http=True, host=host)
    return BearerAuthMiddleware(mcp_app, authenticator)


def main_http() -> None:
    """Console entry point (``hivemind-mcp-http``): one hostable
    streamable-HTTP process.

    One long-lived process serves an unlimited number of agents over a
    single endpoint, with one ``PgStore`` + one embedder + one
    ``Authenticator`` pool (ADR 0010). Each agent authenticates per
    request with its own agent-scoped key (ADR 0008); all agents share one
    Postgres pool, and each write carries that agent's verified
    provenance (never self-reported).
    """
    import uvicorn

    from hivemind.embeddings import build_embedder
    from hivemind.store import build_authenticator, build_store

    settings = Settings()
    store = build_store(settings)
    embedder = build_embedder(settings)
    authenticator = build_authenticator(settings)
    app = build_http_app(settings, store, embedder, authenticator)
    host = os.environ.get("HIVEMIND_HOST", "0.0.0.0")
    port = int(os.environ.get("HIVEMIND_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
