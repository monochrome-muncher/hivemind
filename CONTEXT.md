# Hivemind

Hivemind is a shared memory service for an organization's AI agents: one organization runs one Hivemind, and all of its agents read and write a single pool of entries, so work done with one agent is discoverable work for every other agent in the organization.

This file is the canonical glossary for the project. It is a vocabulary, not a spec — for behavior, see `SPEC.md`; for decisions and their reasons, see `docs/adr/`.

## Language

### Core concepts

**Hivemind**:
The shared memory service for one organization. One organization, one pool (partitioned by fleets and trust levels — §12, ADR 0011).
_Avoid_: backend, memory database

**Entry**:
One unit of memory: a distilled observation, analysis, or decision written by an agent, visible according to its scope and the reader's trust level (§12, ADR 0011).
_Avoid_: memory, record, note, post

**Kind**:
The category of an entry: `fact`, `insight`, or `decision`.
_Avoid_: type, category (the API field is `kind`)

**Fact**:
A dated, checkable observation about the world (e.g., "the churn model uses weekly cohorts").
_Avoid_: claim, datum

**Insight**:
A long-form analysis or finding: a short summary plus a long body, optionally referencing artifacts.
_Avoid_: report, deep-dive (in prose)

**Decision**:
A choice the organization (or a part of it) made, with its rationale.
_Avoid_: ruling, resolution

### Provenance

**Author**:
The verified writer of an entry: the writing agent's registered name, filled server-side from the agent key (ADR 0012). The author is accountable for the entries under that name.
_Avoid_: owner (that is the human's alias, not the entry's writer), creator, user

**Agent**:
A registered identity in the cluster: a unique name bound to one agent key (trust level + home fleet, ADR 0012). Recorded on every entry as `author` — server-verified, never self-reported.
_Avoid_: bot, worker, client, user

**Embedding model**:
The model that produced an entry's vector; recorded on the entry so vector provenance is traceable (SPEC §7, ADR 0005).
_Avoid_: embedding service, vector store

**Embedding dimension**:
The fixed length of an entry's vector — a deploy-time decision baked into the pool's `vector(:dim)` column at migration time (SPEC §7, ADR 0005). The default is 1024 (ADR 0015); the dev Makefile pins 512 for fast local vLLM embedding. A mismatch between the pool's dim and the configured dim is a loud error at migrate time, never a silent assumption (ADR 0015).
_Avoid_: vector width, embedding size, dim (as a bare noun)

**Provenance**:
The origin trail of an entry: author, agent, occurrence time, creation time, and source references.
_Avoid_: audit, lineage

### Lifecycle

**Supersession**:
The explicit replacement of one entry by a newer one; the superseded entry is retained and hidden by default. Supersession is a claim, not an arbitration — the reading agent decides.
_Avoid_: overwrite, update, invalidate (say "supersede"; never "update an entry" — entries are immutable)

**Supersession chain**:
The ordered set of versions reachable from an entry by following supersession links — its successors (what replaced it) and its superseded (what it replaced). The `?history` walk (SPEC §5.1).
_Avoid_: version history, audit trail

**Withdrawal**:
Marking an entry no longer valid without replacing it (retraction, or admin correction). Withdrawn entries are retained, never deleted.
_Avoid_: delete, purge

**Quality**:
The bounded re-scoring signal derived from feedback on an entry; it affects retrieval, not governance.
_Avoid_: score (in specs, "score" always means the retrieval score; "quality" is the feedback-derived factor)

**Feedback**:
A reporting agent's verdict that an entry it relied on was `helpful`, `stale`, or `wrong`. Aggregated into an entry's quality.
_Avoid_: rating, review

### Time

**Occurrence time**:
When the observation behind an entry was actually made; may predate creation (backdating allowed). The basis of the "memory date" filter.
_Avoid_: timestamp (always say occurrence time or creation time explicitly)

**Memory date**:
The agent-facing name for occurrence time: when the observation/analysis actually happened, not when it was written into the pool.
_Avoid_: created_at, ingest time (that is creation time)

**Creation time**:
When the entry was written into the pool (server-assigned, immutable).
_Avoid_: timestamp

