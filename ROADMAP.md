# ROADMAP.md — Hivemind (what is open, what is held)

> **What this document is:** the working plan. It is **not** the spec
> (SPEC.md owns behavior) and **not** the decision log (docs/adr/ owns
> reasons). It sequences work and records what was deliberately left
> open. Section numbers are stable because code and ADRs cite them.

## Where we are

Tiers 1–3 are shipped: the retrieval eval harness, the fleet and trust
model, and the production work (deployment, migrations, vector index,
replicas, audit log, admin panel, agent plugin, the 2.0.0 security
review, and the 2.1.0–2.3.0 retrieval and knowledge-sharing features).
Tier 4's measurement items are closed or deferred with their reasons.
The SPEC §10 extensions (Tier 5) stay held until their triggers fire.

## What is open

- **§3.13 follow-ups**: rate limiting in the app (today only at the
  ingress), unverified owner aliases, structural markers on returned
  entries, the remaining performance items, a contract step for
  `credentials_kind_check`, and the next harnesses.
- **§4.2 (a)**: what goes into the embedded text (tags, kind, entity
  names), to be measured with the eval harness on real long-form entries.
- **Re-measure triggers** on the first real corpus: the recency floor's
  value (§4.6), the HNSW recall cost (§3.6, ADR 0025), and any `rrf_k`
  change.
- **Parked ideas**: a `question` entry kind (wait for the empty-search
  counters of ADR 0056 to show demand) and per-kind half-lives (held,
  §4.6). The legacy `pi-mcp-adapter` route in the Pi plugin is to be
  dropped once Pi's built-in MCP (0.99+) is the norm.
- **4.1 BM25** stays deferred; revisit only on observed keyword misses.

## Tier 1 — validate the core  *(shipped)*

### 1.1 Retrieval eval harness + golden set  *(shipped: `tests/eval/`, `retrieval/eval.py`)*
A deterministic entry set and a golden query set, run through
`SearchService.search`, reporting hit@k, MRR and nDCG, with a CI gate
that pins a floor on them (at landing: hit@5 = 1.0, MRR = 0.75,
nDCG@5 = 0.8155). It turns the config knobs (`rrf_k`, the stream
weights, `candidate_top_k`, `half_life_days`, the `quality_*` weights)
into measured dials, and it is the instrument for the retrieval-quality
triggers in Tier 5.

### 1.2 End-to-end dogfood with a real agent + real embedder  *(shipped)*
A real agent ran the full loop (write, search, feedback, supersede) over
live Postgres and the local 512-dim vLLM embedder on 2026-09-20. It
caught one real defect, a forced `scope="org"` default that rejected
every L1/L2 write with an omitted scope, fixed at all three seams.
Friction is logged in `docs/dogfooding-notes.md`.

## Tier 2 — access control & fleet model (ADRs 0011–0012, SPEC §12)  *(shipped)*

### 2.1 Domain + store: fleets, agents, trust levels  *(shipped)*
`fleets` and `agents` tables, entry scopes `self` / `fleet` (legacy `org`
read-only), visibility-aware reads, omitted-scope resolution.

### 2.2 Registration + admin surface  *(shipped)*
`hive_register` / `POST /v1/agents` and the admin endpoint set; the agent
key is shown once, at activation.

### 2.3 Runners + credential migration  *(shipped)*
The MCP runners present agent keys; `hivemind-keys` is reduced to admin
keys, agent-key issue and org-key rotation.

## Tier 3 — productionize  *(shipped; 3.2 superseded by 3.5)*

### 3.1 Ops runbook  *(shipped: `docs/ops-runbook.md`)*
Deployment, backups, monitoring and key rotation for a single node.

### 3.2 Forward-migration path  *(superseded by §3.5: ADR 0013 → ADR 0020)*
An idempotent re-apply of the schema, which could not change existing
objects, had no backward direction and assumed one replica.

