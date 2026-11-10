"""The table mapping each typed service error to an HTTP status and error code; the message is
``str(exc)``.

``create_app`` registers every entry, so a route catches only where its answer differs. Starlette
walks the MRO, so the most specific class wins (``SupersedeDenied`` over ``PermissionDenied``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from fastapi import Request
from fastapi.responses import JSONResponse

from hivemind.api.schemas import ErrorBody
from hivemind.domain.access import (
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    UnknownAgent,
    UnknownFleet,
)
from hivemind.domain.agent_cleanup import AgentCleanupRefused, UnknownAgentCleanup
from hivemind.domain.entry_restore import (
    EntryNotWithdrawn,
    EntryRedacted,
    RestoreChainConflict,
    RestoreViaCleanup,
)
from hivemind.domain.fleet_merge import FleetMergeRefused, FleetMoveRefused, UnknownFleetMerge
from hivemind.domain.redaction import RedactRefused
from hivemind.domain.succession import AgentRetired, SuccessionRefused, UnknownSuccession
from hivemind.embeddings import EmbeddingError
from hivemind.services.errors import (
    AgentIdentityRequired,
    EntryNotActive,
    EntryNotFound,
    NameTaken,
    PermissionDenied,
    SupersedeDenied,
)

logger = logging.getLogger(__name__)

# error class -> (status, code). ``EmbeddingError`` has its own fixed message
# (``EMBEDDING_UNAVAILABLE``).
ERROR_ANSWERS: Final[dict[type[Exception], tuple[int, str]]] = {
    SupersedeDenied: (403, "supersede_denied"),  # ADR 0033
    PermissionDenied: (403, "forbidden"),
    EntryNotFound: (404, "not_found"),
    UnknownAgent: (404, "not_found"),
    UnknownFleet: (404, "not_found"),
    UnknownFleetMerge: (404, "not_found"),
    UnknownAgentCleanup: (404, "not_found"),
    UnknownSuccession: (404, "not_found"),
    EntryNotActive: (409, "conflict"),
    RedactRefused: (409, "conflict"),  # ADR 0125
    EntryNotWithdrawn: (409, "entry_not_withdrawn"),  # ADR 0124
    EntryRedacted: (409, "entry_redacted"),  # ADR 0125
    RestoreViaCleanup: (409, "restore_via_cleanup"),
    RestoreChainConflict: (409, "restore_chain_conflict"),
    FleetMerged: (409, "fleet_merged"),  # ADR 0107
    FleetExists: (409, "fleet_exists"),
    FleetMoveRefused: (409, "move_refused"),
    FleetMergeRefused: (409, "merge_refused"),
    AgentCleanupRefused: (409, "cleanup_refused"),
    SuccessionRefused: (409, "succession_refused"),
    InvalidAgentStatus: (409, "invalid_status"),
    AgentRetired: (409, "agent_retired"),  # ADR 0113
    NameTaken: (409, "name_conflict"),
    AgentIdentityRequired: (422, "agent_identity_required"),
}

# 502 with a fixed message (SPEC §7, ADR 0005); the detail is logged, never returned.
EMBEDDING_UNAVAILABLE: Final = (502, "embedding_unavailable")
EMBEDDING_UNAVAILABLE_MESSAGE: Final = "the embedding service is unavailable"


def _envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": ErrorBody(code=code, message=message).model_dump()},
    )


def _answer(status: int, code: str) -> Callable[[Request, Exception], JSONResponse]:
    def handle(_request: Request, exc: Exception) -> JSONResponse:
        return _envelope(status, code, str(exc))

    return handle


def handle_embedding_error(_request: Request, exc: Exception) -> JSONResponse:
    logger.warning("embedding unavailable: %s", exc)
    status, code = EMBEDDING_UNAVAILABLE
    return _envelope(status, code, EMBEDDING_UNAVAILABLE_MESSAGE)


def error_handlers() -> dict[type[Exception], Callable[[Request, Exception], JSONResponse]]:
    """Every typed error with its handler, for ``add_exception_handler``."""
    handlers = {cls: _answer(status, code) for cls, (status, code) in ERROR_ANSWERS.items()}
    handlers[EmbeddingError] = handle_embedding_error
    return handlers