### Session control

**Kill switch**:
Disabling an agent's Hivemind integration for a session (e.g., during non-analyst work). A client-side act: the server has no session concept and is unaware of off sessions.
_Avoid_: pause, session disable, opt-out

**Scope**:
The intended audience of an entry: `self` (the writing agent only), `fleet` (the home fleet it was written into — fixed at write time), or `org` (legacy, read-only). An omitted scope resolves to the highest value the writer's trust level permits (§12, ADR 0011).
_Avoid_: namespace, channel (those are the §10 extensions), visibility

### Access (ADR 0011–0012)

**Fleet**:
A named group of agents that can share `fleet`-scoped entries. Created by the admin; no deletion in this increment (ADR 0011).
_Avoid_: team, group, channel (the §10 extension), org (that is the whole cluster)

**Home fleet**:
The single fleet an agent belongs to (ADR 0011). Admin-assigned and re-assignable; an agent's earlier entries stay in the fleet they were written into — moving an agent does not move its entries.
_Avoid_: primary group, default fleet

**Trust level**:
The agent's cumulative privilege ladder, 0–3: `untrusted` (nothing), `lurker` (own + home-fleet read, own write), `contributor` (+ home-fleet write), `privileged` (+ read across all fleets, writes stay local).
_Avoid_: role, permission, clearance

**Pending agent**:
An agent that has registered (unique name + owner alias) but not been activated: trust level `untrusted`, no data-plane access. Activation by the admin issues its agent key (ADR 0012).
_Avoid_: registered agent (ambiguous with active), queued agent, provisional agent

**Registration**:
An agent's first contact with Hivemind: a unique agent name plus the owner's alias, creating a pending record (ADR 0012). Names are durable — revocation does not release a name.
_Avoid_: signup, onboarding, enrollment

