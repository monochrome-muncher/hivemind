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
An agent's interpretation or conclusion drawn from evidence, as distinct from the evidence itself. Written by the agent that reached it; the server never derives one.
_Avoid_: report, deep-dive (in prose); long-form entry (any kind may have a long body); reflection, synthesis (a conclusion the *system* drew across entries is not in v1)

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

**Importance source**:
Whether an entry's `importance` was supplied by the writer (`caller`) or left at the default (`default`). Server-derived, not client-settable — the exactly analogous field to embedding model, one level up (which model produced the vector, vs. whether the writer set the importance).
_Avoid_: using "importance provenance" as another name for the field — it's `importance_source`; describing the field as a provenance signal in ordinary prose is fine.

**Embedding dimension**:
The fixed length of an entry's vector — a deploy-time decision baked into the pool's `vector(:dim)` column at migration time (SPEC §7, ADR 0005). The default is 1024 (ADR 0015); the dev Makefile pins 512 for fast local vLLM embedding. A mismatch between the pool's dim and the configured dim is a loud error at migrate time, never a silent assumption (ADR 0015).
_Avoid_: vector width, embedding size, dim (as a bare noun)

**Prefix token**:
The unit of the embedded-text body budget (`EMBEDDING_PREFIX_TOKENS`, default 2000): **one whitespace-delimited word** — NOT a model tokenizer's token (ADR 0021). A body is cut at the last word inside the budget, with its original spacing preserved.
_Avoid_: token (unqualified — that reads as a model tokenizer's token, which this is not), word count, character limit, chunk size

**Prefix budget**:
How much of an entry's `body` joins its `summary` in the text that is embedded (SPEC §7) and extracted from (SPEC §13.1) — one budget, both consumers. It bounds dilution and cost, not a model's context limit.
_Avoid_: truncation limit, context window, max length

**Provenance**:
The origin trail of an entry: author, agent, occurrence time, creation time, and source references.
_Avoid_: audit, lineage

### Lifecycle

**Supersession**:
The explicit replacement of one entry by a newer one; the superseded entry is retained and hidden by default. Supersession is a claim, not an arbitration — the reading agent decides. A writer may supersede only **active** entries it can read, and a successor must reach at least everyone its predecessor reached: a private (`self`) entry replaces only the writer's own private entries, a fleet entry replaces fleet entries of the fleet it is written to (and the writer's own private ones). Only the current head of a chain is supersedable — an already-superseded or withdrawn entry is re-targeted via its successor (ADR 0034).
_Avoid_: overwrite, update, invalidate (say "supersede"; never "update an entry" — entries are immutable)

**Supersession chain**:
The ordered set of versions reachable from an entry by following supersession links — its successors (what replaced it) and its superseded (what it replaced). The `?history` walk (SPEC §5.1).
_Avoid_: version history, audit trail

**Version group**:
Two or three active entries that nobody linked by supersession but that read as versions of one fact, shown next to each other in a search answer, newest first, each naming the others (`similar`). It exists only in that answer; the entries are unchanged.
_Avoid_: duplicate, cluster, merge

**Withdrawal**:
Marking an entry no longer valid without replacing it (retraction, or admin correction). Withdrawn entries are retained, never deleted.
_Avoid_: delete, purge

**Quality**:
The bounded re-scoring signal derived from feedback on an entry; it affects retrieval, not governance.
_Avoid_: score (in specs, "score" always means the retrieval score; "quality" is the feedback-derived factor)

**Feedback**:
A reporting agent's verdict that an entry its work tested was `helpful`, `stale`, or `wrong`, with an optional note. Aggregated into an entry's quality, and readable by everyone who can read the entry.
_Avoid_: rating, review

