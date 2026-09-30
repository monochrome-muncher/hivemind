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

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from contextvars import ContextVar

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from hivemind.config import Settings, configure_logging, load_settings, redact_url
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
                # Tells a client this is a plain bearer-key API (ONB-8).
                (b"www-authenticate", b'Bearer realm="hivemind"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# Hivemind has no OAuth: MCP clients that get a 401 probe these discovery
# documents and, on anything but a 404, start an OAuth flow and report
# confusing "dynamic client registration" errors instead of "your key was
# rejected". A plain 404 says "no OAuth here" (ONB-8, ADR 0042).
_OAUTH_DISCOVERY_PREFIX = "/.well-known/oauth-"


async def _send_not_found(send: Send) -> None:
    body = json.dumps({"error": "not_found"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 404,
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
        if str(scope.get("path", "")).startswith(_OAUTH_DISCOVERY_PREFIX):
            await _send_not_found(send)
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
_PROBE_DATABASE = "/mcp/health/database"
# Bound on the deep database probe (ADR 0037): a check stuck on the pool
# answers 503 in time instead of hanging the request.
_DATABASE_PROBE_TIMEOUT = 5.0
# The deep probe is unauthenticated (ADR 0019) and costs a pool round-trip,
# so its answer is reused for this long: a flood of GETs costs one query
# per window, not one per request (MCP-10).
_DATABASE_PROBE_CACHE_SECONDS = 2.0


class ProbeRouter:
    """Unauthenticated orchestrator probe endpoints (ADRs 0019, 0037).

    Wraps the (auth-wrapped) MCP app and answers three GET paths BEFORE
    the auth middleware ever sees them:

    * ``GET /mcp/liveness`` — shallow: 200 ``{"status": "ok"}`` whenever
      the process answers (no dependency checks).
    * ``GET /mcp/health`` — shallow too (ADR 0037): the readiness probe.
      Readiness must not depend on the one shared database: an outage
      would pull every replica out of service at once, turning a blip
      into "Hivemind is gone" for every agent, with no replica left to
      serve anything once the database is back.
    * ``GET /mcp/health/database`` — deep, for people and monitoring:
      200 ``{"status": "ok", "database": "ok"}`` when
      ``store.health_check()`` answers within ``_DATABASE_PROBE_TIMEOUT``;
      503 otherwise. The store logs why (the exception) — the response
      never carries connection details.

    Everything else (the ``/mcp`` MCP transport surface, non-GET
    methods, non-HTTP scopes like ``lifespan``) is delegated untouched
    so the MCP app's session management and the auth middleware are
    unaffected.
    """

    def __init__(self, app: ASGIApp, store: Store) -> None:
        self._app = app
        self._store = store
        self._db_probe_lock = asyncio.Lock()
        self._db_probe_result: tuple[float, bool] | None = None  # (monotonic time, healthy)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") in (_PROBE_LIVENESS, _PROBE_HEALTH, _PROBE_DATABASE)
        ):
            await self._serve_probe(scope["path"], send)
            return
        await self._app(scope, receive, send)

    async def _database_healthy(self) -> bool:
        """The deep check, reused for ``_DATABASE_PROBE_CACHE_SECONDS`` and
        single-flight, so this unauthenticated endpoint cannot be used to
        amplify load onto the shared pool."""
        async with self._db_probe_lock:
            cached = self._db_probe_result
            if cached is not None and time.monotonic() - cached[0] < _DATABASE_PROBE_CACHE_SECONDS:
                return cached[1]
            try:
                healthy = await asyncio.wait_for(
                    self._store.health_check(), timeout=_DATABASE_PROBE_TIMEOUT
                )
            except TimeoutError:
                logger.warning(
                    "database probe timed out after %.0fs (pool busy or connection hanging)",
                    _DATABASE_PROBE_TIMEOUT,
                )
                healthy = False
            self._db_probe_result = (time.monotonic(), healthy)
            return healthy

    async def _serve_probe(self, path: str, send: Send) -> None:
        if path in (_PROBE_LIVENESS, _PROBE_HEALTH):
            await _send_json(send, 200, {"status": "ok"})
            return
        healthy = await self._database_healthy()
        if healthy:
            await _send_json(send, 200, {"status": "ok", "database": "ok"})
        else:
            await _send_json(
                send,
                503,
                {"status": "unhealthy", "database": "unreachable", "detail": "see the pod log"},
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


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_transport_security(settings: Settings, host: str) -> TransportSecuritySettings | None:
    """The MCP SDK's DNS-rebinding (Host/Origin) protection settings.

    Unset ``HIVEMIND_MCP_ALLOWED_HOSTS`` keeps the SDK default, which
    protects only a loopback bind and is OFF on ``0.0.0.0`` (so an ingress
    in front, whose Host is the public name, keeps working — ADR 0042).
    Set it (comma-separated Host header values, ``name`` or ``name:*``) to
    turn the check on for any bind; ``HIVEMIND_MCP_ALLOWED_ORIGINS`` then
    also admits those browser Origins (empty = non-browser clients only).
    Origins without hosts is a configuration error.
    """
    hosts = _split_csv(settings.mcp_allowed_hosts)
    origins = _split_csv(settings.mcp_allowed_origins)
    if not hosts:
        if origins:
            raise ValueError(
                "HIVEMIND_MCP_ALLOWED_ORIGINS needs HIVEMIND_MCP_ALLOWED_HOSTS too "
                "(Host validation would otherwise stay off)"
            )
        return None
    # A loopback bind still needs its own names, or local probes get a 421.
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


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
        # Never used to act: the provider fails closed (ADR 0042). Should
        # that ever regress, this is level 0 and access-controlled, not the
        # legacy full-access identity.
        credential=Credential(
            user_id="shared", agent_id="hivemind-mcp-http", access_controlled=True
        ),
    )
    server = build_server(template, credential_provider=make_credential_provider())
    host = os.environ.get("HIVEMIND_HOST", "0.0.0.0")
    mcp_app = server.streamable_http_app(
        stateless_http=True, host=host, transport_security=build_transport_security(settings, host)
    )
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
        redact_url(settings.embedding_endpoint),
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
