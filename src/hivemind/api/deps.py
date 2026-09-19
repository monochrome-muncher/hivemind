"""FastAPI wiring for the Hivemind REST surface (SPEC.md §5, §8.1).

``HivemindApp`` bundles the ports and services a FastAPI app needs;
``create_app`` turns that bundle into a FastAPI app with the auth
dependency wired up. The auth dependency reads the ``X-API-Key``
header and verifies it against the ``Authenticator`` port; a missing
or unknown key yields 401 with the standard error envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hivemind.api.schemas import ErrorBody
from hivemind.config import SearchConfig
from hivemind.ports import Authenticator, Credential, Embedder, Store
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService


@dataclass(frozen=True, slots=True)
class HivemindApp:
    """Everything a FastAPI app needs: ports, config, and services."""

    store: Store
    embedder: Embedder
    authenticator: Authenticator
    search_config: SearchConfig
    write_service: WriteService
    governance_service: GovernanceService
    search_service: SearchService
    access_service: AccessService


class ApiError(Exception):
    """An expected API failure: an HTTP status + a stable error code.

    Raised by the auth dependency (401) and by routes (from service
    exceptions); a single exception handler turns it into the JSON
    envelope ``{"error": {"code", "message"}}``.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def api_error(status_code: int, code: str, message: str) -> ApiError:
    """Build an ApiError (keeps routes to one-line raises)."""
    return ApiError(status_code, code, message)


def get_app(request: Request) -> HivemindApp:
    """The HivemindApp bundle, attached by ``create_app``."""
    return cast(HivemindApp, request.app.state.hivemind)


async def require_credential(request: Request) -> Credential:
    """The auth dependency: verify the X-API-Key header (SPEC.md §8.1).

    Missing header -> 401 ``missing_api_key``; unknown key -> 401
    ``unknown_api_key``. The verified Credential is attached to
    ``request.state`` for downstream use.
    """
    app = get_app(request)
    key = request.headers.get("X-API-Key")
    if key is None:
        raise api_error(401, "missing_api_key", "the X-API-Key header is required")
    credential = await app.authenticator.verify(key)
    if credential is None:
        raise api_error(401, "unknown_api_key", "the API key is unknown or revoked")
    request.state.credential = credential
    return credential


def _handle_api_error(_request: Request, exc: Exception) -> JSONResponse:
    """Turn an ApiError into the standard error envelope."""
    api_exc = cast(ApiError, exc)
    return JSONResponse(
        status_code=api_exc.status_code,
        content={"error": ErrorBody(code=api_exc.code, message=api_exc.message).model_dump()},
    )


def create_app(app: HivemindApp) -> FastAPI:
    """Build the FastAPI app over a HivemindApp (SPEC.md §5)."""
    from hivemind.api.routes import build_router

    fastapi_app = FastAPI(title="Hivemind")
    fastapi_app.state.hivemind = app
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(build_router(app))
    return fastapi_app
