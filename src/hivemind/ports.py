"""The seams tests live at: ``Store``, ``Embedder``, ``Extractor`` (optional, ADR 0016) and
``Authenticator``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    SCOPE_ORG,
    SCOPE_SELF,
    Agent,
    Fleet,
    TrustLevel,
    Visibility,
    write_scopes_for,
)
from hivemind.domain.activity import Activity
from hivemind.domain.agent_cleanup import (
    AgentCleanupRecord,
    CleanupOutcome,
    CleanupScan,
    CleanupUndoOutcome,
)
from hivemind.domain.audit import ActorKind, AuditEvent, AuditFilters, AuditRecord
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    ExtractedEntity,
    SearchCount,
    UsageCount,
    embeddable_text,
)
from hivemind.domain.entry_restore import Restoration
from hivemind.domain.feedback import (
    Feedback,
    FeedbackCounts,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    ReliedOnChange,
)
from hivemind.domain.fleet_merge import (
    DuplicateScan,
    FleetFacts,
    FleetMergeRecord,
    MergeOutcome,
    MoveOutcome,
    MovePlan,
    UnmergeOutcome,
)
from hivemind.domain.fleet_phase import FleetPhase
from hivemind.domain.health import Convergence, FleetHealth
from hivemind.domain.offboarding import OffboardMode, OffboardStep
from hivemind.domain.pin import Pin, PinRefusal, PinWrite
from hivemind.domain.redaction import RedactOutcome
from hivemind.domain.succession import HandedDown, Succession, owned_authors
from hivemind.domain.upstream import UpstreamLink


class SupersedeConflict(Exception):
    """``create_entry`` supersession targets not active at flip time (ADR 0034); nothing written.

    ``ids`` are the offending targets; the write service maps it to ``SupersedeDenied``."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__("supersession targets not active: " + ", ".join(ids))
        self.ids = ids


