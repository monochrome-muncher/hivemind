"""FastAPI wiring for the REST surface (SPEC.md §5, §8.1): the ``HivemindApp`` bundle, the app
factory and the ``X-API-Key`` auth dependency."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hivemind.api.errors import error_handlers
from hivemind.api.guards import BodyTooLarge, PreAuthGuard
from hivemind.api.schemas import ErrorBody
from hivemind.config import SearchConfig
from hivemind.domain.access import KEY_LOCKED_MESSAGE, FleetBusy, KeyLocked, StoreUnavailable
from hivemind.domain.validation import MAX_REQUEST_BODY_BYTES, InvalidInput
from hivemind.ports import Authenticator, Credential, Embedder, Store
from hivemind.services.access import AccessService
from hivemind.services.activity import ActivityService
from hivemind.services.governance import GovernanceService
from hivemind.services.metrics import MetricsService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService

if TYPE_CHECKING:
    from hivemind.api.prometheus import PrometheusMetrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HivemindApp:
    """The ports, config and services a FastAPI app needs."""

    store: Store
    embedder: Embedder
    authenticator: Authenticator
    search_config: SearchConfig
    write_service: WriteService
    read_service: ReadService
    governance_service: GovernanceService
    search_service: SearchService
    access_service: AccessService
    metrics_service: MetricsService
    activity_service: ActivityService
    # Dev-mode credentials sit at trust level 0 (ADR 0082).
    require_stored_keys: bool = False
    # Days without key use before an agent shows as idle (ADR 0110).
    idle_agent_days: int = 30


class ApiError(Exception):
    """An expected API failure (status and stable code), answered as ``{"error": {"code",
    "message"}}``.

    Typed service errors are answered by the table in ``api/errors.py``.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def api_error(status_code: int, code: str, message: str) -> ApiError:
    """Build an ApiError."""
    return ApiError(status_code, code, message)


# ``async`` so FastAPI runs the dependencies inline, not in a worker thread.
async def get_app(request: Request) -> HivemindApp:
    return cast(HivemindApp, request.app.state.hivemind)


async def get_prometheus(request: Request) -> PrometheusMetrics:
    return cast("PrometheusMetrics", request.app.state.prometheus)


async def require_credential(request: Request) -> Credential:
    """Verify the X-API-Key header (SPEC §8.1): 401 ``missing_api_key``, ``unknown_api_key`` or
    ``key_locked`` (ADR 0117).

    The credential is attached to ``request.state``.
    """
    app = await get_app(request)
    key = request.headers.get("X-API-Key")
    if key is None:
        raise api_error(401, "missing_api_key", "the X-API-Key header is required")
    try:
        credential = await app.authenticator.verify(key)
    except KeyLocked as exc:  # say what to do, not "unknown"
        raise api_error(401, "key_locked", KEY_LOCKED_MESSAGE) from exc
    if credential is None:
        raise api_error(401, "unknown_api_key", "the API key is unknown or revoked")
    # ADR 0082.
    credential = credential.effective(app.require_stored_keys)
    request.state.credential = credential
    return credential


def _handle_api_error(_request: Request, exc: Exception) -> JSONResponse:
    api_exc = cast(ApiError, exc)
    return JSONResponse(
        status_code=api_exc.status_code,
        content={"error": ErrorBody(code=api_exc.code, message=api_exc.message).model_dump()},
    )


def _handle_invalid_input(_request: Request, exc: Exception) -> JSONResponse:
    """A rule-based input refusal (ADR 0040) -> 422 in the error envelope."""
    return JSONResponse(
        status_code=422,
        content={"error": ErrorBody(code="invalid_input", message=str(exc)).model_dump()},
    )


def _handle_request_validation(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's 422 without the "input" echo: it can be huge or unencodable (a lone surrogate made
    the default handler 500)."""
    errors = [
        {"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]}
        for e in cast(RequestValidationError, exc).errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


def _handle_body_too_large(_request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={"error": ErrorBody(code="payload_too_large", message=str(exc)).model_dump()},
    )


# A pool acquire or statement timeout is the store's (ROADMAP 3.13; provider calls raise typed
# errors, ADR 0041): 503 with a short Retry-After.
STORE_RETRY_AFTER_SECONDS = 2


def _handle_store_timeout(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, FleetBusy):  # ADR 0107
        code, message = "fleet_busy", str(exc)
    elif isinstance(exc, StoreUnavailable):
        logger.warning("store unreachable (%s); answering 503", exc.cause)
        code, message = "store_unavailable", "the database is busy or unreachable; retry shortly"
    else:
        logger.warning("store call timed out (pool acquire or statement); answering 503")
        code, message = "store_unavailable", "the database is busy or unreachable; retry shortly"
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(STORE_RETRY_AFTER_SECONDS)},
        content={"error": ErrorBody(code=code, message=message).model_dump()},
    )


def create_app(app: HivemindApp) -> FastAPI:
    """Build the FastAPI app over a HivemindApp (SPEC §5)."""
    from hivemind.api.prometheus import PrometheusMetrics, RequestMetrics
    from hivemind.api.routes import build_router

    prometheus = PrometheusMetrics(app.metrics_service.usage_report)
    # Shared route objects, not include_router (it rebuilds every route on the first request);
    # routes passed this way skip ``dependency_overrides``.
    fastapi_app = FastAPI(title="Hivemind", routes=build_router().routes)
    fastapi_app.state.hivemind = app
    fastapi_app.state.prometheus = prometheus
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.add_exception_handler(InvalidInput, _handle_invalid_input)
    for error, handler in error_handlers().items():
        fastapi_app.add_exception_handler(error, handler)
    fastapi_app.add_exception_handler(BodyTooLarge, _handle_body_too_large)
    fastapi_app.add_exception_handler(RequestValidationError, _handle_request_validation)
    fastapi_app.add_exception_handler(TimeoutError, _handle_store_timeout)
    fastapi_app.add_middleware(PreAuthGuard, max_body_bytes=MAX_REQUEST_BODY_BYTES)
    # Outermost, so guard 401/413 answers are counted.
    fastapi_app.add_middleware(RequestMetrics, metrics=prometheus.process)
    fastapi_app.include_router(prometheus.router())
    return fastapi_app
