"""Offboarding an owner: every agent of one owner retired or revoked in one transaction."""

from __future__ import annotations

from collections.abc import Mapping

import asyncpg

from hivemind.domain.access import UnknownAgent
from hivemind.domain.offboarding import (
    OffboardAction,
    OffboardMode,
    OffboardStep,
    UnknownOwner,
    plan_offboarding,
    require_expected,
    require_fitting,
)
from hivemind.store.inheritance import INHERITED_FROM
from hivemind.store.pgstore.agents import REVOKE_AGENT
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import row_to_agent
from hivemind.store.pgstore.successions import INSERT_SUCCESSION, REVOKE_ACTIVE_AGENT

# The owner's rows and the successor's, locked together in name order so two offboardings (or a
# retirement) cannot deadlock.
LOCK_OWNER_AND_SUCCESSOR = (
    "SELECT * FROM agents WHERE owner_alias = $1 OR name = $2 ORDER BY name FOR UPDATE"
)

STANDING_SUCCESSIONS = (
    "SELECT retired_agent, successor FROM successions WHERE retired_agent = ANY($1::text[])"
)


class OffboardingStore(PgConnections):
    async def offboard_owner(
        self,
        alias: str,
        mode: OffboardMode,
        successor: str | None,
        by: str,
        expected: Mapping[str, str] | None = None,
    ) -> list[OffboardStep]:
        require_fitting(mode, successor)
        async with self._transaction() as conn:
            rows = [
                row_to_agent(r)
                for r in await conn.fetch(LOCK_OWNER_AND_SUCCESSOR, alias, successor)
            ]
            owned = sorted((a for a in rows if a.owner_alias == alias), key=lambda a: a.name)
            if not owned:
                raise UnknownOwner(f"no agent has the owner alias {alias!r}")
            heir = next((a for a in rows if a.name == successor), None)
            if successor is not None and heir is None:
                raise UnknownAgent(f"unknown agent: {successor}")
            names = [a.name for a in owned]
            standing = {
                r["retired_agent"]: r["successor"]
                for r in await conn.fetch(STANDING_SUCCESSIONS, names)
            }
            inherited = {n: tuple(await conn.fetchval(INHERITED_FROM, n)) for n in names}
            steps = plan_offboarding(alias, mode, owned, heir, standing, inherited)
            require_expected(steps, expected)
            for step in steps:
                await _apply(conn, step, successor, by)
        return steps


async def _apply(
    conn: asyncpg.Connection, step: OffboardStep, successor: str | None, by: str
) -> None:
    if step.action is OffboardAction.RETIRE:
        await conn.execute(INSERT_SUCCESSION, step.name, successor, by)
        await conn.execute(REVOKE_ACTIVE_AGENT, step.name)
    elif step.action in (OffboardAction.REVOKE, OffboardAction.REJECT):
        await conn.execute(REVOKE_AGENT, step.name)
