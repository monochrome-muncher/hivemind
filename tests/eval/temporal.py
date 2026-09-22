"""The age-varied eval fixture (ROADMAP §4.4: is the recency term worth gating?).

The golden set (``tests/eval/golden.py``) cannot answer §4.4. Every golden
entry is seeded with no ``occurred_at``, so all eight share one timestamp,
the recency factor ``0.5 ** (age_days / half_life_days)`` is *identical*
for every candidate, and it cancels out of the ranking entirely. A fixture
that measures decay has to vary age, so this one does — and, separately,
labels each query with whether it carries **time sense** of its own.

Two axes, deliberately crossed:

* **corpus age** — every entry declares an explicit ``age_days`` (days
  before the eval's fixed "now"), which the runner turns into
  ``EntryDraft.occurred_at``. Ages span 2 to 500 days, i.e. from well inside
  one 30-day half-life to well past sixteen of them.
* **query time sense** — ``temporal=True`` means the query itself asks for
  the *current* state ("current", "latest"); ``temporal=False`` means it
  asks a timeless question, where the best answer is whichever entry says
  the right thing, however old it is.

The corpus is built around two competition patterns, because those are
what a recency multiplier actually arbitrates:

1. **old-exact vs. recent-loose** (entries 0/1 and 2/3) — an old entry
   that matches the query almost word for word, competing with a recent
   entry that merely shares a phrase. The hypothesis §4.4 states. It is
   present for a **non-temporal** query (``NT1``) *and* for a **temporal**
   one (``T1``), because the failure mode is not obviously confined to
   queries without time sense.
2. **superseded-in-fact currency pairs** (4/5, 6/7, 8/9) — two independent
   entries stating an *old* and a *current* value of the same setting, not
   linked by supersession (so SPEC §6.3's invariant is not what is being
   measured). In every pair the OLD entry has the **higher** lexical
   overlap with the query, so the fixture is adversarial to recency: a
   variant only gets these right if the recency term does real work.

Nothing here sets ``importance`` or feedback, so the importance factor
(0.8) and the quality factor (1.0) are constant across every candidate and
cancel. Age is the only thing that varies between variants.
"""

from __future__ import annotations

from dataclasses import dataclass

# The eval's k. Matches the §1.1 gate so the two reports read alike.
TEMPORAL_K = 5


@dataclass(frozen=True, slots=True)
class AgedEntry:
    """One corpus entry with an explicit age (days before the eval's "now")."""

    kind: str
    summary: str
    tags: tuple[str, ...]
    age_days: float


@dataclass(frozen=True, slots=True)
class LabelledQuery:
    """One eval query: its expected-relevant entries + two orthogonal labels.

    ``temporal`` is the *query's* property: does the question ask for the
    current state of something, or is it timeless? This is the axis §4.4
    proposes gating on.

    ``pattern`` is the *corpus competition* the query lands in —
    ``old_exact`` (the right answer is an old near-verbatim match, up
    against a recent entry that merely shares a phrase), ``currency_pair``
    (the right answer is the newer of two entries stating the same setting,
    and the stale one has the higher lexical overlap), or ``timeless`` (no
    engineered age competition; the relevant entry's age is simply whatever
    the corpus gives it). The two labels are deliberately **crossed**: the
    ``old_exact`` pattern appears under both a temporal and a non-temporal
    query, which is what makes it possible to tell whether the query's time
    sense is really the axis that predicts where recency helps.
    """

    query: str
    relevant: tuple[int, ...]
    temporal: bool
    pattern: str
    note: str