### 3.3 Usage counters (trigger instrumentation)  *(shipped: `MetricsService` + `GET /v1/metrics`; Prometheus `GET /metrics`, ADR 0050)*
Entries, fleets, agents (trust distribution, writes per fleet, pending
count), importance source and per-author kind distribution, and searches
and empty searches per fleet (ADR 0056). They make the usage-based Tier 5
triggers measurable.

### 3.4 Kubernetes + GitLab CI/CD deployment story  *(shipped: `DEPLOY.md`, `deploy/kubernetes/`, `.gitlab-ci.yml`, `config/`)*
One image with the runner chosen by `HIVEMIND_RUNNER` and the
entrypoint owning migration (ADR 0018), kustomize manifests with probe
endpoints (ADR 0019), a GitLab pipeline with a first-run key bootstrap
(ADR 0044), environment profiles (ADR 0017) and `revoke-admin`.

### 3.5 Versioned migrations with rollback  *(shipped: ADR 0020, SPEC §8.6)*
An ordered yoyo chain with a `.rollback.sql` per step under a Postgres
advisory lock, a generated `schema.sql`, `startupProbe`s, and CI gates for
checksum immutability, reference currency and expand-and-contract against
previous releases.

### 3.6 Vector index on `entries.embedding`  *(shipped: ADR 0025, migration `0004`)*
HNSW (`vector_cosine_ops`, `m = 16`, `ef_construction = 64`) built
`CONCURRENTLY`, with `iterative_scan = strict_order` and `ef_search = 40`
set per query. It is approximate: the Postgres measurement gave hit@5
1.000 exact vs. 0.625 at 2k distractors and 0.375 at 20k, and raising
`ef_search` plateaus below parity, so nothing was tuned to hide it.
Re-measure on the first real corpus of ≥30k entries. Landing it exposed a
deadlock between `CONCURRENTLY` and a blocking advisory lock; `migrate`
now polls `pg_try_advisory_lock`.

### 3.7 Two app-tier replicas  *(shipped: ADR 0026, supersedes ADR 0007)*
Two replicas per runner with `maxSurge: 1` / `maxUnavailable: 0` and a
`minAvailable: 1` PodDisruptionBudget, for availability only (rolling
deploys and node drains), not throughput or AZ tolerance. One Postgres
node, no Redis.

