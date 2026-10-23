"""Measure Hivemind's search on LongMemEval_S, retrieval only.

LongMemEval (Wu et al., ICLR 2025) is a long-term chat memory benchmark: one
user's long chat history, 500 questions about it (single- and multi-session
recall, preferences, temporal reasoning, knowledge updates, and abstention
questions whose answer is not in the history). The "S" setting gives each
question a haystack of about 50 sessions (~115k tokens). The file is the
cleaned release ``xiaowu0162/longmemeval-cleaned``; set
``EVAL_LONGMEMEVAL_PATH`` to a local copy, or it is downloaded once into
``.cache/longmemeval/``.

This measures **retrieval only**: no answering LLM and no judge. Its numbers
are session-level recall of the evidence sessions, so they are NOT comparable
to the published end-to-end QA accuracy of memory systems (e.g. 92.2%).

Hivemind never records conversations itself, so the setup here is a stand-in
for agent-written entries: each session becomes one entry (kind ``fact``)
whose summary is the session's first user turn (whitespace-collapsed, cut to
the 280-character summary limit) and whose body is the transcript, cut to
``MAX_BODY_CHARS``. The memory date (``occurred_at``) is the session's date.
Each question gets a fresh ``MemoryStore`` holding only its own haystack and
is searched, at the question's date, with ``limit=10``. ``MemoryStore``'s
keyword stream approximates Postgres full-text search, so the keyword side
here is not Postgres's.

Variants, all run on the same seeded store per question:

* ``shipped``: ``SearchConfig()`` defaults (query instruction and the 0.45
  similarity cut-off on), recency hint ``default``;
* ``ignore_age``: shipped, recency hint ``ignore_age`` (what agents are
  steered to);
* ``no_cutoff``: shipped without the similarity cut-off;
* ``keyword_only``: shipped with ``weight_vector=0``.

Metrics, excluding abstention questions (ids ending ``_abs``) as the paper
does: recall_any@5/@10 (at least one evidence session in the top k),
recall_all@5/@10 (every evidence session in the top k) and nDCG@10 (binary
relevance), macro-averaged per question type and overall, plus the number of
searches that returned nothing.

Environment: ``EVAL_LONGMEMEVAL_PER_TYPE=N`` takes the first N non-abstention
questions of each type in the order of ``sha256(question_id)`` (spread out,
reproducible; unset is all); ``EVAL_EMBED_CACHE=dir`` caches vectors on disk
(``tests/eval/embed_cache.py``) so a killed run resumes and variants and runs
share embeddings; ``EVAL_LONGMEMEVAL_JSON=path`` also writes the rows as JSON.
Progress goes to stderr.

Run it against the deployment's embedder (the ``HIVEMIND_EMBEDDING_*``
settings, e.g. ``make vllm`` locally): ``make measure-longmemeval``.
On a CPU the full set takes days; ``scripts/longmemeval-gpu/`` computes the
embeddings on a GPU (a Kaggle notebook) and then runs this harness.
``tests/eval/test_longmemeval_measurement.py`` runs it on the hash embedder,
which checks the machinery only; its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hivemind.config import SearchConfig
from hivemind.domain.entry import _SUMMARY_MAX_CHARS, EntryDraft, Kind
from hivemind.domain.recency import RecencyHint
from hivemind.domain.validation import MAX_BODY_CHARS
from hivemind.memstore import MemoryStore
from hivemind.ports import Embedder
from hivemind.retrieval.eval import ndcg_at_k
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.fakes import make_clock

URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
    "longmemeval_s_cleaned.json"
)
DEFAULT_PATH = Path(".cache/longmemeval/longmemeval_s_cleaned.json")
PATH_ENV = "EVAL_LONGMEMEVAL_PATH"
PER_TYPE_ENV = "EVAL_LONGMEMEVAL_PER_TYPE"
CACHE_ENV = "EVAL_EMBED_CACHE"
JSON_ENV = "EVAL_LONGMEMEVAL_JSON"
LIMIT = 10
SUMMARY_MAX_CHARS = _SUMMARY_MAX_CHARS
METRICS = (
    "recall_any_5",
    "recall_all_5",
    "recall_any_10",
    "recall_all_10",
    "ndcg_10",
)

_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2}) \(\w+\) (\d{2}):(\d{2})")

Question = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Variant:
    """A search setting: a config change and a recency hint."""

    name: str
    recency: RecencyHint = RecencyHint.DEFAULT
    vector_min_similarity: bool = True  # False: no cut-off
    weight_vector: float | None = None  # None: the shipped weight


VARIANTS: tuple[Variant, ...] = (
    Variant("shipped"),
    Variant("ignore_age", recency=RecencyHint.IGNORE_AGE),
    Variant("no_cutoff", vector_min_similarity=False),
    Variant("keyword_only", weight_vector=0.0),
)


def variant_config(variant: Variant) -> SearchConfig:
    config = replace(SearchConfig(), default_limit=LIMIT)
    if not variant.vector_min_similarity:
        config = replace(config, vector_min_similarity=None)
    if variant.weight_vector is not None:
        config = replace(config, weight_vector=variant.weight_vector)
    return config


@dataclass(frozen=True, slots=True)
class Row:
    """One question under one variant."""

    variant: str
    question_id: str
    question_type: str
    hits: int
    recall_any_5: float
    recall_all_5: float
    recall_any_10: float
    recall_all_10: float
    ndcg_10: float


@dataclass(frozen=True, slots=True)
class Summary:
    """Rows of one variant and question type (``"overall"`` for all) averaged."""

    variant: str
    question_type: str
    questions: int
    empty: int
    recall_any_5: float
    recall_all_5: float
    recall_any_10: float
    recall_all_10: float
    ndcg_10: float


# -- dataset ------------------------------------------------------------------


def dataset_path(env: Mapping[str, str] | None = None) -> Path:
    """The dataset file: ``EVAL_LONGMEMEVAL_PATH``, else the cached download
    (fetched once)."""
    env = os.environ if env is None else env
    if given := env.get(PATH_ENV):
        return Path(given)
    if not DEFAULT_PATH.exists():
        DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {URL} -> {DEFAULT_PATH}", file=sys.stderr)
        partial = DEFAULT_PATH.with_suffix(".part")
        with urllib.request.urlopen(URL) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        partial.replace(DEFAULT_PATH)
    return DEFAULT_PATH


def load_questions(path: Path) -> list[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of questions")
    return data


def is_abstention(question: Question) -> bool:
    return str(question["question_id"]).endswith("_abs")


def parse_date(text: str) -> datetime:
    """``2023/05/30 (Tue) 23:40`` as a UTC datetime."""
    match = _DATE_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"unrecognised LongMemEval date: {text!r}")
    year, month, day, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def per_type_limit(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    value = int(raw)
    if value < 1:
        raise ValueError(f"{PER_TYPE_ENV} must be >= 1, got {value}")
    return value


def select(questions: Sequence[Question], per_type: int | None = None) -> list[Question]:
    """The non-abstention questions; with ``per_type``, the first N of each
    question type by ``sha256(question_id)``. Grouped by type (sorted), each
    group in hash order, so the sample is spread out and reproducible."""
    by_type: dict[str, list[Question]] = defaultdict(list)
    for question in questions:
        if not is_abstention(question):
            by_type[question["question_type"]].append(question)
    out: list[Question] = []
    for qtype in sorted(by_type):
        group = sorted(
            by_type[qtype],
            key=lambda q: hashlib.sha256(str(q["question_id"]).encode()).hexdigest(),
        )
        out += group if per_type is None else group[:per_type]
    return out


# -- sessions as entries ------------------------------------------------------


def session_summary(session: Sequence[Mapping[str, Any]]) -> str:
    """The first user turn (the first turn if none is the user's), whitespace
    collapsed and cut to the summary limit."""
    turns = [t for t in session if str(t.get("content", "")).strip()]
    first = next((t for t in turns if t.get("role") == "user"), turns[0] if turns else None)
    text = " ".join(str(first["content"]).split()) if first else ""
    return text[:SUMMARY_MAX_CHARS].rstrip() or "(empty session)"


def session_body(session: Sequence[Mapping[str, Any]]) -> str | None:
    """The transcript as ``role: content`` lines, cut to ``MAX_BODY_CHARS``."""
    text = "\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}" for t in session)
    return text[:MAX_BODY_CHARS].strip() or None


def session_draft(session: Sequence[Mapping[str, Any]], date: datetime) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=session_summary(session),
        body=session_body(session),
        author="eval",
        agent="eval",
        occurred_at=date,
    )


async def seed(question: Question, embedder: Embedder) -> tuple[MemoryStore, dict[str, str]]:
    """A fresh store with the question's haystack, one entry per session, and
    the map entry id -> session id."""
    store = MemoryStore(make_clock())
    writer = WriteService(store, embedder)
    sessions = question["haystack_sessions"]
    dates = question["haystack_dates"]
    session_ids = question["haystack_session_ids"]
    by_entry: dict[str, str] = {}
    for session, date, session_id in zip(sessions, dates, session_ids, strict=True):
        entry = await writer.write(session_draft(session, parse_date(date)))
        by_entry[entry.id] = session_id
    return store, by_entry


# -- metrics ------------------------------------------------------------------


def score(ranked: Sequence[str], answers: Sequence[str]) -> dict[str, float]:
    """Session-level metrics of one ranking of session ids against the answer
    sessions (``ranked`` has no repeats; a session appears once)."""
    relevant = set(answers)
    out: dict[str, float] = {}
    for k in (5, 10):
        top = set(ranked[:k])
        out[f"recall_any_{k}"] = float(bool(relevant & top))
        out[f"recall_all_{k}"] = float(relevant <= top)
    out["ndcg_10"] = ndcg_at_k(list(ranked), dict.fromkeys(relevant, 1), 10)
    return out


# -- measurement --------------------------------------------------------------


async def measure_question(
    question: Question, embedder: Embedder, variants: Sequence[Variant] = VARIANTS
) -> list[Row]:
    """The question's haystack seeded once, then searched under each variant."""
    store, by_entry = await seed(question, embedder)
    asked_at = parse_date(question["question_date"])
    answers = list(question["answer_session_ids"])
    rows = []
    for variant in variants:
        service = SearchService(store, embedder, variant_config(variant), now_fn=lambda: asked_at)
        hits = await service.search(question["question"], limit=LIMIT, recency=variant.recency)
        ranked = list(dict.fromkeys(by_entry[h.entry_id] for h in hits))
        rows.append(
            Row(
                variant.name,
                str(question["question_id"]),
                str(question["question_type"]),
                len(hits),
                **score(ranked, answers),
            )
        )
    return rows


async def measure(
    embedder: Embedder,
    questions: Sequence[Question],
    variants: Sequence[Variant] = VARIANTS,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[Row]:
    """Every selected question under every variant. ``questions`` is used as
    given (abstention questions are dropped; use ``select`` to sample)."""
    chosen = [q for q in questions if not is_abstention(q)]
    started = time.monotonic()
    rows: list[Row] = []
    for i, question in enumerate(chosen, start=1):
        rows += await measure_question(question, embedder, variants)
        if progress:
            progress(f"question {i}/{len(chosen)} ({time.monotonic() - started:.0f}s elapsed)")
    return rows


def summarize(rows: Sequence[Row]) -> list[Summary]:
    """Per variant (in first-seen order): each question type, then overall."""
    variants = list(dict.fromkeys(r.variant for r in rows))
    out = []
    for variant in variants:
        mine = [r for r in rows if r.variant == variant]
        groups = {
            t: [r for r in mine if r.question_type == t]
            for t in sorted({r.question_type for r in mine})
        }
        for name, group in [*groups.items(), ("overall", mine)]:
            n = len(group)
            out.append(
                Summary(
                    variant,
                    name,
                    n,
                    sum(1 for r in group if r.hits == 0),
                    *(sum(getattr(r, m) for r in group) / n for m in METRICS),
                )
            )
    return out


def report(
    rows: Sequence[Row],
    model: str | None = None,
    settings: str = "",
) -> str:
    """The measurement as Markdown: one table per variant."""
    summaries = summarize(rows)
    questions = len({r.question_id for r in rows})
    lines = [
        f"Model: `{model or 'unknown'}`. Questions: {questions} (abstention excluded). "
        f"Sample: {settings or 'all'}. Session-level, limit {LIMIT}; "
        "macro average over questions.",
    ]
    for variant in dict.fromkeys(s.variant for s in summaries):
        lines += [
            "",
            f"**{variant}**",
            "",
            "| question type | n | recall_any@5 | recall_all@5 | recall_any@10 "
            "| recall_all@10 | nDCG@10 | empty |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in (x for x in summaries if x.variant == variant):
            lines.append(
                f"| {s.question_type} | {s.questions} | {s.recall_any_5:.3f} "
                f"| {s.recall_all_5:.3f} | {s.recall_any_10:.3f} | {s.recall_all_10:.3f} "
                f"| {s.ndcg_10:.3f} | {s.empty} |"
            )
    return "\n".join(lines)


def to_json(rows: Sequence[Row], model: str | None = None, settings: str = "") -> str:
    return json.dumps(
        {
            "model": model,
            "limit": LIMIT,
            "sample": settings or "all",
            "summary": [asdict(s) for s in summarize(rows)],
            "rows": [asdict(r) for r in rows],
        },
        indent=2,
    )


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _main() -> None:
    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder
    from tests.eval.embed_cache import DiskCachingEmbedder
    from tests.eval.threshold import CachingEmbedder

    settings = load_settings()
    per_type = per_type_limit(os.environ.get(PER_TYPE_ENV))
    questions = select(load_questions(dataset_path()), per_type)
    inner = build_embedder(settings)
    embedder: Embedder
    if cache_dir := os.environ.get(CACHE_ENV):
        embedder = DiskCachingEmbedder(inner, cache_dir)
    else:
        embedder = CachingEmbedder(inner)
    try:
        rows = await measure(embedder, questions, progress=_progress)
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None
    sample = f"{per_type} per question type" if per_type else "all"
    print(report(rows, embedder.model_name, sample))
    if path := os.environ.get(JSON_ENV):
        Path(path).write_text(to_json(rows, embedder.model_name, sample) + "\n")


if __name__ == "__main__":
    asyncio.run(_main())
