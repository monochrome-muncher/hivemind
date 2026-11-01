"""The MCP service-error table: every typed error answers its code."""

from __future__ import annotations

import pytest

from hivemind.domain.access import FleetMerged
from hivemind.domain.validation import InvalidInput
from hivemind.mcp import app as mcp_app
from hivemind.services.errors import (
    AgentIdentityRequired,
    EntryNotActive,
    EntryNotFound,
    NameTaken,
    PermissionDenied,
    SupersedeDenied,
)

CASES: list[tuple[Exception, str]] = [
    (PermissionDenied("nope"), "permission_denied"),
    (SupersedeDenied(["a", "b"]), "supersede_denied"),
    (EntryNotFound("unknown entry: x"), "not_found"),
    (EntryNotActive("superseded"), "not_active"),
    (InvalidInput("bad"), "invalid_input"),
    (AgentIdentityRequired("who"), "agent_unresolved"),
    (FleetMerged("f1", "f2", "other"), "fleet_merged"),
    (NameTaken("taken"), "name_conflict"),
]


@pytest.mark.parametrize(("exc", "code"), CASES, ids=lambda v: type(v).__name__)
def test_each_service_error_answers_its_code_and_message(exc: Exception, code: str) -> None:
    assert mcp_app._service_error(exc) == {"error": {"code": code, "message": str(exc)}}


def test_table_and_cases_cover_the_same_classes() -> None:
    assert {type(e) for e, _ in CASES} == set(mcp_app._SERVICE_ERROR_CODES)
    assert tuple(mcp_app._SERVICE_ERROR_CODES) == mcp_app._SERVICE_ERRORS


def test_subclass_wins_over_its_base() -> None:
    assert issubclass(SupersedeDenied, PermissionDenied)
    exc = SupersedeDenied(["x"])
    assert mcp_app._service_error(exc) == {
        "error": {"code": "supersede_denied", "message": str(exc)}
    }


def test_a_subclass_outside_the_table_takes_the_nearest_ancestor() -> None:
    class Mine(EntryNotFound):
        pass

    assert mcp_app._service_error(Mine("gone")) == {
        "error": {"code": "not_found", "message": "gone"}
    }


def test_an_unlisted_exception_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError):
        mcp_app._service_error(RuntimeError("bug"))
