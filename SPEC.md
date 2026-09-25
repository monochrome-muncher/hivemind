# Hivemind — v1 Specification

Status: **final v1** (implemented) · Scope: one organization, self-hosted, agent-facing only

## 1. What Hivemind is

Hivemind is an **agentic memory service**: a shared, Postgres-backed pool of memory entries that **multiple agents in one organization** read and write. Each entry is a distilled, agent-authored unit of knowledge — a **fact**, an **insight** (long-form analysis), or a **decision** — with full provenance (which human, which agent, when, from what source).

The problem it solves:

> Analyst 1 works with agent 1 on day 1 and records important findings. Days later, analyst 2 sits down with agent 2. Agent 2 should be able to **dig through** the important facts and analyses agent 1 left behind — instead of the organization re-learning the same things, or analyst 2 re-doing analysis work that already exists.

v1 is **explicit-write, flat-pool, single-org, self-hosted**. Everything else is a documented extension (§10). Fleet scoping and trust levels (v2, ADRs 0011–0012) supersede the flat pool — see §12.

## 2. Canonical scenario

1. **Write.** Analyst 1's agent finishes an analysis and writes three entries: a `fact` ("the churn model uses weekly cohorts"), an `insight` (long-form write-up with the data slice it relied on, referenced by path), and a `decision` ("we keep weekly cohorts; rationale in <ref>").
2. **Retrieve.** Days later, analyst 2's agent searches "cohort churn modeling". It gets **compact hits** (kind, summary, author, memory date, score). It opens the interesting one with a single `get`, receiving the full long-form body.
3. **Judge & feed back.** If the finding proved useful, the agent reports `helpful`; if it turned out to be wrong, it reports `wrong` and writes a `supersession` with the corrected entry. The corrected entry ranks above the stale one; the stale one is hidden from default search but remains in history.
4. **Stay out of the way.** On a day when an analyst is just spitballing, the agent's Hivemind integration is turned off **client-side** for that session — the pool is untouched and the server doesn't know.

## 3. Prior work

Hivemind deliberately stands on existing work rather than reinventing it. Borrowed patterns and deliberate divergences:

