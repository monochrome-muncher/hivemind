"""Retirement and succession schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from hivemind.api.schemas.common import RefStr
from hivemind.domain.succession import Succession
from hivemind.services.successions import RetirePreview


class RetireRequest(BaseModel):
    """``POST /v1/admin/agents/{name}/retire`` (ADR 0113). ``dry_run`` is
    required, so a retirement is never started by leaving it out."""

    successor: RefStr
    dry_run: bool


class SuccessionOut(BaseModel):
    """One standing succession: ``retired_agent`` hands its memories to
    ``successor``."""

    retired_agent: str
    successor: str
    by: str
    at: datetime

    @classmethod
    def from_succession(cls, succession: Succession) -> SuccessionOut:
        return cls(
            retired_agent=succession.retired_agent,
            successor=succession.successor,
            by=succession.by,
            at=succession.at,
        )


class RetirePreviewOut(BaseModel):
    """What a retirement would do (``dry_run: true``): the memories that
    pass over, counted for the retired agent and every agent that already
    hands down to it, and what the successor would own in all."""

    dry_run: Literal[True] = True
    retired_agent: str
    successor: str
    retired_status: str
    revokes_retired: bool
    passes_over_from: list[str]
    self_notes: int
    fleet_memories: int
    fleet_memories_elsewhere: int
    pins: int
    successor_owns: list[str]

    @classmethod
    def from_preview(cls, preview: RetirePreview) -> RetirePreviewOut:
        facts = preview.handed_down
        return cls(
            retired_agent=preview.retired.name,
            successor=preview.successor.name,
            retired_status=preview.retired.status.value,
            revokes_retired=preview.revokes,
            passes_over_from=list(facts.authors),
            self_notes=facts.self_notes,
            fleet_memories=facts.fleet_memories,
            fleet_memories_elsewhere=facts.fleet_memories_elsewhere,
            pins=facts.pins,
            successor_owns=list(preview.successor_owns),
        )


class RetiredOut(SuccessionOut):
    """A retirement that happened."""

    dry_run: Literal[False] = False
