"""The REST error table (``hivemind.api.errors``): each typed service error is
answered with its status and code, and the message is ``str(exc)``, in the
standard envelope. The most specific class wins (Starlette walks the MRO)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from hivemind.api.deps import create_app
from hivemind.api.errors import ERROR_ANSWERS
from hivemind.domain.access import (
    AgentStatus,
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    UnknownAgent,
    UnknownFleet,
)
from hivemind.domain.agent_cleanup import AgentCleanupRefused, UnknownAgentCleanup
from hivemind.domain.fleet_merge import FleetMergeRefused, FleetMoveRefused, UnknownFleetMerge
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
from tests.unit.test_api_endpoints import make_hivemind_app

# One instance of each class in the table; the classes that build their own
# message from other arguments are made here.
BUILDERS: dict[type[Exception], Callable[[], Exception]] = {
    SupersedeDenied: lambda: SupersedeDenied(["e1", "e2"]),
    PermissionDenied: lambda: PermissionDenied("no way"),
    EntryNotFound: lambda: EntryNotFound("unknown entry: e1"),
    UnknownAgent: lambda: UnknownAgent("unknown agent: a"),
    UnknownFleet: lambda: UnknownFleet("unknown fleet: f"),
    UnknownFleetMerge: lambda: UnknownFleetMerge("unknown merge: m"),
    UnknownAgentCleanup: lambda: UnknownAgentCleanup("unknown cleanup: c"),
    UnknownSuccession: lambda: UnknownSuccession("unknown succession: s"),
    EntryNotActive: lambda: EntryNotActive("entry e1 is already withdrawn"),
    FleetMerged: lambda: FleetMerged("f1", "f2", "other"),
    FleetExists: lambda: FleetExists("fleet exists"),
    FleetMoveRefused: lambda: FleetMoveRefused("move refused"),
    FleetMergeRefused: lambda: FleetMergeRefused("merge refused"),
    AgentCleanupRefused: lambda: AgentCleanupRefused("cleanup refused"),
    SuccessionRefused: lambda: SuccessionRefused("succession refused"),
    InvalidAgentStatus: lambda: InvalidAgentStatus("a", AgentStatus.ACTIVE, "activate"),
    AgentRetired: lambda: AgentRetired("a", "b"),
    NameTaken: lambda: NameTaken("name taken"),
    AgentIdentityRequired: lambda: AgentIdentityRequired("who are you"),
}


def _app_raising(make: Callable[[], Exception]) -> FastAPI:
    app = create_app(make_hivemind_app())

    @app.get("/raise")
    async def raise_it() -> None:
        raise make()

    return app


async def _get(make: Callable[[], Exception]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app_raising(make))
    async with httpx.AsyncClient(transport=transport, base_url="http://hivemind.test") as client:
        return await client.get("/raise")


def test_builders_cover_the_whole_table() -> None:
    assert set(BUILDERS) == set(ERROR_ANSWERS)


@pytest.mark.parametrize("error", list(ERROR_ANSWERS), ids=lambda e: e.__name__)
async def test_each_error_gets_its_status_code_and_message(error: type[Exception]) -> None:
    exc = BUILDERS[error]()
    status, code = ERROR_ANSWERS[error]

    response = await _get(lambda: exc)

    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": str(exc)}}


@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (lambda: SupersedeDenied(["e1"]), 403, "supersede_denied"),
        (lambda: PermissionDenied("no"), 403, "forbidden"),
        (lambda: FleetMoveRefused("m"), 409, "move_refused"),
        (lambda: FleetMergeRefused("m"), 409, "merge_refused"),
    ],
    ids=["supersede", "permission", "move", "merge"],
)
async def test_the_most_specific_class_wins(
    make: Callable[[], Exception], status: int, code: str
) -> None:
    response = await _get(make)

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)


def test_the_specific_classes_subclass_the_general_ones() -> None:
    # The precedence cases above only mean something while this holds.
    assert issubclass(SupersedeDenied, PermissionDenied)
    assert issubclass(FleetMoveRefused, FleetMergeRefused)


async def test_embedding_failure_is_a_fixed_502_that_does_not_echo_the_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "https://embed.internal/v1 key=sk-secret refused"

    with caplog.at_level("WARNING"):
        response = await _get(lambda: EmbeddingError(secret))

    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "code": "embedding_unavailable",
            "message": "the embedding service is unavailable",
        }
    }
    assert secret not in response.text
    assert f"embedding unavailable: {secret}" in caplog.text
