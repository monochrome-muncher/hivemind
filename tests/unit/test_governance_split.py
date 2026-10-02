"""``services.governance`` was split into ``write``, ``feedback`` and
``pins``. Old imports must keep naming the same objects, and
``GovernanceService`` must hand feedback and pin calls on unchanged."""

from __future__ import annotations

import inspect

import pytest

from hivemind.services import feedback, governance, pins, write
from hivemind.services.governance import GovernanceService


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("WriteService", write),
        ("WriteResult", write),
        ("RelatedEntry", write),
        ("RELATED_ON_WRITE_LIMIT", write),
        ("FeedbackOutcome", feedback),
    ],
)
def test_old_import_path_names_the_same_object(name: str, module: object) -> None:
    assert getattr(governance, name) is getattr(module, name)


@pytest.mark.parametrize(
    ("method", "service"),
    [
        ("record_feedback", feedback.FeedbackService),
        ("record_feedback_many", feedback.FeedbackService),
        ("feedback_summary", feedback.FeedbackService),
        ("feedback_summaries", feedback.FeedbackService),
        ("quality", feedback.FeedbackService),
        ("pin", pins.PinService),
        ("unpin", pins.PinService),
        ("pinned", pins.PinService),
    ],
)
def test_governance_service_keeps_each_signature(method: str, service: type) -> None:
    assert inspect.signature(getattr(GovernanceService, method)) == inspect.signature(
        getattr(service, method)
    )
