"""The failures the services raise, by meaning.

Each surface maps these to its own answer (REST: a status and an error
code; MCP: an error code). The specific classes subclass the builtin a
surface already catches (``LookupError``, ``ValueError``), so an
``except`` written against the builtin still matches; new code should
raise and catch the specific class. ``InvalidInput`` (a rule-based input
refusal, ADR 0040) lives with the bounds in ``domain.validation``.
"""

from __future__ import annotations


class PermissionDenied(Exception):
    """The caller may not perform this action (403 / ``permission_denied``)."""


class SupersedeDenied(PermissionDenied):
    """A write named supersession targets outside the writer's reach
    (ADR 0033) or at a target that is no longer active (ADR 0034).
    ``ids`` are those targets; the message deliberately says
    "not found or not supersedable by you" for all cases."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(
            "not found or not supersedable by you: "
            + ", ".join(ids)
            + " (you may supersede active entries you can read, with a successor that "
            "reaches at least the same audience — ADR 0033; only the current head of a "
            "chain is supersedable — ADR 0034)"
        )
        self.ids = ids


class EntryNotFound(LookupError):
    """No entry the caller may read has this id (ADR 0033: "invisible" and
    "does not exist" are the same answer). 404 / ``not_found``."""


class EntryNotActive(ValueError):
    """The entry is superseded or withdrawn, and the action needs an active
    one (409 ``conflict`` / ``not_active``)."""


class AgentIdentityRequired(ValueError):
    """The caller's agent identity is unresolved: a legacy key that did not
    self-report ``agent`` (SPEC §8.1). 422 ``agent_identity_required`` /
    ``agent_unresolved``."""
