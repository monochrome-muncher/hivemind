"""Admin activity view schemas."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class DayActivityOut(BaseModel):
    day: date
    writes: int
    helpful: int
    stale: int
    wrong: int


class ActivityTotalsOut(BaseModel):
    writes: int
    verdicts: int
    writers: int
    reporters: int


class ReceivedOut(BaseModel):
    helpful: int
    stale: int
    wrong: int


class AgentActivityOut(BaseModel):
    name: str
    registered: bool
    owner_alias: str | None
    home_fleet: str | None
    status: str | None
    entries: int
    active: int
    recent_writes: int
    last_write_at: datetime | None
    verdicts_given: int
    recent_verdicts: int
    last_verdict_at: datetime | None
    received: ReceivedOut


class FleetActivityOut(BaseModel):
    fleet_id: str | None
    name: str | None
    entries: int
    active: int
    recent_writes: int
    writers: int
    last_write_at: datetime | None
    agents: int


class ActivityOut(BaseModel):
    """The admin activity view: observation only."""

    days: int
    since: datetime
    daily: list[DayActivityOut]
    totals: ActivityTotalsOut
    agents: list[AgentActivityOut]
    fleets: list[FleetActivityOut]
