# ROADMAP.md — Hivemind (what to build next, in what order)

> **What this document is:** the working plan for the *next* increment. It is
> **not** the spec (SPEC.md owns behavior) and **not** the decision log
> (docs/adr/ owns reasons). It links to both and exists only to sequence
> the work. Revisit after Tier 1 lands — the eval numbers will likely
> re-rank Tiers 2–5.

## Where we are

- **v1 is code-complete and review-gated.** Every lane (domain / ports /
  retrieval / services / memstore, Postgres + pgvector store,
  embeddings, REST API, MCP server) is implemented, integrated on
  `main`, and green (mypy strict + ruff clean; full suite green
  including the live-Postgres integration tests).
- **The eval harness (Tier 1) has landed.** The retrieval pipeline is
  now measured on a committed golden set (`tests/eval/`): hit@k / MRR /
  nDCG are reported and a CI gate pins a floor on them (a regression
  below the bar fails the suite). The ~10 config knobs are now
  *measured* dials, not vibes (current: hit@5 = 1.0, MRR = 0.75,
  nDCG@5 = 0.8155).
- **Access control (Tier 2) has landed.** The full fleet / trust-level /
  registration model (ADRs 0011-0012, SPEC 12) is implemented end to
  end: domain (TrustLevel / Agent / Fleet / Visibility), the `Store`
  seam (fleet / agent methods + visibility-aware reads), the v2 key
  model (org / agent / admin; `user` keys retired), the `AccessService`
  (register / activate / trust-level / home-fleet / revoke / org-key
  rotation), the REST surface (`POST /v1/agents` + the admin endpoints),
  the MCP surface (`hive_register` + write-scope + visibility), and the
  reduced `hivemind-keys` CLI.
- **What's next:** Tier 3 (productionize - the ops runbook, forward
  migration, usage counters) is the next workstream. It is no longer
  blocked: Tier 2 (access control) has landed, so the key-rotation story
  (3.1) can now be written against the settled key model.

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

## Tier 1 — validate the core  *(shipped)*

### 1.1 Retrieval eval harness + golden set  *(shipped: `tests/eval/`, `retrieval/eval.py`)*
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
  retrieval-quality triggers in the Tier 5 table below.

### 1.2 End-to-end dogfood with a real agent + real embedder  *(open)*
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
- **Note:** once Tier 2 lands, the dogfooding agent will present an
  **agent key** (ADR 0012) — not the old per-user sub-key. The MCP
  surface is the same seven `hive_*` tools; only the credential kind
  changes.

## Tier 2 — access control & fleet model (ADRs 0011–0012, SPEC §12)  *(shipped)*

The committed access-control capability: fleets, trust levels, and the
registration / key model. This is a new workstream (a capability we
committed to — *not* a §10 trigger), and it sequences **before** the
key-rotation / ops items in Tier 3. It is independent of Tier 1 (the
eval harness) — the two tracks run in parallel.

### 2.1 Domain + store: fleets, agents, trust levels  *(shipped)*
- **What:** `fleets` + `agents` schema; entries gain a fleet reference
  and scope values `self` / `fleet` (plus read-only legacy `org`);
  visibility filtering in search/list/get (trust level + home fleet;
  level 0 → everything empty; reads beyond visibility behave as if the
  entry does not exist); omitted-scope resolution (highest scope the
  writer's level permits; an explicit out-of-permission scope is a
  permission error); L0 semantics (reads empty, writes/feedback
  denied).
- **Why:** the entire §12 model — the store is the seam (ADR 0011).
- **Deliverables:** store changes behind the `Store` port (new ports
  where needed), migration, hermetic unit tests at the seams.

### 2.2 Registration + admin surface  *(shipped)*
- **What:** `hive_register` (the seventh MCP verb; org-key only) +
  `POST /v1/agents` (REST; org key **or** admin key — the seam a
  future human-facing frontend plugs into). The admin endpoint set:
  `GET /v1/admin/agents` / `GET /v1/admin/fleets`,
  `POST /v1/admin/fleets`, `POST /v1/admin/agents/{name}/activate`
  (**returns the generated agent key once** — the only moment a key is
  ever shown), `PATCH /v1/admin/agents/{name}`,
  `POST /v1/admin/agents/{name}/revoke`, and
  `POST /v1/admin/org-key/rotate` (the cluster-wide kill switch).
  `hivemind-keys` is reduced to admin-key issuance + org-key rotation
  (ADR 0012).
- **Why:** registration is the front door of the model; activation is
  the one moment a key is ever shown (ADR 0012).
- **Deliverables:** REST + MCP surface, the admin endpoint set, and the
  error semantics (pending name → idempotent no-op; taken-by-active
  name → "choose a new name").

### 2.3 Runners + credential migration  *(shipped: reduced keys CLI; runner key-kinds updated)*
- **What:** `hivemind-mcp-pg` / `hivemind-mcp-http` present **agent
  keys** (ADR 0012; the ADR 0009/0010 mechanisms are unchanged —
  verified provenance, immediate revocation on the hostable runner);
  migration of the legacy `user` / `agent`-kind credentials to agent
  keys under registered names (a named migration step); the dev runner
  is untouched.
