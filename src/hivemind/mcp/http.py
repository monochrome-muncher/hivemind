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
import logging
import os
from collections.abc import Callable
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from hivemind.config import Settings, configure_logging, load_settings
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.server import build_server
from hivemind.ports import Authenticator, Credential, Embedder, Store
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService

logger = logging.getLogger(__name__)

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


# The unauthenticated orchestrator probe paths (ADR 0019). Served by the
# ``ProbeRouter`` BELOW the (outermost) layer — i.e. OUTSIDE the auth
# middleware — because k8s/compose probes carry no API key.
_PROBE_LIVENESS = "/mcp/liveness"
_PROBE_HEALTH = "/mcp/health"


class ProbeRouter:
    """Unauthenticated orchestrator probe endpoints (ADR 0019).

    Wraps the (auth-wrapped) MCP app and answers two GET paths BEFORE
    the auth middleware ever sees them:

    * ``GET /mcp/liveness`` — shallow: 200 ``{"status": "ok"}`` whenever
      the process answers (no dependency checks).
    * ``GET /mcp/health`` — deep: 200 only when ``store.health_check()``
      answers (the Postgres pool is up); 503 ``{"status": "unhealthy",
      "detail": "database unreachable"}`` otherwise.

    Everything else (the ``/mcp`` MCP transport surface, non-GET
    methods, non-HTTP scopes like ``lifespan``) is delegated untouched
    so the MCP app's session management and the auth middleware are
    unaffected.
    """

    def __init__(self, app: ASGIApp, store: Store) -> None:
        self._app = app
        self._store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") in (_PROBE_LIVENESS, _PROBE_HEALTH)
        ):
            await self._serve_probe(scope["path"], send)
            return
        await self._app(scope, receive, send)

    async def _serve_probe(self, path: str, send: Send) -> None:
        if path == _PROBE_LIVENESS:
            await _send_json(send, 200, {"status": "ok"})
            return
        healthy = await self._store.health_check()
        if healthy:
            await _send_json(send, 200, {"status": "ok"})
        else:
            await _send_json(
                send,
                503,
                {"status": "unhealthy", "detail": "database unreachable"},
            )


async def _send_json(send: Send, status: int, payload: dict[str, str]) -> None:
    """A minimal ASGI JSON response (the probe endpoints are fire-and-forget)."""
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
    from hivemind.extractor import build_extractor

    search_config = settings.search_config()
    extractor = build_extractor(settings)  # optional (ADR 0016): None when the endpoint is unset
    template = McpHivemind(
        store=store,
        write_service=WriteService(store, embedder, extractor),
        search_service=SearchService(store, embedder, search_config),
        governance_service=GovernanceService(store, search_config),
        access_service=AccessService(store, authenticator),
        search_config=search_config,
        credential=Credential(user_id="shared", agent_id="hivemind-mcp-http"),
    )
    server = build_server(template, credential_provider=make_credential_provider())
    host = os.environ.get("HIVEMIND_HOST", "0.0.0.0")
    mcp_app = server.streamable_http_app(stateless_http=True, host=host)
    # Outermost layer: the unauthenticated probe router (ADR 0019) wraps
    # the auth-wrapped MCP app, so the k8s/compose probes are answered
    # without any API key while the MCP surface stays fully protected.
    return ProbeRouter(BearerAuthMiddleware(mcp_app, authenticator), store)


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

    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info(
        "starting hivemind-mcp-http: embedding_endpoint=%s embedding_dim=%d "
        "extraction=%s pool_max_size=%d",
        settings.embedding_endpoint,
        settings.embedding_dim,
        "on" if settings.extractor_endpoint else "off",
        settings.pool_max_size,
    )
    store = build_store(settings)
    embedder = build_embedder(settings)
    authenticator = build_authenticator(settings)
    app = build_http_app(settings, store, embedder, authenticator)
    host = os.environ.get("HIVEMIND_HOST", "0.0.0.0")
    port = int(os.environ.get("HIVEMIND_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
