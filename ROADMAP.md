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
  Postgres advisory lock), which is now **shipped end to end**. **§3.7**
  is shipped too: 2 app-tier replicas per runner with an explicit
  rollout strategy and a PDB each, for **availability only** — ADR 0026,
  which supersedes ADR 0007. **§3.8** is shipped as well: an append-only
  audit log of admin-surface actions (ADR 0027, SPEC §12.5). The next
  workstream is **Tier 4**, whose two measurement items are now closed —
  §4.5 shipped `importance_source`, and §4.4 measured the recency term
  and **rejected** gating it (no code change; numbers in §4.4). The two
  SPEC §11 open items are now resolved as far as they can be without
  production data: **§4.1 (BM25 vs. FTS) is DEFERRED** with its reasons
  written down (it is a deployment-topology change, and RRF fuses
  *ranks*, so most of BM25's advantage never reaches the result), and
  **§4.2 is rescoped** — its "how much, in what unit" half is settled
  by ADR 0021 (a 2000-whitespace-word budget, and the knob now actually
  reaches the embedder), leaving "what goes into the embedded text"
  open and chunking explicitly out of scope. **§4.6** is now measured
  on both cheap levers: sweeping `rrf_k` with decay on does **not**
  recover what the recency term buries (`old_exact` stays at 0.000
  hit@5 at every `k`), because the whole `rrf_k` lever is bounded at
  ~5.3 half-lives of age — but **flooring the recency factor does**. A
  floor of 0.8 takes `old_exact` from 0.000 to 1.000 MRR while holding
  `currency_pair` at 0.778, above decay-off's 0.611: the first variant
  to beat both extremes on the slice each is weak at. **ADR 0022 then
  flipped that floor on by default** (`recency_floor = 0.8`, SPEC §6.4
  amended), which **closes §4.6 direction (a)** and therefore rules out
  (b); per-kind half-lives stays held. The §1.1 gate numbers are
  unchanged by it (the golden set is single-timestamp, so a lower bound
  on a recency factor of 1.0 is a no-op) and its thresholds were not
  re-pinned.

## The keystone is shipped: measure, then Tier 4

The retrieval **eval harness** (§1.1) — the single highest-leverage next
step — has **landed**: it de-risks v1's core (rankings are now measured,
not vibes), it is a reproducible CI gate against retrieval regressions,
and it is the same instrument that lets you *measure* the two
retrieval-quality triggers in SPEC §10 (knowledge graph, per-agent
tuning), turning those future extensions from "vibes" into
data-driven decisions.

With the keystone shipped and §3.5 (the migration chain) in place, the
next workstream is **Tier 4** — all of it measurable with the §1.1
harness instead of guesses. §4.5 is the first schema change to ride the
new chain. §4.4 is the first item the harness closed *against* a
proposed change: it needed a second, age-varied fixture beside the
golden set (the golden set's entries all share one timestamp, so it is
blind to decay), and the numbers rejected the gate it was written to
justify. §4.1 is the first item closed *without* running the harness at
all — the analysis (a custom Postgres image; RRF fusing ranks, not
scores) says what the experiment could pay for before it is worth
running, so it is **deferred with its reasons recorded** rather than
left as an open invitation to re-derive them.

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

## Tier 3 — productionize (former Tier 2)  *(3.1, 3.3–3.8 shipped; 3.2 superseded by 3.5)*

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
**kustomize** manifest tree (two Deployments — 1 replica each as
shipped here; **now 2 each, ADR 0026 / §3.7** — with
unauthenticated probe endpoints — ADR 0019; optional nginx + cert-manager
Ingress with SSE tuning), a **GitLab pipeline** (test on pgvector →
docker build/push → deploy via the pre-configured GitLab Kubernetes
agent, with an idempotent **first-run key bootstrap** that lands the
admin/org keys in the k8s Secret), environment-profile files
(`config/.env.example` + `ENVIRONMENT` selection — ADR 0017), and
`hivemind-keys revoke-admin` (admin-key rotation is now CLI-native).
ADR 0007's single-**Postgres-node** decision is unchanged by k8s hosting;
its *one-process* clause is not — **§3.7 / ADR 0026 supersedes it** with
2 app-tier replicas per runner for availability. The single-node ops
story stays in `docs/ops-runbook.md` (one source of truth per concern).

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
*(Done: **§3.7**, ADR 0026. The draining question resolved to nothing
new — the transport is stateless streamable-HTTP, ADR 0010, and the
`preStop` + `terminationGracePeriodSeconds` pair the manifests already
carry was written for exactly this.)*

