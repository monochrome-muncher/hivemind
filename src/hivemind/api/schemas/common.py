"""Shared request and response pieces: HealthOut, ErrorBody, RefStr and the UTC normaliser."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from hivemind.domain.validation import MAX_ID_CHARS, check_no_nul


def _no_nul(value: str) -> str:
    return check_no_nul(value, "value")


# A reference to a stored record (a fleet id): bounded, NUL-free (ADR 0040).
RefStr = Annotated[str, Field(max_length=MAX_ID_CHARS), AfterValidator(_no_nul)]


def to_utc(value: datetime | None) -> datetime | None:
    """Normalize a naive datetime to UTC (SPEC.md §6.4 / §8: the pool is
    UTC-based; a bare timestamp is assumed UTC so downstream asyncpg
    ``timestamptz`` columns and the recency rescore never see a naive
    value)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class HealthOut(BaseModel):
    """Liveness/readiness (SPEC.md §5.1)."""

    status: str


class ErrorBody(BaseModel):
    """The error envelope: one consistent JSON body for every failure."""

    code: str
    message: str
