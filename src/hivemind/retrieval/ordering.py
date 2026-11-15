"""Display order of scored search candidates (SPEC.md §6.3): the supersession invariant, version
groups (ADR 0074) and the explore re-order (ADR 0078). Pure: order only, nothing scored, dropped or
read from the store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hivemind.domain.access import SCOPE_ORG
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import NO_SIGNALS, FeedbackSignals
from hivemind.retrieval.mmr import mmr_order


def apply_supersession_invariant(
    entries: list[Entry],
    scores: dict[str, float],
    *,
    version_groups: Sequence[Sequence[str]] = (),
) -> list[Entry]:
    """Within a supersession chain the newest entry outranks its predecessors (SPEC.md §6.3).

    Groups by chain head, best score first, newest ``created_at`` first within a group; ties keep
    the order of ``entries`` (stable sorts). ``version_groups`` (ADR 0074; ids in display order)
    place their chains together at the best member's position. Every entry keeps a slot.
    """
    head_of = chain_heads(entries)
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(head_of[entry.id], []).append(entry)
    for members in groups.values():
        members.sort(key=lambda e: e.created_at, reverse=True)

    # Chains joined by a version group, keyed by the first-seen head.
    merged: dict[str, list[str]] = {}
    merged_into: dict[str, str] = {}
    for version_group in version_groups:
        group_heads = [
            h
            for h in dict.fromkeys(head_of[i] for i in version_group if i in head_of)
            if h not in merged_into
        ]
        if len(group_heads) < 2:
            continue
        # Present at the earliest position any of its chains had.
        first = next(h for h in groups if h in group_heads)
        merged[first] = group_heads
        merged_into.update(dict.fromkeys(group_heads, first))

    scored_groups: list[_ScoredGroup] = []
    for head, members in groups.items():
        if head in merged_into and merged_into[head] != head:
            continue  # emitted with the group's first chain
        if head in merged:
            members = [m for h in merged[head] for m in groups[h]]
        head_score = max(scores[m.id] for m in members)
        scored_groups.append(_ScoredGroup(key=head_score, members=members))
    scored_groups.sort(key=lambda g: g.key, reverse=True)

    result: list[Entry] = []
    for group in scored_groups:
        result.extend(group.members)
    return result


@dataclass
class _ScoredGroup:
    key: float
    members: list[Entry] = field(default_factory=list)


MAX_GROUP_SIZE = 3  # a pair or a triple, never a cluster


def _group_key(entry: Entry) -> str | None:
    """Grouping key (ADR 0074): one fleet, or org-scoped; ``None`` groups with nobody."""
    if entry.scope == SCOPE_ORG:
        return "org"
    return f"fleet:{entry.fleet_id}" if entry.fleet_id else None


def group_versions(
    ranked: list[Entry],
    similarity: Mapping[tuple[str, str], float],
    threshold: float,
    feedback: Mapping[str, FeedbackSignals],
) -> list[list[Entry]]:
    """Group unlinked near-copies among ``ranked`` (active, best first), ADR 0074.

    The best ungrouped candidate anchors a group; later ones join at similarity >= ``threshold``
    within ``MAX_GROUP_SIZE`` and one ``_group_key``. Groups of two or more are returned, newest
    first by the later of ``occurred_at`` and ``last_confirmed_at``, flagged members (stale, wrong,
    contested) never before an unflagged one. Pure: no score changes, nothing hidden.
    """

    def newest(entry: Entry) -> datetime:
        confirmed = feedback.get(entry.id, NO_SIGNALS).last_confirmed_at
        return max(entry.occurred_at, confirmed) if confirmed else entry.occurred_at

    def flagged(entry: Entry) -> bool:
        _, stale, wrong = feedback.get(entry.id, NO_SIGNALS).counts
        return stale + wrong > 0

    taken: set[str] = set()
    groups: list[list[Entry]] = []
    for position, anchor in enumerate(ranked):
        key = _group_key(anchor)
        if anchor.id in taken or key is None:
            continue
        members = [anchor]
        for other in ranked[position + 1 :]:
            if len(members) >= MAX_GROUP_SIZE:
                break
            if other.id in taken or _group_key(other) != key:
                continue
            pair = (anchor.id, other.id) if anchor.id < other.id else (other.id, anchor.id)
            if similarity.get(pair, -1.0) >= threshold:
                members.append(other)
        if len(members) < 2:
            continue
        taken.update(m.id for m in members)
        members.sort(key=newest, reverse=True)
        members.sort(key=flagged)  # stable: unflagged first, newest first within each
        groups.append(members)
    return groups


def explore_reorder(
    ordered: list[Entry],
    scores: dict[str, float],
    similarity: Mapping[tuple[str, str], float],
    lam: float,
    version_groups: Sequence[Sequence[str]] = (),
) -> list[Entry]:
    """MMR re-order of ``apply_supersession_invariant``'s output (ADR 0078): order only, nothing
    dropped or re-scored.

    A unit is a supersession chain joined with its version group's chains (ADR 0074); its relevance
    is its best score, so ``lam == 1`` reproduces ``ordered``. Each unit's members go back into the
    unit's slots in their old order, so the supersession invariant holds (SPEC §6.3).
    """
    heads = chain_heads(ordered)
    unit_of = dict(heads)
    for group in version_groups:
        group_heads = list(dict.fromkeys(heads[i] for i in group if i in heads))
        for h in group_heads[1:]:
            for k, v in unit_of.items():
                if v == h:
                    unit_of[k] = group_heads[0]
    best: dict[str, float] = {}
    for entry in ordered:
        unit = unit_of[entry.id]
        best[unit] = max(best.get(unit, scores[entry.id]), scores[entry.id])
    relevance = {e.id: best[unit_of[e.id]] for e in ordered}
    by_id = {e.id: e for e in ordered}
    picked = [by_id[i] for i in mmr_order([e.id for e in ordered], relevance, similarity, lam)]
    slots: dict[str, list[int]] = {}
    for position, entry in enumerate(picked):
        slots.setdefault(unit_of[entry.id], []).append(position)
    members: dict[str, list[Entry]] = {}
    for entry in ordered:
        members.setdefault(unit_of[entry.id], []).append(entry)
    result = list(picked)
    for unit, positions in slots.items():
        for position, member in zip(positions, members[unit], strict=True):
            result[position] = member
    return result


def chain_heads(entries: list[Entry]) -> dict[str, str]:
    """The chain head of each candidate id: follow ``superseded_by`` until the link leaves the
    set."""
    # The first one, should an id repeat.
    by_id: dict[str, Entry] = {}
    for entry in entries:
        by_id.setdefault(entry.id, entry)

    def chain_head(entry: Entry) -> str:
        head = entry
        guard = 0
        while head.superseded_by in by_id:
            head = by_id[head.superseded_by]
            guard += 1
            if guard > 1000:  # defensive: chains are short in practice
                break
        return head.id

    return {entry.id: chain_head(entry) for entry in entries}
