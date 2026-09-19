"""Port definitions — the seams the TDD suite tests at.

These are the only boundaries the rest of the system depends on:

* ``Store`` — data access. Implemented by ``MemoryStore`` (dev/tests)
  and the Postgres adapter (production). Everything above the store
  is storage-agnostic.
* ``Embedder`` — turns text into a fixed-dimension vector. Implemented
  by an OpenAI-compatible client; faked in unit tests.
* ``Authenticator`` — maps an API key to a credential. Implemented by
  a Postgres-backed credential table in production; faked in tests.

Tests live at these seams and nowhere inside them (TDD skill).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    SCOPE_FLEET,
    SCOPE_ORG,
    SCOPE_SELF,
    Agent,
    Fleet,
    TrustLevel,
    Visibility,
)
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    embeddable_text,
)
from hivemind.domain.feedback import Feedback, FeedbackCounts


@runtime_checkable
class Store(Protocol):
    """Read/write access to the memory pool."""

    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
    ) -> Entry:
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to the ``superseded`` state (SPEC.md §4.1). Returns the stored
        Entry (with server-assigned ``id``/``created_at``).

        ``embedding_model`` (SPEC.md §7) records which model produced
        ``embedding`` so the vector's provenance is traceable per entry.
        """
        ...

    async def get_entry(self, entry_id: str) -> Entry | None: ...

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        """Fetch full entries by ID (missing IDs are simply absent)."""
        ...

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an entry to ``withdrawn`` (SPEC.md §4.1). Only active
        entries can be withdrawn; ``by_user`` is the API-layer audit of
        who withdrew it (v1 persists the reason, not the withdrawer —
        SPEC.md §4.1 lists ``withdrawn_reason`` only). Raises
        ``KeyError`` if unknown, ``ValueError`` if not active.
        """
        ...

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        """List / filter entries (SPEC §5.1). When ``visibility`` is
        supplied, only entries visible to that reader are returned
        (ADR 0011); ``None`` keeps the v1 flat-pool behavior."""
        ...

    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        """Ranked entry IDs by keyword relevance (descending).

        When ``visibility`` is supplied, only entries visible to that
        reader are candidates (ADR 0011); ``None`` keeps the v1 behavior.
        """
        ...

    async def search_vector(
        self,
        embedding: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
    ) -> list[str]:
        """Ranked entry IDs by embedding similarity (descending).

        When ``visibility`` is supplied, only entries visible to that
        reader are candidates (ADR 0011); ``None`` keeps the v1 behavior.
        """
        ...

    # -- fleets (ADR 0011) ----------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet. ``ValueError`` if the name already exists."""
        ...

    async def list_fleets(self) -> list[Fleet]: ...

    async def get_fleet(self, fleet_id: str) -> Fleet | None: ...

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register (or re-register) an agent. Idempotent: an existing
        record is returned unchanged; a new record is ``pending`` (level 0,
        no fleet) — ADR 0012."""
        ...

    async def get_agent(self, name: str) -> Agent | None: ...

    async def list_agents(self) -> list[Agent]: ...

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending agent: set its trust level + home fleet and
        flip it to ``active`` (ADR 0012). ``KeyError`` if unknown."""
        ...

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). Demotion to
        level 0 is *dormant* (key still valid, no access) — distinct from
        revocation (ADR 0012). ``KeyError`` if unknown."""
        ...

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``KeyError`` if unknown."""
        ...

    async def record_feedback(self, feedback: Feedback) -> None:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        ...

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        """(helpful, stale, wrong) counts for one entry."""
        ...

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        """Batched feedback counts for a set of entries."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider: text in, fixed-dimension vector out."""

    @property
    def dimension(self) -> int:
        """The fixed vector dimension (deploy-time decision, ADR 0005)."""
        ...

    @property
    def model_name(self) -> str:
        """The embedding model identifier (recorded per entry, ADR 0005)."""
        ...

    async def embed_text(self, text: str) -> list[float]:
        """Embed a query or a standalone text."""
        ...

    async def embed_entry(self, draft: EntryDraft) -> list[float]:
        """Embed the text an entry is indexed by (SPEC.md §7)."""
        ...

    def entry_embeddable_text(self, draft: EntryDraft) -> str:
        """The exact text an entry is embedded from."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    """Credential check + key management (SPEC.md §8, ADR 0012).

    ``verify`` maps a raw key to a ``Credential``. The key-management
    methods (``issue_agent_key`` / ``revoke_agent_key`` / ``rotate_org_key``
    / ``issue_admin_key``) create and retire credentials; each raw key is
    printed **once** at issuance and only its hash is stored (a leaked
    database never leaks usable keys — SPEC.md §8.1).
    """

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None`` if unknown."""
        ...

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to the registered agent name; return
        the raw secret once (never stored)."""
        ...

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the name stays reserved, ADR 0012)."""
        ...

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (the cluster kill switch, ADR 0012);
        returns the new raw secret once."""
        ...

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        ...


@dataclass(frozen=True, slots=True)
class Credential:
    """A verified caller: who acts, under which agent, with which rights.

    ``user_id`` / ``agent_id`` are the v1 identity (SPEC.md §8.1).
    The v2 access model (ADRs 0011-0012) layers on:

    * ``is_org`` — the shared org key (gates registration + health only;
      all data-plane privilege comes from the agent key, ADR 0012).
    * ``trust_level`` / ``home_fleet_id`` — the agent's privilege (ADR 0011);
      ``agent_name`` is the agent's registered name (it becomes the entry's
      ``author``; verified server-side, never self-reported, ADR 0012).
    """

    user_id: str
    agent_id: str | None = None
    is_admin: bool = False
    # v2 access model (ADRs 0011-0012):
    is_org: bool = False
    access_controlled: bool = False
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    home_fleet_id: str | None = None
    agent_name: str | None = None

    def visibility(self) -> Visibility:
        """The reader's ``Visibility`` derived from this credential (ADR
        0011). Admins see everything; others see per the trust matrix."""
        name = self.agent_name or self.user_id
        if self.is_admin or not self.access_controlled:
            return Visibility(
                level=TrustLevel.PRIVILEGED,
                name=name,
                home_fleet_id=self.home_fleet_id,
                is_admin=True,
            )
        return Visibility(
            level=self.trust_level,
            name=name,
            home_fleet_id=self.home_fleet_id,
        )

    def max_write_scope(self) -> str:
        """The highest scope this credential may write (ADR 0011): level
        2/3 (contributor/privileged) may write ``self`` or ``fleet``;
        level 1 (lurker) only ``self``; level 0 (untrusted) may not write
        at all (enforced at the write seam)."""
        if self.is_admin or not self.access_controlled:
            return SCOPE_ORG
        if self.trust_level in (TrustLevel.CONTRIBUTOR, TrustLevel.PRIVILEGED):
            return SCOPE_FLEET
        return SCOPE_SELF

    def can_write(self) -> bool:
        """Whether this credential may write at all (ADR 0011). Level 0
        (untrusted) may not; the admin key always may."""
        if self.is_admin or not self.access_controlled:
            return True
        return self.trust_level > TrustLevel.UNTRUSTED

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        kind = "admin" if self.is_admin else ("org" if self.is_org else "agent")
        return (
            f"Credential({kind}, user={self.user_id!r}, "
            f"agent={self.agent_name or self.agent_id!r}, level={self.trust_level.value})"
        )


def entry_embeddable_text(draft: EntryDraft) -> str:
    """The text an entry is embedded from (SPEC.md §7)."""
    return embeddable_text(draft.summary, draft.body)