- **Why:** per-agent revocation and verified provenance must keep
  working under the new key kinds (ADRs 0009–0010).
- **Deliverables:** migration script + `hivemind-keys` CLI notes.

**Sequencing rule:** 2.1 blocks 2.2 and 2.3 (the admin surface needs
the `agents` / `fleets` tables; the runners need the new credential
kinds). Tier 3.1 (the key-rotation runbook) is **blocked by Tier 2** —
you can't write a rotation story for a key model that's about to
change.

## Tier 3 — productionize (former Tier 2)

### 3.1 Ops runbook
Deployment, **backups** (single-node Postgres, ADR 0007),
monitoring / health, and a **key issuance + rotation** story — now
*defined* by ADR 0012 (the admin surface + org-key rotation define
"who issues keys and how they rotate"). **Blocked by Tier 2.**

### 3.2 Forward-migration path
`make migrate` is an idempotent full re-apply of the schema. There is
no zero-downtime **forward** schema-evolution story for a system with
live data — define how a new column / index lands without rewriting the
whole pool. *(Note: the Tier 2 schema change (fleets / agents /
entry-fleet refs) is the first real test of this story.)*

### 3.3 Usage counters (trigger instrumentation)
A minimal metrics surface — writes, feedbacks, supersessions, distinct
`scope` tags in use, `sources` by type, per-agent query counts. This
makes the **usage-based** SPEC §10 triggers (below) measurable rather
than guesswork. Cheap, high-signal; pair with §1.1 (which covers the
retrieval-quality triggers). *(Add the §12 counters: writes per fleet,
trust-level distribution, pending-agent count, revoked-key count.)*

## Tier 4 — close the spec's open items (SPEC §11) (former Tier 3)

- **4.1 BM25 vs. Postgres FTS.** The store currently uses Postgres
  FTS (`to_tsvector`). Decide on BM25 once the §1.1 harness shows
  where FTS ranks under (a new ADR on the decision).
- **4.2 Embedding prefix length.** The 512-token default (SPEC §11.5)
  is "tune during implementation against real long-form entries" — do
  that tuning with the §1.1 harness on real data.

## Tier 5 — explicitly held: the §10 extensions (former Tier 4)

**Do not build any SPEC §10 extension yet** (passive capture, private
staging, namespaces, binary artifacts, knowledge graph, contradiction
detection, curation, human UI, multi-tenant SaaS, per-agent retrieval
tuning). SPEC §10 is explicit: build it when its trigger fires. With no
production usage, no trigger has fired.

**Trigger metrics (the instrumented version of §10).** Instead of
"when it feels noisy," define a *measurable* condition per extension
so the later decision is data-driven. Two kinds:

| §10 extension | Trigger (spec wording) | Measure to watch | Instrument |
|---|---|---|---|
| Knowledge graph | "entity linking pays off in retrieval" | hit@k gap on entity-linked queries vs. plain hybrid | §1.1 harness |
| Per-agent retrieval tuning | "static decay stops beating per-agent profiles" | per-agent MRR/AUC vs. static model | §1.1 harness |
| Passive capture | "agents forget to write" | high-value agent turns with no write; "should have remembered X" reports / wk | §3.3 counters |
| Private staging | "try before sharing" | fraction of `self`-scoped entries later promoted to the fleet *(the `self` scope is now the staging space — ADR 0011; promotion is the curation story)* | §3.3 counters |
| Namespaces / channels | "flat pool too noisy" | result precision on scope-unspecified queries; count of distinct fleets in use *(partially satisfied by ADR 0011: single-fleet needs are covered; multi-fleet membership is the remaining extension)* | §3.3 counters |
| Binary artifacts | "analyses outgrow file/URL refs" | count of `sources` with `type=file` or payload > threshold | §3.3 counters |
| Contradiction detection | "explicit supersession can't keep up" | count of same-topic, conflicting-value entry pairs *not* linked by a supersession chain | §3.3 counters |
| Curation workflow | "org wants a 'librarian'" | feedback (helpful/stale/wrong) accumulating without action; stale/wrong entries still in top-k | §3.3 counters + §1.1 harness |
| Human read-only UI | "analysts want to see the pool" | direct demand for human browsing (usage signal, not a metric) | qualitative |
| Multi-tenant SaaS / OAuth | "more than one org; an org has an IdP" | count of distinct orgs / tenants requested (count > 1) | §3.3 counters |

## If you do one thing

The eval harness (1.1) and access control (Tier 2) are both shipped. The
next keystone is **Tier 3, productionize** — starting with **3.1, the ops
runbook** (deployment, backups, health, and the key issuance / rotation
story, now writable against the settled ADR 0012 key model). It is the
gating item: 3.2 (forward migration) and 3.3 (usage counters) build on a
stable, operable deployment, and the §10 trigger instrumentation (3.3)
pairs with the §1.1 harness to make the Tier 5 decisions data-driven.

## How to use this document

- This is a **living plan**, not a spec. When a Tier item is built, it
  produces real ADRs (e.g. "adopt BM25") — this doc just sequences the
  work.
- It links to SPEC.md / CONTEXT.md / docs/adr/ for behavior and
  rationale; it does not restate them.
- Revisit after §1.1 lands — the eval numbers will likely re-rank
  Tiers 3–5.