"""Usage, health and activity aggregates."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from hivemind.domain.activity import Activity
from hivemind.domain.entry import (
    UsageCount,
)
from hivemind.domain.health import Convergence, FleetHealth


@runtime_checkable
class MetricsPort(Protocol):
    """Usage, health and activity aggregates."""

    async def usage_counts(self) -> list[UsageCount]:
        """Entry counts per (scope, kind, importance_source, author, fleet_id, active), every state
        included (ROADMAP §3.3)."""
        ...

    async def fleet_health(self, now: datetime) -> FleetHealth:
        """Fleet health aggregates as of ``now``. Revoked reporters (ADR 0067) and set-aside
        verdicts (ADR 0111) are left out; a flag is a standing ``stale``/``wrong`` row, aged by
        ``updated_at``.

        Observation only, no ranking input. Both adapters must agree."""
        ...

    async def activity(self, since: datetime) -> Activity:
        """Admin activity: per (author, fleet) entry counts, per reporter verdict counts, verdicts
        received per author, and per UTC day the writes (``created_at``) and verdicts
        (``updated_at``) from ``since`` on (quiet days have no row). ``recent`` is ``>= since``.

        Revoked reporters (ADR 0067) and set-aside verdicts (ADR 0111) are left out. Observation
        only. Both adapters must agree."""
        ...

    async def fleet_convergence(self, now: datetime) -> Convergence:
        """Convergence figures as of ``now``: echo rate over a sample of the newest writes,
        dispersion of the busiest tags.

        Bounded work (``ECHO_SAMPLE`` lookups, ``DISPERSION_CAP`` entries per tag and window);
        whole pool; observation only."""
        ...
