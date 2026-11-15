"""Liveness of the backing database."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HealthPort(Protocol):
    """Liveness of the backing database."""

    async def health_check(self) -> bool:
        """Whether the database answers. Must never raise: unauthenticated probes call it."""
        ...
