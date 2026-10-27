"""Simulated feedback over time: halving confirmed flags (ADR 0106).

``make measure-flags`` (``uv run python -m tests.eval.flag_dynamics``). No
store, no embedder, not part of pytest: about 30 seconds of pure arithmetic
through the shipped ``feedback_quality`` and ``entry_score``.

Three rivals answer one question about equally well. Each day one agent
searches: its query puts the rivals in a random rank order, it uses the top
hit (or, with ``explore``, a random other hit), succeeds with that entry's
true success rate and rates it half the time. Every helpful carries a note,
as in production, so a helpful newer than every flag confirms the entry. A
success is falsely flagged 3% of the time; a failure is rated helpful with
the ``herding`` share, otherwise 60% stale and 40% wrong. 60 days, 2,000 runs
per cell, the same seed for both settings.

Each cell prints the best entry's share of uses and the share of uses that
failed. The figures show direction and rough size, not a forecast: real
agents open several hits and read correction notes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.retrieval.scoring import entry_score, feedback_quality
from tests.fakes import FIXED_NOW

DAYS = 60
RUNS = 2000
RATE = 0.5
FALSE_FLAG = 0.03


@dataclass
class Rival:
    p: float  # true success rate
    helpful: int = 0
    stale: int = 0
    wrong: int = 0
    last_helpful: int = -2  # day of the newest helpful (noted), -2 none
    last_flag: int = -2  # day of the newest stale or wrong, -2 none; -1 before day 0

    def quality(self, config: SearchConfig) -> float:
        confirmed = self.last_helpful > self.last_flag
        kwargs = config.quality_kwargs(confirmed=confirmed)
        return feedback_quality(self.helpful, self.stale, self.wrong, **kwargs)


def _run(
    start: list[Rival], config: SearchConfig, rng: random.Random, explore: float, herding: float
) -> tuple[float, float]:
    rivals = [replace(r) for r in start]
    best = max(range(len(rivals)), key=lambda i: rivals[i].p)
    used_best = failed = 0
    for day in range(DAYS):
        order = list(range(len(rivals)))
        rng.shuffle(order)
        ranked = sorted(
            (
                (
                    entry_score(
                        1.0 / (config.rrf_k + rank),
                        3,
                        FIXED_NOW,
                        rivals[i].quality(config),
                        FIXED_NOW + timedelta(days=day),
                        config.half_life_days,
                        recency_floor=config.recency_floor,
                        social_max=config.social_max,
                        narrow_importance=config.narrow_importance,
                    ),
                    i,
                )
                for rank, i in enumerate(order, 1)
            ),
            reverse=True,
        )
        pick = ranked[0][1] if rng.random() >= explore else rng.choice(ranked[1:])[1]
        used_best += pick == best
        rival = rivals[pick]
        ok = rng.random() < rival.p
        failed += not ok
        if rng.random() >= RATE:
            continue
        helpful = rng.random() >= FALSE_FLAG if ok else rng.random() < herding
        if helpful:
            rival.helpful += 1
            rival.last_helpful = day
        elif ok or rng.random() < 0.6:
            rival.stale += 1
            rival.last_flag = day
        else:
            rival.wrong += 1
            rival.last_flag = day
    return used_best / DAYS, failed / DAYS


def _cell(start: list[Rival], config: SearchConfig, explore: float, herding: float) -> str:
    rng = random.Random(11)
    best = failed = 0.0
    for _ in range(RUNS):
        b, f = _run(start, config, rng, explore, herding)
        best += b
        failed += f
    return f"best {best / RUNS:5.1%} failed {failed / RUNS:5.1%}"


SCENARIOS: tuple[tuple[str, list[Rival]], ...] = (
    (
        "false wrong on the best (p .9) vs .7, .7",
        [Rival(0.9, wrong=1, last_flag=-1), Rival(0.7), Rival(0.7)],
    ),
    (
        "false stale on the best (p .9) vs .7, .7",
        [Rival(0.9, stale=1, last_flag=-1), Rival(0.7), Rival(0.7)],
    ),
    (
        "true wrong on a bad entry (p .3) vs .7, .7",
        [Rival(0.3, wrong=1, last_flag=-1), Rival(0.7), Rival(0.7)],
    ),
    ("control, no flags: .9 vs .7, .7", [Rival(0.9), Rival(0.7), Rival(0.7)]),
)


def main() -> None:
    on = SearchConfig()
    off = replace(on, confirmation_halves_flags=False)
    for title, start in SCENARIOS:
        print(title)
        for explore in (0.0, 0.2):
            for herding in (0.1, 0.3):
                print(
                    f"  explore {explore:.0%} herding {herding:.0%}:"
                    f"  full flags [{_cell(start, off, explore, herding)}]"
                    f"  halved [{_cell(start, on, explore, herding)}]"
                )


if __name__ == "__main__":
    main()