**Owner alias**:
The human name or address (username or email) an agent self-reports at registration so the admin can deliver its agent key out-of-band (ADR 0012). A contact field on the agent record, not a per-entry provenance claim.
_Avoid_: author (that is the entry's verified writer), user, account

**Org key**:
The single credential shared by the whole cluster (ADR 0012). It gates registration + health only — all data-plane privilege comes from the agent key. Rotating it is the cluster-wide kill switch.
_Avoid_: shared key, membership key, cluster key

**Admin key**:
The single credential gating the admin surface: agent/fleet listing, activation, promote/demote, revoke, fleet creation, org-key rotation (ADR 0012).
_Avoid_: operator key, root key, superuser

### Surfaces

**MCP runner**:
The process that exposes Hivemind's seven `hive_*` tools to an agent. Three kinds: the dev runner (`hivemind-mcp`, in-memory, stdio), the per-agent Postgres-backed runner (`hivemind-mcp-pg`, ADR 0009, one process per agent), and the hostable streamable-HTTP runner (`hivemind-mcp-http`, ADR 0010, one shared process, many agents). All read/write the same pool; every write carries verified provenance.
_Avoid_: Hivemind client (implies a library client), agent connector

**Agent key**:
The admin-issued credential bound to one registered agent (ADR 0012, supersedes the ADR 0008 sub-key). Carries the agent's trust level and home fleet; it is what the MCP runners present so their writes carry verified provenance. One key per agent, returned once at activation.
_Avoid_: sub-key (the ADR 0008 term), token, API key (that is the generic REST term; say "agent key")

**Per-request credential**:
The acting identity a hostable `hivemind-mcp-http` process resolves **per HTTP request**: the request presents an agent key (ADR 0012), a thin ASGI middleware verifies it against the `credentials` table (ADR 0012), and the shared, stateless services are re-bound to that credential on every tool dispatch. Because it is resolved per request, revocation is immediate (ADR 0010).
_Avoid_: agent key (that is the credential itself; this term is the per-request resolution of it), session identity

### Retrieval

**Hybrid search**:
The v1 search pipeline: keyword (Postgres FTS) and vector (pgvector) streams, fused by RRF, then decay-aware rescore (SPEC §6).
_Avoid_: full-text search, semantic search (each is one stream alone)

**Hit**:
A compact search result: entry id, kind, summary, key metadata, and the retrieval score — deliberately no body. The full entry is opened on demand (progressive disclosure).
_Avoid_: result, snippet, match

**Progressive disclosure**:
The token economy of retrieval: scan many compact hits first, open the full entry only when needed.
_Avoid_: lazy loading, pagination

### Entities (ADR 0016)

**Extracted entity**:
A machine-derived `{name, kind}` facet of an entry, produced by the write-time LLM extractor (SPEC §13). `name` is open vocabulary; `kind` is a closed vocabulary (`person | organization | system | service | artifact | concept`). Set once at write time, never mutated (ADR 0001).
_Avoid_: tag (that is an agent-declared facet, not a machine-derived one), domain entity (that is a programming class in `domain/`)

**Entity facet**:
The per-entry entity list as a filterable retrieval facet (AND over names, case-insensitive). A precision instrument, not a graph node — no cross-entry linking in v1.
_Avoid_: knowledge graph (that is the held §10 extension), entity registry (a later, canonical step)

**Extractor**:
An OpenAI-compatible chat model configured at deploy time (a sibling of the embedder port): a fixed prompt + structured JSON output, validated against the entity schema. Optional (unset ⇒ off) and best-effort (a failure never blocks the write — ADR 0016).
_Avoid_: LLM agent (there is no tool-calling or multi-turn; one fixed prompt, one structured response), Pydantic AI (a different shape of problem)

**Entity kind**:
The closed type vocabulary of an extracted entity (`person | organization | system | service | artifact | concept`) — a closed set over open names. Stored + displayed; not filterable in v1.
_Avoid_: entity type taxonomy (it is a fixed six-value set, not a growing taxonomy)

### Evaluation (ROADMAP §1.1)

**Golden set**:
The committed, deterministic entry corpus + query set (each query → the entry(ies) it should surface) that the retrieval eval runs on (`tests/eval/golden.py`). Fixed and small on purpose: it is the yardstick, not a benchmark.
_Avoid_: benchmark corpus, fixture set, test data

**Retrieval metrics** (hit@k / MRR / nDCG):
The signals the eval reports over the golden set: fraction of queries with a relevant hit in the top-k, mean reciprocal rank of the first relevant hit, and normalized DCG (ranking quality) — the measurable dials for the ~10 config knobs (ROADMAP §1.1).
_Avoid_: search quality score, ranking score (that is a per-hit score), relevance score

**Eval gate**:
The CI floor pinned on the retrieval metrics: an improvement passes, a regression below the bar fails the suite (retrieval quality is *measured*, not vibes — ROADMAP §1.1).
_Avoid_: quality threshold, search-quality SLA, retrieval budget

### Operations (Tier 3)

**Forward migration**:
How a schema change lands on a live pool without a rewrite: the idempotent `schema.sql` re-apply (the DDL forward path) + the occasional ordered, idempotent data-migration script (a backfill DDL can't express) — ADR 0013.
_Avoid_: schema upgrade, DB release, data patch (that is the data-migration script alone)

**Schema version**:
The applied schema generation, recorded in the `schema_migrations` table after each successful migrate (ADR 0013); reading it tells you whether a live pool is up to date (drift check).
_Avoid_: DB version, migration cursor, schema fingerprint

**Usage counters**:
The minimal operational metrics surface (`GET /v1/metrics`, admin-gated): entries / fleets / agents counters (trust-level distribution, writes per fleet, pending-agent count) that make the SPEC §10 usage-based triggers measurable rather than guesswork (ROADMAP §3.3).
_Avoid_: analytics, telemetry (that is the broader §10 story), dashboards (a UI is a non-goal, SPEC §9)

### Configuration (ADR 0017)

**Environment profile**:
The per-environment dotenv file selected by the `ENVIRONMENT` env var (`production` → `.env.production`, `staging` → `.env.staging`, `test` → `.env.test`, anything else / unset → `.env.local` — ADR 0017). Real env vars always win over file values; a missing file is silently ignored (the Kubernetes / CI posture: values come from env / Secrets, no file present).
_Avoid_: env file, dotenv (the bare `.env` is retired), environment config (that is the env vars themselves)