| Project | What Hivemind borrows | What Hivemind deliberately does differently |
|---|---|---|
| **Caura** (fleet memory, MCP-native) | Supersession ranking invariant (a successor always outranks what it superseded); agent-scoped credentials; outcome feedback as a first-class signal | No trust tiers, no knowledge graph, no passive "Interviewer" capture, no Redis, no managed SaaS — one Postgres, one org *(the trust-tier + fleet-scoping divergence was re-adopted in v2: ADRs 0011–0012, §12)* |
| **agentmemory** (coding-agent memory) | Progressive disclosure (compact hits → explicit get); hybrid BM25+vector retrieval with rank fusion; decay-aware ranking | Explicit, deliberate writes only (no hook-based passive capture of every tool call); no P2P mesh |
| **Zep / Graphiti** | Temporal validity thinking (an entry's "memory date" vs. its ingest time) | Flat entries with derived validity — no temporal knowledge graph in v1 |
| **Mem0** | Simple write/search ergonomics as the core API shape | Shared org pool rather than per-user memory; org-governed, not library-embedded |
| **pgmemai / pgmemory** (Postgres+pgvector memory) | The Postgres+pgvector substrate pattern; decay-aware recall (similarity × importance × recency) | Service + MCP for a team, not an in-process library |

The v1 design is the **intersection** of the boring, proven parts of these systems: explicit writes, hybrid retrieval, append-only entries with explicit supersession, and Postgres+pgvector. The fancy parts (knowledge graphs, auto-contradiction, trust tiers, passive capture, curation workflows) are **documented extensions**, not v1 commitments.

## 4. Domain model

### 4.1 Entry (the only core object)

One entry entity; a `kind` enum carries the distinction. There are no other read/write objects in v1.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Server-assigned |
| `kind` | `fact` \| `insight` \| `decision` | Required |
| `summary` | text (≤ ~280 chars) | **Required.** Short, always embedded, always shown in compact hits |
| `body` | markdown (optional) | Long-form content (the "analysis work"). Returned **in full** by `get`; never truncated server-side |
| `payload` | JSONB (optional) | Structured data (numbers, small JSON documents) |
| `sources` | array (optional) | `{type: path\|url\|session\|other, ref}` — provenance pointers to where the entry came from |
| `tags` | string[] (optional) | Free-form labels, filterable |
| `occurred_at` | timestamptz | **The "memory date."** When the observation/analysis actually happened. Defaults to now; **backdatable** (e.g., "fact learned from a January report, written up today") |
| `created_at` | timestamptz | Ingest time, server-assigned |
| `author` | agent name | The agent's registered name, server-filled from the agent key (never self-reported; ADR 0012) |
| `agent` | agent instance id | Framework/instance identifier (retired from writes in v2 — ADR 0012; kept in the schema for existing data) |
| `importance` | 1–5 (int) | Writer-declared; feeds retrieval scoring |
| `importance_source` | `caller` \| `default` | **Server-derived, not client-settable** (ROADMAP §4.5): `caller` when the writer supplied `importance`, `default` when it fell out of the default (3). Rows written before migration `0002` are backfilled to `default` regardless of how `importance` was actually set — their true provenance predates the column and is unrecoverable, so `by_importance_source` under-reports `caller` for that pre-migration cohort |
| `scope` | `self` \| `fleet` \| `org` (legacy) | The entry's audience: the author agent only, the home fleet it was written into (fixed at write time), or the legacy org-wide value (read-only); an omitted scope resolves to the highest value the writer's trust level permits (§12, ADR 0011) |
| `embedding` | vector(dim) | Generated at write time (§7) |
| `state` | `active` \| `superseded` \| `withdrawn` | Default `active` |
| `supersedes` | entry id[] (optional) | Ids of entries this entry supersedes; targets flip to `superseded` |
| `superseded_by` | entry id (nullable) | Set when superseded |
| `withdrawn_reason` | text (nullable) | Set on withdrawal |

**Entries are immutable after creation (ADR 0001).** No field of an existing entry is ever edited. Corrections happen by:
- **Supersession** — a new entry is written with `supersedes: [entry_ids]`; targets flip to `state=superseded`, `superseded_by` set. Any user may supersede any entry (a supersession is a *claim*, not an arbitration — the reader judges).
- **Withdrawal** — an entry is marked `withdrawn` (retracted / no longer reliable). Any user may withdraw **their own** entries; **any** entry can be withdrawn by the org operator/admin. Nothing is ever hard-deleted in v1.

**Derived validity (ADR 0001, no `valid_until` column):** an entry is "active as of T" iff `created_at ≤ T` and it has no supersession/withdrawal state transition before T. Read-side time-travel questions ("what did the org know in March?") are answered by `created_at`/`occurred_at` range filters + `state`.

### 4.2 Feedback

A lightweight, **dumb** outcome loop (no learned tuning in v1):

| Field | Notes |
|---|---|
| `entry_id` | Which entry the reporter relied on |
| `reporter` (user, agent) | Who reported; one feedback row per (entry, user, agent) — **upsert semantics**, latest wins |
| `verdict` | `helpful` \| `stale` \| `wrong` |
| `note` | optional free text |

Feedback feeds a bounded `quality` multiplier used only by retrieval scoring (§6.4):

```
quality = clamp( 1.0 + 0.05·(helpful) − 0.10·(stale) − 0.25·(wrong), 0.5, 1.2 )
```

Counts are per-entry (any reporter). Defaults: no feedback → `quality = 1.0`. The formula is a config value, not a constant in code.

### 4.3 What is *not* in the model (v1)

No sessions on the server (the kill switch is client-side, ADR 0003). No knowledge graph, no binary blobs (artifacts are **referenced**, not stored), no user/role tables beyond credential mapping, no UI. *(v2 adds the `agents` + `fleets` registration tables — ADR 0012 — and self/fleet scoping — ADR 0011; still no user/role model and no UI.)*

## 5. API surface

REST is the canonical interface; the **MCP server is the primary agent-facing wrapper** over it (ADR: MCP + REST). No client touches Postgres directly, ever.

### 5.1 REST (v1)

| Method & path | Purpose |
|---|---|
| `POST /v1/entries` | Create an entry (body = §4.1 fields; `supersedes` optional) |
| `GET /v1/entries/{id}` | Full entry (body included). `?history=true` adds the supersession chain |
| `GET /v1/entries` | List/filter **without** a query (filter only; paginated) |
| `POST /v1/search` | Hybrid search (§6) with filters |
| `POST /v1/entries/{id}/withdraw` | Withdraw own entry (or any, with admin credential) |
| `POST /v1/entries/{id}/feedback` | Report `helpful`/`stale`/`wrong` (+note) |
| `GET /v1/health` | Liveness/readiness |
| `GET /v1/whoami` | The calling key's standing: kind (`agent`/`org`/`admin`/`legacy`), agent name, status, trust level, home fleet, `can_read`, `can_write_scopes` (any valid key; not audited — ADR 0030) |
| `POST /v1/agents` | Register an agent (org key or admin key; `{name, owner_alias?}`) → pending agent (§12, ADR 0012) |
| `GET /v1/admin/agents` | List agents: status, trust level, home fleet, owner alias (admin key) |
| `GET /v1/admin/fleets` | List fleets (admin key) |
| `POST /v1/admin/fleets` | Create a fleet (admin key; no deletion in this increment — ADR 0011) |
| `POST /v1/admin/agents/{name}/activate` | Set trust level (default `lurker`) + home fleet; **returns the generated agent key once** (admin key; §12.3). Only from `pending` or `revoked`; `active` → 409 (ADR 0028) |
| `PATCH /v1/admin/agents/{name}` | Change trust level / home fleet — demotion to `untrusted` = dormant (admin key) |
| `POST /v1/admin/agents/{name}/revoke` | Kill the agent's key and set it `revoked`; on a `pending` agent this rejects the registration (admin key; the name stays reserved — ADRs 0012, 0028) |
| `POST /v1/admin/org-key/rotate` | Rotate the shared org key: closes registration to every prior org key; active agents are unaffected (admin key; ADR 0031) |
| `GET /v1/metrics` | Usage counters: entries / fleets / agents (trust-level distribution, writes per fleet, pending count) — operational data (admin key; ROADMAP §3.3) |
| `GET /v1/admin/audit-log` | The audit log of admin-surface actions, newest first; filters `actor`, `action`, `since`, `before`, `limit` (admin key; §12.5, ADRs 0027, 0028) |

### 5.2 MCP tools (the agent's mental model — eight verbs)

| Tool | Maps to |
|---|---|
| `hive_write` | `POST /v1/entries` |
| `hive_search` | `POST /v1/search` |
| `hive_get` | `GET /v1/entries/{id}` |
| `hive_list` | `GET /v1/entries` |
| `hive_withdraw` | `POST /v1/entries/{id}/withdraw` |
| `hive_feedback` | `POST /v1/entries/{id}/feedback` |
| `hive_register` | `POST /v1/agents` (org key only — the agent's first contact with Hivemind; §12.3) |
| `hive_whoami` | `GET /v1/whoami` (any valid key — the key's kind, the agent's status, trust level and home fleet, and what it may read and write; ADR 0030) |

A typical agent prompt contract: *"check where you stand (`hive_whoami`); recall before you analyze; write what you learn; supersede, don't duplicate; report when something you relied on proved wrong."* The agent plugin and skills in `plugins/hivemind/` spell this contract out for agent harnesses.

### 5.3 Filters (search *and* list)

`kind`, `tags`, `entities` (extracted entity names, AND-semantics, case-insensitive; ADR 0016, §13), `scope`, `author`, `agent`, `occurred_from`/`occurred_to` (**memory-date** range), `created_from`/`created_to`, `state` (default `active` only; `include_inactive=true` to include `superseded`/`withdrawn`), `limit`/`offset`.

## 6. Retrieval

### 6.1 Two-stage (progressive disclosure)

`search` returns **compact hits** — `id, kind, summary, tags, author, agent, occurred_at, score` — **not** full bodies. The agent opens what it wants with `hive_get`. This is the token economy the design is built around: scan many, open few.

### 6.2 Hybrid pipeline

```
query ─┬─> keyword list  (Postgres FTS / BM25-style rank) ──┐
       └─> vector list   (pgvector cosine, top-k)          ──┼─> RRF fusion
                                                              │      fused = w_kw·1/(k+rank_kw) + w_vec·1/(k+rank_vec)
                                                              │      k=60,  w_kw=w_vec=0.5  (all config)
                                                              ▼
                     decay-aware rescore (§6.4)  ──>  final order  ──>  compact hits
```

Keyword ranking is Postgres FTS in v1 (a true-BM25 extension such as `pg_search`/Zombi is a drop-in upgrade, not a v1 dependency). Weights `w_kw`/`w_vec`, `k`, top-k, and the rescore factors are all **config values**, so fusion tuning is a config change, not a code change.

**The vector stream is approximate, not exact (ADR 0025).** It is served by an HNSW index on `entries.embedding` (`vector_cosine_ops`, matching the `<=>` operator; `m = 16`, `ef_construction = 64` — migration `0004`). HNSW is an *approximate* nearest-neighbour structure: the stream is **not guaranteed** to be the true top-k by cosine distance, and a true near neighbour can be missed entirely. This is a deliberate behaviour change from the exact sequential scan that preceded it, taken because that scan reads ~4KB per row and grows without bound with the pool — ADR 0025 carries the measured recall cost and the re-measure trigger. The keyword stream is unaffected, and RRF means a vector miss is survivable when the keyword stream also finds the entry.

Two **session-level** settings apply per vector query (`SET LOCAL`, in the same transaction as the search — not on the connection, which a transaction-mode PgBouncer would discard):

- `hnsw.iterative_scan = strict_order` — every query here is filtered (at minimum `state = 'active'`, plus the §12 visibility matrix), and without iterative scan HNSW filters *after* fetching `ef_search` candidates, so a narrow-visibility reader can get back far fewer than `candidate_top_k` rows. `strict_order` and not `relaxed_order` because RRF fuses on **ranks**, not scores: relaxed ordering perturbs exactly the quantity fusion consumes.
- `hnsw.ef_search = 40` — pgvector's default, unchanged. It is **not** the lever for recall (ADR 0025 measured that raising it plateaus below parity).

### 6.3 Supersession ranking invariant

When a result set contains both an entry and its supersession, **the successor always ranks above the predecessor** — a hard boost applied after scoring, before pagination. Superseded/withdrawn entries are **hidden by default**; they surface only with `include_inactive`. A stale row may appear in history, never as the top answer to a live query.

### 6.4 Decay-aware rescore (similarity × importance × recency)

```
final = fused
      × (0.5 + 0.1·importance)                            # 1..5  →  0.6..1.0
      × max(recency_floor, 0.5 ** (age_days / half_life)) # age from occurred_at; half_life 30d, floor 0.8
      × quality                                           # §4.2, 0.5..1.2
```

A fresh, important, well-remembered entry beats a slightly-more-similar but stale one. All three factors are config-tunable; the `0.5 **`/exponents are defaults, not schema.

**The recency factor is bounded below by `recency_floor` (default 0.8 — ADR 0022).** Without it the term is unbounded below, so it spans more than the fused RRF score's whole range (`2(k+20)/(k+1)` = 2.6230x at the §6.2 defaults) after ~42 days and recency becomes the *sort key* rather than the tie-break this section intends. A floor `f` caps the recency factor's range at `1/f` — 1.25x at 0.8, inside the fused range. `recency_floor` is a **band, not a slider**: it must stay above `1/2.6230 = 0.381` to have any effect, and raising it toward 1.0 progressively removes the recency term (at 1.0 it is gone, and at 0.9 it measured *worse* than removing it outright — ADR 0022). The environment-settable range is `(0, 1]`; the pre-ADR-0022 unbounded form is reachable from the environment too — set `HIVEMIND_RECENCY_FLOOR` to an empty value or to `none` (case-insensitive) to get it, rather than approximating it with a negligible floor (ADR 0023).

## 7. Embedding strategy (ADR 0005)

- Generated **at write time**, server-side, from `summary` (whole) + a bounded prefix of `body`.
- **The body prefix is bounded in *prefix tokens* — whitespace-delimited words, not a model tokenizer's tokens** (`EMBEDDING_PREFIX_TOKENS`, default **2000** — ADR 0021). The cut is made at a word boundary and the body's own spacing (newlines, blank lines, indentation) is preserved up to it. ~2000 words is ~2600 model tokens, far under the endpoints' limits: the bound controls **dilution and cost**, not a model limit. The extractor reads this exact same text (§13.1) — one budget, both consumers.
- Produced by an **OpenAI-compatible embeddings endpoint the org configures at deploy** (`EMBEDDING_ENDPOINT`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`). A self-hosted vLLM/Ollama endpoint satisfies "data must not leave the org."
- The store records `embedding_model` + dimension per entry. The vector column has a **fixed dimension at deploy time** (`EMBEDDING_DIM`, default 1024 — ADR 0015).
- **A dim mismatch is a loud failure, never a silent assumption:** if the pool's `vector(:dim)` column differs from the configured dim, `migrate` fails with an actionable error (reset the pool, or set `EMBEDDING_DIM` to the pool's dim) — ADR 0015. The dim is a deploy-time decision (ADR 0005); it is never changed in place.
- **Changing the embedding model or dimension is an operator-run re-embedding migration** (re-embed all active entries, swap the column). This is a named maintenance procedure, not an online feature.
- **Transient embedder failures are retried** (ADR 0014) — timeouts, connection errors, `429`, and 5xx are retried with a bounded exponential backoff (`EMBEDDING_RETRIES`, default 2 retries; `0` disables). Deterministic failures (other 4xx, dimension mismatch) fail fast, and a write still fails after the full budget — a write either lands fully (vector + entry) or fails.

## 8. Identity, credentials, deployment

### 8.1 Credentials (ADR 0012)

| Credential | Binds | Effect |
|---|---|---|
| **org key** | the whole cluster (shared) | Gates registration only; rotating it closes registration to every prior copy, and active agents keep working (ADRs 0012, 0031) |
| **agent key** | one registered agent | Carries the agent's trust level + home fleet; `author` is server-filled with the agent's registered name (§12, ADR 0012) |
| **admin key** | the operator | The admin surface: activate / promote / demote / revoke, fleets, org-key rotation (§12.4) |

No OAuth/SSO; keys are issued by the org operator via the admin surface (§12.4). (An org with an IdP is a later story.) This table supersedes ADR 0008's key kinds (`user`/`agent`/`admin`) — ADR 0012. The agent key is now the credential the MCP runners verify (ADRs 0009–0010).

### 8.2 Deployment (ADR 0026, superseding ADR 0007)

One **self-hosted instance per organization** — one instance = one organization — over **one PostgreSQL 16 (pgvector) node**. No Redis, no sharding, no queue, no read replicas.

**App tier: 2 replicas per runner** (`hivemind-api` and `hivemind-mcp-http`), rolled out `maxSurge: 1` / `maxUnavailable: 0` with a `minAvailable: 1` PodDisruptionBudget each. The replicas are interchangeable: they share the one pool, serve the one organization, hold no server-side session state (§5.1, §8.5), and resolve credentials per request. This buys **availability only** — a zero-downtime rolling deploy and surviving a node drain — not throughput (the app tier is I/O-bound on the embedder, the extractor and Postgres) and not AZ tolerance (there is one Postgres node). Production packaging is Kubernetes (`deploy/kubernetes/`); `docker compose` is the dev and single-node-ops shape. The **admin panel** (`hivemind-admin`, ADR 0029) is a separate, stateless, 1-replica runner in the same image: a static UI plus an allowlisted same-origin proxy to `hivemind-api`. It holds no database credential; the operator's admin key lives only in their browser tab.

**Scale assumptions (spec target):** ~50 users/agents, ~500 sessions/day, ~10k entries/day, single Postgres node. The design does not commit to horizontal *storage* scale; when the assumptions stop holding, §10's extensions apply.

### 8.3 The kill switch (ADR 0003)

Turning Hivemind off for a session is a **client-side act**: the agent's Hivemind integration (its MCP server entry / enabled flag) is disabled for that session, so the tools simply aren't available and the pool is untouched. **The server has no session registry and is unaware of off sessions.** The spec's only server-side commitment is that an absent client is indistinguishable from a quiet one.

### 8.4 MCP runners (ADR 0009)

The MCP stdio surface ships **two runners**:

* **`hivemind-mcp`** — the **dev** path: in-memory store + local hash embedder + a hard-coded `dev` credential. Zero network, ephemeral, single identity; for exercising the eight `hive_*` tools with no dependencies.
* **`hivemind-mcp-pg`** — the **production** path: a DSN-backed `PgStore` + the operator-configured OpenAI-compatible embedder (ADR 0005), with the acting credential resolved by verifying `HIVEMIND_MCP_KEY` against the Postgres `credentials` table (ADR 0012).

**Unified multi-agent pool:** several agents each run their own `hivemind-mcp-pg` process with a distinct `HIVEMIND_MCP_KEY` (an **agent key**, ADR 0012). All of them read/write the **same** Postgres pool over a **unified** MCP interface (the identical eight `hive_*` tools) while every write carries that agent's *verified* provenance (its registered name, server-filled from the key). Revoking an agent's key revokes its access immediately. The dev runner (`hivemind-mcp`) is unchanged.

### 8.5 The hostable streamable-HTTP runner (ADR 0010)

`hivemind-mcp-http` is the **hostable, multi-agent** form of the Postgres-backed runner (ADR 0009): a long-lived streamable-HTTP process serving an **unlimited** number of agents over one shared pool, each authenticating **per request** with its own agent key (ADR 0012).

* **One shared pool per process, per-request auth.** A `hivemind-mcp-http` process owns one `PgStore` + one embedder + one `Authenticator` pool (the same DSN / embedder / credentials the REST API uses), shared by every agent it serves — never a process per agent. Each request presents its own key; a thin ASGI middleware verifies it against the `credentials` table (ADR 0012) and re-binds the shared, *stateless* services to that credential on every tool dispatch. The invariant is the shared pool, not the process count: production runs 2 interchangeable replicas of this runner for availability (§8.2, ADR 0026), and because the transport is stateless and the credential is per-request, either replica answers any request identically.
* **Immediate revocation.** Because the credential is resolved **per request** (not once at process start, as in `hivemind-mcp-pg`), admin revocation (`POST /v1/admin/agents/{name}/revoke`) takes effect on the very next request — no restart required.
* **Per-request transport: stateless streamable-HTTP.** The server runs the SDK's stateless streamable-HTTP transport (one request = one self-contained exchange). The pool is stateless with respect to sessions, so this is a natural fit.
* **Deployment shape: a detached compose service.** `make mcp-http` ships the runner as a detached docker-compose service (one container built from the repo's Dockerfile), published on host port 8088 by default (override with `HIVEMIND_MCP_HTTP_PORT`). The container reads/writes the shared pool and embeds via the local vLLM on the compose network, so pool + embedder + runner come up with a single `docker compose up -d` (ADR 0007).

**MCP runners, at a glance** (three runners, one pool):

| runner | process | pool | credential | revocation |
|---|---|---|---|---|
| `hivemind-mcp` (dev, ADR 0009) | in-memory | in-memory (ephemeral) | hard-coded `dev` | n/a |
| `hivemind-mcp-pg` (per-agent, ADR 0009) | one per agent | shared Postgres | agent key (`HIVEMIND_MCP_KEY`, ADR 0012), verified at start | next restart |
| `hivemind-mcp-http` (hostable, ADR 0010) | shared (2 replicas, §8.2) | shared Postgres | agent key (ADR 0012), verified per request | immediate |

Both `hivemind-mcp-pg` and `hivemind-mcp-http` read/write the same pool with the same verified provenance; choose the per-agent runner for a small dev setup, the hostable runner when many agents share one machine.

### 8.6 Schema migrations (ADR 0020)

The schema is an **ordered chain of versioned migrations** under `src/hivemind/store/migrations/`, applied by `hivemind-migrate` (yoyo-migrations). The chain is the source of truth; `schema.sql` is a CI-generated, non-authoritative reference that is never applied and never hand-edited.

* **Forward.** `migrate` applies every migration not yet recorded in `_yoyo_migration`, in order. It is safe to run on every process start — an up-to-date pool is a no-op — and the image entrypoint does exactly that (ADR 0018). An image whose chain is *older* than the pool applies nothing; it never rolls the schema back.
* **Backward.** Each migration ships a `.rollback.sql` companion, so an incremental change can be reversed without restoring the pool. **A rollback that would destroy data is not written**: reversing a populated column drop or a backfill is a restore from backup, not a migration (ADR 0001 — nothing is hard-deleted).
* **Concurrency.** `migrate` holds a **Postgres advisory lock** for the duration. The lock is session-scoped, so a pod killed mid-migration releases it by dying — many replicas may start simultaneously and exactly one migrates.
* **Version marker.** `current_schema_version` is the latest applied migration id, read from `_yoyo_migration` and reported on the ops surface (§3.3 counters). There is no separate `schema_migrations` table.
* **Online-safe DDL.** New index migrations use `CREATE INDEX CONCURRENTLY` with yoyo's `-- transactional: false` directive, so an index build does not lock a table while a sibling replica serves.
* **Expand-and-contract.** Within a release, schema changes are **additive only** (new columns nullable or defaulted, new tables, new indexes). A removal takes two releases: release N stops reading and writing the column, release N+1 drops it. This is a requirement, not a preference — the runners are deployed as independently released units against one pool, so a migration always runs against some still-deployed older code.
* **Dimension guard.** The ADR 0015 dim-mismatch check runs **before** the lock is taken, so a pool provisioned at a different embedding dimension (§7, ADR 0005) still fails loudly before any DDL.

## 9. Non-goals (v1) — the explicit list

- No human-facing UI (agent-only; a read-only web search is a later extension)
- No passive capture of transcripts/tool events (explicit writes only, ADR 0004)
- No knowledge graph, no auto-contradiction detection (the **facet slice** of entity extraction became a commitment in §13 — ADR 0016; *graph-expanded* retrieval stays a non-goal)
- No curation/verification workflow, no PII pipeline (trust levels are §12, not a non-goal — ADR 0011)
- No multi-tenant SaaS (single-org; self/fleet scoping is §12, ADR 0011)
- No binary/artifact storage (references only)
- No OAuth/SSO, no per-agent learned retrieval tuning
- No HA, sharding, or Redis-backed scale-out

## 10. Documented extensions (out of v1 scope, by design)

| Extension | Trigger to build it |
|---|---|
| **Passive capture** — ingest raw session transcripts, distill with an LLM (crash-safe, dedup'd) | "Agents forget to write" becomes the bottleneck |
| **Private staging + publish** — write to a personal scratch, *promote* it to the fleet | Analysts want to try analyses before sharing. *Partially satisfied: the `self` scope (ADR 0011) is the personal scratch; what remains is promotion — a curation story (see Curation workflow)* |
| **Namespaces / channels** — multi-fleet membership, per-fleet promotion, cross-fleet writes | The flat pool grows too noisy. *Partially satisfied: fleets + one home fleet per agent (ADR 0011) cover single-fleet needs; multi-fleet membership is the remaining extension* |
| **Binary artifacts** — S3-backed artifact store behind `sources` | Analysis references outgrow file/URL references |
| **Knowledge graph** — graph-expanded retrieval over a canonical entity registry | Cross-entry entity linking pays off in retrieval quality. *Partially pre-staged: the entity-extraction **facet** slice is shipped by ADR 0016 / SPEC §13 (a pre-staged §10 extension on scale ambition — the trigger has *not* fired); the graph half of this row (entity registry + multi-hop expansion) remains trigger-held* |
| **LLM-assisted contradiction detection** — at write time, surface entries above a similarity floor that the new entry may conflict with, as a *claim for the writer to judge* (never an automatic resolution — ADR 0001: a supersession is a claim, not an arbitration) | Explicit supersession can't keep up with contradictory writes. **Measurable trigger:** the first observed incident of a contradicting entry landing without a supersession, **or** pool size > 10k entries — whichever comes first. Until then the similarity floor cannot be tuned (there is no corpus to tune it against), and an untuned floor is the failure mode to avoid |
| **Curation workflow** — verify/promote/retire roles | An org wants a "librarian" function |
| **Human read-only UI** — browse/search the pool in a browser | Analysts want to see the pool without an agent |
| **Multi-tenant SaaS / OAuth** | More than one org wants it; an org has an IdP |
| **Learned per-agent retrieval tuning** | Static decay factors stop beating per-agent profiles |

## 11. Open questions (resolved at implementation, not spec)

These were deliberately left open in the spec and are now settled by the
implementation (v1); they are recorded here for the audit trail, not to
change behavior.

1. **Language/runtime** — resolved: **Python 3.14** (`uv`-managed dev env; Postgres + pgvector via docker).
2. **Exact FTS config** — resolved: **Postgres FTS** (`to_tsvector`); no true-BM25 extension in v1 (a BM25 extension is a drop-in upgrade, §6.2).
3. **Pagination style** — resolved: **offset** (v1); cursor is a later extension.
4. **`hive_list` default order** — resolved: **most-recent-first** (`created_at DESC, id DESC`), confirmed in the first implementation.
5. **Embedding prefix length** — resolved: a bounded **2000-prefix-token body prefix** default (`EMBEDDING_PREFIX_TOKENS`; a prefix token is a whitespace-delimited word, *not* a model tokenizer's token — ADR 0021). This supersedes the original ~2048-char (≈ 512-token) answer, which was both the wrong unit (chars per token varies 2–3x between prose and code) and, on the embedding side, an inert knob. The *unit and size* question is closed; **what** goes into the embedded text, and whether to chunk at all, stay open (ROADMAP §4.2).

## 12. Fleets, trust levels, and registration (v2 — ADRs 0011–0012)

This section supersedes the flat-pool commitment of §1 and the "no trust tiers" non-goal of §9. ADR 0011 owns the fleet/trust model; ADR 0012 owns the credential model. This section is the spec commitment; the ADRs own the *why*.

### 12.1 Fleets and scope

* A **fleet** is a named group of agents. The admin creates fleets (`POST /v1/admin/fleets`); **no fleet deletion in this increment** (ADR 0011).
* Every active agent belongs to exactly **one home fleet** — admin-assigned, re-assignable at any time via `PATCH /v1/admin/agents/{name}`. One home fleet per agent in this increment; multi-fleet membership is a §10 extension.
* An entry is written into exactly one scope: `self` (the author agent only) or `fleet` (the home fleet it was written into). **An entry's fleet membership is fixed at write time** — when an agent moves fleets, its earlier entries stay in the old fleet (ADR 0011).
* Legacy `scope='org'` entries are **read-only**: no new write may use `org`; they remain readable at trust level 1+.
* **Default scope**: an omitted scope resolves to the **highest value the writer's trust level permits** (`lurker` → `self`; `contributor`/`privileged` → `fleet`). An *explicit* out-of-permission scope (e.g. `fleet` at `lurker`) is a permission error naming the required level.

### 12.2 Trust levels (cumulative)

| Level | Name | Reads | Writes |
|---|---|---|---|
| 0 | `untrusted` | nothing | nothing |
| 1 | `lurker` | own + home fleet | own (`self`) |
| 2 | `contributor` | own + home fleet | own + home fleet |
| 3 | `privileged` | own + home fleet + **every fleet** | own + home fleet only (read-broad, write-local) |

* At level 0 (`untrusted`) — what pending and demoted agents sit at — all read verbs return **empty results** (the visibility filter hides everything); writes and feedback are explicit permission errors.
* Reads beyond visibility behave **as if the entry does not exist** (no existence leaking).
* `self`-scoped entries are private to their author **even at level 3** (that is what `self` is for).
* **Feedback and withdrawal follow readability**: an agent may feedback entries it can read, and withdraw its own entries; the admin key may withdraw any entry (the §4.1 withdrawal rules now ride the trust matrix).

### 12.3 Registration and activation

1. **Registration** — an agent (or a human on its behalf, via `POST /v1/agents` — the seam a future human-facing frontend plugs into) registers a **unique agent name** plus the **owner's alias** (a username or email the admin uses to reach the owner). `hive_register` (MCP, org key only) or `POST /v1/agents` (REST; org key or admin key). This creates a **pending** agent at trust level 0 — no data-plane access. *Name already pending → idempotent no-op ("registered — awaiting admin activation"); name already active or revoked → "name already registered — choose a new name."*
2. **Activation** — the admin activates via `POST /v1/admin/agents/{name}/activate`, setting the trust level (default `lurker`) and the home fleet (required; fleets are created first). The service generates the agent key and returns it **once** — the only moment a key is ever shown. The admin delivers the key **out-of-band** (chat/DM/email, using the owner alias); the service has **no notification channel**.
3. **Live traffic** — an active agent presents **only its agent key** on every request (one key per request, ADR 0031); per-agent / per-request verification (ADRs 0009–0010) applies unchanged. **Demotion** (to `untrusted`) and **revocation** are distinct verbs: demotion keeps the key valid but the agent can do nothing; revocation kills the key, sets the agent `revoked`, and the **name stays reserved** (ADR 0012). A revoked agent can be re-activated, which issues a fresh key (ADR 0028).
4. **Lifecycle** (ADR 0028) — `pending → active` (activate), `pending → revoked` (revoke: a rejected registration), `active → revoked` (revoke), `revoked → active` (activate, fresh key). Any other transition is **409**; activation never issues a second key to an agent that already holds one.

### 12.4 Admin surface

| Endpoint | Effect |
|---|---|
| `GET /v1/admin/agents` / `GET /v1/admin/fleets` | List agents (status, level, home fleet, alias) / fleets |
| `POST /v1/admin/fleets` | Create a fleet (ADR 0011: no deletion in this increment) |
| `POST /v1/admin/agents/{name}/activate` | Set level + home fleet; **returns the agent key once** (§12.3) |
| `PATCH /v1/admin/agents/{name}` | Change level / home fleet (demotion to `untrusted` = dormant) |
| `POST /v1/admin/agents/{name}/revoke` | Kill the key, status → `revoked` (name stays reserved; re-activation issues a *new* key). On a pending agent: reject the registration |
| `POST /v1/admin/org-key/rotate` | Rotate the org key — closes registration to every prior copy (ADR 0031) |

The single admin key gates this surface; admin-issued entries use the reserved name `admin` (ADR 0012). The admin panel (ADR 0029) is a browser front end over exactly this table plus the audit log; issuing and revoking admin keys stays with `hivemind-keys`.

### 12.5 Audit log

Every admin-surface action is recorded in an append-only **audit log** (ADR 0027) — distinct from an entry's provenance and its supersession chain.

**What is recorded.** One row per successful action, with `occurred_at`, `actor_kind`, `actor`, `action`, `target` and a `detail` object:

| `action` | Recorded by | `target` | `detail` |
|---|---|---|---|
| `agent.activate` | `POST /v1/admin/agents/{name}/activate` | agent name | `{trust_level, home_fleet_id}` |
| `agent.trust_level_set` | `PATCH /v1/admin/agents/{name}` | agent name | `{from, to}` |
| `agent.home_fleet_set` | `PATCH /v1/admin/agents/{name}` | agent name | `{from, to}` |
| `agent.revoke` | `POST /v1/admin/agents/{name}/revoke`, `hivemind-keys revoke` | agent name | `{"from": "pending"\|"active"}` (ADR 0028; a revoke from `pending` is a rejected registration) |
| `fleet.create` | `POST /v1/admin/fleets` | fleet id | `{name}` |
| `org_key.rotate` | `POST /v1/admin/org-key/rotate`, `hivemind-keys rotate-org` | — | `{}` |
| `entry.withdraw` | `POST /v1/entries/{id}/withdraw` **only** when the admin key withdraws another agent's entry (§4.1) | entry id | `{author, reason}` |
| `admin_key.issue` | `hivemind-keys issue-admin` | key fingerprint | `{}` |
| `admin_key.revoke` | `hivemind-keys revoke-admin` (a successful revocation only) | key fingerprint | `{}` |
| `agent_key.issue` | `hivemind-keys issue-agent` | agent name | `{}` |

**Actors.** `actor_kind` is `admin_key` for the REST admin surface — `actor` is `admin:<fingerprint>` of the verified admin key — or `cli` for `hivemind-keys`, whose `actor` is the operator-supplied `--actor` (default: the OS user) and is **unverified**. A key **fingerprint** is the first 12 hex characters of the key's stored SHA-256 hash (what `hivemind-keys list` shows).

**Never recorded.** No raw API key appears in any column. Registration (`POST /v1/agents`, `hive_register`) and read-only calls are not audited; nor is an author withdrawing their own entry.

**Guarantees.** The CLI writes each row in the same transaction as its change. The REST surface writes the row **after** the change succeeds and not atomically with it: if the audit write fails the request fails (the change stands, unaudited), and a process crash between the two leaves the change unaudited.

**Reading it.** `GET /v1/admin/audit-log` (admin key) returns rows newest first; `actor` (exact), `action` (one of the vocabulary above), `since` (inclusive timestamp; naive = UTC), `before` (a row id: only rows strictly older than it, for paging back — ADR 0028) and `limit` (1–1000, default 100) filter it. There is no MCP verb — like `GET /v1/metrics`, it is an operator concern.

## 13. Entity extraction (v3 — ADR 0016)

This section supersedes the "no entity extraction" non-goal of §9 **for the facet slice only**. ADR 0016 owns the extractor; this section is the spec commitment; the ADR owns the *why*. The knowledge-graph half of the §10 row (entity registry + graph-expanded retrieval) stays trigger-held — facets are not a graph. This is a **pre-staged** §10 extension: the §10 trigger ("cross-entry entity linking pays off in retrieval quality") has *not* fired; the facet slice is shipped on scale ambition (300+ agents / multiple fleets) and is measured with the §1.1 harness.

### 13.1 What is extracted

* **At write time**, server-side, over the same text the embedder sees: `summary` + a bounded body prefix (`EMBEDDING_PREFIX_TOKENS` — whitespace-delimited words, ADR 0021). One budget feeds both consumers, so raising it raises the extractor's LLM input per write in step (§7).
* A fixed-prompt LLM call (`EXTRACTOR_MODEL` — a deploy-time decision, ADR 0016; e.g. a Qwen3-27B-class chat model on a *separate service* from the embeddings server).
* **All-or-nothing, schema-validated output:** `entities` is a list of `{name, kind}` where `name` is open vocabulary (trimmed, non-empty, ≤ 128 chars) and `kind` is a **closed** vocabulary (`person | organization | system | service | artifact | concept`); ≤ 10 entities per entry, deduped. Any malformed output fails the call — no partial salvage.

### 13.2 Storage and immutability

* `entries.entities jsonb` + `entries.entities_model text` — symmetric with the `embedding` / `embedding_model` pair (ADR 0005): one machine writer, immutable after write (ADR 0001), provenance via `entities_model`.
* **No backfill in v1:** pre-existing entries keep `entities: []`; an operator-run backfill is a named maintenance procedure (the ADR 0005 re-embedding-migration pattern — the one sanctioned in-place exception to ADR 0001, for machine metadata only), not an online feature.

### 13.3 Query surface

* `EntryFilters.entities` — **AND-semantics over names, case-insensitive** (mirrors `tags`); exposed on REST `GET /v1/search` + `GET /v1/entries` and `hive_search` / `hive_list`.
* `kind` is stored + displayed only (not filterable in v1); entries expose an `entities` field in responses (MCP + REST).

### 13.4 Posture: optional + best-effort

* **Optional:** `EXTRACTOR_ENDPOINT` unset ⇒ extraction is off (entries land with `entities: []`, zero LLM cost) — the same dev-mode stance as "no authenticator".
* **Best-effort inline:** an extraction failure (timeout, retry exhaustion, schema mismatch) never blocks the write — the entry lands with `entities: []` and no `entities_model`. Entry-level write semantics are unchanged (ADR 0014); only the enrichment is lost.
* **Bounded retries** on transient failures (ADR 0014 pattern; `EXTRACTOR_RETRIES` default 2; `0` disables).
* **Machine-only:** agents declare facets via `tags` (the existing channel); `entities` is a pure machine signal. Agent-supplied `entities` is a later extension (it would need a source flag).

### 13.5 Explicitly out of scope

* Graph expansion / multi-hop retrieval (stays a §10 / Tier 5 trigger; ADR 0006's rejection stands).
* A canonical entity registry / alias resolution (the next step after facets).
* Agent-supplied `entities` (needs a source flag; a later extension).
* Auto-contradiction detection (stays a §10 trigger).