### 3.6 Vector index on `entries.embedding`  *(shipped: ADR 0025, migration `0004`, SPEC §6.2)*
`search_vector` had no index: every vector query sequentially scanned the
whole table, computing a true cosine distance against a ~4KB embedding
per row (1024 dims — ADR 0015). Append-only entries (ADR 0001) mean that
scan only ever grows, and the replica topology §3.5 already assumes
multiplies it into concurrent full-table scans against one Postgres node
(ADR 0007). Shipped as an **HNSW** index (`vector_cosine_ops`, matching
the `<=>` operator; `m = 16`, `ef_construction = 64` — pgvector's
defaults, unchanged) built `CONCURRENTLY` under ADR 0020's
`-- transactional: false`, with `hnsw.iterative_scan = strict_order` and
`hnsw.ef_search = 40` applied per query via `SET LOCAL`. HNSW over
IVFFlat because IVFFlat trains on the rows and so cannot be built inside
a migration against the empty pool `0001` provisions; `strict_order`
because RRF fuses **ranks, not scores**.

Landing it surfaced a defect in ADR 0020 itself: `0004` is the first
migration to use decision 6 (`CREATE INDEX CONCURRENTLY`), and a
concurrent index build **deadlocks** against decision 3's
`pg_advisory_lock` — a sibling blocked inside `pg_advisory_lock` holds a
virtual xid for the whole wait, which the winner's build waits on
forever, and Postgres cannot detect it because the lock holder is idle.
`migrate` now polls `pg_try_advisory_lock` instead; same lock, same
session scope, short transactions.
`test_concurrent_migrators_are_serialised_by_the_advisory_lock` hung
indefinitely before the fix.

**This is exact → approximate, and it is not free.** The measurement
(`tests/integration/test_vector_index_recall.py`, the §1.1 golden set on
real Postgres) did **not** confirm parity: hit@5 / MRR / nDCG@5 are
1.000 exact against 0.625 / 0.563 / 0.579 at 2k distractors and
0.375 / 0.313 / 0.329 at 20k. Sweeping `ef_search` to 800 plateaus below
parity, so the loss is graph *reachability*, not candidate budget — the
knob was deliberately not tuned. ADR 0025 records the numbers, the
caveat (a uniform high-dimensional hash embedder with the relevant
entries as isolated outliers is close to the worst case for a graph
index, and real embeddings cluster), and the re-measure trigger: the
first real corpus at ≥30k entries, which is where Postgres starts
choosing the index unprompted. The §1.1 gate runs against `MemoryStore`,
has no index and no `<=>`, and was **not** re-pinned.

### 3.7 Two app-tier replicas  *(shipped: ADR 0026, SPEC §8.2, `deploy/kubernetes/`)*
The replica bump §3.5 deferred, and the supersession of ADR 0007 it
forced. Both Deployments go to **`replicas: 2`** with an explicit
`maxSurge: 1` / `maxUnavailable: 0` rollout and a **`minAvailable: 1`
PodDisruptionBudget** each (`hivemind-api-pdb.yaml`,
`hivemind-mcp-pdb.yaml`).

**Availability only.** Two replicas buy a zero-downtime rolling deploy
and a pod that survives a node drain — nothing else, and the ADR says
so in its own text so it cannot later be cited for more. *Throughput* is
rejected on the workload's shape: the app tier is I/O-bound (embedder,
extractor, Postgres — see the 123s worst-case write arithmetic on both
Deployments), so a second replica adds pressure to three shared
bottlenecks and capacity to none. *AZ failure* is rejected because it
cannot be cashed: one Postgres node means an AZ failure takes the
database whatever the app tier does. *Three replicas* is rejected as
protection against a second simultaneous failure that the
availability-only framing does not ask for.

The PDB is the load-bearing part: `maxUnavailable` on a Deployment
governs **rollouts**, not voluntary disruption — `kubectl drain`
consults the PDB and nothing else. The explicit strategy is written
down because k8s' 25% defaults only coincide with it at `replicas: 2`.

Nothing in the app tier had to change: the REST surface holds no
session state, the MCP transport is stateless streamable-HTTP with
per-request credentials (ADR 0010), concurrent startup is already
serialised by ADR 0020's advisory lock, and ADR 0025's HNSW index had
just removed the per-replica sequential-scan pressure this would
otherwise have multiplied. The connection footprint doubles but is
bounded and tunable (`HIVEMIND_POOL_MIN_SIZE`/`_MAX_SIZE` behind a
transaction-mode PgBouncer — `docs/ops-runbook.md`).

**Redis is still not bought.** Its §10 trigger reads "API replicas > 1,
or …" and that clause is now literally satisfied — and it still buys
nothing, because there is no distributed rate limiting, no fan-out and
no distributed lock outside Postgres. That trigger row wants rewriting
around the *rate-limit ceiling*, not the replica count.

