"""``apply_supersession_invariant`` finds chain links by dict lookup. This
pins it to the earlier linear-scan version on random candidate sets:
chains, forks, links leaving the set, cycles and score ties included."""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from hivemind.domain.entry import Entry, Kind
from hivemind.services.search import apply_supersession_invariant

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _Group:
    key: float
    members: list[Entry] = field(default_factory=list)


def _reference(entries: list[Entry], scores: dict[str, float]) -> list[Entry]:
    """The implementation as it was before the dict lookup (verbatim logic)."""
    id_set = {e.id for e in entries}

    def chain_head(entry: Entry) -> str:
        head = entry
        guard = 0
        while head.superseded_by in id_set:
            head = next((e for e in entries if e.id == head.superseded_by), head)
            guard += 1
            if guard > 1000:
                break
        return head.id

    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(chain_head(entry), []).append(entry)
    scored: list[_Group] = []
    for members in groups.values():
        members.sort(key=lambda e: e.created_at, reverse=True)
        scored.append(_Group(max(scores[m.id] for m in members), members))
    scored.sort(key=lambda g: g.key, reverse=True)
    return [m for g in scored for m in g.members]


def _entry(i: int, created: int) -> Entry:
    return Entry(
        id=f"e{i}",
        kind=Kind.FACT,
        summary=f"entry {i}",
        author="a",
        agent="a",
        occurred_at=T0,
        created_at=T0 + timedelta(minutes=created),
    )


@pytest.mark.parametrize("seed", range(300))
def test_matches_the_linear_scan_version(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(0, 25)
    entries = [_entry(i, rng.randint(0, 8)) for i in range(n)]
    for idx, entry in enumerate(entries):
        roll = rng.random()
        if roll < 0.5 and n:
            target = f"e{rng.randrange(n)}"  # in the set; may form forks or cycles
        elif roll < 0.6:
            target = "outside"  # a successor that is not a candidate
        else:
            continue
        entries[idx] = replace(entry, superseded_by=target)
    rng.shuffle(entries)
    scores = {e.id: float(rng.randint(0, 4)) for e in entries}  # many ties
    expected = [e.id for e in _reference(list(entries), scores)]
    assert [e.id for e in apply_supersession_invariant(list(entries), scores)] == expected
