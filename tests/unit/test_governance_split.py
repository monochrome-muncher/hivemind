"""``services.governance`` was split into ``write``, ``feedback`` and
``pins``. ``GovernanceService`` must hand feedback and pin calls on unchanged."""

from __future__ import annotations

import inspect

import pytest

from hivemind.services import feedback, pins
from hivemind.services.governance import GovernanceService


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
