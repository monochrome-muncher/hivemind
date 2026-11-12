"""Agent roles (ADR 0144): an admin marks an agent as a scout.

A role is a hint, not a permission. It only adds a one-line hint to the agent's ``hive_whoami``;
what the agent may read or write, ranking and search stay as they were.
"""

from __future__ import annotations

from enum import StrEnum

DEFAULT_FLEET_NAME = "your fleet"
OTHER_AGENTS = "other"


class AgentRole(StrEnum):
    SCOUT = "scout"


_HINTS: dict[AgentRole, str] = {
    AgentRole.SCOUT: (
        "You are a scout for {fleet}: on low-stakes tasks, prefer the less-tested of two rivals, "
        "search with explore: true when results look settled, re-check old unconfirmed entries "
        "you rely on, and write what you tried, including dead ends. Your access is unchanged."
    ),
}


def role_hint(role: AgentRole, fleet_name: str | None) -> str:
    return _HINTS[role].format(fleet=fleet_name or DEFAULT_FLEET_NAME)


__all__ = ["DEFAULT_FLEET_NAME", "OTHER_AGENTS", "AgentRole", "role_hint"]
