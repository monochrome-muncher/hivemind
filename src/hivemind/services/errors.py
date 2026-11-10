"""The failures the services raise, by meaning.

Each surface maps these in one table (REST: ``api/errors.py``) and catches these classes only; any
other exception is a bug and surfaces as a 500 or tool error. They subclass the builtin they
replace (``LookupError``, ``ValueError``). ``InvalidInput`` (ADR 0040) lives in
``domain.validation``; ``UnknownAgent``, ``UnknownFleet``, ``FleetExists`` and
``InvalidAgentStatus`` in ``domain.access``.
"""

from __future__ import annotations


class PermissionDenied(Exception):
    """The caller may not perform this action (403 / ``permission_denied``)."""


class SupersedeDenied(PermissionDenied):
    """Supersession targets outside the writer's reach (ADR 0033) or no longer active (ADR 0034).
    The message says the same for every case, on purpose."""

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
    """No entry the caller may read has this id (ADR 0033). 404 / ``not_found``."""


class EntryNotActive(ValueError):
    """The action needs an active entry (409 ``conflict`` / ``not_active``)."""


class AgentIdentityRequired(ValueError):
    """A legacy key that did not self-report ``agent`` (SPEC §8.1). 422 ``agent_identity_required``
    / ``agent_unresolved``."""


class NameTaken(ValueError):
    """The name belongs to another registration (ADR 0039); says nothing about it. 409
    ``name_conflict``."""
