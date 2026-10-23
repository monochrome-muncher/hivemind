"""A same-origin web app over ``hivemind-api``: static UI, allowlisted
proxy, strict security headers (ADR 0029).

The admin panel (``hivemind.admin``) and the agent portal
(``hivemind.portal``) are both one of these, each with its own static
files and its own allowlist. Three jobs, nothing else:

1. Serve the static UI at ``/`` and ``/static/``.
2. Forward an **allowlisted** set of ``/v1/*`` calls to ``hivemind-api``
   so the browser stays same-origin (no CORS on the API).
3. Put strict security headers on every response. The key lives in the
   browser's ``sessionStorage``, so the Content-Security-Policy is what
   keeps an injected script from running or phoning home with it.

The proxy forwards ``X-API-Key`` and never logs a header or a body: some
responses carry a raw key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hivemind.domain.validation import MAX_REQUEST_BODY_BYTES

logger = logging.getLogger(__name__)

Allowlist = tuple[tuple[str, re.Pattern[str]], ...]

# Request headers passed through to the API; nothing else crosses.
_FORWARDED_REQUEST_HEADERS = ("x-api-key", "content-type", "accept")
# The one allowlisted call the API answers without a key (SPEC §5.1).
_PUBLIC_PATHS = frozenset({"health"})

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


def allowlist(pairs: Iterable[tuple[str, str]]) -> Allowlist:
    """Compile (method, path-regex) pairs; each regex matches the whole
    path after ``/v1/``."""
    return tuple((method, re.compile(pattern)) for method, pattern in pairs)


def allows(allowed: Allowlist, method: str, path: str) -> bool:
    """Whether ``allowed`` forwards ``method /v1/<path>``."""
    return any(m == method and p.fullmatch(path) for m, p in allowed)


def _error(status: int, code: str, message: str) -> JSONResponse:
    """The API's own error envelope, so the UI handles one shape."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_proxy_app(
    api_url: str,
    *,
    allowed: Allowlist,
    static_dir: Path,
    shared_static: tuple[Path, Iterable[str]] | None = None,
    name: str,
    api_url_setting: str,
    verify: bool | str = True,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> Starlette:
    """Build a web app serving ``static_dir`` and proxying ``allowed``
    calls to ``api_url``.

    ``shared_static`` serves the named files of another static folder at
    ``/static/<name>`` too (the shared front-end modules, ADR 0091).
    ``name`` is the app's name in refusals ("the admin panel");
    ``api_url_setting`` names its URL setting in the startup error.
    ``verify`` is passed to httpx (a CA bundle path for an internal CA);
    ``transport`` lets tests route the proxy straight into an in-process
    API app.
    """
    base = api_url.rstrip("/")
    if not base:
        raise ValueError(f"{api_url_setting} is required")
    refusal = f"{name} does not forward this call"
    client = httpx.AsyncClient(base_url=base, verify=verify, transport=transport, timeout=timeout)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    async def proxy(request: Request) -> Response:
        path = request.path_params["path"]
        if not allows(allowed, request.method, path):
            return _error(404, "not_proxied", refusal)
        # ADR 0040: ``path`` is percent-DECODED by Starlette. Re-quote every
        # segment so a name holding ``?``, ``#``, ``%`` or ``/`` reaches the
        # resource the allowlist matched, and refuse dot segments (which
        # httpx would collapse onto a different route).
        segments = path.split("/")
        if any(segment in (".", "..") for segment in segments):
            return _error(404, "not_proxied", refusal)
        upstream_path = "/v1/" + "/".join(quote(segment, safe="") for segment in segments)
        # The API's own pre-auth guard (ADR 0040), applied here too: the
        # panel would otherwise buffer any body, with no key at all, before
        # the API got the chance to refuse it.
        if path not in _PUBLIC_PATHS and "x-api-key" not in request.headers:
            return _error(401, "missing_api_key", "the X-API-Key header is required")
        body = await _read_body(request, MAX_REQUEST_BODY_BYTES)
        if body is None:
            return _error(
                413, "payload_too_large", f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
            )
        headers = {
            name: value
            for name in _FORWARDED_REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        try:
            upstream = await client.request(
                request.method,
                upstream_path,
                params=str(request.query_params),
                headers=headers,
                content=body,
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
        # Some of these bodies are raw keys (admin activate, org-key rotate).
        response.headers["Cache-Control"] = "no-store"
        return response

    async def index(_request: Request) -> Response:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    async def healthz(_request: Request) -> Response:
        """The app's own probe — does not depend on the API being up."""
        return JSONResponse({"status": "ok"})

    routes: list[BaseRoute] = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/v1/{path:path}", proxy, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
    ]
    if shared_static is not None:
        shared_dir, names = shared_static
        for file_name in names:
            # Exact names only: nothing else of the other folder is served.
            routes.append(Route(f"/static/{file_name}", _file(shared_dir / file_name)))
    routes.append(Mount("/static", StaticFiles(directory=static_dir), name="static"))
    return Starlette(
        routes=routes,
        middleware=[Middleware(SecurityHeaders), Middleware(StaticCacheHeaders)],
        lifespan=lifespan,
    )


def _file(path: Path) -> Callable[[Request], Awaitable[Response]]:
    """A route endpoint serving one fixed file."""

    async def serve(_request: Request) -> Response:
        return FileResponse(path)

    return serve


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or ``None`` once it exceeds ``limit`` bytes (by
    ``Content-Length`` up front, then by counting the streamed chunks)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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


class StaticCacheHeaders:
    """Pure-ASGI middleware: put ``Cache-Control: no-cache`` on every
    ``/static/`` response. The bundle is unversioned static files, and a
    browser that keeps a cached module across a redeploy runs old code
    with no sign of it (a view added in a later release routes to the
    landing page). ``no-cache`` makes every full load revalidate against
    the ETag ``StaticFiles`` already sends — a 304 when unchanged —
    instead of trusting a heuristic cache."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_cache)
