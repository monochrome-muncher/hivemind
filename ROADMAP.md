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
- **What's next:** Tier 3.1–3.4 are shipped, including the Kubernetes +
  GitLab CI/CD deployment story (3.4). **§3.2 has been superseded by
  §3.5** (ADR 0020: an ordered, rollback-capable migration chain under a
  Postgres advisory lock), which is now **shipped end to end**. The next
  workstream is **Tier 4** (the SPEC §11 open items: the
  BM25-vs-FTS decision and the embedding-prefix tuning, both measurable
  with the §1.1 eval harness, plus the 4.4/4.5 measurement items).

## The keystone is shipped: measure, then Tier 4

The retrieval **eval harness** (§1.1) — the single highest-leverage next
step — has **landed**: it de-risks v1's core (rankings are now measured,
not vibes), it is a reproducible CI gate against retrieval regressions,
and it is the same instrument that lets you *measure* the two
retrieval-quality triggers in SPEC §10 (knowledge graph, per-agent
tuning), turning those future extensions from "vibes" into
data-driven decisions.

With the keystone shipped and §3.5 (the migration chain) in place, the
next workstream is **Tier 4** (the BM25-vs-FTS decision, the
embedding-prefix tuning, and the two measurement items 4.4/4.5) — all of
which are now measurable with the §1.1 harness instead of guesses. §4.5
is the first schema change to ride the new chain.

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

### 1.2 End-to-end dogfood with a real agent + real embedder  *(shipped)*
- **What:** wire a real agent (pi / Claude) to `hivemind-mcp` over a
  live Postgres + a real OpenAI-compatible embedder, and run a
  realistic loop (write → search back → feedback → supersede).
- **Why:** the unit / integration suite runs on `FakeEmbedder`. This
  validates the two seams only exercised with fakes: (a) the real
  embedder (here the local 512-dim vLLM `Qwen3-Embedding-0.6B`), and
  (b) whether the MCP tool descriptions are good enough for an LLM to
  use well.
- **Deliverable:** `docs/dogfooding-notes.md` capturing friction
  (tool-description gaps, error-code clarity, whether agents actually
  reach for feedback / supersession).
- **Note:** once Tier 2 lands, the dogfooding agent will present an
  **agent key** (ADR 0012) — not the old per-user sub-key. The MCP
  surface is the same seven `hive_*` tools; only the credential kind
  changes.
- **Result (2026-09-20):** the full loop ran clean on a real embedder
  (Postgres 512-dim + vLLM). It caught one real defect: `hive_write`
 's forced `scope="org"` default (in the registered MCP wrapper and the
  REST schema) rejected every L2/L1 "unthinked" write, bypassing
  ADR 0011's omitted-scope rule — now fixed at all three seams (app,
  registered wrapper, REST) and locked by tests. Empty-`entry_id`
  guards and `fleet_id` in entry reads were added in the same change.
  Ops friction (build-cache / token-cache / pool-reset / dim footgun)
  is logged in the notes doc.

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

## Tier 3 — productionize (former Tier 2)  *(3.1–3.4 shipped; 3.2 superseded by 3.5)*

### 3.1 Ops runbook  *(shipped: `docs/ops-runbook.md`)*
Deployment, **backups** (single-node Postgres, ADR 0007),
monitoring / health, and a **key issuance + rotation** story — now
*defined* by ADR 0012 (the admin surface + org-key rotation define
"who issues keys and how they rotate"). **Blocked by Tier 2.**

### 3.2 Forward-migration path  *(shipped, then **superseded by §3.5** — ADR 0013 → ADR 0020)*
`make migrate` is an idempotent full re-apply of the schema. There is
no zero-downtime **forward** schema-evolution story for a system with
live data — define how a new column / index lands without rewriting the
whole pool. *(Note: the Tier 2 schema change (fleets / agents /
entry-fleet refs) is the first real test of this story.)*
*(Superseded: the idempotent re-apply cannot express a change to an
existing object, shipped no backward direction, and assumed a
single replica. See §3.5.)*

