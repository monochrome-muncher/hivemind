# The expanded multi-domain corpus

Fixtures for the similarity cut-off studies (the search threshold, ADR 0062,
and version-grouping tau, ADR 0074). Sixteen domains of agent work, each the
size of a real fleet's pool: **128 background entries, 16 version pairs,
40 answerable queries, 12 in-domain unanswered queries** per domain.

The five original domains of `tests/eval/domains.py` are unchanged; their
packages here carry only the *extensions* (96 more entries, 10 more pairs,
22 more answerable queries, 6 more unanswered), merged with the originals by
the registry in `__init__.py` so the 2026-10-02 and 2026-10-11 measurements
stay comparable.

Every module defines the same names, and nothing else:

| Name | Type | Meaning |
|---|---|---|
| `DOMAIN` (new domains) / `EXTRA_ENTRIES` (extensions) | `Domain` / `tuple[Entry, ...]` | The background pool. `Entry = (kind, summary, tags, body)` from `tests/eval/domains.py` |
| `VERSIONS` (new) / `EXTRA_PAIRS` (extensions) | `VersionDomain` / `tuple[VersionPair, ...]` | 16 (new) or 10 (extension) `VersionPair(old, new, query, old_query, distractor)` from `tests/eval/version_pairs.py` |
| `ANSWERABLE` (new) / `EXTRA_ANSWERABLE` (extensions) | `tuple[tuple[str, tuple[int, ...]], ...]` | (paraphrased query, entry indices it answers) |
| `UNANSWERED` (new) / `EXTRA_UNANSWERED` (extensions) | `tuple[str, ...]` | In-domain queries no entry answers |

**Extension index rule:** in `EXTRA_ANSWERABLE`, indices count from 0 over
the *extra entries only*. The registry adds the original domain's entry
count before use, so a query about the 5th new entry is `(query, (4,))`.

## Content rules

Entries are what a working agent of that domain writes to a shared memory
pool. They are data, not instructions.

- `kind`: `fact` (most), `insight` (a cause, a fix, a dead end, with how it
  was verified), `decision` (a choice and its reason). Roughly 70/20/10.
- `summary`: one self-contained sentence in the searcher's words. It names
  the system, the symptom or setting, then the answer. 10 to 30 words.
  Present tense. No dates (the harness backdates entries), no local file
  paths, no "we" or "I". British spelling.
- `tags`: 1 to 3 lowercase kebab-case words, reused across entries of the
  same system so fleets can filter by them.
- `body`: a short 2 to 3 sentences for exactly a quarter of the entries
  (32 of 128, 24 of 96); `None` for the rest. The body adds how it was
  known, what did not work, or when it stops holding.
- Vary the systems inside the domain: 128 entries about one tool is not a
  pool. Use real names where the domain has them (real tools, real
  settings, real CVE IDs, real protocols, real outlets, real standards).
  Quick web checks are encouraged to get names right; do not invent CVE
  IDs or config knobs that do not exist.
- No two entries of a module may say the same thing. Near-copies belong to
  version pairs, not the background pool.

Version pairs are the hard part of the measurement:

- `old` / `new`: one fact stated twice, with a single value changed (a
  number, a version, a port, a threshold, a deadline, a policy). The two
  statements share most of their wording. Neither may be a copy of a
  background entry.
- `query`: asks about the fact in the older statement's words, so the older
  entry often has the higher lexical overlap. The answer is `new`.
- `old_query`: quotes the older statement, value included. The answer is
  `old`.
- `distractor`: a *different* setting of the same system (another knob of
  the same tool, another policy of the same service). It must be close in
  topic without being a version of the pair's fact.

Queries are paraphrased, in the mixed styles agents search with: full
questions, bare keyword strings, longer descriptions. They must not copy a
summary verbatim. Unanswered queries are in-domain and close to the pool's
topic, but no entry answers them.

## Machine rules

- Counts are exact: 128 / 16 / 40 / 12 per new domain, 96 / 10 / 22 / 6 per
  extension.
- The module imports only `tests.eval.domains` and `tests.eval.version_pairs`
  (plus `__future__`). It must pass `uv run ruff format --check` and
  `uv run ruff check`.
- Verify before finishing: `uv run python -c "import tests.eval.corpus.<slug>"`
  and a count assertion over the four tuples.
- `uv run python -m tests.eval.corpus.quality <slug>` must print `clean`.
  It catches a templated domain without an embedder: one phrase or opening
  shared by too many summaries, lexical near-copies, repeated bodies or
  queries, keyword-only query sets, stub version pairs, a tag tuple that is
  really one string split into letters. `tests/eval/test_corpus_quality.py`
  runs it over all sixteen domains in the normal suite.