### 3.8 Audit log of admin actions  *(shipped: ADR 0027, SPEC §12.5, migration `0005`)*
An insert-only audit log written by the REST admin surface (actor: the
admin key's fingerprint) and the CLI (an unverified `--actor`), read at
`GET /v1/admin/audit-log`. Registrations are audited since ADR 0046.

### 3.9 Admin panel  *(shipped: ADRs 0028–0029, migration `0006`, DEPLOY.md §8)*
A browser front end for the admin surface, with a pending-agent queue.
It needed the `revoked` agent status first (ADR 0028). The admin key
lives in the tab's `sessionStorage` behind a strict CSP; keep the panel
on an internal ingress.

### 3.10 Agent plugin and skills  *(shipped: ADRs 0030–0031, `plugins/hivemind/`)*
The plugin and skills that make Hivemind an agent's long-term memory, now
for Claude Code, Codex, DeepSeek Harness, Hermes, Pi, Oh My Pi and
OpenCode, with setup notes for Gemini CLI. It needed `hive_whoami`
(ADR 0030) and the one-key-per-request correction (ADR 0031).

### 3.11 Server-enforced access on every entry path  *(shipped: ADR 0033)*
Reads by id, the supersession chain, feedback and supersession follow
readability; provenance comes from the key on both surfaces.

### 3.12 Incognito sessions and foreign entries  *(shipped in 1.1.0: ADRs 0035–0036)*
A client-side off switch with marked local notes, and "use, don't relay"
for privileged agents. **Held:** a per-fleet "sensitive" flag, if the
conservative default proves too loose (ADR 0036).

### 3.13 Security, correctness and performance review  *(shipped in 2.0.0: ADRs 0039–0043, migrations `0007`–`0008`)*
An atomic key lifecycle (ADR 0039), shared input bounds (ADR 0040),
provider deadlines (ADR 0041), a per-call MCP credential (ADR 0042),
entries as untrusted data (ADR 0043), atomic supersession, indexed
history walks and a locked, non-root image. Later fixes: the key
bootstrap writes the Secret itself (ADR 0044), case-variant names are
refused (ADR 0045), registrations are audited (ADR 0046), and zero or
out-of-range vectors are rejected.

**Open follow-ups** (found, deliberately not done; each needs a decision
or its own trigger):
- **Rate limiting** (garbage-key amplification on `/mcp`, the 2 MiB body
  parse for a present-but-unknown key): handled at the ingress, which has
  per-IP limits, a `/v1/admin` source allowlist (deny by default) and an
  opt-in default-deny egress policy (`optional/networkpolicy/egress/`).
  The app itself has no limiter.
- **Registration**: the owner alias is unverified; the admin confirms the
  requester out of band.
- Structural untrusted-content markers on returned entries (deferred in
  ADR 0043). Case-variant names registered before ADR 0045 stay, and
  vectors stored before the zero/range checks are not re-checked.
- Performance: search still loads full bodies it partly discards (a
  slim-row port method is open); a query-embedding cache is unmeasured;
  very low-selectivity list filters at ~1M rows can be slower with the
  global list index; the HNSW stream can truncate under a narrow
  visibility filter (ADR 0025).
- Contract step for a later release: narrow `credentials_kind_check` to
  drop `user` once no deployment has such rows.
- **Harnesses**: next GitHub Copilot CLI (its `sessionStart` hook injects
  context), then Cursor (re-test `${env:}` in remote headers first), then
  Kiro and Amp as reference-only; Qwen Code is a Gemini CLI variant.
  Still unconfirmed: whether Claude Code's `deniedMcpServers` matches the
  plugin-scoped server name, Gemini CLI's SessionStart context injection,
  and whether DeepSeek Harness keeps the server instructions after
  compaction.

### 3.14 Retrieval and knowledge sharing  *(shipped in 2.1.0–2.3.0: ADRs 0047–0058, migrations `0010`–`0012`)*
The keyword stream matches any term (ADR 0047), at most 16 on Postgres
(ADR 0049), and search falls back to it when the embedder is down
(ADR 0048). A Prometheus scrape endpoint (ADR 0050). Feedback is readable
(ADR 0051); writes return the nearest entries (ADR 0052); batch feedback
and batch reads (ADRs 0053, 0055); a `flagged` filter (ADR 0054);
empty-search counters (ADR 0056); see-also links (ADR 0057); a pinned
fleet briefing (ADR 0058).

## Tier 4 — close the spec's open items (SPEC §11)

*(4.1–4.2 are the §11 open items. 4.3–4.6 share the §1.1 harness.)*

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
- **4.3 Entity-extraction facets** *(shipped: ADR 0016, SPEC §13)*. A
  pre-staged §10 extension, not a §11 item: the knowledge-graph trigger
  has not fired. An optional, best-effort write-time extractor with
  schema-validated `{name, kind}` facets and an AND, case-insensitive
  name filter. The graph half stays held in Tier 5.
- **4.4 Gate the recency term to temporal queries.** *(measured: gating
  rejected, no code change.)* The always-on recency term did depress
  non-temporal recall (MRR 0.208 with it vs. 0.917 without), but an
  oracle-gated variant still lost to decay-off on every aggregate. The
  root cause is arithmetic: at the §6.2 defaults the fused RRF score
  spans at most 2.62x while recency halves every 30 days, so age became
  the sort key. Carried forward as §4.6. Tables and method:
  [docs/retrieval-experiments.md](docs/retrieval-experiments.md#44-gate-the-recency-term-to-temporal-queries).
- **4.5 Record how `importance` was chosen.** *(shipped: migrations
  `0002`/`0003`, SPEC §4.1)* `importance_source` (`caller` / `default`) at
  every write seam, surfaced as `by_importance_source`. `kind` needs no
  such field (it is always required); kind consistency is answered by
  `by_author_kind` on `GET /v1/metrics`.
- **4.6 The form of the recency term is mismatched to RRF's range.**
  *(direction (a) shipped: ADR 0022.)* Sweeping `rrf_k` cannot fix it:
  the whole lever is worth at most ~5.3 half-lives, and `rrf_k` stays 60.
  A floor on the recency factor can: at 0.8 the `old_exact` slice goes
  from 0.000 to 1.000 MRR while `currency_pair` holds at 0.778 (decay-off
  0.611). The floor is a band, not a slider (0.9 measures worse than
  decay-off). Shipping (a) rules out (b), the additive form; reopening (b)
  means replacing the floor under a new ADR. Per-kind half-lives stay
  held. **Still open:** the value 0.8, from a synthetic fixture.
  **Re-measure trigger:** the first real corpus with meaningful age
  spread; sweep 0.5–0.9 and expect to move the value, not the form.
  Tables and method:
  [docs/retrieval-experiments.md](docs/retrieval-experiments.md#46-the-form-of-the-recency-term-is-mismatched-to-rrfs-range).

## Tier 5 — explicitly held: the §10 extensions

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
| Passive capture | "agents forget to write" | high-value agent turns with no write; "should have remembered X" reports / wk; the empty-search rate per fleet *(counted since ADR 0056: `hivemind_searches_empty_total` / `hivemind_searches_total`)* | §3.3 counters |
| Private staging | "try before sharing" | fraction of `self`-scoped entries later promoted to the fleet *(the `self` scope is now the staging space — ADR 0011; promotion is the curation story)* | §3.3 counters |
| Namespaces / channels | "flat pool too noisy" | result precision on scope-unspecified queries; count of distinct fleets in use *(partially satisfied by ADR 0011: single-fleet needs are covered; multi-fleet membership is the remaining extension)* | §3.3 counters |
| Binary artifacts | "analyses outgrow file/URL refs" | count of `sources` with `type=file` or payload > threshold | §3.3 counters |
| Contradiction detection | "explicit supersession can't keep up" | count of same-topic, conflicting-value entry pairs *not* linked by a supersession chain | §3.3 counters |
| Curation workflow | "org wants a 'librarian'" | feedback (helpful/stale/wrong) accumulating without action; stale/wrong entries still in top-k *(the set is now listable: the `flagged` filter, ADR 0054)* | §3.3 counters + §1.1 harness |
| Human read-only UI | "analysts want to see the pool" | direct demand for human browsing (usage signal, not a metric) | qualitative |
| Multi-tenant SaaS / OAuth | "more than one org; an org has an IdP" | count of distinct orgs / tenants requested (count > 1) | §3.3 counters |
| Redis (cache / rate limits / queue) | "API scales to multiple replicas; org demands distributed rate limiting" | API replicas > 1, or distinct orgs > 1, or a cross-process per-agent rate-limit ceiling is needed *(the replica clause is now satisfied — 2 replicas per runner, ADR 0026 — and still buys nothing: there is no distributed rate limiting, no fan-out, and the only cross-process lock is in Postgres (ADR 0020). The live half of this trigger is the **rate-limit ceiling**, not the replica count; a Redis queue for embedding is superseded by the Postgres-outbox design below)* | §3.3 counters + replica count |
| Async embedding pipeline | "embedding latency/throughput starts blocking writes" | p95 write latency; embedder failure/retry rate; backlog of unembedded entries *(the embedder already has a bounded retry budget for transient failures — ADR 0014, `HIVEMIND_EMBEDDING_RETRIES`, default 2 — so this trigger is about the **fallback** (persist without a vector + catch-up), not retries. Design: a Postgres **outbox** — insert entry with `embedding IS NULL` in the same transaction, then a `FOR UPDATE SKIP LOCKED` worker embeds + updates; a single source of truth, no second queue service. New ADR required: it changes write semantics — an entry is vector-searchable only after its vector lands)* | §3.3 counters + p95 write latency |

## How to use this document

- This is a living plan, not a spec. When an item is built it produces
  ADRs and SPEC changes; this document records that it shipped, in a line
  or two, and what it left open.
- It links to SPEC.md, CONTEXT.md and docs/adr/ for behavior and
  rationale; it does not restate them.