### 3.3 Usage counters (trigger instrumentation)  *(shipped: `MetricsService` + `GET /v1/metrics`)*
A minimal metrics surface — writes, feedbacks, supersessions, distinct
`scope` tags in use, `sources` by type, per-agent query counts. This
makes the **usage-based** SPEC §10 triggers (below) measurable rather
than guesswork. Cheap, high-signal; pair with §1.1 (which covers the
retrieval-quality triggers). *(Add the §12 counters: writes per fleet,
trust-level distribution, pending-agent count, revoked-key count.)*

### 3.4 Kubernetes + GitLab CI/CD deployment story  *(shipped: `DEPLOY.md`, `deploy/kubernetes/`, `.gitlab-ci.yml`, `config/` env-profile quickstart)*
A production deployment story: **one generic image** (runner selected by
`HIVEMIND_RUNNER` — api / mcp-http / migrate / keys; the entrypoint owns
the idempotent migration pre-step — ADR 0018), a plain-YAML
**kustomize** manifest tree (two 1-replica Deployments — ADR 0007 — with
unauthenticated probe endpoints — ADR 0019; optional nginx + cert-manager
Ingress with SSE tuning), a **GitLab pipeline** (test on pgvector →
docker build/push → deploy via the pre-configured GitLab Kubernetes
agent, with an idempotent **first-run key bootstrap** that lands the
admin/org keys in the k8s Secret), environment-profile files
(`config/.env.example` + `ENVIRONMENT` selection — ADR 0017), and
`hivemind-keys revoke-admin` (admin-key rotation is now CLI-native).
ADR 0007's single-node decision is unchanged by k8s hosting (1 replica,
no HA); the single-node ops story stays in `docs/ops-runbook.md`
(one source of truth per concern).

### 3.5 Versioned migrations with rollback  *(shipped: ADR 0020, SPEC §8.6, `src/hivemind/store/migrations/`)*
Replaces §3.2. An ordered migration chain (`src/hivemind/store/migrations/`,
yoyo-migrations) with a `.rollback.sql` per step, applied under a
**Postgres advisory lock** so many replicas may start at once and exactly
one migrates. Driven by three facts §3.2 could not absorb: the declarative
re-apply is blind to changes on existing objects (seven `CHECK`
constraints are frozen; `api_keys.kind` has already drifted), the
`migrations/` directory ADR 0013 reserved was never built, and the
deployment target is now GitLab AutoDevOps — one runner type per project,
so `hivemind-api` and `hivemind-mcp-http` become independently released
projects at 2–3 replicas each against one pool.

Work items:
- `0001.initial-schema` (current `schema.sql`, dim-parameterised) + the
  chain; `migrate.py` keeps its public surface, swaps its body.
- Retire `schema_migrations`; `current_schema_version()` reads
  `_yoyo_migration` (ops surface unchanged).
- `schema.sql` becomes a CI-generated reference, never applied.
- `startupProbe` on both Deployments (the migration runs before the
  server listens; liveness would otherwise kill a slow index build).
- CI: migration-file checksum immutability, generated-`schema.sql`
  currency, and the **previous three releases' tests against a
  HEAD-migrated pool** (enforces expand-and-contract by testing the
  property, not by grepping for DDL verbs).
- Deps: `yoyo-migrations` + `psycopg` (startup path only; `asyncpg`
  stays the sole request-path driver).

*Not in scope here:* the replica bump itself (`replicas: 1` → 2–3) is a
separate change — it drags in rollout strategy and **SSE connection
draining on `hivemind-mcp-http`**, which is currently unexamined.

## Tier 4 — close the spec's open items (SPEC §11) (former Tier 3)

*(4.1–4.2 are the §11 open items. 4.3–4.5 are measurement commitments
that live here because they share the §1.1 harness, not because they
are §11 items — each says so.)*

- **4.1 BM25 vs. Postgres FTS.** The store currently uses Postgres
  FTS (`to_tsvector`). Decide on BM25 once the §1.1 harness shows
  where FTS ranks under (a new ADR on the decision).
