"""The MCP error envelope ``{"error": {"code", "message"}}`` and every code a tool call can answer
with. Tools return the envelope instead of raising; ``SERVICE_ERROR_CODES`` maps each typed service
error to its code, as ``api/errors.py`` does for REST."""

from __future__ import annotations

import logging

from hivemind.domain.access import FleetMerged
from hivemind.domain.validation import InvalidInput
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

ERR_INVALID_INPUT = "invalid_input"
ERR_NOT_FOUND = "not_found"
ERR_PERMISSION_DENIED = "permission_denied"
ERR_NOT_ACTIVE = "not_active"
ERR_AGENT_UNRESOLVED = "agent_unresolved"
ERR_SUPERSEDE_DENIED = "supersede_denied"
ERR_FLEET_MERGED = "fleet_merged"
ERR_FLEET_BUSY = "fleet_busy"  # a merge, move or rename holds what the write needs: retry shortly
ERR_EMBEDDING_UNAVAILABLE = "embedding_unavailable"  # provider down or misconfigured
ERR_UNAUTHENTICATED = "unauthenticated"  # ADR 0042: the key is gone / not resolvable
ERR_KEY_LOCKED = "key_locked"  # locked from the portal; ask the admin for a new key
ERR_INVALID_VERDICT = "invalid_verdict"
ERR_NAME_CONFLICT = "name_conflict"
ERR_STORE_UNAVAILABLE = "store_unavailable"  # a store call timed out: retry shortly
STORE_UNAVAILABLE_MESSAGE = "the database is busy or unreachable; retry shortly"

ERROR_CODES = frozenset(
    {
        ERR_INVALID_INPUT,
        ERR_NOT_FOUND,
        ERR_PERMISSION_DENIED,
        ERR_NOT_ACTIVE,
        ERR_AGENT_UNRESOLVED,
        ERR_SUPERSEDE_DENIED,
        ERR_FLEET_MERGED,
        ERR_FLEET_BUSY,
        ERR_EMBEDDING_UNAVAILABLE,
        ERR_UNAUTHENTICATED,
        ERR_KEY_LOCKED,
        ERR_INVALID_VERDICT,
        ERR_NAME_CONFLICT,
        ERR_STORE_UNAVAILABLE,
    }
)


def error_reply(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def embedding_unavailable_reply(exc: EmbeddingError) -> dict[str, object]:
    """A fixed message; the sanitized detail is logged server-side."""
    logger.warning("embedding unavailable: %s", exc)
    return error_reply(
        ERR_EMBEDDING_UNAVAILABLE,
        "the embedding service is unavailable; don't retry in a loop: keep the entry locally "
        "and write it later",
    )


# Typed service errors to MCP error codes, looked up by walking the exception's MRO, so a subclass
# (``SupersedeDenied``) wins over its base and the order here does not matter.
SERVICE_ERROR_CODES: dict[type[Exception], str] = {
    PermissionDenied: ERR_PERMISSION_DENIED,
    SupersedeDenied: ERR_SUPERSEDE_DENIED,
    EntryNotFound: ERR_NOT_FOUND,
    EntryNotActive: ERR_NOT_ACTIVE,
    InvalidInput: ERR_INVALID_INPUT,
    AgentIdentityRequired: ERR_AGENT_UNRESOLVED,
    FleetMerged: ERR_FLEET_MERGED,
    NameTaken: ERR_NAME_CONFLICT,
}
SERVICE_ERRORS = tuple(SERVICE_ERROR_CODES)


def service_error_reply(exc: Exception) -> dict[str, object]:
    """The error envelope for a typed service error."""
    for cls in type(exc).__mro__:
        code = SERVICE_ERROR_CODES.get(cls)
        if code is not None:
            return error_reply(code, str(exc))
    raise exc  # not a service error: the caller caught too much
