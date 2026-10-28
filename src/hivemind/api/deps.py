"""FastAPI wiring for the Hivemind REST surface (SPEC.md §5, §8.1).

``HivemindApp`` bundles the ports and services; ``create_app`` turns it
into a FastAPI app with the ``X-API-Key`` auth dependency wired up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hivemind.api.guards import BodyTooLarge, PreAuthGuard
from hivemind.api.schemas import ErrorBody
from hivemind.config import SearchConfig
from hivemind.domain.access import KEY_LOCKED_MESSAGE, FleetBusy, KeyLocked
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
    """Everything a FastAPI app needs: ports, config, and services."""

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
    # ADR 0082: downgrade dev-mode credentials to trust level 0.
    require_stored_keys: bool = False
    # ADR 0110: days without a key use before the admin list calls an agent idle.
    idle_agent_days: int = 30


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


# The accessors are ``async`` so FastAPI runs them inline as dependencies
# instead of hopping to a worker thread on every request.
async def get_app(request: Request) -> HivemindApp:
    """The HivemindApp bundle, attached by ``create_app``."""
    return cast(HivemindApp, request.app.state.hivemind)


async def get_prometheus(request: Request) -> PrometheusMetrics:
    """The app's Prometheus metrics, attached by ``create_app``."""
    return cast("PrometheusMetrics", request.app.state.prometheus)


async def require_credential(request: Request) -> Credential:
    """The auth dependency: verify the X-API-Key header (SPEC.md §8.1).

    Missing header -> 401 ``missing_api_key``; unknown key -> 401
    ``unknown_api_key``; a key locked from the portal -> 401 ``key_locked``
    (ADR 0117). The verified Credential is attached to
    ``request.state`` for downstream use.
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
    # ADR 0082: a dev-mode credential sits at trust level 0 when the
    # deployment requires stored keys.
    credential = credential.effective(app.require_stored_keys)
    request.state.credential = credential
    return credential


def _handle_api_error(_request: Request, exc: Exception) -> JSONResponse:
    """Turn an ApiError into the standard error envelope."""
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
    """FastAPI's 422 without the "input" echo: the echoed value can be huge
    and can itself be unencodable (a lone surrogate crashed the default
    handler while rendering: a 500). Shape otherwise unchanged:
    {"detail": [{"type", "loc", "msg"}]}."""
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


# A pool acquire (or a statement) that times out means the database is
# saturated or unreachable for now, not that the request was wrong: 503 with
# a short Retry-After so clients back off and retry (ROADMAP 3.13). The
# provider calls never surface a bare TimeoutError (they raise typed errors,
# ADR 0041), so on this surface a TimeoutError is the store's.
STORE_RETRY_AFTER_SECONDS = 2


def _handle_store_timeout(_request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, FleetBusy):  # ADR 0107: a fleet write waited for a merge
        code, message = "fleet_busy", str(exc)
    else:
        logger.warning("store call timed out (pool acquire or statement); answering 503")
        code, message = "store_unavailable", "the database is busy or unreachable; retry shortly"
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(STORE_RETRY_AFTER_SECONDS)},
        content={"error": ErrorBody(code=code, message=message).model_dump()},
    )


def create_app(app: HivemindApp) -> FastAPI:
    """Build the FastAPI app over a HivemindApp (SPEC.md §5)."""
    from hivemind.api.prometheus import PrometheusMetrics, RequestMetrics
    from hivemind.api.routes import build_router

    # ADR 0050: the Prometheus scrape target and per-process request metrics.
    prometheus = PrometheusMetrics(app.metrics_service.usage_report)
    # The shared /v1 route objects, not include_router, which would rebuild
    # every route on the app's first request. Routes passed this way skip
    # the app's ``dependency_overrides``.
    fastapi_app = FastAPI(title="Hivemind", routes=build_router().routes)
    fastapi_app.state.hivemind = app
    fastapi_app.state.prometheus = prometheus
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.add_exception_handler(InvalidInput, _handle_invalid_input)
    fastapi_app.add_exception_handler(BodyTooLarge, _handle_body_too_large)
    fastapi_app.add_exception_handler(RequestValidationError, _handle_request_validation)
    fastapi_app.add_exception_handler(TimeoutError, _handle_store_timeout)
    fastapi_app.add_middleware(PreAuthGuard, max_body_bytes=MAX_REQUEST_BODY_BYTES)
    # Added last, so outermost: the guards' 401 / 413 answers are counted too.
    fastapi_app.add_middleware(RequestMetrics, metrics=prometheus.process)
    fastapi_app.include_router(prometheus.router())
    return fastapi_app
