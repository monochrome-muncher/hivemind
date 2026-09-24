"""The admin panel ASGI app (ADR 0029).

Three jobs, nothing else:

1. Serve the static UI (``static/``) at ``/``.
2. Forward an **allowlisted** set of ``/v1/*`` calls to ``hivemind-api``
   so the browser stays same-origin (no CORS on the API). Only the admin
   surface, the metrics counters and the health probe are reachable —
   the panel is never a relay onto the data plane.
3. Put strict security headers on every response. The admin key lives in
   the browser's ``sessionStorage`` (the operator's choice for
   simplicity), so the Content-Security-Policy is what keeps an injected
   script from running or phoning home with it.

The proxy forwards ``X-API-Key`` and never logs a header or a body:
responses to ``activate`` and ``org-key/rotate`` carry a raw key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# (method, path-regex) pairs the proxy forwards; everything else is 404.
# Anchored: the path is matched whole, after the `/v1/` prefix.
_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"health"),
        ("GET", r"metrics"),
        ("GET", r"admin/agents"),
        ("GET", r"admin/fleets"),
        ("POST", r"admin/fleets"),
        ("GET", r"admin/audit-log"),
        ("POST", r"admin/agents/[^/]+/activate"),
        ("POST", r"admin/agents/[^/]+/revoke"),
        ("PATCH", r"admin/agents/[^/]+"),
        ("POST", r"admin/org-key/rotate"),
    )
)

# Request headers passed through to the API; nothing else crosses.
_FORWARDED_REQUEST_HEADERS = ("x-api-key", "content-type", "accept")

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def is_allowed(method: str, path: str) -> bool:
    """Whether the proxy forwards ``method /v1/<path>`` (ADR 0029)."""
    return any(m == method and p.fullmatch(path) for m, p in _ALLOWED)


def _error(status: int, code: str, message: str) -> JSONResponse:
    """The API's own error envelope, so the UI handles one shape."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_admin_app(
    api_url: str,
    *,
    verify: bool | str = True,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> Starlette:
    """Build the admin panel app, proxying to ``api_url``.

    ``verify`` is passed to httpx (a CA bundle path for an internal CA);
    ``transport`` lets tests route the proxy straight into an in-process
    API app.
    """
    base = api_url.rstrip("/")
    if not base:
        raise ValueError("HIVEMIND_ADMIN_API_URL is required for hivemind-admin")
    client = httpx.AsyncClient(base_url=base, verify=verify, transport=transport, timeout=timeout)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    async def proxy(request: Request) -> Response:
        path = request.path_params["path"]
        if not is_allowed(request.method, path):
            return _error(404, "not_proxied", "the admin panel does not forward this call")
        headers = {
            name: value
            for name in _FORWARDED_REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        try:
            upstream = await client.request(
                request.method,
                f"/v1/{path}",
                params=str(request.query_params),
                headers=headers,
                content=await request.body(),
            )
        except httpx.HTTPError as exc:
            # The exception type only: its message can echo the request.
            logger.warning("hivemind-api unreachable: %s", type(exc).__name__)
            return _error(502, "api_unreachable", "hivemind-api could not be reached")
        response = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )
        # Some of these bodies are raw keys (activate, org-key rotate).
        response.headers["Cache-Control"] = "no-store"
        return response

    async def index(_request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def healthz(_request: Request) -> Response:
        """The panel's own probe — does not depend on the API being up."""
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/v1/{path:path}", proxy, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
        middleware=[Middleware(SecurityHeaders)],
        lifespan=lifespan,
    )


class SecurityHeaders:
    """Pure-ASGI middleware: add ``SECURITY_HEADERS`` to every HTTP
    response (static files, the proxy, errors alike)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
