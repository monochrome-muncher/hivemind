"""Port definitions — the seams the TDD suite tests at.

* ``Store`` — data access (``MemoryStore`` for dev/tests, ``PgStore``).
* ``Embedder`` — text to a fixed-dimension vector.
* ``Extractor`` — entity facets from entry text (ADR 0016, SPEC §13);
  optional (absent ⇒ extraction off).
* ``Authenticator`` — API key to credential, plus key management.

Tests live at these seams and nowhere inside them.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from hivemind.domain.feedback import (
    Feedback,
    FeedbackCounts,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
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
from hivemind.domain.health import Convergence, FleetHealth
from hivemind.domain.pin import Pin, PinRefusal, PinWrite
from hivemind.domain.succession import HandedDown, Succession, owned_authors
from hivemind.domain.upstream import UpstreamLink


class SupersedeConflict(Exception):
    """A ``create_entry`` named supersession targets that are not (or no
    longer) active at flip time (ADR 0034). Nothing was written. ``ids``
    are the offending targets. The write service maps it to its
    ``SupersedeDenied``."""

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
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to ``superseded`` (SPEC.md §4.1); return the stored Entry.

        ``embedding_model`` / ``entities_model`` record provenance
        (SPEC.md §7, ADR 0016). Supersession is atomic (ADR 0034): if any
        target is unknown or no longer active, nothing is written and
        ``SupersedeConflict`` is raised. ``draft.see_also`` links (ADR
        0057) go in the same transaction; the caller has checked read
        access, and ids naming no entry are dropped. A fleet-scoped draft
        aimed at a merged fleet raises ``FleetMerged`` and writes nothing
        (ADR 0107).
        """
        ...

    async def get_entry(self, entry_id: str) -> Entry | None: ...

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        """Fetch full entries by ID (missing IDs are simply absent)."""
        ...

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        """Cosine similarity between the stored vectors of every pair among
        ``entry_ids`` (ADR 0074), keyed ``(a, b)`` with ``a < b`` as strings.
        A pair is absent when either entry is unknown or has no stored
        vector. Reads vectors only (no embedder call), so it works when the
        embedder is down; unfiltered by visibility (the caller passes
        candidates it may already read).
        """
        ...

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        """Every entry whose ``superseded_by`` is one of ``entry_ids``, in
        any order (SPEC §5.1 ``?history``). Unfiltered by state or
        visibility: the chain walk filters what it returns (ADR 0033)."""
        ...

    async def entry_links(self, entry_id: str, limit: int) -> tuple[list[str], list[str]]:
        """The "see also" links of one entry: the ids it links
        to (oldest link first), and the ids of at most ``limit`` entries
        that link to it (newest link first). Unfiltered by state or
        visibility (the caller filters)."""
        ...

    async def entry_links_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, tuple[list[str], list[str]]]:
        """``entry_links`` for several entries in one read (ADR 0059),
        keyed by every requested id; an id with no links (or naming no
        entry) maps to ``([], [])``."""
        ...

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        """The outgoing ``see_also`` links of several entries with what
        happened to each target, in one read (ADR 0081): per entry, its
        links (at most ``MAX_SEE_ALSO``, by the write cap) oldest first then by target id,
        each target limited to what ``visibility`` may read, in any state
        (``None`` = all); a link to an unreadable target is left out as if
        it did not exist. Only entries that have such a link are keys;
        malformed ids are left out. Facts only: ``upstream_changed`` applies
        the rule."""
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
        """Pin ``entry_id`` to ``fleet_id`` with a fresh
        ``pinned_at`` / ``pinned_by``: a new row, or the existing one
        refreshed (a re-pin acknowledges a change since pinned). Pins named
        in ``replaces`` (older versions it now shows) are removed in the
        same step and reported in ``PinWrite.replaced``. A ``PinRefusal`` (nothing
        written) when the fleet would hold more than ``limit`` pins (a move
        or refresh never grows the count) or, with ``agent_limit``, when
        ``pinned_by`` would hold more than that many of the fleet's pins
        afterwards (a refresh restamps ``pinned_by``, so it counts; ``None``
        is exempt); the pins of the agents in ``also_owned_by`` (the ones that
        handed down to ``pinned_by``, ADR 0113) count as its own. Both are
        checked atomically (ADR 0096; ``pin_refusal``).
        A malformed ``entry_id`` is refused as ``FLEET`` too, writing nothing.
        A merged ``fleet_id`` raises ``FleetMerged`` (ADR 0107).
        The caller has checked the entry and the permission."""
        ...

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        """Remove a pin; whether there was one."""
        ...

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        """A fleet's pins, newest first."""
        ...

    async def usage_counts(self) -> list[UsageCount]:
        """Entry counts per (scope, kind, importance_source, author,
        fleet_id, active), every state included; ``MetricsService`` folds
        them into the ROADMAP §3.3 counters."""
        ...

    async def flag_counts(self) -> list[FlagCount]:
        """Per reporter (the ``"user"`` column), the ``stale``/``wrong`` rows
        written while their entry was still current, and how many of those
        entries were superseded after the row (ADR 0068). Rows on entries
        already superseded when written, and revoked reporters (ADR 0067)
        and set-aside ratings (ADR 0111), are left out; a reporter with no flags has no row; any order."""
        ...

    async def fleet_health(self, now: datetime) -> FleetHealth:
        """The fleet health aggregates as of ``now``: counts the
        service turns into the ``health`` report. Verdicts of revoked
        reporters (ADR 0067) and set-aside ones (ADR 0111) are left out; a flag is a standing
        ``stale``/``wrong`` row, its age its ``updated_at``. Observation
        only: no ranking input. Both adapters must agree."""
        ...

    async def activity(self, since: datetime) -> Activity:
        """Who wrote and who reported what, for the admin
        activity view: per (author, fleet) entry counts, per reporter
        verdict counts, the verdicts received per author, and per UTC day
        the writes (``created_at``) and verdicts (``updated_at``) from
        ``since`` on (days with no activity have no row). ``recent`` is
        ``>= since``. Verdicts of revoked reporters (ADR 0067)
        and set-aside ones (ADR 0111) are left out. Observation only: no ranking input. Both adapters
        must agree."""
        ...

    async def fleet_convergence(self, now: datetime) -> Convergence:
        """The vector-backed convergence figures as of ``now``:
        the echo rate over a bounded sample of the newest writes and the
        dispersion of the busiest tags. Bounded work (``ECHO_SAMPLE``
        nearest-neighbour lookups, ``DISPERSION_CAP`` entries per tag and
        window); admin view over the whole pool; observation only."""
        ...

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        """Count one first-page search made from ``fleet_id`` (``None``: no
        home fleet), whether it found nothing (ADR 0056) and whether it was
        an ``explore`` search (ADR 0078). Counts only: no query text, no
        agent name."""
        ...

    async def search_counts(self) -> list[SearchCount]:
        """The search counters recorded so far, one row per home fleet
        in any order."""
        ...

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an active entry to ``withdrawn`` (SPEC.md §4.1). Only the
        reason is persisted, not ``by_user``. ``KeyError`` if unknown,
        ``ValueError`` if not active.
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
        """List / filter entries (SPEC §5.1), restricted to what
        ``visibility`` may read (ADR 0011); ``None`` = the v1 flat pool."""
        ...

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (SPEC §5.3) without fetching
        them; ``visibility`` as in ``list_entries``."""
        ...

    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        """Ranked entry IDs by keyword relevance (descending);
        ``visibility`` as in ``list_entries``."""
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
        """Ranked entry IDs by embedding similarity (descending), leaving
        out entries with cosine similarity <= ``min_similarity`` (0 by
        default, ADR 0061; raised by the search similarity threshold, ADR
        0062); ``visibility`` as in ``list_entries``."""
        ...

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """The active entries nearest to ``embedding``, as ``(id,
        similarity)`` pairs (cosine similarity, highest first), leaving out
        ``exclude_id`` when given (ADR 0052; check mode has no entry yet). Restricted to ``visibility`` when given.
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
        """Give a fleet a new name (ADR 0109), merged fleets included; the
        id never changes. Returns the fleet as it was and as it is now (the
        same object when ``name`` is the current one, then nothing is
        written). ``UnknownFleet`` for an id that names none, ``FleetExists``
        when another fleet has the name."""
        ...

    # -- fleet merge (ADR 0107) ------------------------------------------------

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        """The agents whose home fleet is ``fleet_id`` and its entries by
        state (a state with none is absent)."""
        ...

    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        """For the newest ``cap`` active entries of ``fleet_id`` that have a
        vector, the nearest active entry of ``other_id`` when its cosine
        similarity is at least ``threshold``, highest similarity first.
        ``truncated`` when ``fleet_id`` had more such entries than ``cap``.
        Approximate on Postgres, like ``search_vector`` (ADR 0025)."""
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
        """Merge ``source_id`` into an existing target or a new fleet, in one
        transaction (ADR 0107): re-file its entries (any state), move its
        agents, re-key the kept pins and unpin the rest, add its search
        counters to the target's and mark it merged. Records everything
        moved so ``unmerge_fleet`` can reverse it. ``UnknownFleet`` for an
        unknown side, ``FleetMerged`` when either is already merged,
        ``FleetMergeRefused`` for the same fleet on both sides,
        ``FleetExists`` for a taken new name, ``InvalidInput`` for exactly
        neither or both targets, or a pin choice that ``choose_pins``
        refuses. ``expected_pin_ids`` (all pins the preview showed): the
        merge is refused (``FleetMergeRefused``) when the pins now differ.
        A write into a fleet being merged waits briefly, then raises
        ``FleetBusy`` (retryable). Nothing changes on a refusal."""
        ...

    async def plan_fleet_move(self, source_id: str, agents: Sequence[str]) -> MovePlan:
        """Which memories moving ``agents`` out of ``source_id`` would
        re-file (``plan_move_entries``, ADR 0112), read-only."""
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
        """Move ``agents`` (all with ``source_id`` as home fleet) and the
        ``fleet``-scoped memories they wrote there into an existing target or
        a new fleet, in one transaction (ADR 0112): the memories
        ``plan_move_entries`` selects are re-filed (any state), their pins
        follow, and the target's own pins plus those that followed are
        trimmed with ``choose_pins`` (the same pin picker as a merge). The
        source stays a normal fleet, and search counters stay. Recorded in
        the merge list as kind ``move`` so ``unmerge_fleet`` can reverse it.
        ``UnknownFleet``, ``FleetMerged`` (either side), ``FleetExists``,
        ``InvalidInput`` (no agents, not exactly one target, a bad pin
        choice) and ``FleetMoveRefused`` (same fleet, an agent not in the
        source, ``expected_pin_ids`` no longer the candidate pins, busy
        locks). Writes into the two fleets wait briefly, then raise
        ``FleetBusy``. Nothing changes on a refusal."""
        ...

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        """Undo a merge or a move (ADR 0107, 0112): the recorded entries
        still in the target go back to the source, as do the recorded agents
        still there, the pins on those entries and the dropped pins that
        fit. Search counters stay summed. A merge also clears the source's
        merged mark. ``UnknownFleetMerge`` for an unknown id,
        ``FleetMergeRefused`` (``FleetMoveRefused`` for a move) when
        ``undo_refusal`` says no."""
        ...

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        """Recorded merges, newest first, at most ``limit`` of them."""
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
        """What cleaning up ``agent`` over ``[since, until)`` would change,
        changing nothing. ``authors`` are the names whose memories count
        (``[agent]`` when omitted; the portal passes every author the agent
        owns, ADR 0118); ``visibility`` limits the restorable memories to ones
        that reader may read; the ratings are always ``agent``'s own. The scan
        holds the active memories of those authors whose ``created_at`` is
        in the window (any scope), the agent's ratings whose ``updated_at``
        is in the window and are not already set aside, the pins on those memories,
        and the memories those superseded (still ``superseded``, their
        ``superseded_by`` one of the memories to withdraw). Lists are cut
        to the preview limits, newest first; the totals are exact."""
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
        """Clean up ``agent`` in one transaction (ADR 0111): withdraw the
        memories ``scan_agent_cleanup`` lists (reason ``cleanup_reason``),
        take the pins off them, set the window's ratings aside and put the
        ``restore_ids`` back to ``active``. Records what changed so
        ``undo_agent_cleanup`` can reverse it; with nothing to change no
        record is kept (``record`` is ``None``). ``AgentCleanupRefused``
        when a ``restore_ids`` memory is not restorable now, when the counts
        differ from ``expected_*`` (what the preview showed) or when another
        cleanup of the agent is running; ``InvalidInput`` for an empty
        window. Nothing changes on a refusal. The agent's own writes and
        ratings wait for the row locks, never interleave. ``authors`` as for
        ``scan_agent_cleanup``; ``self_service`` records that the agent ran
        it itself (ADR 0118); ``visibility`` (the self-service path) limits the
        memories that may be offered and restored to what that reader may read."""
        ...

    async def mark_cleanup_revoked(self, cleanup_id: str) -> None:
        """Note on the record that the cleanup also revoked the agent."""
        ...

    async def undo_agent_cleanup(self, cleanup_id: str) -> CleanupUndoOutcome:
        """Undo a cleanup (ADR 0111): re-activate the memories still
        withdrawn with its reason, supersede the restored ones again (only if
        still active and their former successor is active), clear its rating
        marks and re-pin where the fleet is under the caps. It does not
        un-revoke. ``UnknownAgentCleanup`` for an unknown id,
        ``AgentCleanupRefused`` when ``undo_refusal`` says no."""
        ...

    async def list_agent_cleanups(
        self, limit: int, *, agent: str | None = None
    ) -> list[AgentCleanupRecord]:
        """Recorded cleanups, newest first, at most ``limit`` of them; only
        ``agent``'s when given."""
        ...

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None: ...

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently: an existing record
        comes back unchanged, a new one is ``pending`` (level 0, no fleet).
        A case-only variant of an existing name returns **that** agent
        (ADR 0045)."""
        ...

    async def get_agent(self, name: str) -> Agent | None: ...

    async def list_agents(self) -> list[Agent]: ...

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Atomically flip a pending or revoked agent to ``active`` with a
        trust level and home fleet (ADRs 0012, 0028). ``UnknownAgent`` if
        unknown; ``InvalidAgentStatus`` if already ``active``;
        ``FleetMerged`` if the home fleet was merged (ADR 0107)."""
        ...

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked``. The
        credential is the ``Authenticator``'s; this only moves the status.
        ``UnknownAgent`` if unknown; ``InvalidAgentStatus`` if already
        ``revoked``."""
        ...

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). Demotion to
        level 0 is *dormant* (key still valid, no access) — distinct from
        revocation (ADR 0012). ``UnknownAgent`` if unknown."""
        ...

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet. The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``UnknownAgent`` if unknown;
        ``FleetMerged`` if the fleet was merged (ADR 0107)."""
        ...

    # -- successions (ADR 0113) ------------------------------------------------

    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        """Record that ``retired`` hands every memory it wrote to ``successor``
        and revoke ``retired`` (when active), in one transaction. The
        credential row is the ``Authenticator``'s: the key stops working
        with the status. ``UnknownAgent`` for an unknown name;
        ``InvalidAgentStatus`` when ``retired`` is pending or ``successor`` is
        not active; ``SuccessionRefused`` when ``retired`` already hands down
        or ``successor`` hands down to it, directly or through a chain (a
        cycle). The result's ``was`` is the retired agent's status seen under
        the lock. Nothing changes on a refusal."""
        ...

    async def undo_succession(self, retired: str) -> Succession:
        """Delete the succession of ``retired``: its memories are no longer
        owned by the successor. The agent stays revoked. ``UnknownSuccession``
        when none stands."""
        ...

    async def get_succession(self, retired: str) -> Succession | None: ...

    async def list_successions(self) -> list[Succession]:
        """Every standing succession, newest first."""
        ...

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        """The agents that hand down to ``name``, transitively, sorted: the
        names whose memories ``name`` owns besides its own (what a key check
        puts on ``Credential.inherits_from``). Empty when none."""
        ...

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        """What owning ``authors`` brings: their active ``self`` notes and
        ``fleet`` memories (and how many of those sit outside
        ``home_fleet_id``) and their pins."""
        ...

    # -- audit log (ADR 0027, SPEC §12.5) ------------------------------------

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one admin-action row (insert-only). Failures propagate:
        an audit write is never swallowed (ADR 0027)."""
        ...

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Audit-log rows matching ``filters``, newest first, at most
        ``limit`` of them."""
        ...

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        """Upsert a feedback verdict (one row per entry+user+agent) and
        return the row as stored. A row without ``updated_at`` is stamped
        with the store's clock, the one ``pinned_at`` and ``created_at``
        use. Rating again a memory whose rating an admin cleanup set aside
        clears the mark (ADR 0111): the row counts again."""
        ...

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        """Upsert several feedback rows in one transaction: all land or
        none do. Returns the rows as stored, in order; rows without
        ``updated_at`` share one store-clock stamp (ADR 0079). An empty
        list is a no-op."""
        ...

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        """(helpful, stale, wrong) counts for one entry. Every feedback read
        leaves out revoked reporters' rows (ADR 0067) and set-aside ones
        (ADR 0111)."""
        ...

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        """Batched feedback counts for a set of entries: every requested id
        that is a well-formed UUID is a key, ``(0, 0, 0)`` when it has no
        feedback or names no entry; malformed ids are left out."""
        ...

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        """``quality_counts`` plus each entry's ``last_confirmed_at`` (SPEC
        §4.2), ``newest_flag_at`` (the newest stale or wrong row, noted or not)
        and ``correction`` (the newest stale or wrong report with a
        non-empty note, its note cut by ``truncate_note``; SPEC §6.1), in one
        read: every requested id that is a well-formed UUID is a key (zero
        counts and ``None`` when it has no feedback or names no entry);
        malformed ids are left out. Revoked reporters are excluded as in the
        counts.

        With ``count_supersedes``, ``supersedes`` is how many entries name
        the id in ``superseded_by`` and are readable under ``visibility``
        (``None`` = all, as everywhere else); otherwise it stays 0."""
        ...

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's feedback rows, at most ``limit`` of them: the
        informative ones first (verdict stale or wrong, or a non-empty note),
        then the bare ``helpful`` ones (ADR 0064); within each group newest
        first (``updated_at`` DESC), then reporter. No visibility
        check: the caller has already established the entry is readable."""
        ...

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        """``list_feedback`` for several entries in one read (ADR 0059),
        keyed by every requested id (``[]`` when it has no feedback), each in
        ``list_feedback`` order."""
        ...

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries matching ``filters``, each with the
        entry it is about: newest first (``updated_at`` DESC), then entry
        id, reporter and agent. Only rows whose entry is readable under
        ``visibility`` (``None`` = all, as in ``list_entries``); revoked
        reporters (ADR 0067) and set-aside ratings (ADR 0111) are left out. A malformed ``entry_id`` or
        ``fleet_id`` matches nothing."""
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
        """Active entries with a standing ``stale`` or ``wrong`` verdict
        (ADR 0093), oldest first flag first, then entry id. Revoked
        reporters' rows (ADR 0067) and set-aside ones (ADR 0111) are left out, so an entry only they
        flagged is not listed. ``orphaned`` is judged at ``now``: the
        author is a revoked agent, or wrote nothing in the
        ``ORPHAN_SILENCE_DAYS`` before it (as ``fleet_health`` counts).
        Only entries readable under ``visibility`` (``None`` = all); a
        malformed ``fleet_id`` matches nothing."""
        ...

    async def health_check(self) -> bool:
        """Deep probe: whether the database answers. Must
        never raise (unauthenticated probe endpoints call it).
        """
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
    """Entity extractor (ADR 0016, SPEC §13): entry text in, validated
    facets out. Optional (no endpoint ⇒ off) and best-effort (a failure
    never blocks the write).
    """

    @property
    def model_name(self) -> str:
        """The extractor model identifier (recorded per entry)."""
        ...

    async def extract_entry(self, draft: EntryDraft) -> tuple[ExtractedEntity, ...]:
        """Extract entity facets from the text an entry is written from
        (the summary + bounded body prefix, the same text the embedder
        sees — SPEC §13.1)."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    """Credential check + key management (SPEC.md §8, ADR 0012).

    Each raw key is returned **once** at issuance and only its hash is
    stored, so a leaked database leaks no usable keys (SPEC.md §8.1).
    """

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None`` if unknown.

        An agent key resolves only while its agent is ``active`` (ADR 0039):
        a key of a pending / revoked / missing agent, a pre-v2 ``user`` key
        and a name-less agent key all give ``None``."""
        ...

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to the registered agent name; return
        the raw secret once (never stored). Raises ``UnknownAgent`` if the agent
        is unknown and ``InvalidAgentStatus`` unless it is ``active``
        (ADR 0039); replaces any stale key row, so one live key per agent."""
        ...

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        """Swap an active agent's key for a new one in one transaction
        (ADR 0108): the old key stops authenticating at once. The
        ``agent_key.replace`` audit row (both key fingerprints, never a key)
        is written in that same transaction, under the given actor. Raises
        ``UnknownAgent`` if the agent is unknown and ``InvalidAgentStatus``
        unless it is ``active``. The raw key is returned once."""
        ...

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the name stays reserved, ADR 0012)."""
        ...

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (closes registration, ADR 0031);
        returns the new raw secret once."""
        ...

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        ...


@dataclass(frozen=True, slots=True)
class KeyReplacement:
    """The outcome of ``Authenticator.replace_agent_key`` (ADR 0108): the
    new raw key (shown once) and the non-secret fingerprints of the old
    and the new key, for the audit log. ``old_key_id`` is ``None`` if the
    agent held no key."""

    raw_key: str = field(repr=False)
    new_key_id: str
    old_key_id: str | None


@dataclass(frozen=True, slots=True)
class Credential:
    """A verified caller: who acts, under which agent, with which rights.

    ``user_id`` / ``agent_id`` are the v1 identity (SPEC.md §8.1).
    v2 (ADRs 0011-0012) adds:

    * ``is_org`` — the shared org key (registration + health only).
    * ``trust_level`` / ``home_fleet_id`` — the agent's privilege;
      ``agent_name`` is its server-verified name (the entry ``author``).
    * ``key_id`` — the key's non-secret fingerprint (first 12 hex chars
      of its stored hash, as ``hivemind-keys list`` shows), for the audit
      log (ADR 0027); ``None`` with no stored key (dev mode, fakes).
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
    key_id: str | None = None  # ADR 0027: key fingerprint, never the key
    # ADR 0113: the agents that hand down to this one, transitively. Their
    # memories are this agent's to read, supersede and withdraw, and it cannot
    # rate them; ``author`` on them stays the original name.
    inherits_from: tuple[str, ...] = ()

    @property
    def owned_authors(self) -> tuple[str, ...]:
        """The authors whose memories this caller owns: its own name, then
        every agent that hands down to it (ADR 0113)."""
        return owned_authors(self.agent_name or self.user_id, self.inherits_from)

    def owns(self, author: str) -> bool:
        """Whether a memory by ``author`` is this caller's own."""
        return author == (self.agent_name or self.user_id) or author in self.inherits_from

    def audit_actor(self) -> str:
        """The audit-log ``actor`` (ADR 0027): ``admin:<fingerprint>``
        (all admin keys share ``user_id = "admin"``), ``org:<fingerprint>``
        for an org-key caller (ADR 0046), else the agent's name (a
        contributor's or privileged agent's pin, ADR 0096)."""
        if not (self.is_org or self.is_admin):
            return self.agent_name or self.user_id
        if self.key_id is None:
            return self.user_id
        return f"{'org' if self.is_org else 'admin'}:{self.key_id}"

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
            inherited=self.inherits_from,
        )

    def max_write_scope(self) -> str:
        """The highest scope this credential may write (ADR 0011): level
        2/3 (contributor/privileged) may write ``self`` or ``fleet``;
        level 1 (lurker) only ``self``; level 0 (untrusted) may not write
        at all (enforced at the write seam)."""
        if self.is_admin or not self.access_controlled:
            return SCOPE_ORG
        # ADR 0011 ladder, single-sourced in the domain: the level's
        # highest writable scope (level 0 keeps `self`; it writes nothing,
        # gated by `can_write`).
        scopes = write_scopes_for(self.trust_level)
        return scopes[-1] if scopes else SCOPE_SELF

    def can_write(self) -> bool:
        """Whether this credential may write at all. Level 0
        (untrusted) may not; the admin key always may."""
        if self.is_admin or not self.access_controlled:
            return True
        return self.trust_level > TrustLevel.UNTRUSTED

    @property
    def is_stored(self) -> bool:
        """Whether this credential was issued by the authenticator (ADR
        0082): an admin key, an org key, or an active agent's key. Anything
        else is a *dev-mode* credential — a hand-built identity such as the
        dev runner's fixed ``dev``."""
        return self.is_admin or self.is_org or self.agent_name is not None

    def effective(self, require_stored_keys: bool = False) -> Credential:
        """This credential, or — when the deployment requires stored keys
        (``HIVEMIND_REQUIRE_STORED_KEYS``, ADR 0082) and this one is
        dev-mode — its trust-level-0 stand-in: access-controlled,
        untrusted, same ``user_id``. Stored credentials are returned
        unchanged (the same object)."""
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
    """The text an entry is embedded from (SPEC.md §7) at the default
    budget (ADR 0021), for embedders without ``Settings``; otherwise use
    the embedder's own ``entry_embeddable_text``.
    """
    return embeddable_text(draft.summary, draft.body)