@runtime_checkable
class Store(Protocol):
    """Read/write access to the memory pool."""

    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        entities: tuple[ExtractedEntity, ...] = (),
        entities_model: str | None = None,
    ) -> Entry:
        """Insert an entry and flip its ``draft.supersedes`` targets to ``superseded`` (SPEC §4.1).

        - ``embedding_model`` / ``entities_model`` record provenance.
        - Atomic (ADR 0034): an unknown or inactive target raises ``SupersedeConflict``, nothing
          written.
        - ``draft.see_also`` links (ADR 0057) go in the same transaction; ids naming no entry are
          dropped.
        - A fleet-scoped draft aimed at a merged fleet raises ``FleetMerged``, nothing written (ADR
          0107).
        """
        ...

    async def get_entry(self, entry_id: str) -> Entry | None: ...

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        """Fetch full entries by ID (missing IDs are simply absent)."""
        ...

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        """Cosine similarity of every pair among ``entry_ids`` (ADR 0074), keyed ``(a, b)`` with ``a
        < b`` as strings.

        A pair is absent when either entry is unknown or has no vector. Stored vectors only (no
        embedder call); not filtered by visibility.
        """
        ...

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        """Every entry whose ``superseded_by`` is in ``entry_ids``, any order (SPEC §5.1
        ``?history``).

        Unfiltered by state or visibility; the chain walk filters (ADR 0033)."""
        ...

    async def entry_links(
        self, entry_id: str, limit: int, *, visibility: Visibility | None = None
    ) -> tuple[list[str], list[str]]:
        """The "see also" links of one entry: ids it links to (oldest first, unfiltered), and
        at most ``limit`` ids linking to it (newest first).

        Only active sources ``visibility`` may read count toward the incoming ``limit`` (``None`` =
        no restriction)."""
        ...

    async def entry_links_many(
        self, entry_ids: list[str], limit: int, *, visibility: Visibility | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        """``entry_links`` for several entries in one read (ADR 0059); every requested id is a key,
        ``([], [])`` if none."""
        ...

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        """Outgoing ``see_also`` links of several entries, with what happened to each target (ADR
        0081).

        Per entry: at most ``MAX_SEE_ALSO`` links, oldest first then by target id, targets in any
        state but readable under ``visibility`` (``None`` = all); an unreadable target is left out
        as if absent. Only entries with a link are keys; malformed ids are left out. Facts only:
        ``upstream_changed`` applies the rule."""
        ...

    async def pin_entry(
        self,
        fleet_id: str,
        entry_id: str,
        pinned_by: str,
        limit: int,
        replaces: Sequence[str] = (),
        agent_limit: int | None = None,
        also_owned_by: Sequence[str] = (),
    ) -> PinWrite | PinRefusal:
        """Pin ``entry_id`` to ``fleet_id`` with a fresh ``pinned_at`` / ``pinned_by`` (a re-pin
        refreshes).

        - Pins in ``replaces`` are removed in the same step and reported in ``PinWrite.replaced``.
        - ``PinRefusal``, nothing written, when the fleet would hold more than ``limit`` pins (a
          move or refresh never grows the count), or ``pinned_by`` more than ``agent_limit`` of them
          (``None`` = exempt; a refresh restamps ``pinned_by``, so it counts). Both checked
          atomically (ADR 0096, ``pin_refusal``); pins of ``also_owned_by`` (ADR 0113) count as its
          own.
        - A malformed ``entry_id`` is refused as ``FLEET``; a merged ``fleet_id`` raises
          ``FleetMerged`` (ADR 0107).
        - The caller has checked the entry and the permission."""
        ...

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        """Remove a pin; whether there was one."""
        ...

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        """A fleet's pins, newest first."""
        ...

    async def usage_counts(self) -> list[UsageCount]:
        """Entry counts per (scope, kind, importance_source, author, fleet_id, active), every state
        included (ROADMAP §3.3)."""
        ...

    async def flag_counts(self) -> list[FlagCount]:
        """Per reporter (the ``"user"`` column): ``stale``/``wrong`` rows written while the entry
        was current, and how many of those entries were superseded after the row (ADR 0068).

        Left out: rows on already superseded entries, revoked reporters (ADR 0067), set-aside
        ratings (ADR 0111). A reporter with no flags has no row; any order."""
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

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        """Count one first-page search from ``fleet_id`` (``None`` = no home fleet), empty (ADR
        0056) or ``explore`` (ADR 0078). Counts only: no query text, no agent name."""
        ...

    async def search_counts(self) -> list[SearchCount]:
        """The search counters, one row per home fleet, any order."""
        ...

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an active entry to ``withdrawn`` (SPEC §4.1); persists the reason, not ``by_user``.

        ``KeyError`` if unknown, ``ValueError`` if not active."""
        ...

    async def restore_entry(self, entry_id: str) -> Restoration:
        """Put a withdrawn entry back to ``active``, clearing its withdrawal reason and time (ADR
        0124); pins are not restored.

        State check and update are one atomic step. ``KeyError`` if unknown; ``RestoreRefused``
        (a subclass says why) if not withdrawn, withdrawn by a standing agent cleanup, or it would
        leave two active heads in a chain.
        """
        ...

    async def redact_entry(self, entry_id: str) -> RedactOutcome:
        """Wipe a memory's text for good, in one transaction (ADR 0125): summary, body, payload,
        tags, sources, entities, outgoing see-also links, withdrawal reason, embedding, keyword
        index.

        - Sets ``redacted_at``; kind, scope, author, fleet, dates, supersession, feedback and the
          audit log stay.
        - Returns the memory now, the count of feedback notes (and stale/wrong flag notes among
          them) left unedited, and of audit rows holding a withdrawal reason (``entry.withdraw``
          ``reason``, ``entry.restore`` ``previous_reason``).
        - Allowed on ``withdrawn`` or ``superseded``. ``KeyError`` if unknown; ``RedactRefused`` if
          active or already redacted.
        - Every path that reactivates a withdrawn memory skips a redacted one."""
        ...

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        """List and filter entries (SPEC §5.1), limited to what ``visibility`` may read (ADR 0011);
        ``None`` = the flat pool."""
        ...

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (SPEC §5.3); ``visibility`` as in ``list_entries``."""
        ...

    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        """Entry IDs by keyword relevance, descending; ``visibility`` as in ``list_entries``."""
        ...

    async def search_vector(
        self,
        embedding: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
        min_similarity: float = 0.0,
    ) -> list[str]:
        """Entry IDs by embedding similarity, descending, leaving out cosine similarity <=
        ``min_similarity`` (0 by default, ADR 0061; ADR 0062); ``visibility`` as in
        ``list_entries``."""
        ...

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """Active entries nearest ``embedding`` as ``(id, cosine similarity)``, highest first, minus
        ``exclude_id`` (ADR 0052) and limited to ``visibility`` when given.

        Approximate on Postgres, like ``search_vector`` (ADR 0025)."""
        ...

    # -- fleets (ADR 0011) ----------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet. ``FleetExists`` if the name already exists."""
        ...

    async def list_fleets(self) -> list[Fleet]:
        """Every fleet, merged ones included (``Fleet.merged_into``)."""
        ...

    async def get_fleet(self, fleet_id: str) -> Fleet | None: ...

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        """Rename a fleet, merged ones included; the id never changes (ADR 0109).

        Returns the fleet before and after (the same object, nothing written, when ``name`` is
        unchanged). ``UnknownFleet``; ``FleetExists`` when another fleet has the name."""
        ...

    async def set_fleet_phase(self, fleet_id: str, phase: FleetPhase | None) -> Fleet:
        """Replace the fleet's phase, or clear it with ``None`` (ADR 0143); the fleet after.
        ``UnknownFleet``; ``FleetMerged`` for a merged fleet, nothing written."""
        ...

    # -- fleet merge (ADR 0107) ------------------------------------------------

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        """Agents whose home fleet is ``fleet_id`` and its entry counts by state (an empty state is
        absent)."""
        ...

    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        """For the newest ``cap`` active vectored entries of ``fleet_id``, the nearest active entry
        of ``other_id`` at cosine similarity >= ``threshold``, highest first.

        ``truncated`` when more than ``cap`` qualified. Approximate on Postgres (ADR 0025)."""
        ...

    async def merge_fleets(
        self,
        source_id: str,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        merged_by: str,
    ) -> MergeOutcome:
        """Merge ``source_id`` into an existing target or a new fleet, in one transaction (ADR
        0107).

        - Re-files its entries (any state), moves its agents, re-keys the kept pins, unpins the
          rest, adds its search counters to the target's, marks it merged.
        - Records everything moved so ``unmerge_fleet`` can reverse it.
        - Refusals change nothing: ``UnknownFleet``; ``FleetMerged`` (either side already merged);
          ``FleetMergeRefused`` (same fleet on both sides, or the pins now differ from
          ``expected_pin_ids``, the pins the preview showed); ``FleetExists`` (taken new name);
          ``InvalidInput`` (neither or both targets, or a pin choice ``choose_pins`` refuses).
        - A write into a fleet being merged waits briefly, then raises ``FleetBusy`` (retryable)."""
        ...

    async def plan_fleet_move(self, source_id: str, agents: Sequence[str]) -> MovePlan:
        """Read-only: the memories moving ``agents`` out of ``source_id`` would re-file
        (``plan_move_entries``, ADR 0112)."""
        ...

    async def move_agents(
        self,
        source_id: str,
        agents: Sequence[str],
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        moved_by: str,
    ) -> MoveOutcome:
        """Move ``agents`` (home fleet ``source_id``) and the ``fleet``-scoped memories they wrote
        there into an existing target or a new fleet, in one transaction (ADR 0112).

        - Memories ``plan_move_entries`` selects are re-filed (any state); their pins follow; the
          target's pins plus those are trimmed with ``choose_pins``.
        - The source stays a normal fleet; search counters stay.
        - Recorded in the merge list as kind ``move`` so ``unmerge_fleet`` can reverse it.
        - Refusals change nothing: ``UnknownFleet``, ``FleetMerged`` (either side), ``FleetExists``,
          ``InvalidInput`` (no agents, not exactly one target, bad pin choice), ``FleetMoveRefused``
          (same fleet, an agent not in the source, ``expected_pin_ids`` stale, busy locks).
        - Writes into the two fleets wait briefly, then raise ``FleetBusy``."""
        ...

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        """Undo a merge or move (ADR 0107, 0112): recorded entries and agents still in the target go
        back to the source, with their pins and the dropped pins that fit.

        Search counters stay summed; a merge also clears the source's merged mark.
        ``UnknownFleetMerge``; ``FleetMergeRefused`` (``FleetMoveRefused`` for a move) when
        ``undo_refusal`` says no."""
        ...

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        """Recorded merges, newest first."""
        ...

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None: ...

    # -- agent cleanup (ADR 0111) ------------------------------------------------

    async def scan_agent_cleanup(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        authors: Sequence[str] | None = None,
        visibility: Visibility | None = None,
    ) -> CleanupScan:
        """Read-only: what cleaning up ``agent`` over ``[since, until)`` would change (ADR 0111).

        - ``authors``: whose memories count (``[agent]`` if omitted; the portal passes every author
          the agent owns, ADR 0118). ``visibility`` limits restorable memories; ratings are always
          ``agent``'s own.
        - Holds: those authors' active memories created in the window (any scope), the agent's
          ratings updated in the window and not already set aside, the pins on those memories, and
          the memories they superseded (still ``superseded`` by a memory to withdraw).
        - Lists are cut to the preview limits, newest first; totals are exact."""
        ...

    async def cleanup_agent(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        restore_ids: Sequence[str] | None = None,
        expected_withdraw: int | None = None,
        expected_ratings: int | None = None,
        cleaned_by: str,
        authors: Sequence[str] | None = None,
        self_service: bool = False,
        visibility: Visibility | None = None,
    ) -> CleanupOutcome:
        """Clean up ``agent`` in one transaction (ADR 0111).

        - Withdraws the memories ``scan_agent_cleanup`` lists (reason ``cleanup_reason``), unpins
          them, sets the window's ratings aside, puts ``restore_ids`` back to ``active``.
        - Records the change for ``undo_agent_cleanup``; nothing to change keeps no record
          (``record`` is ``None``).
        - ``AgentCleanupRefused`` (nothing changes): a ``restore_ids`` memory not restorable, counts
          differ from ``expected_*`` (the preview), or another cleanup of the agent is running.
          ``InvalidInput`` for an empty window.
        - The agent's own writes and ratings wait for the row locks, never interleave.
        - ``authors`` as in ``scan_agent_cleanup``; ``self_service`` records the agent ran it itself
          (ADR 0118); ``visibility`` (self-service) limits what may be offered and restored."""
        ...

    async def mark_cleanup_revoked(self, cleanup_id: str) -> None:
        """Note on the record that the cleanup also revoked the agent."""
        ...

    async def undo_agent_cleanup(self, cleanup_id: str) -> CleanupUndoOutcome:
        """Undo a cleanup (ADR 0111): reactivate memories still withdrawn with its reason,
        re-supersede the restored ones (only if still active and their former successor is active),
        clear its rating marks, re-pin where the fleet is under the caps. Does not un-revoke.

        ``UnknownAgentCleanup``; ``AgentCleanupRefused`` when ``undo_refusal`` says no."""
        ...

    async def list_agent_cleanups(
        self, limit: int, *, agent: str | None = None
    ) -> list[AgentCleanupRecord]:
        """Recorded cleanups, newest first; only ``agent``'s when given."""
        ...

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None: ...

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register idempotently: an existing agent comes back unchanged, a new one is ``pending``
        (level 0, no fleet). A case-only variant of a name returns that agent (ADR 0045)."""
        ...

    async def get_agent(self, name: str) -> Agent | None: ...

    async def list_agents(self) -> list[Agent]: ...

    async def agent_key_last_used(self, name: str) -> datetime | None:
        """When the agent's key last authenticated, to the hour (ADR 0110);
        ``None`` if it never has, or the agent has no key (ADR 0114)."""
        ...

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Atomically flip a pending or revoked agent to ``active`` (ADR 0012, 0028).

        ``UnknownAgent``; ``InvalidAgentStatus`` if already ``active``; ``FleetMerged`` (ADR
        0107)."""
        ...

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked``; the credential is the ``Authenticator``'s.

        ``UnknownAgent``; ``InvalidAgentStatus`` if already ``revoked``."""
        ...

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Change an agent's trust level (ADR 0011). Level 0 is dormant (key valid, no access), not
        revoked (ADR 0012). ``UnknownAgent``."""
        ...

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent; its earlier ``fleet``-scoped entries stay where written.

        ``UnknownAgent``; ``FleetMerged`` (ADR 0107)."""
        ...

    async def set_agent_owner_alias(self, name: str, owner_alias: str) -> tuple[Agent, Agent]:
        """Set an agent's owner alias, any status (ADR 0122).

        Returns the agent before and after, read under one row lock so the audit ``from`` is the
        value replaced; an equal alias changes nothing. The alias lives only on the agent row.
        ``UnknownAgent``."""
        ...

    # -- successions (ADR 0113) ------------------------------------------------

    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        """Record that ``retired`` hands its memories to ``successor`` and revoke ``retired`` (when
        active), in one transaction (ADR 0113). The key stops working with the status.

        Refusals change nothing: ``UnknownAgent``; ``InvalidAgentStatus`` (``retired`` pending or
        ``successor`` not active); ``SuccessionRefused`` (``retired`` already hands down, or
        ``successor`` hands down to it directly or through a chain). ``was`` is ``retired``'s
        status seen under the lock."""
        ...

    async def offboard_owner(
        self,
        alias: str,
        mode: OffboardMode,
        successor: str | None,
        by: str,
        expected: Mapping[str, str] | None = None,
    ) -> list[OffboardStep]:
        """Offboard every agent whose owner alias is exactly ``alias`` (ADR 0135), in one
        transaction with their rows (and the successor's) locked, so states are re-checked here
        and the plan is applied whole or not at all.

        ``mode`` ``retire``: each active or revoked agent is retired to ``successor`` as
        ``retire_agent`` does, a pending one is rejected. ``revoke``: active agents are revoked,
        pending ones rejected, ``successor`` is ``None``. Agents already revoked (or retired) are
        skipped. An agent that is retired yet active (an older pod re-activated it) is revoked under its
        succession in both modes. Returns one step per agent, by name. ``UnknownOwner``; ``UnknownAgent``
        (successor); ``InvalidAgentStatus`` (successor not active); ``SuccessionRefused``
        (successor of the same owner, or a cycle); ``InvalidInput`` (mode and successor do not go
        together, checked first). ``expected`` (agent name to action, the preview's) refuses with
        ``OffboardChanged`` when the plan under the locks differs. Every refusal changes nothing."""
        ...

    async def undo_succession(self, retired: str) -> Succession:
        """Delete ``retired``'s succession; the agent stays revoked. ``UnknownSuccession`` when none
        stands."""
        ...

    async def get_succession(self, retired: str) -> Succession | None: ...

    async def list_successions(self) -> list[Succession]:
        """Every standing succession, newest first."""
        ...

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        """Agents that hand down to ``name``, transitively, sorted
        (``Credential.inherits_from``)."""
        ...

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        """What owning ``authors`` brings: active ``self`` notes, ``fleet`` memories (and how many
        sit outside ``home_fleet_id``) and pins."""
        ...

    # -- audit log (ADR 0027, SPEC §12.5) ------------------------------------

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one admin-action row (insert-only). Failures propagate, never swallowed (ADR
        0027)."""
        ...

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Audit rows matching ``filters``, newest first."""
        ...

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        """Upsert a verdict (one row per entry+user+agent); returns the row as stored.

        No ``updated_at`` is stamped with the store clock. Rating again a memory whose rating a
        cleanup set aside clears the mark (ADR 0111)."""
        ...

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        """Upsert several rows in one transaction, all or none (ADR 0079); returns them as stored,
        in order. Rows without ``updated_at`` share one store-clock stamp. Empty is a no-op."""
        ...

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        """(helpful, stale, wrong) counts. Every feedback read leaves out revoked reporters (ADR
        0067) and set-aside rows (ADR 0111)."""
        ...

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        """Batched counts: every well-formed UUID requested is a key, ``(0, 0, 0)`` if none;
        malformed ids are left out."""
        ...

    async def relied_on_changes(
        self, entry_ids: list[str], reporter: tuple[str, str]
    ) -> dict[str, ReliedOnChange]:
        """For each of ``entry_ids`` the ``reporter`` (user, agent) rated helpful, what changed
        since (ADR 0138), judged as the ``relied_on_changed`` filter does (ADR 0098), in one
        read. Entries it did not rate helpful, malformed ids and unknown ids are left out."""
        ...

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        """``quality_counts`` plus ``last_confirmed_at`` (SPEC §4.2), ``newest_flag_at`` (newest
        stale or wrong row, noted or not) and ``correction`` (newest stale or wrong report with a
        non-empty note, cut by ``truncate_note``; SPEC §6.1), in one read.

        Every well-formed UUID requested is a key (zero counts, ``None``s if none); malformed ids
        are left out. Revoked reporters are excluded. With ``count_supersedes``, ``supersedes``
        counts entries naming the id in ``superseded_by`` that ``visibility`` may read; otherwise
        0."""
        ...

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's feedback rows, at most ``limit``: informative ones first (stale or wrong, or a
        note), then bare ``helpful`` (ADR 0064); each group newest ``updated_at`` first, then
        reporter.

        No visibility check: the caller has established the entry is readable."""
        ...

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        """``list_feedback`` for several entries in one read (ADR 0059); every requested id is a key
        (``[]`` if none)."""
        ...

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries matching ``filters``, each with its entry: newest
        ``updated_at`` first, then entry id, reporter, agent.

        Only rows whose entry ``visibility`` may read; revoked reporters (ADR 0067) and set-aside
        ratings (ADR 0111) are left out. A malformed ``entry_id`` or ``fleet_id`` matches
        nothing."""
        ...

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        """Active entries with a standing ``stale`` or ``wrong`` verdict (ADR 0093), oldest flag
        first, then entry id.

        Revoked reporters (ADR 0067) and set-aside rows (ADR 0111) are left out, so an entry only
        they flagged is not listed. ``orphaned`` at ``now``: the author is revoked or wrote nothing
        in the ``ORPHAN_SILENCE_DAYS`` before it (as ``fleet_health``). Only entries ``visibility``
        may read; a malformed ``fleet_id`` matches nothing."""
        ...

    async def health_check(self) -> bool:
        """Whether the database answers. Must never raise: unauthenticated probes call it."""
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
        """The embedding model identifier (recorded per entry)."""
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
class Extractor(Protocol):
    """Entity extractor (ADR 0016, SPEC §13): optional (no endpoint = off) and best-effort (a
    failure never blocks the write)."""

    @property
    def model_name(self) -> str:
        """The extractor model identifier (recorded per entry)."""
        ...

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        """Entity facets from the entry's embedded text (SPEC §13.1)."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    """Credential check and key management (SPEC §8, ADR 0012). A raw key is returned once; only its
    hash is stored (SPEC §8.1)."""

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None``.

        An agent key resolves only while its agent is ``active`` (ADR 0039): pending, revoked or
        missing agents, pre-v2 ``user`` keys and name-less agent keys give ``None``. A portal-locked
        key of an active agent raises ``KeyLocked`` (ADR 0117)."""
        ...

    async def credential_for_agent(self, name: str) -> Credential | None:
        """What ``verify`` returns for the agent's key, built from the agent's record instead of a
        key (``key_id`` is ``None``; a locked key changes nothing). ``None`` unless the agent is
        ``active``, whether unknown, pending or revoked (ADR 0136)."""
        ...

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key; returns the raw secret once. ``UnknownAgent``; ``InvalidAgentStatus``
        unless ``active`` (ADR 0039). Replaces any stale key row: one live key per agent."""
        ...

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        """Swap an active agent's key in one transaction (ADR 0108): the old key stops at once.

        The ``agent_key.replace`` audit row (both fingerprints, never a key) is written in the same
        transaction under the given actor. ``UnknownAgent``; ``InvalidAgentStatus`` unless
        ``active``. The raw key is returned once."""
        ...

    async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
        """Lock the agent's own key from the portal (ADR 0117): ``verify`` raises ``KeyLocked`` from
        the next request.

        Only the key with fingerprint ``key_id`` is locked, so a key replaced meanwhile is left
        alone. Writes the ``agent_key.lock`` audit row (actor kind ``agent_key``, the agent's name,
        the fingerprint, ``via: "portal"``, never a key) in the same transaction. ``False``, nothing
        written, when that key is already locked or gone."""
        ...

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the name stays reserved, ADR 0012)."""
        ...

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (closes registration, ADR 0031); returns the raw secret
        once."""
        ...

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        ...


@dataclass(frozen=True, slots=True)
class KeyReplacement:
    """Outcome of ``Authenticator.replace_agent_key`` (ADR 0108): the new raw key (shown once) and
    both non-secret fingerprints; ``old_key_id`` is ``None`` if the agent held no key."""

    raw_key: str = field(repr=False)
    new_key_id: str
    old_key_id: str | None


@dataclass(frozen=True, slots=True)
class Credential:
    """A verified caller: who acts and with which rights.

    ``user_id`` / ``agent_id`` are the v1 identity (SPEC §8.1). v2 adds (ADRs 0011-0012):

    * ``is_org``: the shared org key (registration and health only).
    * ``trust_level`` / ``home_fleet_id``: the agent's privilege; ``agent_name`` is its verified
      name (the entry ``author``).
    * ``key_id``: the key's non-secret fingerprint (first 12 hex chars of its hash, ADR 0027);
      ``None`` with no stored key (dev mode, fakes).
    """

    user_id: str
    agent_id: str | None = None
    is_admin: bool = False
    is_org: bool = False
    access_controlled: bool = False
    trust_level: TrustLevel = TrustLevel.UNTRUSTED
    home_fleet_id: str | None = None
    agent_name: str | None = None
    key_id: str | None = None  # ADR 0027: key fingerprint, never the key
    # Agents handing down to this one, transitively (ADR 0113): their memories are its to read,
    # supersede and withdraw, not to rate; ``author`` stays the original name.
    inherits_from: tuple[str, ...] = ()

    @property
    def owned_authors(self) -> tuple[str, ...]:
        """Authors whose memories this caller owns: its own name, then every agent handing down to
        it (ADR 0113)."""
        return owned_authors(self.agent_name or self.user_id, self.inherits_from)

    def owns(self, author: str) -> bool:
        """Whether a memory by ``author`` is this caller's own."""
        return author == (self.agent_name or self.user_id) or author in self.inherits_from

    def audit_actor(self) -> str:
        """The audit ``actor`` (ADR 0027): ``admin:<fingerprint>`` (admin keys share ``user_id``),
        ``org:<fingerprint>`` (ADR 0046), else the agent's name (ADR 0096)."""
        if not (self.is_org or self.is_admin):
            return self.agent_name or self.user_id
        if self.key_id is None:
            return self.user_id
        return f"{'org' if self.is_org else 'admin'}:{self.key_id}"

    def visibility(self) -> Visibility:
        """The reader's ``Visibility`` (ADR 0011): admins see everything, others per the trust
        matrix."""
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
            inherited=self.inherits_from,
        )

    def max_write_scope(self) -> str:
        """The highest scope this credential may write (ADR 0011): levels 2/3 ``fleet``, level 1
        ``self``; level 0 writes nothing (enforced at the write seam)."""
        if self.is_admin or not self.access_controlled:
            return SCOPE_ORG
        # Level 0 keeps `self` here but writes nothing: `can_write` gates it.
        scopes = write_scopes_for(self.trust_level)
        return scopes[-1] if scopes else SCOPE_SELF

    def can_write(self) -> bool:
        """Whether this credential may write at all: not level 0, always the admin key."""
        if self.is_admin or not self.access_controlled:
            return True
        return self.trust_level > TrustLevel.UNTRUSTED

    @property
    def is_stored(self) -> bool:
        """Whether the authenticator issued this credential (ADR 0082): an admin, org or active
        agent key. Otherwise it is a dev-mode, hand-built identity."""
        return self.is_admin or self.is_org or self.agent_name is not None

    def effective(self, require_stored_keys: bool = False) -> Credential:
        """This credential, or its trust-level-0 stand-in when ``HIVEMIND_REQUIRE_STORED_KEYS`` (ADR
        0082) is set and this one is dev-mode. Stored credentials come back as the same object."""
        if require_stored_keys and not self.is_stored:
            return Credential(
                user_id=self.user_id,
                agent_id=self.agent_id,  # attribution survives the downgrade
                access_controlled=True,
                trust_level=TrustLevel.UNTRUSTED,
            )
        return self

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        kind = "admin" if self.is_admin else ("org" if self.is_org else "agent")
        return (
            f"Credential({kind}, user={self.user_id!r}, "
            f"agent={self.agent_name or self.agent_id!r}, level={self.trust_level.value})"
        )


def entry_embeddable_text(draft: EntryDraft) -> str:
    """The embedded text at the default budget (SPEC §7, ADR 0021), for embedders without
    ``Settings``."""
    return embeddable_text(draft.summary, draft.body)
