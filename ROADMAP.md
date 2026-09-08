# ROADMAP.md — Hivemind (what to build next, in what order)

> **What this document is:** the working plan for the *next* increment. It is
> **not** the spec (SPEC.md owns behavior) and **not** the decision log
> (docs/adr/ owns reasons). It links to both and exists only to sequence
> the work. Revisit it after Tier 1 lands — the eval numbers will likely
> re-rank Tiers 2–4.

## Where we are

- **v1 is code-complete and review-gated.** Every lane (domain / ports /
  retrieval / services / memstore, Postgres + pgvector store, embeddings,
  REST API, MCP server) is implemented, integrated on `main`, and green
  (mypy strict + ruff clean; full suite green including the live-Postgres
  integration tests).
- **But code-complete ≠ validated.** The core retrieval pipeline runs
  ~10 config knobs at uncalibrated spec defaults with **no evaluation
  harness** — there is no way to know whether the rankings are actually
  good, and no way to catch regressions in retrieval quality. For a
  system whose entire value is "an agent finds the *right* memory,"
  that black box is the top risk.

## The keystone: build the measurement instrument first

The retrieval **eval harness** (§1.1) is the single highest-leverage
next step, because it does three things at once:

1. de-risks v1's core (proves / improves ranking quality),
2. becomes a reproducible CI gate against retrieval regressions, and
3. is the same instrument that lets you *measure* the two
   retrieval-quality triggers in SPEC §10 (knowledge graph, per-agent
   tuning) — turning those future extensions from "vibes" into
   data-driven decisions.

---

## Tier 1 — validate the core (do these first)

### 1.1 Retrieval eval harness + golden set
- **What:** a fixed, realistic entry set (seeded deterministically on
  top of the existing `tests/fakes.py`) + a **golden query set** (each
  query → expected top-k / expected #1), run through
  `SearchService.search` and reporting **hit@k, MRR, nDCG**.
- **Why:** ~10 knobs in `src/hivemind/config.py` — `rrf_k`,
  `weight_keyword` / `weight_vector`, `candidate_top_k`, `default_limit`,
  `half_life_days`, and the five `quality_*` weights — are all "SPEC
  default" and have never been calibrated. This turns them from
  guesses into measurable, tunable dials.
- **Deliverables:** a small eval runner (e.g. `tests/eval/`) + a
  committed golden set + a CI gate pinning a few golden queries so
  future retrieval changes are measured, not vibes.
- **Feeds:** Tier 3 (BM25, prefix-length tuning) and the two
  retrieval-quality triggers in the Tier 4 table below.

### 1.2 End-to-end dogfood with a real agent + real embedder
- **What:** wire a real agent (pi / Claude) to `hivemind-mcp` over a
  live Postgres + a real OpenAI-compatible embedder, and run a
  realistic loop (write → search back → feedback → supersede).
- **Why:** the unit / integration suite runs on `FakeEmbedder`. This
  validates the two seams only exercised with fakes: (a) the real
  1536-dim `OpenAICompatEmbedder`, and (b) whether the MCP tool
  descriptions are good enough for an LLM to use well.
- **Deliverable:** a short dogfooding-notes doc capturing friction
  (tool-description gaps, error-code clarity, whether agents actually
  reach for feedback / supersession).

## Tier 2 — productionize (needed before real users)

### 2.1 Ops runbook
Deployment, **backups** (single-node Postgres, ADR 0007),
monitoring / health, and a **key issuance + rotation** story. The
`hivemind-keys` CLI exists; "who issues keys and how they rotate" is
still undefined.

### 2.2 Forward-migration path
`make migrate` is an idempotent full re-apply of the schema. There is
no zero-downtime **forward** schema-evolution story for a system with
live data — define how a new column / index lands without rewriting the
whole pool.

### 2.3 Usage counters (trigger instrumentation)
A minimal metrics surface — writes, feedbacks, supersessions, distinct
`scope` tags in use, `sources` by type, per-agent query counts. This
makes the **usage-based** SPEC §10 triggers (below) measurable rather
than guesswork. Cheap, high-signal; pair with §1.1 (which covers the
retrieval-quality triggers).

## Tier 3 — close the spec's open items (SPEC §11)

- **3.1 BM25 vs. Postgres FTS.** The store currently uses Postgres
  FTS (`to_tsvector`). Decide on BM25 once the §1.1 harness shows
  where FTS ranks under (a new ADR on the decision).
- **3.2 Embedding prefix length.** The 512-token default (SPEC §11.5)
  is "tune during implementation against real long-form entries" — do
  that tuning with the §1.1 harness on real data.

## Tier 4 — explicitly held: the §10 extensions (build only when the trigger fires)

**Do not build any SPEC §10 extension yet** (passive capture, private
staging, namespaces, binary artifacts, knowledge graph, contradiction
detection, curation, human UI, multi-tenant SaaS, per-agent retrieval
tuning). SPEC §10 is explicit: build it when its trigger fires. With no
production usage, no trigger has fired.

**Trigger metrics (the instrumented version of §10).** Instead of "when
it feels noisy," define a *measurable* condition per extension so the
later decision is data-driven. Two kinds:

| §10 extension | Trigger (spec wording) | Measure to watch | Instrument |
|---|---|---|---|
| Knowledge graph | "entity linking pays off in retrieval" | hit@k gap on entity-linked queries vs. plain hybrid | §1.1 harness |
| Per-agent retrieval tuning | "static decay stops beating per-agent profiles" | per-agent MRR/AUC vs. static model | §1.1 harness |
| Passive capture | "agents forget to write" | high-value agent turns with no write; "should have remembered X" reports / wk | §2.3 counters |
| Private staging | "try before sharing" | fraction of writes later withdrawn as "shouldn't have been shared" | §2.3 counters |
| Namespaces / channels | "flat pool too noisy" | result precision on scope-unspecified queries; count of distinct `scope` tags in use | §1.1 harness + §2.3 counters |
| Binary artifacts | "analyses outgrow file/URL refs" | count of `sources` with `type=file` or payload > threshold | §2.3 counters |
| Contradiction detection | "explicit supersession can't keep up" | count of same-topic, conflicting-value entry pairs *not* linked by a supersession chain | §2.3 counters |
| Curation workflow | "org wants a 'librarian'" | feedback (helpful/stale/wrong) accumulating without action; stale/wrong entries still in top-k | §2.3 counters + §1.1 harness |
| Human read-only UI | "analysts want to see the pool" | direct demand for human browsing (usage signal, not a metric) | qualitative |
| Multi-tenant SaaS / OAuth | "more than one org; an org has an IdP" | count of distinct orgs / tenants requested (count > 1) | §2.3 counters |

## If you do one thing

Build **§1.1, the retrieval eval harness.** It is the keystone: it
de-risks v1's core, it is a CI gate against regressions, and it is the
same instrument that tells you when the Tier 4 triggers fire.
Everything else in this doc either depends on it or is downstream of it.

## How to use this document

- This is a **living plan**, not a spec. When a Tier-1 item is built,
  it produces real ADRs (e.g. "adopt BM25") — this doc just sequences
  the work.
- It links to SPEC.md / CONTEXT.md / docs/adr/ for behavior and
  rationale; it does not restate them.
- Revisit after §1.1 lands — the eval numbers will likely re-rank
  Tiers 2–4.