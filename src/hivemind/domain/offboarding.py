"""Offboarding an owner (ADR 0135): what happens to each of their agents when they leave.

``plan_offboarding`` is the one rule both stores apply under their row locks and the service
previews with, so a preview and an apply cannot disagree. No I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from hivemind.domain.access import Agent, AgentStatus, InvalidAgentStatus
from hivemind.domain.succession import SuccessionRefused
from hivemind.domain.validation import InvalidInput


class UnknownOwner(KeyError):
    """No registered agent has this owner alias (404 ``unknown_owner``). A ``KeyError``, so
    ``str(exc)`` is quoted."""


class OffboardChanged(Exception):
    """The plan under the locks is not the one the admin previewed (409 ``offboard_changed``)."""


class OffboardMode(StrEnum):
    RETIRE = "retire"
    REVOKE = "revoke"


class OffboardAction(StrEnum):
    RETIRE = "retire"
    REVOKE = "revoke"
    REJECT = "reject"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class OffboardStep:
    """What offboarding does to one agent; ``was`` is its status when planned, ``reason`` says
    why a step is skipped."""

    name: str
    action: OffboardAction
    was: AgentStatus
    reason: str | None = None


def plan_offboarding(
    alias: str,
    mode: OffboardMode,
    agents: Sequence[Agent],
    successor: Agent | None,
    successions: Mapping[str, str],
    inherited: Mapping[str, tuple[str, ...]],
) -> list[OffboardStep]:
    """One step per agent of ``alias``, in name order.

    ``successions`` maps each retired agent among ``agents`` to its successor, ``inherited`` each
    agent to those handing down to it. Raises ``SuccessionRefused`` or ``InvalidAgentStatus``
    for a successor that cannot take over (or when a cycle would form), ``InvalidInput`` for a
    mode and successor that do not go together.
    """
    require_fitting(mode, successor.name if successor else None)
    if mode is OffboardMode.REVOKE:
        return [_revoke_step(a, successions) for a in agents]
    assert successor is not None  # require_fitting
    _require_successor(alias, successor)
    steps = [_retire_step(a, successions) for a in agents]
    for step in steps:
        if step.action is OffboardAction.RETIRE and successor.name in inherited.get(step.name, ()):
            raise SuccessionRefused(
                f"{successor.name!r} already hands its memories down to {step.name!r}, "
                "directly or through others: that would be a cycle"
            )
    return steps


def require_fitting(mode: OffboardMode, successor: str | None) -> None:
    """``retire`` goes with a successor and ``revoke`` with none; ``InvalidInput`` otherwise. Checked
    before anything is looked up."""
    if mode is OffboardMode.REVOKE and successor is not None:
        raise InvalidInput("revoking takes no successor")
    if mode is OffboardMode.RETIRE and successor is None:
        raise InvalidInput("retiring needs a successor")


def require_expected(steps: Sequence[OffboardStep], expected: Mapping[str, str] | None) -> None:
    """``OffboardChanged`` unless ``steps`` do exactly what the admin previewed (agent name to
    action); ``None`` accepts any plan."""
    if expected is None:
        return
    planned = {s.name: s.action.value for s in steps}
    if planned != dict(expected):
        changed = sorted(
            n for n in planned.keys() | expected.keys() if planned.get(n) != expected.get(n)
        )
        raise OffboardChanged(
            f"the agents changed since the preview ({', '.join(changed)}): preview it again"
        )


def _require_successor(alias: str, successor: Agent) -> None:
    if successor.status is not AgentStatus.ACTIVE:
        raise InvalidAgentStatus(successor.name, successor.status, "make a successor of")
    if successor.owner_alias == alias:
        raise SuccessionRefused(
            f"{successor.name!r} belongs to {alias!r}, who is leaving: "
            "choose an active agent of another owner"
        )


def _standing_retirement(agent: Agent, successions: Mapping[str, str]) -> str | None:
    return successions.get(agent.name)


def _retired_step(agent: Agent, heir: str) -> OffboardStep:
    """A retired agent is revoked already, so nothing is left to do. An *active* one (an older pod
    can activate it, ADR 0113) still has a working key: it is revoked under its succession."""
    if agent.status is AgentStatus.ACTIVE:
        return OffboardStep(
            agent.name,
            OffboardAction.REVOKE,
            agent.status,
            f"retired in favour of {heir!r} but active again; its key is revoked",
        )
    return OffboardStep(
        agent.name, OffboardAction.SKIP, agent.status, f"already retired in favour of {heir!r}"
    )


def _revoke_step(agent: Agent, successions: Mapping[str, str]) -> OffboardStep:
    if (heir := _standing_retirement(agent, successions)) is not None:
        return _retired_step(agent, heir)
    if agent.status is AgentStatus.ACTIVE:
        return OffboardStep(agent.name, OffboardAction.REVOKE, agent.status)
    if agent.status is AgentStatus.PENDING:
        return OffboardStep(agent.name, OffboardAction.REJECT, agent.status)
    return OffboardStep(agent.name, OffboardAction.SKIP, agent.status, "already revoked")


def _retire_step(agent: Agent, successions: Mapping[str, str]) -> OffboardStep:
    if (heir := _standing_retirement(agent, successions)) is not None:
        return _retired_step(agent, heir)
    if agent.status is AgentStatus.PENDING:
        return OffboardStep(agent.name, OffboardAction.REJECT, agent.status)
    return OffboardStep(agent.name, OffboardAction.RETIRE, agent.status)