- **4.2 Embedding prefix length.** The 512-token default (SPEC §11.5)
  is "tune during implementation against real long-form entries" — do
  that tuning with the §1.1 harness on real data. (The embedding
  *dimension* itself is a separate deploy-time decision: the default is
  now 1024, ADR 0015 — the dim tuning, if it lands, happens in the same
  harness work.)
- **4.3 Entity-extraction facets (pre-staged §10 extension — ADR 0016, SPEC §13).** *(shipped: `src/hivemind/extractor.py` + the `WriteService` best-effort hook + the REST/MCP read surface, SPEC §13)*
  *This is not a §11 open item: it is the knowledge-graph §10 extension,
  pre-staged on scale ambition (300+ agents / multiple fleets) — the
  §10 trigger ("cross-entry entity linking pays off in retrieval
  quality") has NOT fired; the facet slice is what was built, and it
  is measurable with the §1.1 harness. Graph-expanded retrieval and
  the canonical entity registry stay trigger-held under Tier 5.* A
  write-time, **optional + best-effort** LLM extractor (fixed prompt,
  all-or-nothing schema-validated `{name, kind}` output; closed kind
  vocabulary; `entities jsonb` on the entry, symmetric with the
  embedding pair; name facet is AND + case-insensitive; `kind` is
  display-only). A new `Extractor` port sits beside the `Embedder`
  port (no Pydantic AI — the repo's existing Pydantic v2 + httpx seam
  pattern). Dev/test endpoint: `http://localhost:8080/v1`
  (`qwen3.8-27b`, API key `dummy`).
- **4.4 Gate the recency term to temporal queries.** `entry_score`
  (`retrieval/scoring.py`) multiplies every hit by
  `0.5 ** (age_days / half_life_days)` — **unconditionally**, on every
  query. An always-on recency term is a known way to depress recall on
  non-temporal queries: an old, exactly-right entry loses to a recent,
  vaguely-related one even when the query carries no time sense at all.
  Measure it with the §1.1 harness (the golden set already has
  non-temporal queries), and gate or floor the term only if the numbers
  say so. A change here is a SPEC §6.4 change, so it lands with an ADR.
  *(Prior art: an external system measured this exact regression and
  moved to a gated, additive recency term; that is a hypothesis to test
  here, not a result to copy.)*
- **4.5 Record how `kind` and `importance` were chosen.** Both are
  writer-declared (SPEC §4.1) and neither records whether the value was
  supplied deliberately or fell out of a default. With a heterogeneous
  fleet, that makes "are agents using the three kinds consistently?" an
  unanswerable question — which is the question that would decide
  whether auto-classification is ever worth its cost. Add provenance at
  write time (caller-supplied vs. defaulted), surface it in the §3.3
  counters, and *then* decide. Additive columns, so it is the first real
  exercise of the §3.5 migration chain.

## Tier 5 — explicitly held: the §10 extensions (former Tier 4)

**Do not build any SPEC §10 extension yet** (passive capture, private
staging, namespaces, binary artifacts, knowledge graph, contradiction
detection, curation, human UI, multi-tenant SaaS, per-agent retrieval
tuning). SPEC §10 is explicit: build it when its trigger fires. With no
production usage, no trigger has fired.

**Trigger metrics (the instrumented version of §10).** Instead of
"when it feels noisy," define a *measurable* condition per extension
so the later decision is data-driven. Two kinds:

> **Scale ambition (updated).** The org's target has grown from the
> ~50 agents in ADR 0007 to **300+ users/agents across multiple
> fleets**. That still fits comfortably on one Postgres node + one API
> process (a few writes/sec at peak, index-backed reads) — so the
> *single-node decision* in ADR 0007 holds; what changes is the
> constant, not the decision. "Multiple fleets" is a logical partition
> (`fleet_id` + trust-level visibility, ADR 0011), not a new
> infrastructure. The infra question (Redis / multiple replicas) only
> opens when the **scale-out trigger** below fires — it is *not*
> triggered by 300 agents or by multiple fleets.

| §10 extension | Trigger (spec wording) | Measure to watch | Instrument |
|---|---|---|---|
| Knowledge graph | "entity linking pays off in retrieval" | hit@k gap on entity-linked queries vs. plain hybrid *(the facet slice is pre-staged and shipped by ADR 0016 / SPEC §13 — see Tier 4.3; only the graph half of this row remains held)* | §1.1 harness |
| Per-agent retrieval tuning | "static decay stops beating per-agent profiles" | per-agent MRR/AUC vs. static model | §1.1 harness |
| Passive capture | "agents forget to write" | high-value agent turns with no write; "should have remembered X" reports / wk | §3.3 counters |
| Private staging | "try before sharing" | fraction of `self`-scoped entries later promoted to the fleet *(the `self` scope is now the staging space — ADR 0011; promotion is the curation story)* | §3.3 counters |
| Namespaces / channels | "flat pool too noisy" | result precision on scope-unspecified queries; count of distinct fleets in use *(partially satisfied by ADR 0011: single-fleet needs are covered; multi-fleet membership is the remaining extension)* | §3.3 counters |
| Binary artifacts | "analyses outgrow file/URL refs" | count of `sources` with `type=file` or payload > threshold | §3.3 counters |
| Contradiction detection | "explicit supersession can't keep up" | count of same-topic, conflicting-value entry pairs *not* linked by a supersession chain | §3.3 counters |
| Curation workflow | "org wants a 'librarian'" | feedback (helpful/stale/wrong) accumulating without action; stale/wrong entries still in top-k | §3.3 counters + §1.1 harness |
| Human read-only UI | "analysts want to see the pool" | direct demand for human browsing (usage signal, not a metric) | qualitative |
| Multi-tenant SaaS / OAuth | "more than one org; an org has an IdP" | count of distinct orgs / tenants requested (count > 1) | §3.3 counters |
| Redis (cache / rate limits / queue) | "API scales to multiple replicas; org demands distributed rate limiting" | API replicas > 1, or distinct orgs > 1, or a cross-process per-agent rate-limit ceiling is needed *(in-process / Postgres-based rate limiting suffices at single-replica; a Redis queue for embedding is superseded by the Postgres-outbox design below)* | §3.3 counters + replica count |
| Async embedding pipeline | "embedding latency/throughput starts blocking writes" | p95 write latency; embedder failure/retry rate; backlog of unembedded entries *(the embedder already has a bounded retry budget for transient failures — ADR 0014, `HIVEMIND_EMBEDDING_RETRIES`, default 2 — so this trigger is about the **fallback** (persist without a vector + catch-up), not retries. Design: a Postgres **outbox** — insert entry with `embedding IS NULL` in the same transaction, then a `FOR UPDATE SKIP LOCKED` worker embeds + updates; a single source of truth, no second queue service. New ADR required: it changes write semantics — an entry is vector-searchable only after its vector lands)* | §3.3 counters + p95 write latency |

## If you do one thing

Tier 1 (the eval harness), Tier 2 (access control), and Tier 3
(productionize) are all shipped. The next keystone is **Tier 4 — close
the SPEC §11 open items**, starting with **4.1, the BM25-vs-FTS
decision**: run the §1.1 harness (and a BM25 variant) over the golden
set, see where the Postgres-FTS keyword stream ranks under, and adopt
BM25 with a new ADR only if the numbers say so. **4.2** (the
embedding-prefix tuning) uses the same harness on real long-form
entries. Tier 5 (the §10 extensions) stays held until its triggers fire
— the §1.1 harness + §3.3 counters are the instruments that will tell
you when.

## How to use this document

- This is a **living plan**, not a spec. When a Tier item is built, it
  produces real ADRs (e.g. "adopt BM25") — this doc just sequences the
  work.
- It links to SPEC.md / CONTEXT.md / docs/adr/ for behavior and
  rationale; it does not restate them.
- Revisit after §1.1 lands — the eval numbers will likely re-rank
  Tiers 3–5.