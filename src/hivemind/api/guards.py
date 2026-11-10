"""Pure-ASGI guard that runs before FastAPI reads a body (ADR 0040).

``PreAuthGuard`` answers 401 to a protected ``/v1`` route with no ``X-API-Key`` before the body is
read (else a malformed body got a 422), and 413 to a body over ``max_body_bytes`` (by
``Content-Length``, or by counting streamed bytes). It does not verify the key (a store round trip):
a present but unknown key is parsed, then refused by ``require_credential``.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Public probes (SPEC §5.1, ADR 0019): no credential by design.
_PUBLIC_PATHS = frozenset({"/v1/health", "/v1/liveness"})


class BodyTooLarge(HTTPException):
    """A streamed body is too big; an ``HTTPException`` so FastAPI's body parsing re-raises it
    untouched."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body exceeds {limit} bytes")
        self.limit = limit


def envelope(status: int, code: str, message: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": {"code": code, "message": message}}).encode()


async def _respond(send: Send, status: int, code: str, message: str) -> None:
    status, body = envelope(status, code, message)
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


class PreAuthGuard:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        path: str = scope["path"]
        if (
            path.startswith("/v1/")
            and (path.rstrip("/") or "/") not in _PUBLIC_PATHS
            and scope["method"] != "OPTIONS"
            and b"x-api-key" not in headers
        ):
            await _respond(send, 401, "missing_api_key", "the X-API-Key header is required")
            return
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            await _respond(
                send, 413, "payload_too_large", f"request body exceeds {self.max_body_bytes} bytes"
            )
            return

        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body_bytes:
                    raise BodyTooLarge(self.max_body_bytes)
            return message

        await self.app(scope, counting_receive, send)
