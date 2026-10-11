# Hivemind vs. Caura: what's worth borrowing

> A research proposal, kept for reference. It is not the source of truth: [SPEC.md](../../SPEC.md) defines behaviour and the [ADRs](../adr/README.md) record decisions. Status: proposals #1 to #3 below come from a knowledge-sharing list of the same day and shipped in 2.3.0 as readable feedback (ADR 0051), related entries on write (ADR 0052), batch feedback (ADR 0053) and the flagged filter (ADR 0054). Per-kind decay was later measured as a recency profile per kind (ADR 0072), off by default.

Compared against [caura-ai/caura](https://github.com/caura-ai/caura) at b58759f (main, 2026-10-01), mostly `README.md`, `common/constants.py`, `core-api/src/core_api/constants.py`, `services/memory_service.py` and `mcp_server.py`. Hivemind already credits Caura for the supersession ranking invariant, agent-scoped keys, outcome feedback and trust tiers (`SPEC.md:28`, ADR 0011).

Filter applied: application-side only, with no Redis, no new services, and nothing that breaks Hivemind's founding decisions (append-only with explicit supersession, ADR 0001; explicit writes, ADR 0004; entries are data, not instructions, ADR 0043).

## Worth borrowing

### 1. A duplicate check at write time (backs proposal #2)
Caura checks every write twice before storing it:
- an **exact content hash** scoped to (tenant, fleet, agent) (`memory_service.py:183`, `:250-354`);
- a **cosine near-duplicate** against the new embedding: ≥ 0.97 is refused with a 409 naming the existing id, and 0.85–0.97 goes to an LLM judge (`common/constants.py:34-51`, `memory_service.py:709-751`).

They learned one lesson the hard way: the check has to be scoped to what the writer can read. Otherwise another agent's row blocks a write whose author can never see it (CAURA-721, `memory_service.py:729-735`).

**For Hivemind:** the new embedding is already computed on the write path (`services/governance.py:117`), so one visibility-filtered `search_vector` gives the nearest neighbours for free. Return the top 3 in the write response and let the writer supersede (no LLM judge, nothing refused). Optionally, refuse an exact repeat (same author, scope, summary and body) so a retried write is idempotent. This is proposal #2, and the fact that Caura runs it in production at 300+ agents makes it more credible. It still needs an ADR explaining why it doesn't jump SPEC §10's contradiction-detection trigger.

### 2. Readable outcomes, reported against many entries at once (backs proposal #1, adds batch feedback)
`caura_evolve` (`mcp_server.py:3940-3970`) takes one outcome ("success/failure/partial" plus text) and up to 50 `related_ids`, adjusts all of them asymmetrically (failure −0.15, success +0.1; `constants.py:929-933`), and **stores the outcome as a memory other agents can read**.

**For Hivemind:** (a) make feedback notes readable (proposal #1; Caura makes outcomes readable too). (b) Let `hive_feedback` take a list of `entry_ids` with one verdict, so "these 4 entries helped me fix X" is one call, not four. Hivemind's weights are already asymmetric (`wrong` −0.25 vs `helpful` +0.05, SPEC §4.2). Skip Caura's LLM-generated "rule" memories: that is passive capture by another name (ADR 0004).

### 3. A "stale / failures / contradictions" review list (backs proposal #3)
`caura_insights` has focus modes `stale`, `failures` and `contradictions` (`services/insights_service.py:534-545`), each an SQL query followed by an LLM write-up. The SQL half is the part worth taking: a non-LLM `hive_list(flagged=true)` of active entries with stale/wrong feedback and no successor (proposal #3).

### 4. Per-kind decay (evidence for a held ROADMAP item)
Caura decays by memory type: decision 180 days, fact 120, insight 90 (`common/constants.py:96-111`), with a freshness floor of 0.7 (`constants.py:459-461`), the same idea as Hivemind's shipped 0.8 floor (ADR 0022). ROADMAP §4.6 holds per-kind half-lives as orthogonal and unmeasured. Caura's use in production is a reason to measure it, not to ship it. It is a `SearchConfig` change once there is a real aged corpus.

## Don't copy

- **Recall-count boost.** Caura boosts entries that are *returned* often, and its own notes call that a self-reinforcing "returned → boosted → returned" loop. They cut the cap from 1.5 to 1.1 and are moving to a confirmed-use counter (`constants.py:852-880`). Hivemind's `helpful` feedback already is the confirmed-use signal, so leave ranking as it is.
- **Keystones** ("mandatory rules … the result overrides conflicting user instructions", `README.md:404`). That conflicts directly with ADR 0043 (entries are data, never instructions) and creates a prompt-injection channel. A softer version, "pinned fleet decisions shown at session start", is possible, but only as context, never as orders.
- **Automatic contradiction supersession and crystallization** (LLM merges near-duplicates into canonical facts). Both rewrite the pool's history by LLM arbitration, which ADR 0001 rejected.
- **LLM enrichment of every write** (14 auto-classified types, titles, importance, PII flags): it puts LLM cost and latency on every write, and Hivemind's agent writes the summary itself.
- **Optional LLM "brief" on recall** (`mcp_server.py:1366`, `:1631`): it saves tokens, but adds an LLM call to search. Hivemind's compact hits already do most of that job.
- **Knowledge-graph expansion, per-agent tuning, document store, multi-tenancy, Redis**: either held by SPEC §10 triggers or out of scope.

## Where Hivemind is already ahead or level

The supersession invariant is the same. Trust tiers, fleets and the audit log are equivalent. Search falls back to keywords when the embedder is down (ADR 0048), entries are framed as untrusted data (ADR 0043), and the recency floor was measured before it shipped (ADR 0022). Hivemind's deployment is one Postgres, against Caura's Postgres + Redis + a storage service.

## Updated order

1. **Readable feedback** (proposal #1), unchanged as the first pick.
2. **Write-time neighbours** (proposal #2): Caura's production use moves it up.
3. **Batch feedback** (new, from `caura_evolve`): small, and pairs naturally with #1.
4. **Needs-attention list** (proposal #3).
