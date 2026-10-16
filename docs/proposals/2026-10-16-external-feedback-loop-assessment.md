# An external fleet-memory feedback loop: what Hivemind already has

> A research proposal, kept for reference. It is not the source of truth: [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions.

*Assessment, 2026-10-16. Checked against `main` at `c98b531` (v2.9.0). Read-only: nothing in the repository was changed. The compared system is named in the 2026-10-01 comparison in this directory and in the [SPEC §2](../../SPEC.md) lineage table; this document keeps the capability list anonymous.*

## Why this document exists

On 2026-10-16 a ChatGPT summary of an external fleet-memory system's agent-feedback loop was used to compare features with Hivemind: outcome-based reinforcement of recalled memories, preventive rules generated from failures, explicit supersession of outdated memories, scope and trust on shared state, contradiction detection, and per-agent retrieval tuning. The repository has already compared against the same system (the 2026-10-01 comparison in this directory); the items it found worth borrowing shipped in 2.3.0-2.8.0. This pass re-checks each capability in the summary against the current code and records what already exists, what is a recorded deliberate rejection, and what is trigger-held, so a future comparison does not rediscover the same ground. The capability list was checked against the system's current agent documentation.

## Status table

| Capability in the external system | Hivemind mechanism | Status |
|---|---|---|
| Outcome-reporting tool that reinforces the weights of the memories used, on success | `hive_feedback` with per-entry `helpful` / `stale` / `wrong` verdicts and readable notes; consensus-share quality (ADR 0063) under the 1.2 social cap (ADR 0070) | Shipped. Hivemind is finer-grained: a verdict and a note per entry, batchable (ADR 0053) |
| A failure generates a preventive `rule` memory, server-generated | The agent writes a `dead-end` / insight entry; the skill says "write when reality differed from what Hivemind told you" | Convention. Server-side rule generation rejected as passive capture by another name (ADR 0004) |
| Supersede outdated memories, preserving history and provenance | Append-only with explicit supersession (ADR 0001); `include_history`, `supersedes` counts, withdrawal | Foundation; shipped |
| Scope and trust levels restrict which agents modify shared state | Fleets, four trust levels, self/fleet/org scopes, `may_supersede`, feedback follows readability (ADRs 0011, 0033) | Shipped |
| Detect contradictory or stale facts | `contested`, `correction`, `flagged` (ADR 0054), `similar` (off until τ is measured, ADR 0074), `upstream_changed` (ADR 0081) | Shipped as markers. LLM-assisted detection is trigger-held (SPEC §10) |
| Reinforce memories based on repeated evidence and corroboration | Independent `helpful` verdicts with `verified:` notes, `last_confirmed_at`, the guardrail invariants (ADR 0070) | Shipped |
| A standalone helpful / not-helpful rating | `hive_feedback` with the `helpful` verdict, batchable over up to 16 entries | Shipped. The summary notes the external system has no standalone rating API |
| Automatically learn an agent's retrieval preferences | None | Trigger-held: "static decay stops beating per-agent profiles" (SPEC §10) |

## The one true gap

LLM-assisted contradiction detection at write time is the only capability in the summary that Hivemind does not have. It is a SPEC §10 extension with a measurable trigger (the first observed contradicting entry landing without a supersession, or a pool above 10,000 entries) and is deliberately held: with a small pool there is no corpus to tune a similarity floor against, and an untuned floor is the failure mode to avoid. The pre-staging already exists: every write reports its three nearest entries with their similarity (ADR 0052), and `check: true` mode stores nothing and returns warnings (ADR 0077).

**Decision, 2026-10-16:** keep it held. Do not build it before the trigger fires; watch the ROADMAP Tier 5 counters.

## Where Hivemind is already ahead

Per-entry outcome verdicts with readable notes (the external system reports one outcome against a list of memory ids). Batch feedback. The `flagged` filter. Write-time `related` with no floor and no verdict. Keyword fallback when the embedder is down (ADR 0048). Entries as data, never instructions (ADR 0043). A recency floor measured before it shipped (ADR 0022). One Postgres node, against the external system's Postgres + Redis + storage service.

## Remaining open, non-breaking items

From the [hive-mind study](2026-10-10-hive-mind-ideas.md) plan. None of them is specific to the compared system; all are off by default or display-only; none is a breaking change; migrations are acceptable where noted.

| # | Item | Plan row | Notes |
|---|---|---|---|
| 1 | Measure τ for `similar` version grouping on a real aged corpus | 16 | Cheapest next step. Pure measurement; behaviour changes only when a value is chosen. The same corpus also serves the recency-floor re-measure and the per-kind recency profile (ADR 0072), which are waiting on real aged data |
| 2 | `distilled_in` on hits: which syntheses build on this entry | 15 | Display-only |
| 3 | Narrow importance to 0.9-1.1 | 12 | A ranking change under the existing guardrail invariants |
| 4 | Per-owner vote collapse | 17 | The only open independence gap (one owner, several agent keys); `owner_alias` is self-reported |
| 5 | Helpful evaporation | 18 | Reserve; only if social scenarios S1/S3 still fail after 1 and 4 |

**No implementation in this pass.** Item 1 is flagged as the cheapest next step once a real aged corpus exists.
