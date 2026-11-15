"""The MCP service-error table: every typed error answers its code."""

from __future__ import annotations

import pytest

from hivemind.domain.access import FleetMerged
from hivemind.domain.validation import InvalidInput
from hivemind.mcp import errors as mcp_errors
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
    assert mcp_errors.service_error_reply(exc) == {"error": {"code": code, "message": str(exc)}}


def test_table_and_cases_cover_the_same_classes() -> None:
    assert {type(e) for e, _ in CASES} == set(mcp_errors.SERVICE_ERROR_CODES)
    assert tuple(mcp_errors.SERVICE_ERROR_CODES) == mcp_errors.SERVICE_ERRORS


def test_a_subclass_outside_the_table_takes_the_nearest_ancestor() -> None:
    class Mine(EntryNotFound):
        pass

    assert mcp_errors.service_error_reply(Mine("gone")) == {
        "error": {"code": "not_found", "message": "gone"}
    }


def test_an_unlisted_exception_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError):
        mcp_errors.service_error_reply(RuntimeError("bug"))
