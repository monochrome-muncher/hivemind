"""The hostable streamable-HTTP MCP runner (ADR 0010): each agent authenticates **per request**
with its own key (ADR 0008). The credential goes in a context var and ``build_server`` re-binds
the services to it on every dispatch, so revocation is immediate."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from hivemind.config import Settings, bind_host, bind_port, configure_logging, load_settings
from hivemind.domain.access import KEY_LOCKED_MESSAGE, KeyLocked, StoreUnavailable
from hivemind.http_metrics import UNMATCHED_ROUTE, RequestMetrics
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.metrics import McpMetrics
from hivemind.mcp.server import build_server
from hivemind.ports import Authenticator, Credential, Embedder, Store
from hivemind.services.wiring import build_services, log_startup

logger = logging.getLogger(__name__)

# The acting credential for the current request, set by the auth middleware.
_current_credential: ContextVar[Credential | None] = ContextVar(
    "hivemind_mcp_credential", default=None
)


def current_credential() -> Credential | None:
    """The acting credential for the current request, or ``None``."""
    return _current_credential.get()


def make_credential_provider() -> Callable[[], Credential | None]:
    """``build_server``'s ``credential_provider``: the request's credential."""
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


async def _send_response(
    send: Send,
    status: int,
    body: bytes,
    content_type: bytes = b"application/json",
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    """One minimal ASGI response (nothing streams)."""
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
                *extra_headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_unauthorized(
    send: Send,
    error: str = "unauthorized",
    detail: str = "a valid API key is required (Authorization: Bearer <key> or X-Api-Key)",
) -> None:
    """A minimal 401; a locked key says ``key_locked`` rather than unknown."""
    await _send_json(
        send,
        401,
        {"error": error, "detail": detail},
        # Tells a client this is a plain bearer-key API.
        extra_headers=((b"www-authenticate", b'Bearer realm="hivemind"'),),
    )


async def _send_store_unavailable(send: Send) -> None:
    """503 + Retry-After when key verification timed out on the store: the key may be fine, so a
    401 would mislead (ROADMAP 3.13)."""
    await _send_json(
        send,
        503,
        {"error": "store_unavailable", "detail": "the database is busy; retry shortly"},
        extra_headers=((b"retry-after", b"2"),),
    )


# No OAuth here: MCP clients probe these after a 401, and anything but a 404 starts a confusing
# OAuth flow (ADR 0042).
_OAUTH_DISCOVERY_PREFIX = "/.well-known/oauth-"


async def _send_not_found(send: Send) -> None:
    await _send_json(send, 404, {"error": "not_found"})


class BearerAuthMiddleware:
    """Verify the request's API key, stash the credential and dispatch, or send a 401. Non-HTTP
    scopes pass through."""

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
        try:
            credential = await self._authenticator.verify(key) if key else None
        except KeyLocked:
            await _send_unauthorized(send, "key_locked", KEY_LOCKED_MESSAGE)
            return
        except StoreUnavailable as exc:
            logger.warning("key verification failed on the store (%s); answering 503", exc.cause)
            await _send_store_unavailable(send)
            return
        except TimeoutError:
            logger.warning("key verification timed out on the store; answering 503")
            await _send_store_unavailable(send)
            return
        if credential is None:
            await _send_unauthorized(send)
            return
        token = _current_credential.set(credential)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_credential.reset(token)


# Unauthenticated probe paths (ADR 0019), served outside the auth middleware: probes carry no key.
_PROBE_LIVENESS = "/mcp/liveness"
_PROBE_HEALTH = "/mcp/health"
_PROBE_DATABASE = "/mcp/health/database"
_METRICS_PATH = "/metrics"
_MCP_PATH = "/mcp"
# The paths the runner serves; any other path is one label, ``unmatched``.
_KNOWN_PATHS = frozenset(
    {_MCP_PATH, _PROBE_LIVENESS, _PROBE_HEALTH, _PROBE_DATABASE, _METRICS_PATH}
)
# A deep probe stuck on the pool answers 503 in time (ADR 0037).
_DATABASE_PROBE_TIMEOUT = 5.0
# The deep probe's answer is reused this long, so unauthenticated GETs cost one query per window.
_DATABASE_PROBE_CACHE_SECONDS = 2.0


class ProbeRouter:
    """Unauthenticated probe endpoints before the auth middleware (ADRs 0019, 0037); all else is
    delegated untouched.

    * ``/mcp/liveness`` and ``/mcp/health``: shallow, so a database outage does not pull every
      replica out at once.
    * ``/mcp/health/database``: deep, 200 or 503; the cause is logged, never returned.

    With ``metrics``, ``GET /metrics`` serves this process's series (ADR 0104), no agent, fleet or
    key in any label; without, a 404.
    """

    def __init__(self, app: ASGIApp, store: Store, metrics: McpMetrics | None = None) -> None:
        self._app = app
        self._store = store
        self._metrics = metrics
        self._db_probe_lock = asyncio.Lock()
        self._db_probe_result: tuple[float, bool] | None = None  # (monotonic time, healthy)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") == "GET":
            path = scope.get("path")
            if path in (_PROBE_LIVENESS, _PROBE_HEALTH, _PROBE_DATABASE):
                await self._serve_probe(path, send)
                return
            if path == _METRICS_PATH:
                # Before the key check: off means a plain 404, not a 401.
                if self._metrics is None:
                    await _send_not_found(send)
                else:
                    await _send_metrics(send, self._metrics)
                return
        await self._app(scope, receive, send)

    async def _database_healthy(self) -> bool:
        """The deep check, cached and single-flight so this unauthenticated endpoint cannot amplify
        load onto the shared pool."""
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


async def _send_metrics(send: Send, metrics: McpMetrics) -> None:
    await _send_response(send, 200, metrics.render(), metrics.content_type.encode())


async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, str],
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    """A minimal ASGI JSON response."""
    await _send_response(send, status, json.dumps(payload).encode(), extra_headers=extra_headers)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_transport_security(settings: Settings, host: str) -> TransportSecuritySettings | None:
    """The MCP SDK's DNS-rebinding (Host/Origin) protection (ADR 0042,
    ``Settings.mcp_allowed_hosts``); ``None`` (SDK default, loopback binds only) when no hosts are
    set. Origins without hosts is a configuration error."""
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


def _route_label(scope: Scope) -> str:
    path = str(scope.get("path", ""))
    return path if path in _KNOWN_PATHS else UNMATCHED_ROUTE


def build_http_app(
    settings: Settings,
    store: Store,
    embedder: Embedder,
    authenticator: Authenticator,
) -> ASGIApp:
    """Build the streamable-HTTP ASGI app: shared services, credential per request."""
    from hivemind.extractor import build_extractor

    # Opt-in (ADR 0104).
    metrics = McpMetrics() if settings.mcp_metrics_enabled else None
    search_config = settings.search_config()
    extractor = build_extractor(settings)
    services = build_services(
        store,
        embedder,
        search_config,
        extractor=extractor,
        authenticator=authenticator,
        require_stored_keys=settings.require_stored_keys,
    )
    template = McpHivemind.from_services(
        store,
        services,
        # Never used to act: the provider fails closed (ADR 0042). Should that regress, this is
        # level 0 and access-controlled, not the legacy full-access identity.
        credential=Credential(
            user_id="shared", agent_id="hivemind-mcp-http", access_controlled=True
        ),
        on_degraded_search=metrics.degraded_search if metrics else None,
    )
    server = build_server(
        template,
        credential_provider=make_credential_provider(),
        require_stored_keys=settings.require_stored_keys,
        metrics=metrics,
    )
    host = bind_host()
    mcp_app = server.streamable_http_app(
        stateless_http=True, host=host, transport_security=build_transport_security(settings, host)
    )
    # Probes and /metrics answer without a key, MCP stays protected; request counting is outermost
    # so 401s and probes are counted.
    router = ProbeRouter(BearerAuthMiddleware(mcp_app, authenticator), store, metrics)
    if metrics is None:
        return router
    return RequestMetrics(router, metrics.process, _route_label)


def main_http() -> None:
    """Console entry point (``hivemind-mcp-http``, ADR 0010)."""
    import uvicorn

    from hivemind.embeddings import build_embedder
    from hivemind.store import build_authenticator, build_store

    settings = load_settings()
    configure_logging(settings.log_level)
    log_startup(logger, "hivemind-mcp-http", settings)
    store = build_store(settings)
    embedder = build_embedder(settings)
    authenticator = build_authenticator(settings)
    app = build_http_app(settings, store, embedder, authenticator)
    host = bind_host()
    port = bind_port(8000)
    uvicorn.run(app, host=host, port=port)
