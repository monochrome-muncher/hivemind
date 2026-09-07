# Hivemind — v1 Specification

Status: **draft v1** · Scope: one organization, self-hosted, agent-facing only

## 1. What Hivemind is

Hivemind is an **agentic memory service**: a shared, Postgres-backed pool of memory entries that **multiple agents in one organization** read and write. Each entry is a distilled, agent-authored unit of knowledge — a **fact**, an **insight** (long-form analysis), or a **decision** — with full provenance (which human, which agent, when, from what source).

The problem it solves:

> Analyst 1 works with agent 1 on day 1 and records important findings. Days later, analyst 2 sits down with agent 2. Agent 2 should be able to **dig through** the important facts and analyses agent 1 left behind — instead of the organization re-learning the same things, or analyst 2 re-doing analysis work that already exists.

v1 is **explicit-write, flat-pool, single-org, self-hosted**. Everything else is a documented extension (§10).

## 2. Canonical scenario

1. **Write.** Analyst 1's agent finishes an analysis and writes three entries: a `fact` ("the churn model uses weekly cohorts"), an `insight` (long-form write-up with the data slice it relied on, referenced by path), and a `decision` ("we keep weekly cohorts; rationale in <ref>").
2. **Retrieve.** Days later, analyst 2's agent searches "cohort churn modeling". It gets **compact hits** (kind, summary, author, memory date, score). It opens the interesting one with a single `get`, receiving the full long-form body.
3. **Judge & feed back.** If the finding proved useful, the agent reports `helpful`; if it turned out to be wrong, it reports `wrong` and writes a `supersession` with the corrected entry. The corrected entry ranks above the stale one; the stale one is hidden from default search but remains in history.
4. **Stay out of the way.** On a day when an analyst is just spitballing, the agent's Hivemind integration is turned off **client-side** for that session — the pool is untouched and the server doesn't know.

## 3. Prior work

Hivemind deliberately stands on existing work rather than reinventing it. Borrowed patterns and deliberate divergences:

| Project | What Hivemind borrows | What Hivemind deliberately does differently |
|---|---|---|
| **Caura** (fleet memory, MCP-native) | Supersession ranking invariant (a successor always outranks what it superseded); agent-scoped credentials; outcome feedback as a first-class signal | No trust tiers, no knowledge graph, no passive "Interviewer" capture, no Redis, no managed SaaS — one Postgres, one org |
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
| `author` | user id | From the credential (who the agent acts for) |
| `agent` | agent instance id | Framework/instance identifier |
| `importance` | 1–5 (int) | Writer-declared; feeds retrieval scoring |
| `scope` | `org` (v1) | The flat-pool tag; seam for future narrowing (§10, ADR 0002) |
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

No sessions on the server (the kill switch is client-side, ADR 0003). No personal spaces, no channels, no knowledge graph, no binary blobs (artifacts are **referenced**, not stored), no user/role tables beyond credential mapping, no UI.

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

### 5.2 MCP tools (the agent's mental model — six verbs)

| Tool | Maps to |
|---|---|
| `hive_write` | `POST /v1/entries` |
| `hive_search` | `POST /v1/search` |
| `hive_get` | `GET /v1/entries/{id}` |
| `hive_list` | `GET /v1/entries` |
| `hive_withdraw` | `POST /v1/entries/{id}/withdraw` |
| `hive_feedback` | `POST /v1/entries/{id}/feedback` |

A typical agent prompt contract: *"recall before you analyze; write what you learn; supersede, don't duplicate; report when something you relied on proved wrong."*

### 5.3 Filters (search *and* list)

`kind`, `tags`, `scope`, `author`, `agent`, `occurred_from`/`occurred_to` (**memory-date** range), `created_from`/`created_to`, `state` (default `active` only; `include_inactive=true` to include `superseded`/`withdrawn`), `limit`/`offset`.

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

### 6.3 Supersession ranking invariant

When a result set contains both an entry and its supersession, **the successor always ranks above the predecessor** — a hard boost applied after scoring, before pagination. Superseded/withdrawn entries are **hidden by default**; they surface only with `include_inactive`. A stale row may appear in history, never as the top answer to a live query.

### 6.4 Decay-aware rescore (similarity × importance × recency)

```
final = fused
      × (0.5 + 0.1·importance)                     # 1..5  →  0.6..1.0
      × 0.5 ** (age_days / half_life_days)          # age from occurred_at, half_life default 30d
      × quality                                      # §4.2, 0.5..1.2
```