### 3.8 Audit log of admin actions  *(shipped: ADR 0027, SPEC §12.5, migration `0005`)*
"Who promoted this agent, and when?" had no answer: the admin surface
mutated agents, fleets and keys and left nothing behind. Migration
`0005.audit-log` adds an insert-only `audit_log` table (no foreign
keys — a revoked agent's history outlives its record), written from
both admin write paths and read through `GET /v1/admin/audit-log`
(admin key; no MCP verb, same reasoning as `/v1/metrics`).

**Two actor kinds, because the two paths know different things.** The
REST admin surface records `admin_key` rows whose actor is the verified
admin key's **fingerprint** — which needed a fix first: every admin key
carried `user_id = "admin"`, so `Credential` gained `key_id`. The
`hivemind-keys` CLI records `cli` rows in the same transaction as its
change, under an operator-typed `--actor` that is **unverified** (anyone
with database access can type anything) — the kind says so.

**Honest limits.** The REST path spans two ports with no shared
transaction, so its row is written after the change and is **not**
atomic with it: an audit-write failure fails the request (the change
stands), and a crash in between leaves the change unaudited.
Registration and the read-only listings are deliberately not audited.
No raw key is ever recorded, on either path — enforced by construction
and by a test that performs every key-producing action and searches
every audit row for the keys.

## Tier 4 — close the spec's open items (SPEC §11) (former Tier 3)

*(4.1–4.2 are the §11 open items. 4.3–4.5 are measurement commitments
that live here because they share the §1.1 harness, not because they
are §11 items — each says so.)*

- **4.1 BM25 vs. Postgres FTS.** *(**DEFERRED** — the analysis below is
  the reason; do not re-derive it. Deferred, not dismissed: the thing
  BM25 would buy is real and named at the end.)* The store uses Postgres
  FTS (`to_tsvector` + `ts_rank`). Two findings moved this off the
  "measure it next" list:

  **1. It is a deployment-topology change, not a query change.**
  Postgres has no native BM25. Getting it means an *extension* —
  ParadeDB `pg_search` or VectorChord-BM25 — and neither ships in the
  stock `pgvector/pgvector:pg16` image this deployment runs (ADR 0007,
  `docker-compose.yaml`, `.gitlab-ci.yml`, the k8s manifests). Adopting
  one means building and maintaining a custom Postgres image and
  mirroring it into the air-gapped network the org deploys into. That
  is a different class of decision from "tune a ranker", and it has to
  be priced as one.

  **2. RRF consumes ranks, not scores — so most of BM25's advantage is
  discarded at the fusion seam.** `retrieval/rrf.py` computes
  `w / (k + rank)`; the keyword stream's *scores* never reach the fused
  result. BM25's headline advantages — calibrated magnitudes, IDF
  weighting, document-length normalisation — survive fusion **only**
  insofar as they change the **top-20 ordering** (`candidate_top_k`)
  that `ts_rank` already produces. The upside is bounded by "does BM25
  order the first 20 keyword hits better", not by "is BM25 a better
  scorer", and the honest version of this experiment measures exactly
  that.

  **What BM25 would actually buy, and why this is deferred rather than
  closed:** `ts_rank` has **no IDF at all** — it weights a rare,
  discriminating term the same as a common one. On an org-wide pool
  where nearly every entry says "service", "deploy" or "agent", that is
  a real ranking defect, and it is the one BM25 fixes. Revisit when
  keyword-stream misses are an observed retrieval problem on a real
  corpus (not a synthetic one), and price the custom image in the same
  decision. Note the coupling recorded in §4.6: `rrf_k` controls how
  much the *weaker* ranker's mistakes cost, and `ts_rank` is the weaker
  ranker.
- **4.2 What goes into the embedded text.** *(rescoped — this item was
  one knob standing in for three separable questions)*

  **(a) WHAT is embedded — still open.** Today: `summary` + a bounded
  `body` prefix (SPEC §7). Not included: `tags`, `kind`, the extracted
  entity names (ADR 0016). Whether adding any of them helps or just
  adds noise to one shared vector is unmeasured, and it is the part of
  this item that still needs the §1.1 harness on real long-form
  entries.

  **(b) HOW MUCH, and in what unit — settled by ADR 0021.** The budget
  is **2000 whitespace-delimited words** (`EMBEDDING_PREFIX_TOKENS`),
  replacing the 2048-character bound. The unit question is closed; the
  *value* stays tunable as ordinary config.

  **Why this could not have been measured before:** the knob was
  **inert on the embedding side**. `OpenAICompatEmbedder` never took a
  prefix parameter and always used the function default, so
  `HIVEMIND_EMBEDDING_PREFIX_CHARS` moved entity extraction and changed
  the embedding not at all. Any sweep of it would have measured a
  constant. ADR 0021 fixed that and pinned the ADR 0016 lockstep with a
  test; *this* is what makes the (a) experiment runnable at all.

  **(c) CHUNKING — explicitly out of scope for this item.** SPEC §4.1
  commits to **one embedding per entry**. Chunking means several
  vectors per entry plus a retrieval change (which chunk's score
  represents the entry, how duplicates collapse before fusion, what a
  `hit` even points at). That is a different decision with its own ADR,
  not a tuning pass — do not fold it in here.

  (The embedding *dimension* is a separate deploy-time decision: the
  default is 1024, ADR 0015.)
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
- **4.4 Gate the recency term to temporal queries.** *(measured —
  **gating rejected, no code change**; fixture + runner:
  `tests/eval/temporal.py`, `tests/eval/test_decay_experiment.py`)*
  `entry_score` (`retrieval/scoring.py`) multiplies every hit by
  `0.5 ** (age_days / half_life_days)` — **unconditionally**, on every
  query. An always-on recency term is a known way to depress recall on
  non-temporal queries: an old, exactly-right entry loses to a recent,
  vaguely-related one even when the query carries no time sense at all.
  *(Prior art: an external system measured this exact regression and
  moved to a gated, additive recency term; that is a hypothesis to test
  here, not a result to copy.)*

  **The golden set could not answer this.** Every §1.1 golden entry is
  seeded with no `occurred_at`, so all eight share one timestamp, the
  recency factor is identical for every candidate and cancels out of the
  ranking. Measuring decay needed a second, age-varied fixture
  (`tests/eval/temporal.py`: 16 entries aged 2–500 days, 10 queries each
  labelled temporal / non-temporal). The pinned §1.1 gate is untouched.

  **Measured** (k=5; variants realised through `SearchConfig.half_life_days`
  only, so the fused RRF score is identical across all three and every
  difference is attributable to the recency factor):

  | variant | non-temporal hit@5 / MRR / nDCG@5 | temporal hit@5 / MRR / nDCG@5 | all |
  |---|---|---|---|
  | **A** always-on (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | 0.500 / 0.375 / 0.406 |
  | **B** off | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | 1.000 / 0.833 / 0.876 |
  | **C** gated (oracle label) | 1.000 / 0.917 / 0.938 | 0.750 / 0.625 / 0.658 | 0.900 / 0.800 / 0.826 |

  The hypothesis is **confirmed** (A's non-temporal MRR 0.208 vs. 0.917
  without the term) but the **proposed fix is refuted**: C loses to B on
  every aggregate (all-query MRR 0.800 vs. 0.833, hit@5 0.900 vs. 1.000),
  and C's numbers are an *oracle* ceiling — they use the fixture's
  hand-written label, not a classifier the service has. Sliced by
  competition pattern instead of by query time sense: on "old entry
  matches almost verbatim, recent entry shares a phrase" A scores
  **0.000** hit@5 and B **1.000**, for the temporal and the non-temporal
  query alike — so time sense is not the axis that predicts where the
  term helps, and gating on it only narrows where the failure shows up.

  **Root cause, and it is not fixture-dependent.** With the SPEC §6.2
  defaults (`rrf_k=60`, `w=0.5/0.5`, `candidate_top_k=20`) the fused
  score spans at most 2.62x across a candidate list (1.31x when both
  candidates appear in both streams). The recency factor spans 2x *per
  half-life*. So an entry ~42 days older than a competitor is outranked
  by it no matter how much better it matches — 12 days when both are in
  both streams. Multiplicative decay against RRF's compressed range is
  not a tie-break, it is the sort key.

  **Resolved: no change to `scoring.py`.** Gating is rejected on the
  numbers — that conclusion stands and is not reopened here. The
  residual finding, that the *form* of §6.4's recency term is mismatched
  to RRF's range independent of gating, is carried forward as its own
  tracked item: **§4.6**.

  *(The table above is a point-in-time measurement, not a pinned gate —
  it reflects the repo as of commit `f65a4e0` and can drift if the eval
  fixture or scoring config changes without a re-run.)*
- **4.5 Record how `importance` was chosen.** *(shipped: `importance_source`
  — `entries.importance_source text NOT NULL DEFAULT 'default'`, migration
  `0002.importance-source`, SPEC §4.1)* `importance` is writer-declared
  (SPEC §4.1) and previously recorded nothing about whether the value was
  supplied deliberately or fell out of a default — a dead ranking input
  (SPEC §6.4) is indistinguishable from a used one. Provenance is now
  recorded at write time (`caller` vs. `default`) at every write seam
  (REST + MCP) and surfaced in the §3.3 counters
  (`EntriesMetrics.by_importance_source`). **`kind` needs no such
  provenance**: it is a required parameter at every seam (`EntryDraft.kind`,
  `CreateEntryRequest.kind`, `hive_write(kind: str)`), so it is always
  caller-supplied and a provenance field for it would be a constant.
  The question this was meant to unblock — "are agents using the three
  kinds consistently?" — is answered instead by a **per-author `kind`
  distribution**, now *shipped* as `by_author_kind` on `GET /v1/metrics`
  (`EntriesMetrics.by_author_kind`). It is keyed by the registered agent
  roster rather than by a `DISTINCT` over `entries.author`, so an agent
  who has written nothing is representable and the query count stays
  bounded by the roster instead of by the pool. Building it surfaced a
  latent defect it depended on: `EntryFilters.matches` compares `kind` by
  identity, so a draft carrying the raw string `"fact"` was invisible to
  a `kind=` filter — silently under-counting the existing `by_kind` too
  (fixed in `EntryDraft.__post_init__`).
- **4.6 The *form* of the recency term is mismatched to RRF's range.**
  *(**direction (a) is RESOLVED and SHIPPED — ADR 0022**: the recency
  factor is floored, `SearchConfig.recency_floor = 0.8` by default, and
  SPEC §6.4 now carries the floored formula. That **rules out direction
  (b)**, the additive form — per this section the two are competing
  answers to the same defect and must not be stacked; reopening (b)
  means *replacing* the floor under a new ADR. **Per-kind half-lives
  stays HELD**: orthogonal, not a fix for this finding. The `rrf_k`
  sweep came back negative and changed no default. What remains open
  here is the *value* 0.8, not the form — see the re-measure trigger at
  the end of this item.)* §4.4 measured
  and rejected *gating* the recency term. Underneath that result sits a
  separate, arithmetic mismatch that gating would not have fixed either
  way: with the SPEC §6.2 defaults the fused RRF score spans at most
  **2.6230x** across a candidate list, while `entry_score`'s
  `0.5 ** (age_days / half_life_days)` (SPEC §6.4) spans **2x per
  30-day half-life** — so **~41.7 days** of age difference (**11.7
  days** when both candidates appear in both retrieval streams)
  outranks any match-quality difference, however large. This is
  arithmetic on the two formulas, not a property of the §4.4 fixture —
  it holds for any candidate list, synthetic or real, at the §6.2
  defaults.

  **Measure `rrf_k` FIRST — ahead of the floor and the additive form.**
  The mismatch has *two* sides, and everything above only ever looked at
  the recency side. `rrf_k=60` is the side doing the **compressing**.
  With the SPEC §6.2 defaults, the fused score of the best possible
  candidate (rank 1 in **both** streams, `w=0.5/0.5`) is `1/(k+1)` and
  the worst retained candidate (rank 20 in **one** stream,
  `candidate_top_k=20`) is `0.5/(k+20)`, so the whole fused range is

      2(k + 20) / (k + 1)

  | `rrf_k` | fused range | = half-lives of age | = days at a 30-day half-life |
  |---|---|---|---|
  | 60 (today) | 2.62x | 1.39 | **41.7** |
  | 20 | 3.81x | 1.93 | 57.9 |
  | 10 | 5.45x | 2.45 | 73.4 |
  | 5 | 8.33x | 3.06 | 91.8 |

  Lowering `k` widens the fused range, and every doubling of that range
  buys exactly one more half-life before age outranks match quality.
  It is a **pure `SearchConfig` value**: no SPEC change, no ADR, no new
  code, reversible in one line — where (a) and (b) are both §6.4
  redesigns. Measuring the free knob before the expensive ones is the
  order this item is now written in; the measurement itself is §4.6's
  table below.

  **The coupling, which nobody had noted:** a *lower* `k` does not
  only help. RRF's `w/(k+rank)` gets steeper as `k` shrinks, so a low
  `k` **amplifies whichever stream orders badly** — it puts more of the
  fused score on that stream's top one or two positions. Per §4.1,
  `ts_rank` is the weaker ranker (no IDF at all: a rare discriminating
  term is weighted like a common one). So `rrf_k` and the BM25 question
  are **coupled**: the case for a low `k` is strongest when both
  streams order well, and lowering `k` raises the price of
  `ts_rank`'s mistakes. A `k` chosen on today's keyword stream should
  be re-checked if §4.1 ever lands.

  Two further directions were identified, neither measured yet:
  **(a)** bound the term hard enough that it can no longer dominate the
  fused range — which, at that tightness, is close to removing it — or
  **(b)** move to the additive form the prior art (§4.4) used, so
  recency competes on the same scale as the fused score instead of
  multiplying it. Landing either is a SPEC §6.4 redesign, not a scoring
  tweak, and gets its own ADR, not a quiet edit of `scoring.py`.

  **These are NOT a sequence — do not work them in order.** `rrf_k` is
  a config change and comes first because it is free and reversible.
  **(a) and (b) are competing forms of the same fix: pick one, never
  both** — a floored multiplicative term and an additive term are two
  answers to "stop recency being the sort key", and stacking them just
  makes the term untunable. *Per-kind half-lives* (a `fact` and a
  `decision` decaying at different rates) is a third, **orthogonal**
  idea — it changes which entries decay, not how decay competes with
  match quality — and it **stays held**: it is not a fix for this
  finding and would only add a knob on top of an already-mismatched
  form.

  **`rrf_k` MEASURED — and the answer is no.** *(fixture +
  runner: `tests/eval/temporal.py`, `tests/eval/temporal_runner.py`,
  `tests/eval/test_rrf_k_experiment.py`; `SearchConfig.rrf_k` is
  **unchanged** at 60 and the §1.1 gate is untouched)* The sweep runs
  `rrf_k` over {60, 20, 10, 5} with **decay ON** (§4.4's variant A) —
  everything else held fixed and shared with the §4.4 experiment:

  | `rrf_k` | non-temporal hit@5 / MRR / nDCG@5 | temporal | `old_exact` | all |
  |---|---|---|---|---|
  | **60** (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.500 / 0.375 / 0.406 |
  | **20** | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.395 / 0.445 |
  | **10** | 0.500 / 0.264 / 0.322 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.408 / 0.456 |
  | **5** | 0.500 / 0.292 / 0.344 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.425 / 0.469 |
  | *(60, decay off — §4.4's B, for reference)* | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | *1.000 / 1.000 / 1.000* | 1.000 / 0.833 / 0.876 |

  **The question was: does lowering `rrf_k` recover the recall the
  unconditional recency term destroys, without a change to
  `scoring.py`? It does not.** The `old_exact` slice — the pattern
  §4.4 identified as the real failure mode — stays at **0.000 hit@5 at
  every `k`**, while decay-off answers both of those queries perfectly.
  The relevant entries never enter the top 5 at any `k` in the sweep.

  **And that is arithmetic, not a fixture artifact.** `2(k+20)/(k+1)`
  is bounded above by **40x** (its limit as `k` -> 0), so the *entire*
  `rrf_k` lever — from today's 60 all the way down to a degenerate `k`
  nobody would ship — is worth at most **log2(40) = 5.3 half-lives**,
  ~160 days at the 30-day default. The `old_exact` entries are 420 and
  380 days old: **14 and 12.7 half-lives**. No value of `rrf_k` closes
  a gap that size, for this corpus or any other. **`rrf_k` is not a
  cheap substitute for fixing the form of the term** — this is the
  single most useful thing the sweep establishes, and it is the reason
  §4.6 still needs (a) or (b).

  **What `rrf_k` *does* buy here** (so the verdict is not overstated):
  all-query MRR rises monotonically as `k` falls (0.375 -> 0.395 ->
  0.408 -> 0.425) and non-temporal hit@5 improves 0.333 -> 0.500 at
  `k`<=20. The gain is entirely on the non-temporal / timeless side;
  the `temporal` and `currency_pair` slices are **bit-identical across
  the whole sweep**, so nothing is being traded away for it on this
  fixture.

  **Verdict on `k=10` as a default: NOT SUPPORTED by this evidence —
  and the evidence is too weak to support any new default.** The
  direction is mildly favourable and the sweep shows no downside here,
  but the differences are a handful of rank positions over 10
  hand-written queries scored by a **4-dimension hash embedder**
  (`tests/fakes.FakeEmbedder`) — which is exactly the evidence §4.4
  refused to land a change on, and the numbers do not separate `k=10`
  from `k=5` or `k=20` in any case. What the fixture *can* establish
  (the bound above) argues the opposite of the hypothesis that
  motivated the sweep. **`SearchConfig.rrf_k` stays at 60**; changing
  it is a decision on real-corpus evidence, not a consequence of this
  table.

  **(a) THE FLOOR, MEASURED — and this one clears the bar.** *(seam
  shipped **default-off**: `entry_score(..., recency_floor=...)` +
  `SearchConfig.recency_floor: float | None = None`, validated to
  `(0, 1]`, threaded through `SearchService`. The **default is
  unchanged** — `None` is bit-for-bit today's behaviour, asserted at
  both the pure-function and the eval seam. Sweep:
  `tests/eval/test_decay_experiment.py::TestRecencyFloorSweep`; the
  §1.1 gate and `tests/eval/golden.py` are untouched.)*

  Where `rrf_k` widens the *fused* range against an unbounded recency
  factor, a floor **bounds the unbounded factor itself**:
  `recency = max(floor, 0.5 ** (age/half_life))`, so the recency range
  collapses from `2 ** (age spread / half_life)` to exactly `1/floor`.
  That has no analogue of the 40x cap. The threshold is derivable from
  the pipeline's own constants *in advance*: match quality outranks age
  once `1/floor` is narrower than the fused range, i.e.
  **`floor > 1/2.6230 = 0.381`** at `rrf_k=60`.

  Measured at `rrf_k=60`, decay ON, k=5 (A and B repeated from §4.4 as
  the two limits of the same family — `floor -> 0` is A, `floor = 1`
  is B):

  | variant | non-temporal hit@5 / MRR / nDCG@5 | temporal | `old_exact` | `currency_pair` | `timeless` | all |
  |---|---|---|---|---|---|---|
  | **A** always-on (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 1.000 / **0.833** / 0.877 | 0.400 / 0.250 / 0.286 | 0.500 / 0.375 / 0.406 |
  | **B** decay off | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | 1.000 / 1.000 / 1.000 | 1.000 / **0.611** / 0.710 | 1.000 / 0.900 / 0.926 | 1.000 / 0.833 / 0.876 |
  | **D** floor 0.2 | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | 0.500 / 0.100 / 0.193 | 1.000 / 0.833 / 0.877 | 0.400 / 0.250 / 0.286 | 0.600 / 0.395 / 0.445 |
  | **D** floor 0.381 | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | 0.500 / 0.100 / 0.193 | 1.000 / 0.833 / 0.877 | 0.400 / 0.250 / 0.286 | 0.600 / 0.395 / 0.445 |
  | **D** floor 0.5 | 1.000 / 0.556 / 0.665 | 1.000 / 0.708 / 0.783 | 1.000 / 0.500 / 0.631 | 1.000 / 0.778 / 0.833 | 1.000 / 0.567 / 0.672 | 1.000 / 0.617 / 0.712 |
  | **D** floor 0.7 | 1.000 / 0.556 / 0.665 | 1.000 / 0.833 / 0.875 | 1.000 / 0.750 / 0.815 | 1.000 / 0.778 / 0.833 | 1.000 / 0.567 / 0.672 | 1.000 / 0.667 / 0.749 |
  | **D** floor **0.8** | 1.000 / 0.653 / 0.738 | 1.000 / 0.833 / 0.875 | **1.000 / 1.000 / 1.000** | 1.000 / **0.778** / 0.833 | 1.000 / 0.583 / 0.686 | 1.000 / 0.725 / 0.793 |
  | **D** floor 0.9 | 1.000 / 0.833 / 0.877 | 1.000 / 0.688 / 0.765 | 1.000 / 1.000 / 1.000 | 1.000 / **0.583** / 0.687 | 1.000 / 0.800 / 0.852 | 1.000 / 0.775 / 0.832 |

  **The discriminating question — does a floor recover the `old_exact`
  cases A scores 0.000 on, *while keeping* the `currency_pair` cases B
  loses? Yes, and 0.8 is the value.** At floor 0.8 `old_exact` goes
  0.000 -> **1.000** MRR (the relevant entry is rank 1 for both
  queries, matching B exactly) while `currency_pair` holds at
  **0.778** — above B's 0.611, below A's 0.833. Floors 0.5 and 0.7
  clear the same bar less completely (`old_exact` MRR 0.500 / 0.750,
  same 0.778 on currency pairs). **This is the first variant anything
  in §4.4 or §4.6 has produced that beats *both* extremes on the slice
  each is weak at** — gating (C) did not, and no `rrf_k` did.

  **A floor is a band, not a slider.** At 0.9 the recency range is
  1.11x — narrower than the fused spread of almost any pair — so the
  variant is nearly B again and pays B's price: `currency_pair` MRR
  drops to 0.583, *below* decay-off's 0.611. Push the floor to 1.0 and
  it **is** B. That non-monotonicity is why this reports a value, not
  a direction.

  **The arithmetic predicted this before the queries ran, and the
  measurement confirms the prediction's shape.** Below the derived
  0.381 threshold (floors 0.2 and 0.381) `old_exact` MRR stays at
  0.100 — the entry surfaces at rank 5 at best. The recovery begins
  just above it and completes by 0.8. The threshold is a *lower bound*
  on where the effect can start, not where it finishes: two candidates
  that both appear in **both** streams span far less than the
  best-vs-worst 2.62x, so flipping those needs a tighter floor. 0.381
  predicts the onset; 0.8 is where the fixture says it is done.

  **Why this evidence is stronger than the `rrf_k` table** (it is the
  same 10 hand-written queries over 16 entries scored by the same
  4-dimension hash embedder, and that has not changed): A, B and D
  differ **only** by a bounded rebalance of the *same* fused scores
  from the *same* embedder — no stream is re-ranked, no candidate set
  moves, the fused values are bit-identical — and the direction and
  approximate threshold were derived from the two formulas *before*
  the sweep. What the fixture still **cannot** establish: that 0.8 is
  the optimum on a real corpus (the band 0.5–0.8 is barely separated
  here, and `timeless`/`non_temporal` MRR keeps rising past 0.8 while
  `currency_pair` falls, so the optimum is a *trade*, not a peak the
  4-dim embedder can locate); that real queries distribute over these
  competition patterns the way this fixture does; or that a real
  embedder's vector stream would place the same candidates in the same
  streams at all. It establishes **a mechanism that works and the
  sign of its effect**, not a tuned constant.

  **Verdict: SHIPPED — ADR 0022 flipped the default to `0.8`.** The
  seam landed behaviour-preserving in `b61db0f`; the decision to turn
  it on was taken on these numbers by a human and is recorded in
  **[ADR 0022](docs/adr/0022-recency-floor-so-match-quality-is-the-sort-key.md)**,
  which amends SPEC §6.4. Three things that ADR insists on and this
  table should not be read without: the floor is a **band, not a
  slider** (operators must not tune it up — 0.9 measures *worse* than
  decay-off on `currency_pair`); it is **not free** (`currency_pair`
  MRR 0.833 → 0.778, traded for `old_exact` 0.000 → 1.000); and on the
  all-query aggregate decay-off still wins on this fixture (0.833 vs.
  0.725) — the floor is chosen to keep the currency slice, not to win
  that average. Landing (a) **rules out (b)**: per this section, (a)
  and (b) are competing forms of the same fix and must not be stacked.

  **Still open in (a): the value, not the form.** 0.8 comes from 16
  synthetic entries and 10 queries scored by a 4-dimension hash
  embedder. The *arithmetic* (`1/floor` against the 2.6230x fused
  range) is fixture-independent; the constant is not. **Re-measure
  trigger:** the first real corpus with meaningful age spread — run the
  §1.1 harness against real queries on an aged production pool and
  sweep the 0.5–0.9 band. Expect to move the value, not the form.

  The §1.1 gate was **not** re-pinned for this: every golden entry is
  seeded without `occurred_at`, so all eight share one timestamp, every
  recency factor is 1.0 and a lower bound on 1.0 is a no-op. The gate's
  report is bit-identical with and without the floor, asserted in
  `test_eval_gate.py::...::test_the_shipped_recency_floor_cannot_move_this_gate`.

  **Trigger:** for **(b)** and for any `rrf_k` default change,
  unchanged — the first real corpus with meaningful age spread, i.e.
  run the §1.1 harness against real queries on an aged pool once there
  is production usage. Not before: 10 synthetic queries scored by a
  4-dimension hash embedder is exactly the wrong evidence to land a
  scoring change on (the same reasoning §4.4 closed on). What *is*
  settled without that trigger is the bound: `rrf_k` cannot substitute
  for (a) or (b), so the cheap knob is now measured and out of the
  way. **(a) is closed (ADR 0022)**, so its trigger no longer gates
  anything: the floor is on at 0.8 and the real corpus is what should
  *re-check* that number within the 0.5–0.9 band, not what unblocks the
  idea. Note that (b) is now ruled out by (a) having landed — its
  trigger firing means reconsidering the floor itself, under a new ADR,
  not adding an additive term on top of it.

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
> fleets**. That still fits comfortably on one Postgres node + a small,
> fixed app tier (a few writes/sec at peak, index-backed reads) — so the
> *single-Postgres-node decision* in ADR 0007 holds; what changes is the
> constant, not the decision. "Multiple fleets" is a logical partition
> (`fleet_id` + trust-level visibility, ADR 0011), not a new
> infrastructure. The infra question (Redis / scaling *for load*) only
> opens when the **scale-out trigger** below fires — it is *not*
> triggered by 300 agents or by multiple fleets, and it is *not*
> triggered by ADR 0026's replica count, which is an availability
> decision that explicitly rejects throughput as a motive.

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
| Redis (cache / rate limits / queue) | "API scales to multiple replicas; org demands distributed rate limiting" | API replicas > 1, or distinct orgs > 1, or a cross-process per-agent rate-limit ceiling is needed *(the replica clause is now satisfied — 2 replicas per runner, ADR 0026 — and still buys nothing: there is no distributed rate limiting, no fan-out, and the only cross-process lock is in Postgres (ADR 0020). The live half of this trigger is the **rate-limit ceiling**, not the replica count; a Redis queue for embedding is superseded by the Postgres-outbox design below)* | §3.3 counters + replica count |
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