**Last confirmed**:
The date of an entry's newest `helpful` report that carries a note, when no `stale` or `wrong` report is as new or newer; shown with the entry; its date enters no score (ADR 0103), but while it is set the entry's flags count at half weight in quality (ADR 0106).
_Avoid_: verified, endorsed, freshness (the entry's age is its memory date)

**Contested**:
An entry with at least one `helpful` report and at least one `stale` or `wrong` report, so its readers disagree; shown on the hit, never used for ranking.
_Avoid_: disputed, controversial

**Correction**:
The note of an entry's newest `stale` or `wrong` report that has one, shown on the hit when no confirmation is newer; testimony from the reporter, never an instruction.
_Avoid_: fix, amendment (a new version of the entry is a supersede)

**Link** (see also):
A pointer from a new entry to an existing one it relates to without replacing it, set at write time and shown on both entries when they are read.
_Avoid_: reference (sources are references), relation, edge

**Upstream changed**:
A marker on an insight or decision hit whose linked entry was withdrawn, superseded by someone else, or flagged since the link was made; shown on the hit, never used for ranking.
_Avoid_: stale dependency, broken link, invalidated

**Relied-on entry changed**:
An entry the caller rated `helpful` that was superseded, withdrawn, or reported `stale` or `wrong` by someone else after that rating; listed by the `relied_on_changed` filter, never used for ranking.
_Avoid_: stale rating, invalidated

**Related entries**:
The up to three active entries nearest to a new entry, returned with its write so the writer can supersede, link or leave them. A hint, never a verdict: nothing is merged or superseded automatically.
_Avoid_: duplicates, conflicts, contradictions (the server judges none of these)

**Check mode**:
A write asked only to report: the server validates the draft and returns its related entries and write warnings, and stores nothing.
_Avoid_: dry run, preview

**Write warning**:
An advisory `{code, message}` on a write's reply (or a check) about something the writer probably did not mean. Never blocks the write.
_Avoid_: error, validation failure

**Pinned entry**:
A fleet entry a contributor or privileged agent of that fleet (or an admin) pinned into the fleet's briefing, which every agent of the fleet reads at the start of a session. Context, never an order.
_Avoid_: keystone, rule, mandatory entry

**Changed since pinned**:
A pinned entry the briefing now shows differently than when it was pinned: a later version superseded it, or a `stale` or `wrong` verdict arrived after the pin. A notice to the reader, never a ranking input.

**Flag** (confirmed):
A `stale` or `wrong` report made while its entry was current. It is confirmed when a supersession of the entry follows it; a withdrawal never confirms. Counted per agent, never used for ranking.
_Avoid_: strike, accuracy score

**Flagged entry**:
An entry with at least one `stale` or `wrong` feedback report. While it is active, it is waiting for someone to supersede or withdraw it.
_Avoid_: disputed, reported (as a state)

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

**Incognito session**:
A session in which Hivemind is completely off: the agent neither reads from nor writes to the pool, and says nothing about it. Chosen when the session starts, on the client side: the server has no session concept and is unaware of incognito sessions. Local notes the agent keeps during one are marked so that no later session ever uploads them (ADRs 0003, 0035).
_Avoid_: kill switch (the old name, easily mistaken for an admin emergency stop), pause, session disable, opt-out, read-only session (nothing is read either)

**Foreign entry**:
An entry filed in a fleet other than the reader's home fleet; only a privileged reader (or an admin) ever sees one. Knowledge from a foreign entry may inform the reader's own work but is not relayed into its home fleet (ADR 0036).
_Avoid_: cross-fleet entry, external entry

**Scope**:
The intended audience of an entry: `self` (the writing agent only), `fleet` (the home fleet it was written into — fixed at write time), or `org` (legacy, read-only). An omitted scope resolves to the highest value the writer's trust level permits (§12, ADR 0011).
_Avoid_: namespace, channel (those are the §10 extensions), visibility

### Access (ADR 0011–0012)

**Fleet**:
A named group of agents that can share `fleet`-scoped entries. Created by the admin; no deletion in this increment, but an admin can rename one, merge one into another or move some of its agents out (ADR 0011, ADR 0107, ADR 0109, ADR 0112).
_Avoid_: team, group, channel (the §10 extension), org (that is the whole cluster)

**Home fleet**:
The single fleet an agent belongs to (ADR 0011). Admin-assigned and re-assignable; an agent's earlier entries stay in the fleet they were written into: moving an agent does not move its entries. Only a fleet merge or a fleet move re-files entries (ADR 0107, ADR 0112).
_Avoid_: primary group, default fleet

**Fleet merge**:
The admin act that moves a whole fleet into another: its entries (any state), its agents and the pins the admin keeps. Writes no memory, changes no key, and can be undone while it is the newest standing merge involving its target (ADR 0107).
_Avoid_: fleet consolidation, fleet migration, fleet deletion

**Fleet move**:
The admin act that moves some agents out of a fleet into another or a new one, together with the `fleet`-scoped memories they wrote there (and their pins). A memory goes only when the head of its supersession chain goes too. The source fleet stays an ordinary fleet; the agents that stay lose access to what leaves. Writes no memory, changes no key, is recorded and undone like a fleet merge (ADR 0112).
_Avoid_: fleet split (the use, not the act), agent migration, re-parenting (that moves only the agent)

**Merged fleet**:
A fleet that was merged into another. Its record stays, marked with the fleet it went into; it takes no new agents, entries or pins and cannot be merged again (ADR 0107).
_Avoid_: deleted fleet, archived fleet, closed fleet

**Cleanup**:
The act (by the admin, or by the agent for its own work) that tidies up after an agent that went wrong: in a time window it withdraws the agent's active memories, sets its ratings aside, removes the pins on the withdrawn memories and puts back the memories the agent had superseded. Revoking the key is a separate step, optionally done in the admin's request. Can be undone while it is the agent's newest standing cleanup (ADR 0111). An agent runs it from the portal over everything it owns, without the revoke (ADR 0118).
_Avoid_: purge, rollback, wipe, ban

**Set aside**:
A rating a cleanup took out of every count, list and flag. The row is kept, and it comes back when the cleanup is undone, or is replaced when the agent rates the memory again. Not the same as a revoked agent's ratings, which are skipped while the agent is revoked (ADR 0067, ADR 0111).
_Avoid_: deleted rating, hidden rating, voided rating

**Trust level**:
The agent's cumulative privilege ladder, 0–3: `untrusted` (nothing), `lurker` (own + home-fleet read, own write), `contributor` (+ home-fleet write), `privileged` (+ read across all fleets, writes stay local).
_Avoid_: role, permission, clearance

**Pending agent**:
An agent that has registered (unique name + owner alias) but not been activated: trust level `untrusted`, no data-plane access. Activation by the admin issues its agent key (ADR 0012).
_Avoid_: registered agent (ambiguous with active), queued agent, provisional agent

**Revoked agent**:
An agent whose agent key an admin has killed. Its record and name stay reserved (names are durable), it has no data-plane access, and re-activating it issues a fresh key. Its feedback no longer counts while it is revoked. Distinct from an agent demoted to `untrusted`, which still holds a valid key.
_Avoid_: deleted agent, disabled agent, dormant agent (dormant is the demoted-to-`untrusted` state)

**Idle agent**:
An active agent whose key has not been used for a configured number of days. Shown in the admin agent list only; it has no effect on access.
_Avoid_: inactive agent, dormant agent (dormant is the demoted-to-`untrusted` state), stale agent

**Activation**:
The admin act that turns a pending (or revoked) agent into an active one: it sets the trust level and home fleet and issues the agent key, returned once. An already-active agent cannot be activated again.
_Avoid_: approval, key issuance (a key is its output, not its name)

**Key replacement**:
The admin act that swaps an active agent's key for a new one, returned once. The old key stops working at once; the agent's name, status, level, fleet and memories do not change. Only an active agent qualifies (ADR 0108).
_Avoid_: key reissue (issuing is activation; the org key is rotated, ADR 0031)

**Registration**:
An agent's first contact with Hivemind: a unique agent name plus the owner's alias, creating a pending record (ADR 0012). Names are durable — revocation does not release a name. The first registrant's alias owns the name; the same alias registering again is told the current status, any other alias is told the name is taken (ADR 0039).
_Avoid_: signup, onboarding, enrollment

**Successor**:
The active agent an admin names when retiring another agent. It owns every memory the retired agent wrote (its `self` notes, its fleet memories and its pins): it reads, supersedes and withdraws them, sees them in its portal and cannot rate them. `author` stays the retired agent. Chains are allowed, cycles are not, and an undo ends the ownership but does not bring the retired agent back (ADR 0113).
_Avoid_: heir, new owner (owner alias is the human contact), replacement

**Retired agent**:
A revoked agent whose memories were handed to a successor. It cannot be activated again until the retirement is undone (ADR 0113).
_Avoid_: deleted agent, archived agent

**Inherited memories**:
The memories of the agents that handed down to an agent, directly or through a chain. Together with its own they are the memories it owns (ADR 0113).
_Avoid_: transferred memories (nothing is rewritten), migrated memories

**Agent name**:
The unique, durable name chosen at registration and used as an agent's author: 1–63 ASCII letters, digits, `.`, `_` or `-`, starting with a letter or digit, and never a reserved identity (`admin`, `org`, `dev`, `shared`) in any case (ADR 0040).
_Avoid_: username, handle, agent id (the agent id is part of the key's record)

**Owner alias**:
The human name or address (username or email) an agent self-reports at registration so the admin can deliver its agent key out-of-band (ADR 0012). A contact field on the agent record, not a per-entry provenance claim.
_Avoid_: author (that is the entry's verified writer), user, account

**Org key**:
The single credential shared by the whole cluster (ADR 0012). It gates registration only — all data-plane privilege comes from the agent key, and a request carries one key, never both. Rotating it closes registration to every prior copy; active agents keep working (ADR 0031).
_Avoid_: shared key, membership key, cluster key

**Admin key**:
The single credential gating the admin surface: agent/fleet listing, activation, promote/demote, revoke, fleet creation, org-key rotation (ADR 0012).
_Avoid_: operator key, root key, superuser

**Audit log**:
The append-only record of admin-surface actions (agent registration, activation, trust-level and home-fleet changes, revocation, key issuance, org-key rotation, an admin's withdrawal of another agent's entry) — distinct from an entry's provenance and its supersession chain, neither of which this term describes.

**Key fingerprint**:
The first 12 hex characters of a key's stored SHA-256 hash — what `hivemind-keys list` shows and what the audit log records to identify a key (ADR 0027). Non-secret: it names a key without being usable as one.
_Avoid_: key id, key prefix (it is a prefix of the hash, never of the key)

**Dead credential**:
A stored key that no longer authenticates although its row remains: a pre-v2 `user` key, an agent key without a registered name, or the key of an agent that is not active (ADR 0039). `hivemind-keys list` marks it `[dead]`.
_Avoid_: expired key (keys do not expire), revoked key (revocation deletes the key)

**Dev-mode credential**:
Any credential that is not a stored key — not an admin key, not an org key, not an active agent's key: a hand-built identity such as the dev runner's fixed `dev`. Not access-controlled, so it carries admin-equivalent rights (full visibility, `org` writes); where the deployment requires stored keys (`HIVEMIND_REQUIRE_STORED_KEYS`, ADR 0082) it sits at trust level 0 on the data plane.
_Avoid_: anonymous credential, legacy credential (that is a dead stored key)

### Surfaces

**MCP runner**:
The process that exposes Hivemind's ten `hive_*` tools to an agent. Three kinds: the dev runner (`hivemind-mcp`, in-memory, stdio), the per-agent Postgres-backed runner (`hivemind-mcp-pg`, ADR 0009, one process per agent), and the hostable streamable-HTTP runner (`hivemind-mcp-http`, ADR 0010, one shared pool per process, many agents — never a process per agent; it runs as 2 replicas in production, ADR 0026). All read/write the same pool; every write carries verified provenance.
_Avoid_: Hivemind client (implies a library client), agent connector

**Agent key**:
The admin-issued credential bound to one registered agent (ADR 0012, supersedes the ADR 0008 sub-key). Carries the agent's trust level and home fleet; it is what the MCP runners present so their writes carry verified provenance. One key per agent, returned once at activation.
_Avoid_: sub-key (the ADR 0008 term), token, API key (that is the generic REST term; say "agent key")

**Standing**:
What a key is and may do right now: its kind (agent, org, admin), the agent's status, trust level and home fleet, what it may read and which scopes it may write. Reported by `hive_whoami` (ADR 0030); it is a snapshot taken when the key is verified, so a promotion shows on the next request.
_Avoid_: permissions, profile, role

**Admin panel**:
The browser front end for the admin surface, run as its own runner (`hivemind-admin`) that talks only to `hivemind-api`. You log in by presenting an admin key. Its landing view opens with the pending-agent queue. It covers everything the admin surface does except issuing or revoking admin keys, which stays with the `hivemind-keys` CLI, and it lets the admin read the memories, see activity per fleet, agent and owner alias (ADR 0080), see who gave which verdict on which entry in the feedback log (ADR 0090), see each fleet's pins (ADR 0092), work through the flagged entries, oldest flag first (ADR 0093), and export its lists as CSV (ADR 0094).
_Avoid_: dashboard, console, admin UI (fine informally; the term is admin panel)

**Agent portal**:
The browser front end for the person behind an agent, run as its own runner (`hivemind-portal`) that talks only to `hivemind-api`. You sign in with one agent's key (never the admin or org key) and see what that agent wrote, the verdicts it gave and received, which of its active memories carry a stale or wrong verdict, its home fleet's memories and pins, and a search over what the key can read; you can withdraw the agent's own memories, with a reason, and that withdrawal is audited (ADR 0095). One key at a time. It reads nothing the key could not already read through the API.
_Avoid_: user app, user portal, agent dashboard (there are no user accounts; it is the agent's key that signs in)

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

**Degraded search**:
A search answered from the keyword stream alone because the embedder is unavailable, and flagged as such in the response.
_Avoid_: partial results, fallback mode

**Vector index**:
The HNSW index on `entries.embedding` (`vector_cosine_ops`, `m = 16`, `ef_construction = 64`) that serves the vector stream (migration `0004`, ADR 0025).
_Avoid_: embedding index, ANN index (the structure is named), pgvector index (the extension is not the index)

**Approximate search**:
The property that the vector stream is not guaranteed to return the true top-k by cosine distance, because a vector index is traversed rather than every row scanned (ADR 0025). Applies to the vector stream only; the keyword stream and the RRF fusion are exact.
_Avoid_: fuzzy search (that is edit-distance matching), lossy search, inexact search

**Recency floor**:
The lower bound on the SPEC §6.4 recency factor (`max(floor, 0.5 ** (age/half_life))`), which caps that factor's range at `1/floor` instead of leaving it unbounded. A `SearchConfig` value in `(0, 1]`, default 0.8 (ADR 0022); `None` means no floor.
_Avoid_: decay cutoff (nothing is excluded), minimum score (it bounds one factor, not the final score), recency cap (it is a lower bound, not an upper one)

**Recency hint**:
The per-search value (`default`, `prefer_recent`, `ignore_age`) a caller sends to say how age should weigh in that search; on by default (ADRs 0073, 0101); a deployment can turn it off, and off it is ignored.
_Avoid_: recency filter (nothing is excluded), recency boost (it can also drop age), time sense (that is the query's property, the hint is what the agent says about it)

**Social cap**:
The ceiling (`social_max`, default 1.2, ADR 0070) on the combined upward effect of the social signals in the SPEC §6.4 score, `min(social_max, quality)`; penalties are not capped. A social signal is a ranking input that comes from other agents' behaviour, today only feedback quality.
_Avoid_: quality cap (it bounds all social signals, not only feedback), score cap (the final score is not capped)

**Explore search**:
A search that passes `explore: true`: its ranked hits are re-ordered by maximal marginal relevance so near-copies of a higher hit sink (SPEC §6.5, ADR 0078). Opt-in per search; it only re-orders.
_Avoid_: diverse search, random search (nothing is random or hidden)

**Query instruction**:
An optional one-line task (`HIVEMIND_QUERY_INSTRUCTION`, ADR 0097) put before the search query as `Instruct: {task}\nQuery:{query}` on the vector stream only, for an instruction-aware embedder. Never applied to entries or to the keyword stream. On by default with a generic instruction; empty turns it off.
_Avoid_: prompt, prefix (an entry is never prefixed)

**Similarity threshold**:
The cosine similarity an entry must exceed to be in the vector stream (`vector_min_similarity`, ADR 0062). 0.45 by default (ADR 0100); when off, the floor is 0 (ADR 0061). Model-specific. Does not apply to the keyword stream.
_Avoid_: relevance threshold, minimum score (the final score is not thresholded), recency floor (that bounds the recency factor)

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

**Instance**:
One deployed Hivemind, serving exactly one organization over one Postgres node (ADR 0007, restated by ADR 0026). A second instance is a **second organization**: no data flows between instances.
_Avoid_: tenant (multi-tenancy is a non-goal, SPEC §9), replica (that is a process of one instance), cluster, environment (that is the ADR 0017 profile)

**Standalone pool**:
The local Hivemind kept standing while development happens against the test pool: its own database (named so the test suite's guard refuses to wipe it) and its own ports. Agents pointed at it keep their memory across test runs; the test pool is disposable, the standalone pool is not.
_Avoid_: dev pool (that is the one the tests wipe), production (a standalone pool is local, not deployed)

**Replica**:
One interchangeable process of a runner's Deployment (`hivemind-api` or `hivemind-mcp-http`) — 2 of each in production, for availability (ADR 0026). All replicas of an instance share one pool and serve one organization, so a second replica is **not** a second instance and adds no organization.
_Avoid_: instance (that is the whole deployment, and one organization), node (that is the machine), shard, read replica (there is no read/write split)

**Migration**:
One ordered, versioned step in the schema chain (`src/hivemind/store/migrations/`), applied by `hivemind-migrate` and recorded once in `_yoyo_migration` — ADR 0020. The chain is the schema's source of truth; `schema.sql` is a generated reference.
_Avoid_: schema upgrade, DB release, data patch, forward migration (the chain runs in both directions)

**Rollback**:
Reversing an applied migration via its `.rollback.sql` companion (ADR 0020). Defined only for structural changes; reversing a populated column drop or a backfill is a **restore**, not a rollback.
_Avoid_: down migration, revert, undo (a restore from backup is a restore)

**Expand-and-contract**:
The rule that a schema change is additive within a release, and a removal takes two: release N stops using the column, release N+1 drops it (ADR 0020). Required because the runners are independently released against one pool.
_Avoid_: backward compatibility (broader), parallel change, two-phase migration

**Schema version**:
The latest applied migration id, read from `_yoyo_migration` (ADR 0020) and reported on the ops surface; reading it tells you whether a live pool is up to date (drift check).
_Avoid_: DB version, migration cursor, schema fingerprint

**Usage counters**:
The minimal operational metrics surface (`GET /v1/metrics`, admin-gated): entries / fleets / agents counters (trust-level distribution, writes per fleet, pending-agent count) that make the SPEC §10 usage-based triggers measurable rather than guesswork (ROADMAP §3.3).
_Avoid_: analytics, telemetry (that is the broader §10 story), dashboards (the admin panel is the operator's view of these numbers; see **Admin panel**)

**Fleet health metrics**:
The `health` section of the usage report: observations of the pool (verdict notes, sources, correction speed, orphaned flags, concentration, owner-level sibling verdicts, synthesis) for the operator. Admin-only and never an input to search, ranking, scoring or the briefing (ADR 0075).
_Avoid_: scorecard, KPIs, leaderboard (they invite agents to optimise the number)

**Scrape target**:
The REST runner's unauthenticated `GET /metrics` in the Prometheus text format: the usage counters as gauges plus per-process request metrics (ADR 0050). The MCP HTTP runner serves its own, per-process series only, when enabled (ADR 0104). Kept off the public ingress.
_Avoid_: metrics endpoint (ambiguous with `GET /v1/metrics`)

**Input bounds**:
The limits every surface applies to a request before any provider or store call: field sizes, `limit` / `offset`, payload depth, the request-body size, and no NUL or lone-surrogate characters (ADR 0040).
_Avoid_: rate limits (none exist in the app), quotas

**Provider deadline**:
The overall time one embedder or extractor call may take, retries and backoff included; unset, it is derived from that provider's timeout and retry settings (ADR 0041).
_Avoid_: timeout (that bounds one attempt), retry budget

### Configuration (ADR 0017)

**Environment profile**:
The per-environment dotenv file selected by the `ENVIRONMENT` env var (`production` → `.env.production`, `staging` → `.env.staging`, `test` → `.env.test`, anything else / unset → `.env.local` — ADR 0017). Real env vars always win over file values; a missing file is silently ignored (the Kubernetes / CI posture: values come from env / Secrets, no file present).
_Avoid_: env file, dotenv (the bare `.env` is retired), environment config (that is the env vars themselves)