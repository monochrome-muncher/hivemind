"""Feedback on entries, one at a time or in a batch."""

from __future__ import annotations

from fastapi import APIRouter

from hivemind.api.routes.params import API_PREFIX, AppDep, CredentialDep
from hivemind.api.schemas import (
    BatchFeedbackRequest,
    FeedbackOut,
    FeedbackRequest,
)

router = APIRouter(prefix=API_PREFIX)


@router.post("/feedback", response_model=list[FeedbackOut])
async def batch_feedback(
    request: BatchFeedbackRequest, credential: CredentialDep, app: AppDep
) -> list[FeedbackOut]:
    """One verdict (and note) on up to 16 entries at once (ADR 0053).

    All or nothing: an id the caller may not read is a 404 naming it,
    and one of the caller's own is a 403 (ADR 0065); nothing is recorded.
    """
    outcomes = await app.governance_service.record_feedback_many(
        credential,
        request.entry_ids,
        request.verdict,
        request.note,
        agent=request.agent,
    )
    return [FeedbackOut.from_outcome(o) for o in outcomes]


@router.post("/entries/{entry_id}/feedback", response_model=FeedbackOut)
async def feedback(
    entry_id: str, request: FeedbackRequest, credential: CredentialDep, app: AppDep
) -> FeedbackOut:
    """Report a verdict on an entry (SPEC.md §4.2); upsert per reporter.

    A plain user key self-reports the agent instance via ``agent``
    (SPEC.md §8.1); an agent sub-key's credential agent wins.
    """
    outcome = await app.governance_service.record_feedback(
        credential,
        entry_id,
        request.verdict,
        request.note,
        agent=request.agent,
    )
    return FeedbackOut.from_outcome(outcome)