A fresh, important, well-remembered entry beats a slightly-more-similar but stale one. All three factors are config-tunable; the `0.5 **`/exponents are defaults, not schema.

## 7. Embedding strategy (ADR 0005)

- Generated **at write time**, server-side, from `summary` + a bounded prefix of `body` (default: first ~512 tokens of body).
- Produced by an **OpenAI-compatible embeddings endpoint the org configures at deploy** (`EMBEDDING_ENDPOINT`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`). A self-hosted vLLM/Ollama endpoint satisfies "data must not leave the org."
- The store records `embedding_model` + dimension per entry. The vector column has a **fixed dimension at deploy time** (`EMBEDDING_DIM`, default 1536).
- **Changing the embedding model or dimension is an operator-run re-embedding migration** (re-embed all active entries, swap the column). This is a named maintenance procedure, not an online feature.

## 8. Identity, credentials, deployment

### 8.1 Credentials (ADR 0008)

| Credential | Binds | Effect |
|---|---|---|
| **user key** | the human author | Entry's `author` = key's user; the agent self-reports its instance id via header/body |
| **agent sub-key** | (user, agent instance) | `author` **and** `agent` are server-filled from the key — agent attribution is verified, not self-reported |
| **admin key** | operator | May withdraw any entry |

v1 has no OAuth/SSO; keys are issued by the org operator. (An org with an IdP is a v2 story.)

### 8.2 Deployment (ADR 0007)

One **self-hosted instance per organization**, one `docker compose` file: **Hivemind service + PostgreSQL 16 (pgvector)**. No Redis, no sharding, no queue.

**Scale assumptions (spec target):** ~50 users/agents, ~500 sessions/day, ~10k entries/day, single Postgres node. The design does not commit to horizontal scale; when the assumptions stop holding, §10's extensions apply.

### 8.3 The kill switch (ADR 0003)

Turning Hivemind off for a session is a **client-side act**: the agent's Hivemind integration (its MCP server entry / enabled flag) is disabled for that session, so the tools simply aren't available and the pool is untouched. **The server has no session registry and is unaware of off sessions.** The spec's only server-side commitment is that an absent client is indistinguishable from a quiet one.

## 9. Non-goals (v1) — the explicit list

- No human-facing UI (agent-only; a read-only web search is a later extension)
- No passive capture of transcripts/tool events (explicit writes only, ADR 0004)
- No knowledge graph, no entity extraction, no auto-contradiction detection
- No curation/verification workflow, no trust tiers, no PII pipeline
- No personal spaces, channels, or multi-tenant SaaS
- No binary/artifact storage (references only)
- No OAuth/SSO, no per-agent learned retrieval tuning
- No HA, sharding, or Redis-backed scale-out

## 10. Documented extensions (out of v1 scope, by design)

| Extension | Trigger to build it |
|---|---|
| **Passive capture** — ingest raw session transcripts, distill with an LLM (crash-safe, dedup'd) | "Agents forget to write" becomes the bottleneck |
| **Private staging + publish** — write to a personal scratch, promote to the pool | Analysts want to try analyses before sharing |
| **Namespaces / channels** — team/project/topic scopes with membership | The flat pool grows too noisy; `scope` tag is the seam |
| **Binary artifacts** — S3-backed artifact store behind `sources` | Analysis references outgrow file/URL references |
| **Knowledge graph** — entity extraction + graph-expanded retrieval | Cross-entry entity linking pays off in retrieval quality |
| **LLM-assisted contradiction detection** — flag likely conflicts for human review | Explicit supersession can't keep up with contradictory writes |
| **Curation workflow** — verify/promote/retire roles | An org wants a "librarian" function |
| **Human read-only UI** — browse/search the pool in a browser | Analysts want to see the pool without an agent |
| **Multi-tenant SaaS / OAuth** | More than one org wants it; an org has an IdP |
| **Learned per-agent retrieval tuning** | Static decay factors stop beating per-agent profiles |

## 11. Open questions (resolve at implementation, not spec)

1. **Language/runtime** for the Hivemind service (Node/TypeScript vs. Python) — pick at build time; the spec is language-neutral.
2. **Exact FTS config** (language, normalization, whether to adopt a BM25 extension) — implementation detail of §6.2.
3. **Pagination style** — offset (v1) vs. cursor (later) — implementation detail.
4. **`hive_list` default order** — most recent first is the assumed default; confirm in first implementation.
5. **Embedding prefix length** (default 512 tokens of body) — tune during implementation against real long-form entries.