# --- corpus -----------------------------------------------------------------
# Index order is the seed order; ``age_days`` is subtracted from the eval's
# fixed "now" to produce ``occurred_at``.
TEMPORAL_CORPUS: list[AgedEntry] = [
    # 0/1 — old-exact vs. recent-loose, exercised by the NON-temporal NT1.
    AgedEntry(
        "fact",
        "Token bucket rate limiter allows 100 requests per minute per client key",
        ("rate-limit", "api"),
        420.0,
    ),
    AgedEntry(
        "fact",
        "Rate limiter dashboard panel was added to the ops Grafana board",
        ("ops", "grafana"),
        3.0,
    ),
    # 2/3 — old-exact vs. recent-loose, exercised by the TEMPORAL T1. Nothing
    # newer than entry 2 exists on its topic, so "our current approach" is
    # still entry 2 even though the query carries time sense.
    AgedEntry(
        "decision",
        "We chose Reciprocal Rank Fusion to merge the keyword and vector streams",
        ("retrieval", "rrf"),
        380.0,
    ),
    AgedEntry(
        "insight",
        "Fusion of telemetry streams in the billing pipeline doubled ingest latency",
        ("billing", "telemetry"),
        6.0,
    ),
    # 4/5, 6/7, 8/9 — currency pairs. The OLD member of each pair outranks the
    # new one on lexical overlap with the query, on purpose.
    AgedEntry(
        "fact",
        "PgBouncer transaction pool size limit is 20 connections per database",
        ("postgres", "pgbouncer"),
        400.0,
    ),
    AgedEntry(
        "fact",
        "PgBouncer pool size raised to 90 connections",
        ("postgres", "pgbouncer"),
        5.0,
    ),
    AgedEntry(
        "decision",
        "Deploy canary holds at 10 percent for 30 minutes before full rollout across every region",
        ("deploy", "canary"),
        300.0,
    ),
    AgedEntry(
        "decision",
        "Deploy canary now holds at 25 percent for 15 minutes before full rollout",
        ("deploy", "canary"),
        8.0,
    ),
    AgedEntry(
        "fact",
        "JWT access token lifetime is 60 minutes and the refresh token lasts 30 days",
        ("auth", "jwt"),
        340.0,
    ),
    AgedEntry(
        "fact",
        "JWT access token lifetime shortened to 15 minutes",
        ("auth", "jwt"),
        12.0,
    ),
    # 10..15 — timeless entries whose ages are spread across the range, so the
    # non-temporal query set is not stacked at one end of it (relevant ages:
    # 4, 45, 90, 150, 420, 500 days).
    AgedEntry(
        "insight",
        "A 1024 dimension embedding beats 512 on semantic recall for long form entries",
        ("embeddings", "search"),
        4.0,
    ),
    AgedEntry(
        "fact",
        "MCP hive_search returns compact hits without bodies so agents scan many and open few",
        ("mcp", "tools"),
        90.0,
    ),
    AgedEntry(
        "fact",
        "Postgres advisory lock guards the migration runner against concurrent replicas",
        ("migrations", "postgres"),
        45.0,
    ),
    AgedEntry(
        "insight",
        "Recency decay makes entries lose retrieval weight on a 30 day half life",
        ("retrieval", "decay"),
        500.0,
    ),
    AgedEntry(
        "fact",
        "Retrieval latency budget for the search endpoint is 200 milliseconds",
        ("retrieval", "perf"),
        2.0,
    ),
    AgedEntry(
        "fact",
        "Nightly backup runs at 02:00 UTC and is verified by a restore drill each Friday",
        ("ops", "backup"),
        150.0,
    ),
]

# --- queries ----------------------------------------------------------------
TEMPORAL_QUERIES: list[LabelledQuery] = [
    LabelledQuery(
        "token bucket rate limiter allows 100 requests per minute per client key",
        (0,),
        temporal=False,
        pattern="old_exact",
        note="old-exact (420d) vs recent-loose (3d); the §4.4 hypothesis",
    ),
    LabelledQuery(
        "which embedding dimension gives better semantic recall for long form entries",
        (10,),
        temporal=False,
        pattern="timeless",
        note="timeless; relevant entry is recent (4d)",
    ),
    LabelledQuery(
        "mcp hive_search compact hits without bodies for agents to scan",
        (11,),
        temporal=False,
        pattern="timeless",
        note="timeless; relevant entry is mid-aged (90d)",
    ),
    LabelledQuery(
        "how does the migration runner guard against concurrent replicas advisory lock",
        (12,),
        temporal=False,
        pattern="timeless",
        note="timeless; relevant entry is recent-ish (45d)",
    ),
    LabelledQuery(
        "recency decay half life retrieval weight of old entries",
        (13,),
        temporal=False,
        pattern="timeless",
        note="timeless; relevant entry is the oldest in the corpus (500d)",
    ),
    LabelledQuery(
        "nightly backup restore drill verification",
        (15,),
        temporal=False,
        pattern="timeless",
        note="timeless; relevant entry is mid-aged (150d)",
    ),
    LabelledQuery(
        "what is our current approach to merge keyword and vector streams fusion",
        (2,),
        temporal=True,
        pattern="old_exact",
        note="temporal wording, but the right answer is the old (380d) exact match",
    ),
    LabelledQuery(
        "current pgbouncer transaction pool size limit per database connections",
        (5,),
        temporal=True,
        pattern="currency_pair",
        note="currency pair 4/5; the stale entry has the higher lexical overlap",
    ),
    LabelledQuery(
        "latest deploy canary percent hold duration before full rollout across every region",
        (7,),
        temporal=True,
        pattern="currency_pair",
        note="currency pair 6/7; the stale entry has the higher lexical overlap",
    ),
    LabelledQuery(
        "current jwt access token lifetime minutes and refresh token days",
        (9,),
        temporal=True,
        pattern="currency_pair",
        note="currency pair 8/9; the stale entry has the higher lexical overlap",
    ),
]


def temporal_corpus() -> list[AgedEntry]:
    """The age-varied corpus, in seed order."""
    return list(TEMPORAL_CORPUS)


def temporal_queries() -> list[LabelledQuery]:
    """The labelled query set (temporal and non-temporal)."""
    return list(TEMPORAL_QUERIES)
