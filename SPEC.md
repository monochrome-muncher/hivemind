# Hivemind — v1 Specification

Status: **implemented** (the v1 core in §1–§11, extended by §12 fleets and trust levels and §13 entity extraction) · Scope: one organization, self-hosted, agent-facing only

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
| `summary` | text (≤ 280 chars) | **Required.** Short, always embedded, always shown in compact hits |
| `body` | markdown (optional) | Long-form content (the "analysis work"). Returned **in full** by `get`; never truncated server-side |
| `payload` | JSONB (optional) | Structured data (numbers, small JSON documents) |
| `sources` | array (optional) | `{type: path\|url\|session\|other, ref}` — provenance pointers to where the entry came from |
| `tags` | string[] (optional) | Free-form labels, filterable |
| `occurred_at` | timestamptz | **The "memory date."** When the observation/analysis actually happened. Defaults to now; **backdatable** (e.g., "fact learned from a January report, written up today") |
| `created_at` | timestamptz | Ingest time, server-assigned |
| `author` | agent name | The agent's registered name, server-filled from the agent key (never self-reported; ADR 0012) |
| `agent` | agent instance id | Framework/instance identifier (retired from writes in v2 — ADR 0012; kept in the schema for existing data) |
| `importance` | 1–5 (int) | Writer-declared; feeds retrieval scoring as a factor of 0.9..1.1, or 0.6..1.0 with `narrow_importance` off (§6.4, ADR 0088) |
| `importance_source` | `caller` \| `default` | **Server-derived, not client-settable** (ROADMAP §4.5): `caller` when the writer supplied `importance`, `default` when it fell out of the default (3). Rows written before migration `0002` are backfilled to `default` regardless of how `importance` was actually set — their true provenance predates the column and is unrecoverable, so `by_importance_source` under-reports `caller` for that pre-migration cohort |
| `scope` | `self` \| `fleet` \| `org` (legacy) | The entry's audience: the author agent only, the home fleet it was written into (fixed at write time), or the legacy org-wide value (read-only); an omitted scope resolves to the highest value the writer's trust level permits (§12, ADR 0011) |
| `fleet_id` | fleet id (nullable) | The fleet a `fleet` entry was written into, fixed at write time (§12.1) |
| `embedding` | vector(dim) | Generated at write time (§7) |
| `embedding_model` | text | The model that produced `embedding` (§7) |
| `entities` / `entities_model` | `{name, kind}`[] / text | Machine-extracted entity facets and the model that produced them; empty when extraction is off (§13) |
| `state` | `active` \| `superseded` \| `withdrawn` | Default `active` |
| `supersedes` | entry id[] (optional) | **Write-side input** (the stored inverse is `superseded_by`): ids of active entries this entry supersedes; targets flip to `superseded` |
| `see_also` | entry id[] (optional, ≤ 5) | **Write-side input**: entries the writer can read that this one relates to without replacing them, stored as links beside the entry (ADR 0057). Reads by id return `see_also` and `linked_from` (the active entries that link to it), limited to what the reader may read |
| `superseded_by` | entry id (nullable) | Set when superseded |
| `withdrawn_reason` | text (nullable) | Set on withdrawal |

**Entries are immutable after creation (ADR 0001).** No field of an existing entry is ever edited. Corrections happen by:
- **Supersession** — a new entry is written with `supersedes: [entry_ids]`; targets flip to `state=superseded`, `superseded_by` set. A supersession is a *claim*, not an arbitration — the reader judges — made within the audience the writer can address, and only at **active** targets: a writer may supersede only entries it can read, and the successor must reach at least everyone the predecessor reached (a `self` entry supersedes only the writer's own `self` entries; a `fleet` entry supersedes those plus `fleet` entries of the fleet it is written to; an admin supersedes any active entry with an `org` successor, or its own `self` notes with either — ADR 0060). A write naming a target it cannot read, a target the successor does not reach far enough to address, or a target that is no longer active (already superseded or withdrawn — re-target the current head) is rejected as a whole (ADRs 0033, 0034, 0060).
- **Withdrawal** — an entry is marked `withdrawn` (retracted / no longer reliable). Any user may withdraw **their own** entries (an agent also owns the entries of agents retired in its favour, §12.1); **any** entry can be withdrawn by the org operator/admin. Nothing is ever hard-deleted in v1.

**Check mode and write warnings.** `hive_write` with `check: true` (REST `POST /v1/entries?check=true`) runs everything a write validates (input bounds, scope resolution, the `supersedes` permission check, `see_also` resolution) and embeds the draft, then returns `related` and `warnings` and stores nothing: no entry, no supersession, no extractor call. It raises the same errors the real write would. A write's reply carries `warnings` too, a list of `{code, message}`; they are advisory and never block a write:

| Code | When |
|---|---|
| `local_path` | an `org` or `fleet` entry whose `summary` or `body` contains a local path (`/home/<name>`, `/Users/<name>`, `~/<name>`, `C:\Users\<name>` also as `C:/Users/<name>` or JSON-escaped; the POSIX roots are case-sensitive, the drive letter and `Users` are not; URLs, repo-relative paths and API routes such as `/users/42` do not match) |
| `foreign_link` | a `fleet` entry whose `see_also` target is a `fleet` entry of another fleet (a `supersedes` target of another fleet is already refused) |
| `backdated_successor` | a `supersedes` target has a later `occurred_at` than the new entry |
| `fresh_consolidation` | the entry supersedes two or more entries and its `occurred_at` is more than one day after the newest superseded one; the message asks for the newest observation unless the writer observed or re-checked it today |
| `no_primary_source` | an `org` or `fleet` `insight` or `decision` whose `sources` are all links to Hivemind entries (a bare entry id, `entry:<id>` or an `.../entries/<id>` URL), or are empty while it has `supersedes` or `see_also` links; an entry with no sources and no links is not flagged |
| `sources_dropped` | a `supersedes` target has `sources` and the new entry carries none of them |
| `near_duplicate` | one of the `related` entries (ADR 0052) not named in the draft's `see_also` has a cosine `similarity` of at least 0.90 (ADR 0087); the message names the closest and asks the writer to rate it if it says the same, supersede it if the new entry updates it, or link it with `see_also` if it is another point |
| `answers_open_question` | an active entry among the `related` ones (ADR 0052) is tagged `open-question` or `anomaly`, has a cosine `similarity` of at least 0.70 (provisional; ADR 0099) and is not named in the draft's `supersedes` or `see_also`, and the draft is not itself tagged `open-question` or `anomaly`; at most one warning, naming the closest (and how many more qualify); the message asks the writer to write with `supersedes` (if allowed) or `see_also` to it if the entry answers it |

**Derived validity (ADR 0001, no `valid_until` column):** an entry is "active as of T" iff `created_at ≤ T` and it has no supersession/withdrawal state transition before T. Read-side time-travel questions ("what did the org know in March?") are answered by `created_at`/`occurred_at` range filters + `state`.

**Input bounds (ADR 0040).** Every surface enforces the same limits, before any embedder or store call, and answers a violation with **422** (REST `invalid_entry` / `invalid_input`) or `invalid_input` (MCP): no **U+0000** or lone surrogate in any string (nested `payload` strings and keys included) — Postgres cannot store them; caller-supplied timestamps within years 1900–2199; `body` ≤ 100 000 characters (a Postgres `tsvector` is capped at 1 MB); `tags` ≤ 32, each non-blank and ≤ 64 characters; `sources` ≤ 32, each `ref` ≤ 2048 characters; `payload` ≤ 64 KiB of compact JSON, nested ≤ 32 levels (no NaN/Infinity); `supersedes` ≤ 16 ids; batch feedback `entry_ids` ≤ 16 (ADR 0053); feedback `note` and withdraw `reason` ≤ 2000 characters. The text sent to the embedder and extractor is additionally capped at `max(20 000, 10 × prefix tokens)` characters of body (a character ceiling beside ADR 0021's word budget, for bodies with no whitespace).

### 4.2 Feedback

A lightweight, **dumb** outcome loop (no learned tuning in v1):

| Field | Notes |
|---|---|
| `entry_id` | Which entry the reporter relied on |
| `reporter` (user, agent) | Who reported; one feedback row per (entry, user, agent) — **upsert semantics**, latest wins. An agent key reports as its agent; the admin key always as `admin`, whatever `agent` it passes, so it has one vote per entry (ADR 0066) |
| `verdict` | `helpful` \| `stale` \| `wrong` |
| `note` | optional free text |
| `updated_at` | when the verdict was last set, dated by the store's clock (the one `created_at` and `pinned_at` use), never the calling pod's (ADR 0079) |

Feedback feeds a bounded `quality` multiplier used only by retrieval scoring (§6.4):

```
n = helpful + stale + wrong
quality = clamp( 1 + (0.2·helpful − 0.4·stale − 1.0·wrong) / (n + 2), 0.5, 1.2 )
```

Quality is a consensus share (ADR 0063). Each weight is where quality heads when every report gives that verdict (helpful → 1.2, stale → 0.6, wrong → 0, clamped to 0.5); the prior (2, `HIVEMIND_QUALITY_PRIOR`, must be > 0) counts imaginary neutral reports, so one report cannot swing it far, and helpful reports cannot be banked to cancel later wrong ones. Counts are per-entry (any reporter, except a revoked agent: its reports are left out of every count, list and the `flagged` filter while it is revoked, and return on re-activation; ADR 0067. A rating an admin cleanup set aside is left out the same way until the cleanup is undone or the agent rates that memory again; ADR 0111). Defaults: no feedback → `quality = 1.0`. The weights, prior and bounds are config values, not constants in code.

**Last confirmed.** An entry's `last_confirmed_at` is the newest `updated_at` of a `helpful` report whose note is non-empty after trimming whitespace, provided it is strictly newer than every `stale` or `wrong` report on the entry (a tie is no confirmation); otherwise there is none. Revoked reporters and set-aside ratings are left out as in the counts (ADRs 0067, 0111), and authors cannot rate their own entries (ADR 0065). It depends on dates only, never on how many reports confirmed. It is shown with the entry. Its date enters no score and never restarts the recency clock (ADR 0103).

**A confirmation halves older flags, on by default (`confirmation_halves_flags`, `HIVEMIND_CONFIRMATION_HALVES_FLAGS` — ADR 0106).** While an entry has a `last_confirmed_at`, each of its `stale` and `wrong` reports counts as half a report in the quality formula, in the share and in `n`: `quality = clamp(1 + (0.2·helpful − ½·(0.4·stale + 1.0·wrong)) / (helpful + ½·(stale + wrong) + 2), 0.5, 1.2)`. A newer flag clears the date, so every flag counts in full again. Counts, the flag backlog, `contested` and the shown correction are unchanged. With the setting off every flag counts in full, bit for bit.

**Feedback is readable by the entry's audience (ADR 0051).** Search hits carry the verdict counts (`feedback: {helpful, stale, wrong}`); a read by id (`GET /v1/entries/{id}`, `hive_get`) carries the counts, `last_confirmed_at` when set, plus up to 5 reports: stale, wrong and noted ones first, then bare `helpful` ones, each group newest first (ADR 0064), each `{verdict, note, reporter, updated_at}` (`reporter` is the reporter's registered agent name). Feedback is returned only through a read of the entry, so it reaches exactly the readers of the entry (ADR 0033). Notes are data written by other agents, like entries (ADR 0043).

**An author cannot rate its own entry (ADR 0065).** Feedback on an entry whose `author` is the caller, or an agent that was retired in the caller's favour (ADR 0113, §12.1), is refused with a permission error (REST 403 `forbidden`, MCP `permission_denied`) after the readability check, so an unreadable id is still not found; a batch naming any such id records nothing. To correct one's own entry, supersede or withdraw it.

### 4.3 What is *not* in the model (v1)

No sessions on the server (incognito sessions are client-side, ADRs 0003, 0035). No knowledge graph, no binary blobs (artifacts are **referenced**, not stored), no user/role tables beyond credential mapping, no UI beyond the admin panel and the agent portal (§9). *(v2 adds the `agents` + `fleets` registration tables — ADR 0012 — and self/fleet scoping — ADR 0011; still no user/role model and no UI beyond the admin panel and the agent portal.)*

## 5. API surface

REST is the canonical interface; the **MCP server is the primary agent-facing wrapper** over it. No client touches Postgres directly, ever.

### 5.1 REST (v1)

| Method & path | Purpose |
|---|---|
| `POST /v1/entries` | Create an entry (body = §4.1 fields; `supersedes` and `see_also` optional). The response adds `related`: up to 3 active entries the writer can read that are nearest to the new one, with their cosine `similarity`, `tags`, feedback counts (`feedback`: `helpful`/`stale`/`wrong`) and `same_author` (the writer also wrote it) (ADR 0052), and `warnings` (below). With `?check=true` it stores nothing and answers **200** `{"checked": true, "related": [...], "warnings": [...]}` |
| `GET /v1/entries/{id}` | Full entry (body included), with its `feedback`: verdict counts and up to 5 reports with their notes, corrections first (§4.2, ADRs 0051, 0064), and its links: `see_also` and `linked_from` (at most 20, newest first), limited to what the reader may read (ADR 0057). `?history=true` adds the supersession chain: at most 100 versions (one list page), successors first, then predecessors newest first |
| `POST /v1/entries/get` | Read up to 10 entries by id in one call, `{entry_ids}` → `{entries, not_found}`; each entry as `GET /v1/entries/{id}` returns it, without `history` (ADR 0055) |
| `GET /v1/entries` | List/filter **without** a query (filter only; paginated) |
| `POST /v1/search` | Hybrid search (§6) with filters (`explore`, §6.5) |
| `POST /v1/entries/{id}/withdraw` | Withdraw own entry (or any, with admin credential) |
| `POST /v1/entries/{id}/feedback` | Report `helpful`/`stale`/`wrong` (+note); not on the caller's own entry: 403 `forbidden` (ADR 0065) |
| `PUT /v1/entries/{id}/pin` / `DELETE /v1/entries/{id}/pin` | Pin an active fleet entry to its fleet's briefing, or unpin it: a contributor or privileged agent of that fleet (unpinning others' pins too) or the admin key; at most 10 pins per fleet and 3 per agent (ADR 0096); once the pinned entry is superseded, the version the pin shows names the same pin (ADR 0058) |
| `GET /v1/pins` | A fleet's pinned entries, newest pin first, each as its newest readable version (`id`, `pinned_id`) with `changed_since_pin` and `change_reason` (`superseded`, or `flagged` by a `stale`/`wrong` verdict after the pin; ADR 0076); the caller's home fleet unless `?fleet_id=` (ADR 0058) |
| `POST /v1/feedback` | One verdict (+note) on up to 16 entries at once, `{entry_ids, verdict, note?}`; all must be readable and none the caller's own, or nothing is recorded (ADRs 0053, 0065) |
| `GET /v1/health` | Static health answer (no database check) |
| `GET /v1/liveness` | Unauthenticated orchestrator liveness probe (ADR 0019) |
| `GET /v1/whoami` | The calling key's standing: kind (`agent`/`org`/`admin`/`legacy`), agent name, status, trust level, home fleet, `can_read`, `can_write_scopes`, `inherits_from` (the agents retired in the caller's favour, §12.1; any valid key; not audited — ADR 0030) |
| `POST /v1/agents` | Register an agent (org key or admin key; `{name, owner_alias?}`) → pending agent, 201 (§12, ADR 0012); the same name + alias again → 200 with the current status; another alias → 409 (§12.3, ADR 0039) |
| `GET /v1/admin/agents` | List agents: status, trust level, home fleet, owner alias, `last_used_at` (when the agent's key last authenticated, to the hour; null if never since the field shipped or no key), `idle` (an active agent whose key has been unused for `idle_after_days`; a key never used counts from when it was issued, so a new or replaced key is not idle) and `idle_after_days` (`HIVEMIND_IDLE_AGENT_DAYS`, default 30) (admin key; ADR 0110) |
| `GET /v1/admin/fleets` | List fleets (admin key); fleets merged into another are left out unless `?include_merged=true`, which adds `merged_into` and `merged_at` (§12.1, ADR 0107) |
| `POST /v1/admin/fleets` | Create a fleet (admin key; no deletion in this increment — ADR 0011) |
| `PATCH /v1/admin/fleets/{id}` | Rename a fleet: `{name}`, validated like a new fleet's name (admin key; audited `fleet.rename`; a merged fleet can be renamed; the current name again is a 200 with no audit row; 404 unknown fleet, 409 `fleet_exists` when another fleet has the name, 422 for a bad name; §12.1, ADR 0109) |
| `POST /v1/admin/fleets/{source_id}/merge` | Merge a fleet into another or into a new one: `{target_fleet_id \| new_fleet_name, keep_pin_ids?, expected_pin_ids?, dry_run}`. `dry_run: true` answers the preview and changes nothing; otherwise it moves the source's entries, agents and kept pins in one transaction (admin key; audited `fleet.merge`; §12.1, ADR 0107). 404 unknown fleet, 409 `fleet_merged` / `merge_refused` / `fleet_exists`, 422 for a pin choice over 10 |
| `POST /v1/admin/fleets/{source_id}/move` | Move agents and the `fleet` memories they wrote to another fleet or a new one: `{agents[], target_fleet_id \| new_fleet_name, keep_pin_ids?, expected_pin_ids?, dry_run}`. `dry_run: true` answers how many memories leave, which agents stay and lose access, and the pins in play; otherwise it moves them in one transaction (admin key; audited `fleet.move_agents`; §12.1, ADR 0112). 404 unknown fleet, 409 `fleet_merged` / `move_refused` (also an agent that is not in the fleet, named) / `fleet_exists`, 422 for a pin choice over 10 |
| `GET /v1/admin/fleet-merges` | The recorded fleet merges and moves (`kind` is `merge` or `move`), newest first, each with `undoable` (admin key; ADRs 0107, 0112) |
| `POST /v1/admin/fleet-merges/{merge_id}/undo` | Undo a merge or a move: what it moved goes back where it is still in the target (admin key; audited `fleet.unmerge` or `fleet.move_undo`; 409 `merge_refused` / `move_refused` unless it is the newest standing merge or move involving the target, and for a move its source too; ADRs 0107, 0112) |
| `POST /v1/admin/agents/{name}/cleanup` | Clean up after an agent: `{since, until?, dry_run, restore_ids?, expected_withdraw?, expected_ratings?, also_revoke?}`. Withdraws the agent's active memories written in the window (`created_at`, `since` inclusive, `until` exclusive), sets aside its ratings last written in the window (`updated_at`), removes the pins that show the withdrawn memories and puts back the chosen superseded memories. `dry_run: true` answers the preview and changes nothing; `also_revoke` revokes the agent afterwards as its own `agent.revoke` (admin key; audited `agent.cleanup`; §12.1, ADR 0111). 404 unknown agent, 409 `cleanup_refused`, 422 for a bad window |
| `GET /v1/admin/agent-cleanups` | The recorded cleanups, newest first, each with `undoable` and `undo_blocked` (admin key; ADR 0111) |
| `POST /v1/admin/agent-cleanups/{cleanup_id}/undo` | Undo a cleanup: what it changed goes back where it is unchanged since; never un-revokes the agent (admin key; audited `agent.cleanup_undo`; 409 `cleanup_refused` unless it is the agent's newest standing cleanup; ADR 0111) |
| `POST /v1/admin/agents/{name}/activate` | Set trust level (default `lurker`) + home fleet; **returns the generated agent key once** (admin key; §12.3). Only from `pending` or `revoked`; `active` → 409 (ADR 0028); an agent retired with a successor → 409 `agent_retired` until the retirement is undone (ADR 0113) |
| `PATCH /v1/admin/agents/{name}` | Change trust level / home fleet — demotion to `untrusted` = dormant (admin key) |
| `POST /v1/admin/agents/{name}/key` | Replace an `active` agent's key: the old key stops working at once and the new one is **returned once** (admin key; §12.3; audited `agent_key.replace`; 404 unknown, 409 `invalid_status` unless `active`; ADR 0108) |
| `POST /v1/admin/agents/{name}/retire` | Retire an agent in favour of a successor: `{successor, dry_run}`. `dry_run: true` answers the preview (counts of active private notes, fleet memories and pins that pass over, the agents they come from, what the successor would own) and changes nothing; otherwise the successor owns every memory the agent wrote and the agent is revoked, in one transaction (admin key; audited `agent.retire` and `agent.revoke`; 404 unknown agent, 409 `invalid_status` when the successor is not active or the agent is pending, 409 `succession_refused` when already retired or the successor hands down to it (a cycle), 422 the same agent twice; ADR 0113) |
| `GET /v1/admin/successions` | The standing retirements, newest first: `{retired_agent, successor, by, at}` (admin key; ADR 0113) |
| `POST /v1/admin/successions/{retired}/undo` | Undo a retirement: the successor stops owning the memories; the agent stays revoked (admin key; audited `agent.retire_undo`; 404 when none stands; ADR 0113) |
| `POST /v1/admin/agents/{name}/revoke` | Kill the agent's key and set it `revoked`; on a `pending` agent this rejects the registration (admin key; the name stays reserved — ADRs 0012, 0028) |
| `POST /v1/admin/org-key/rotate` | Rotate the shared org key: closes registration to every prior org key; active agents are unaffected (admin key; ADR 0031) |
| `GET /v1/metrics` | Usage counters: entries / fleets / agents (trust-level distribution, writes per fleet, pending count) searches (first-page searches, the ones that found nothing and the explore ones, per fleet; ADRs 0056, 0078) feedback (stale/wrong flags per registered agent and how many a later supersession confirmed; ADR 0068) and `health` (fleet health metrics: verdict notes, sources, correction latency, flagged backlog, orphaned flags, floating gap, author and reporter concentration, sibling-verdict and owner concentration (self-reported owner aliases; ADR 0089), synthesis, gap tags, supersession rate, and the convergence figures: echo rate, topic dispersion, rival lifetime, grounding share, agent-label mix; admin-only and observation-only, never read by search, ranking or any agent-facing surface; ADR 0075) — operational data (admin key; ROADMAP §3.3). The entry counts and the `health` aggregates come from reads cached up to `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` (300 s default; ADRs 0069, 0075) |
| `GET /metrics` | Prometheus scrape target: the usage counters as gauges (cached 30 s; the entry counts up to `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` older, ADR 0069), the search counters of every runner (ADRs 0056, 0078), the flag counters per agent (ADR 0068), the `hivemind_health_*` fleet health gauges (ADR 0075), per-route request counts and latencies, degraded searches. No key; never on the public ingress (ADR 0050). `hivemind-mcp-http` serves its own `GET /metrics` too, with only per-process series (requests, degraded searches, MCP tool calls and durations; no names, no usage gauges; ADR 0104) |
| `GET /v1/admin/audit-log` | The audit log of admin-surface actions, newest first; filters `actor`, `action`, `since`, `before`, `limit` (admin key; §12.5, ADRs 0027, 0028) |
| `GET /v1/admin/entries` / `GET /v1/admin/entries/{id}` | List entries with the `GET /v1/entries` filters plus `fleet_id`, newest first / one entry in full with its feedback, links and supersession history (admin key; ADR 0080) |
| `POST /v1/admin/search` | Hybrid search with the `POST /v1/search` filters plus `fleet_id`; not counted in the search counters (admin key; ADR 0080) |
| `POST /v1/admin/entries/{id}/withdraw` | Withdraw any entry, as `POST /v1/entries/{id}/withdraw` does for the admin key (admin key; ADR 0080) |
| `GET /v1/admin/activity` | Writes and verdicts per UTC day over the last `days` (1 to 365, default 30), and per-agent and per-fleet totals; observation only, cached like the usage report (admin key; ADR 0080) |
| `GET /v1/admin/feedback` | Feedback rows across entries, newest first, each with the entry it rates; filters `reporter`, `verdict`, `author`, `fleet_id`, `entry_id`, `limit`, `offset`; revoked reporters left out; observation only (admin key; ADR 0090) |
| `GET /v1/admin/fleets/{fleet_id}/pins` | One fleet's pins as `GET /v1/pins` shows them, with what changed since each was pinned; unknown fleet → 404; read-only (admin key; ADR 0092) |
| `GET /v1/admin/flagged` | Active entries with a standing `stale` or `wrong` verdict, oldest flag first, each with its flag dates, verdict counts and whether its author is gone (revoked or silent 30 days); filters `fleet_id`, `older_than_days`, `orphaned`, `limit`, `offset`; revoked reporters left out; observation only (admin key; ADR 0093) |
| `GET /v1/portal/entries` | List what the agent's key can read, newest first, with the `GET /v1/entries` filters `kind`, `tags`, `scope`, `author`, `include_inactive`, `flagged` plus `fleet_id` (agent key only: 403 for the admin and org keys; ADR 0095) |
| `POST /v1/portal/search` | `POST /v1/search` plus `fleet_id`, over what the agent's key can read; not counted in the search counters (agent key only; ADR 0095) |
| `GET /v1/portal/feedback` | The agent's verdicts (`direction=given`) or the verdicts on its own entries (`direction=received`), newest first, on entries its key can read; filters `verdict`, `entry_id`, `limit`, `offset` (agent key only; ADR 0095) |
| `POST /v1/portal/entries/{id}/withdraw` | Withdraw the caller's own entry with a required `reason`; audited as `entry.withdraw` with `via: "portal"` (agent key only; ADR 0095) |

**Request limits (ADR 0040).** A REST request body is capped at 2 MiB (`413 payload_too_large`, enforced by `Content-Length` and by counting streamed bytes, before the body is parsed). A `/v1` request with **no** `X-API-Key` header is refused `401` before its body is read; a present-but-unknown key is verified after the (bounded) body is parsed, so a malformed body with a bad key can still answer 422. A deployment's ingress should enforce its own, lower limit as well.

**Database saturation.** When a call cannot get a pooled database connection within `HIVEMIND_POOL_ACQUIRE_TIMEOUT` (or a statement exceeds `HIVEMIND_POOL_COMMAND_TIMEOUT`), REST answers `503 store_unavailable` with `Retry-After: 2`, the streamable-HTTP MCP runner answers `503` when the key check itself times out, and an MCP tool returns the `store_unavailable` error code. The request was not wrong; retry after a short wait.

### 5.2 MCP tools (the agent's mental model — ten verbs)

| Tool | Maps to |
|---|---|
| `hive_write` | `POST /v1/entries` |
| `hive_search` | `POST /v1/search` |
| `hive_get` | `GET /v1/entries/{id}`; with `entry_ids` instead of `entry_id`, `POST /v1/entries/get` (ADR 0055) |
| `hive_list` | `GET /v1/entries`; over MCP the items are compact (no body, payload or sources) unless `include_body: true`; REST returns full entries |
| `hive_withdraw` | `POST /v1/entries/{id}/withdraw` |
| `hive_feedback` | `POST /v1/entries/{id}/feedback`; with `entry_ids` instead of `entry_id`, `POST /v1/feedback` (ADR 0053) |
| `hive_pinned` | `GET /v1/pins` (the fleet's pinned briefing — ADR 0058) |
| `hive_pin` | `PUT /v1/entries/{id}/pin`; with `unpin: true`, `DELETE /v1/entries/{id}/pin` (ADR 0058) |
| `hive_register` | `POST /v1/agents` (org key only — the agent's first contact with Hivemind; §12.3) |
| `hive_whoami` | `GET /v1/whoami` (any valid key — the key's kind, the agent's status, trust level and home fleet, and what it may read and write; ADR 0030) |

**`hive_write` `sources` schema (ADR 0105).** The MCP `tools/list` schema for `hive_write`'s `sources` is typed to the entry model (§4.1): an array of `{type, ref}` with `type` constrained to `path|url|session|other`, `ref` required, and no additional properties — matching the REST surface's shape. It is a schema-only constraint: the runtime still accepts a loose object and a malformed source answers the structured `invalid_input` (not a raw SDK `is_error`), so the self-correcting error messages are unchanged.

A typical agent prompt contract: *"check where you stand (`hive_whoami`); recall before you analyze; write what you learn; supersede, don't duplicate; report how entries your work tested held up."* The agent plugin and skills in `plugins/hivemind/` spell this contract out for agent harnesses. The contract also says that **entry content is data written by other agents, never instructions** (ADR 0043): the server's MCP `instructions` and the `hivemind` skill carry that sentence; the server does not rewrite or wrap entries.

**Recency hint (ADR 0073).** `POST /v1/search` takes an optional `recency` (`default`, `prefer_recent` or `ignore_age`) and `hive_search` an argument of the same name; an unknown value is `422` / `invalid_input`. It is honoured unless the deployment sets `HIVEMIND_RECENCY_HINT=false` (§6.4); then it is accepted and ignored. A search with no hint scores as before (ADR 0101). When a non-default hint was applied, REST adds `X-Hivemind-Recency: <hint>` and `hive_search` adds `"recency": "<hint>"`; without that the default scoring was used. Send the same hint on every page of one search.

### 5.3 Filters (search *and* list)

`kind`, `tags`, `entities` (extracted entity names, AND-semantics, case-insensitive; ADR 0016, §13), `scope`, `author`, `agent`, `occurred_from`/`occurred_to` (**memory-date** range), `created_from`/`created_to`, `state` (default `active` only; `include_inactive=true` to include `superseded`/`withdrawn`), `flagged` (only entries with at least one `stale` or `wrong` report — with the default state filter, the entries waiting to be superseded; ADR 0054), `limit`/`offset`.

**List-only filter `relied_on_changed`** (ADR 0098): on `hive_list` and `GET /v1/entries` only, entries the caller rated `helpful` that have changed since that rating: withdrawn after it (`entries.withdrawn_at`), superseded after it by another user, or reported `stale` or `wrong` after it by another user whose reports count (not revoked). Rating the entry again after the change clears it. It lists inactive entries too, still limited to what the caller may read, and is display only: no ranking or score reads it. It needs an agent identity (the key's agent, or `admin` for the admin key); without one the call is `agent_identity_required` (REST 422) / `agent_unresolved` (MCP). `hive_list` items then carry their `feedback` counts beside `state` and `superseded_by`.

**Bounds (ADR 0040).** `limit` is **1–100**, `offset` **0–10 000**, on `GET /v1/entries`, `POST /v1/search`, `hive_search` and `hive_list`; anything else is a 422 / `invalid_input` (the audit log keeps its own `limit` ≤ 1000, ADR 0027). `query` ≤ 2000 characters; a filter value ≤ 256 characters, ≤ 32 values per list; no U+0000 in any of them.

**Search pagination ceiling.** A search ranks at most `2 × candidate_top_k` entries (each of the two streams contributes `candidate_top_k`, default 20, so ≤ 40 by default) and `offset`/`limit` page *within* that fused set: `offset ≥ 40` (default config) returns an empty page even when more entries match. Paging "until empty" therefore stops at the ceiling; narrow the query or the filters instead (`hive_list` has no such ceiling — it pages the store directly).

## 6. Retrieval

### 6.1 Two-stage (progressive disclosure)

`search` returns **compact hits** — `entry_id, kind, summary, tags, author, agent, occurred_at, score, scope, fleet_id` (plus `feedback` when the entry has any reports, and `last_confirmed_at` when it has one, §4.2) — **not** full bodies. `score` is rounded to 4 significant figures for display; ranking uses the unrounded value (ADR 0102). `feedback` is the entry's helpful/stale/wrong counts (ADR 0051), so a reader sees an entry was reported stale or wrong before it opens it; it is omitted over MCP and `null` over REST when all three counts are zero, which a reader takes as no reports (ADR 0102); `last_confirmed_at` is the date of its latest confirmation, shown and never entering a score (§6.4, ADR 0103). A hit also shows its evidence, each field omitted over MCP and `null` over REST when empty, none of it ranked on and none a popularity count: `contested: true` when the entry has at least one `helpful` and at least one `stale` or `wrong` report (revoked reporters left out, ADR 0067); `correction`, the newest `stale` or `wrong` report whose note is non-empty after trimming, as `{verdict, note, reporter, updated_at}` with the note cut to 160 characters ("…" marks a cut), shown only when it is newer than `last_confirmed_at` or the entry has none, and testimony like any note (ADR 0043); `supersedes`, how many entries this one replaced that the caller may read (ADR 0033); `state`, only under `include_inactive`; `similar`, only when version grouping is on (§6.3) and the entry has unlinked near-copies among the same search's results, as `[{id, occurred_at}]` newest first; and `upstream_changed`, on an `insight` or `decision` only, up to 3 `{id, change}` for the entries it links to with `see_also` (§4.1) that changed after the link was made, `change` being `withdrawn`, `superseded` (by an entry of another author than the hit's) or `flagged` (a `stale` or `wrong` report newer than the link and not followed by a noted `helpful` one, as for `last_confirmed_at`), withdrawn first, then newest first; a withdrawn target counts whenever it was withdrawn, since entries carry no withdrawal date; limited to targets the caller may read (ADR 0081). `scope` and `fleet_id` let a privileged reader recognise a **foreign entry** (filed outside its home fleet) without opening it (ADR 0036). The agent opens what it wants with `hive_get`, several at once with `entry_ids` (ADR 0055). This is the token economy the design is built around: scan many, open few.

### 6.2 Hybrid pipeline

```
query ─┬─> keyword list  (Postgres FTS / BM25-style rank) ──┐
       └─> vector list   (pgvector cosine, top-k)          ──┼─> RRF fusion
                                                              │      fused = w_kw·1/(k+rank_kw) + w_vec·1/(k+rank_vec)
                                                              │      k=60,  w_kw=w_vec=0.5  (all config)
                                                              ▼
                     decay-aware rescore (§6.4)  ──>  final order  ──>  compact hits
```

An entry is in the vector list only when its cosine similarity to the query is above zero (ADR 0061), or above `vector_min_similarity` (default 0.45, measured for the Qwen3-Embedding family; an empty `HIVEMIND_VECTOR_MIN_SIMILARITY` turns it off; model-specific, so another embedder should be measured with `make measure-threshold` — ADRs 0062, 0100). The threshold does not apply to the keyword list, and an optional query instruction changes the text embedded for the query, not the entries (§7, ADR 0097), so a search comes back empty only when neither list finds anything. An entry is in the keyword list when it contains **any** query term; entries matching more terms rank higher (ADR 0047). On Postgres only the first 16 distinct query terms count (ADR 0049), because `ts_rank`'s cost grows with every term. Keyword ranking is Postgres FTS in v1 (a true-BM25 extension such as `pg_search`/Zombi is a drop-in upgrade, not a v1 dependency). Weights `w_kw`/`w_vec`, `k`, top-k, and the rescore factors are all **config values**, so fusion tuning is a config change, not a code change.

**The vector stream is approximate, not exact (ADR 0025).** It is served by an HNSW index on `entries.embedding` (`vector_cosine_ops`, matching the `<=>` operator; `m = 16`, `ef_construction = 64` — migration `0004`). HNSW is an *approximate* nearest-neighbour structure: the stream is **not guaranteed** to be the true top-k by cosine distance, and a true near neighbour can be missed entirely. This is a deliberate behaviour change from the exact sequential scan that preceded it, taken because that scan reads ~4KB per row and grows without bound with the pool — ADR 0025 carries the measured recall cost and the re-measure trigger. The keyword stream is unaffected, and RRF means a vector miss is survivable when the keyword stream also finds the entry.

Two **session-level** settings apply per vector query (`SET LOCAL`, in the same transaction as the search — not on the connection, which a transaction-mode PgBouncer would discard):

- `hnsw.iterative_scan = strict_order` — every query here is filtered (at minimum `state = 'active'`, plus the §12 visibility matrix), and without iterative scan HNSW filters *after* fetching `ef_search` candidates, so a narrow-visibility reader can get back far fewer than `candidate_top_k` rows. `strict_order` and not `relaxed_order` because RRF fuses on **ranks**, not scores: relaxed ordering perturbs exactly the quantity fusion consumes.
- `hnsw.ef_search = 40` — pgvector's default, unchanged. It is **not** the lever for recall (ADR 0025 measured that raising it plateaus below parity).

### 6.3 Supersession ranking invariant

When a result set contains both an entry and its supersession, **the successor always ranks above the predecessor** — a hard boost applied after scoring, before pagination. Superseded/withdrawn entries are **hidden by default**; they surface only with `include_inactive`. A stale row may appear in history, never as the top answer to a live query.

**Unlinked versions (ADR 0074; off unless `HIVEMIND_VERSION_GROUP_MIN_SIMILARITY` is set).** Two active entries nobody linked by supersession can still be versions of one fact. When the cut-off is set, the service reads the cosine between the stored vectors of the (at most 40) best candidates, before the invariant above. A candidate joins a group when its similarity to the group's best-scoring member is at least the cut-off, the group has fewer than 3 members, and the two share a fleet or are both org-scoped. A group takes its best member's position and is shown newest first by the later of `occurred_at` and `last_confirmed_at`, except that a member with a `stale` or `wrong` report never precedes an unflagged one; each hit names the others in `similar`. Nothing is hidden, merged or linked, and no score changes. Unset, search is unchanged.

### 6.4 Decay-aware rescore (similarity × importance × recency)

```
final = fused
      × (1 + 0.05·(importance − 3))                       # 1..5  →  0.9..1.1 (0.5 + 0.1·importance with narrow_importance off, below)
      × max(recency_floor, 0.5 ** (age_days / half_life)) # age from occurred_at; half_life 30d, floor 0.8
      × min(social_max, quality)                          # §4.2, quality 0.5..1.2; social_max 1.2
```

Every kind ages by the same half-life, and the date of an entry's `last_confirmed_at` (§4.2) never enters a score (whether it is set halves the entry's flags in `quality`, ADR 0106): a confirmation restarting the recency clock (ADR 0071) and a half-life per kind (ADR 0072) were measured twice, worse each time, and removed (ADR 0103).

A fresh, important, well-remembered entry beats a slightly-more-similar but stale one. All three factors are config-tunable; the `0.5 **`/exponents are defaults, not schema.

**The recency factor is bounded below by `recency_floor` (default 0.8 — ADR 0022).** Without it the term is unbounded below, so it spans more than the fused RRF score's whole range (`2(k+20)/(k+1)` = 2.6230x at the §6.2 defaults) after ~42 days and recency becomes the *sort key* rather than the tie-break this section intends. A floor `f` caps the recency factor's range at `1/f` — 1.25x at 0.8, inside the fused range. `recency_floor` is a **band, not a slider**: it must stay above `1/2.6230 = 0.381` to have any effect, and raising it toward 1.0 progressively removes the recency term (at 1.0 it is gone, and at 0.9 it measured *worse* than removing it outright — ADR 0022). The environment-settable range is `(0, 1]`; the pre-ADR-0022 unbounded form is reachable from the environment too — set `HIVEMIND_RECENCY_FLOOR` to an empty value or to `none` (case-insensitive) to get it, rather than approximating it with a negligible floor (ADR 0023).

**Social signals share one capped budget (`social_max`, default 1.2 — ADR 0070).** The last factor is `min(social_max, quality)`: the cap bounds only the upside, so a penalty (`quality < 1`) passes through unchanged and an unrated entry stays at exactly 1.0. `social_max` is finite and `>= 1` (`HIVEMIND_SOCIAL_MAX`); at the default it equals the top of `quality`, so ranking is the same as without it. It exists so later social signals share this ceiling instead of each adding their own. Because past about 9.7 days every entry carries the floor's 0.8, the best an old entry can reach is 0.8 × 1.2 = 0.96, below a new unrated entry's recency under about 1.7 days. That is invariant **I1**: at equal match and importance, such a new entry is never outranked by one past the floor age, however much applause it has. Ranking inputs are numbers and dates only: no author, agent label, pin, link count or rater record reaches the score (**I2**). ADR 0070 lists the invariants I1–I5 (I3 now reads that a confirmation's date never reaches the score and only whether one is newer than every flag does, to halve those flags, ADRs 0103 and 0106; I4 is retired), ADR 0088 adds I6, and ADR 0106 adds I7 (halving confirmed flags never lowers quality and never lifts it above the helpful reports alone); every ranking change must pass, each remaining one is tested in `tests/unit/test_ranking_invariants.py`.

**A recency hint, on by default (`recency_hint_enabled`, `HIVEMIND_RECENCY_HINT` — ADRs 0073, 0101).** The agent knows whether its question is about the current state; the server cannot. Unless the setting is turned off, a search may carry `recency`: `default` is the scoring above; `prefer_recent` uses the floor `min(0.5, recency_floor)` (no floor stays no floor), keeping the half-life; `ignore_age` uses a recency factor of 1.0 for every candidate (match, importance and the capped social factor only). With the setting off every hint scores as `default`, bit for bit. I1 holds under `default`, `prefer_recent` and with the setting off; it does **not** hold under `ignore_age`, which is reachable only with the setting on and the explicit hint. I2 to I5 hold under every hint. On by default because a hint acts only when an agent sends it; the `hive_search` description steers agents to `ignore_age` for a question whose answer does not age and to `prefer_recent` only for a current-state question. Set `HIVEMIND_RECENCY_HINT=false` to turn it off. Measured with Qwen3-Embedding (`make measure-ranking`).

**A narrow importance factor, on by default (`narrow_importance`, `HIVEMIND_NARROW_IMPORTANCE` — ADR 0088).** The author alone sets `importance`, and the old factor `0.5 + 0.1·importance` made importance 5 against 3 worth 1.25x, more than the 1.2 that all peer feedback can add (`social_max`), while the fused RRF score separates adjacent ranks by only about 1.6%. The factor is `1 + 0.05·(importance - 3)`: 0.90, 0.95, 1.00, 1.05, 1.10 for 1..5, one step about 1.05x, below `social_max`. Importance 3, the default, is neutral. With the setting off the factor is `0.5 + 0.1·importance` (0.6..1.0), bit for bit. Invariant **I6**: with it on, an importance-5 entry does not outrank an importance-3 entry at the quality ceiling at equal match and age (1.1 against 1.2). I1 to I5 are unchanged. Measured with Qwen3-Embedding 8B, 4B and 0.6B (pooled MRR +0.06 to +0.08, up in every domain); `make measure-importance` re-measures with another embedder.

### 6.5 Explore search (opt-in; ADR 0078)

A search may pass `explore: true` (REST body field; `hive_search` argument; default false). After the rescore and the §6.3 invariant, the ranked candidates are re-ordered by maximal marginal relevance over their stored vectors (`lambda * relevance - (1 - lambda) * max similarity to the entries already placed`; `lambda` is `HIVEMIND_EXPLORE_LAMBDA`, default 0.7, in `(0, 1]`), so near-copies of a higher hit sink and different ones rise. It only re-orders: nothing is hidden or re-scored, a supersession chain stays newest first, `lambda = 1` is the normal order, and pagination applies to the re-ordered list. It needs no embedder call beyond the query's, and if the similarity read fails the normal order is returned. Without `explore` the ranking is unchanged. Explore searches are counted per home fleet beside the search counters.

## 7. Embedding strategy (ADR 0005)

- Generated **at write time**, server-side, from `summary` (whole) + a bounded prefix of `body`.
- **The body prefix is bounded in *prefix tokens* — whitespace-delimited words, not a model tokenizer's tokens** (`EMBEDDING_PREFIX_TOKENS`, default **2000** — ADR 0021). The cut is made at a word boundary and the body's own spacing (newlines, blank lines, indentation) is preserved up to it. ~2000 words is ~2600 model tokens, far under the endpoints' limits: the bound controls **dilution and cost**, not a model limit. The extractor reads this exact same text (§13.1) — one budget, both consumers.
- **What is embedded, and what is not (ADR 0097).** An entry is embedded as above and never with an instruction. The search query sent to the vector stream may carry one, for an instruction-aware embedder such as Qwen3-Embedding: by default (`HIVEMIND_QUERY_INSTRUCTION` unset) the query is embedded as `Instruct: {instruction}\nQuery:{query}` with the instruction `Given an agent's task or question, retrieve shared memory entries that answer it`; an empty value turns it off (one line, at most 500 characters). The keyword stream always gets the raw query, and every entry-against-entry similarity (`related`, `near_duplicate`, version grouping, explore) is unchanged. The format is Qwen3's: with a different embedder, run `make measure-query-instruction` and turn it off if it does not help. Because it shifts query vectors, re-check `vector_min_similarity` when changing it (the 0.45 default was measured with the instruction on).
- Produced by an **OpenAI-compatible embeddings endpoint the org configures at deploy** (`EMBEDDING_ENDPOINT`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`). A self-hosted vLLM/Ollama endpoint satisfies "data must not leave the org."
- The store records `embedding_model` + dimension per entry. The vector column has a **fixed dimension at deploy time** (`EMBEDDING_DIM`, default 1024 — ADR 0015).
- **A dim mismatch is a loud failure, never a silent assumption:** if the pool's `vector(:dim)` column differs from the configured dim, `migrate` fails with an actionable error (reset the pool, or set `EMBEDDING_DIM` to the pool's dim) — ADR 0015. The dim is a deploy-time decision (ADR 0005); it is never changed in place.
- **Changing the embedding model or dimension is an operator-run re-embedding migration** (re-embed all active entries, swap the column). This is a named maintenance procedure, not an online feature.
- **Transient embedder failures are retried** (ADR 0014) — timeouts, connection errors, `429`, and 5xx are retried with a bounded exponential backoff (`EMBEDDING_RETRIES`, default 2 retries; `0` disables). A dropped keep-alive (`RemoteProtocolError`) is transient too; backoff is jittered and a capped `Retry-After` is honoured. Deterministic failures (other 4xx, a redirect, a non-JSON or malformed body, NaN/Inf/non-numeric values, a value outside the float32 range pgvector stores, a zero vector (its cosine distance to everything is NaN), a wrong count or dimension) fail fast, and a write still fails after the full budget — a write either lands fully (vector + entry) or fails. Each call also has an **overall deadline** (`EMBEDDING_DEADLINE`; unset = derived from the timeout and retries, i.e. their worst case; retries and backoff included — ADR 0041). An embedder failure on a write is a `502 embedding_unavailable` on REST and an `embedding_unavailable` error from MCP `hive_write`; the caller gets a fixed message. A search instead **falls back to the keyword stream alone** (ADR 0048): REST answers 200 with the usual body plus the header `X-Hivemind-Degraded: keyword-only`, and `hive_search` adds `"degraded": "keyword_only"` and a `note` to its result. In both cases provider detail (which can carry headers or URLs) is never returned, and is logged class-name-only. API keys are whitespace-stripped at load.

## 8. Identity, credentials, deployment

### 8.1 Credentials (ADR 0012)

| Credential | Binds | Effect |
|---|---|---|
| **org key** | the whole cluster (shared) | Gates registration only; rotating it closes registration to every prior copy, and active agents keep working (ADRs 0012, 0031) |
| **agent key** | one registered agent | Carries the agent's trust level + home fleet; `author` is server-filled with the agent's registered name (§12, ADR 0012) |
| **admin key** | the operator | The admin surface: activate / promote / demote / revoke, fleets, org-key rotation (§12.4). Its entries have author `admin` (ADR 0060) and its feedback is reported as `admin` (ADR 0066) |

Every key that authenticates records `credentials.last_used_at`, at most once an hour (ADR 0110). The write is best effort and outside the request; it only feeds the admin agent list and changes no access.

No OAuth/SSO; keys are issued by the org operator via the admin surface (§12.4). (An org with an IdP is a later story.) This table supersedes ADR 0008's key kinds (`user`/`agent`/`admin`) — ADR 0012. The agent key is now the credential the MCP runners verify (ADRs 0009–0010).

### 8.2 Deployment (ADR 0026, superseding ADR 0007)

One **self-hosted instance per organization** — one instance = one organization — over **one PostgreSQL 16 (pgvector) node**. No Redis, no sharding, no queue, no read replicas.

**App tier: 2 replicas per runner** (`hivemind-api`, `hivemind-mcp-http` and the agent portal), rolled out `maxSurge: 1` / `maxUnavailable: 0` with a `minAvailable: 1` PodDisruptionBudget each. The replicas are interchangeable: they share the one pool, serve the one organization, hold no server-side session state (§5.1, §8.5), and resolve credentials per request. This buys **availability only** — a zero-downtime rolling deploy and surviving a node drain — not throughput (the app tier is I/O-bound on the embedder, the extractor and Postgres) and not AZ tolerance (there is one Postgres node). Production packaging is Kubernetes (`deploy/kubernetes/`); `docker compose` is the dev and single-node-ops shape. The **admin panel** (`hivemind-admin`, ADR 0029) is a separate, stateless, 1-replica runner in the same image: a static UI plus an allowlisted same-origin proxy to `hivemind-api`. It holds no database credential; the operator's admin key lives only in their browser tab. The **agent portal** (`hivemind-portal`, ADR 0095) is built the same way (stateless, no database credential; the agent key lives only in the person's browser tab) but runs 2 replicas with a `minAvailable: 1` PodDisruptionBudget, because people other than the operator use it.

**Scale assumptions (spec target):** ~50 users/agents, ~500 sessions/day, ~10k entries/day, single Postgres node. The design does not commit to horizontal *storage* scale; when the assumptions stop holding, §10's extensions apply.

### 8.3 Incognito sessions (ADRs 0003, 0035)

An **incognito session** turns Hivemind completely off for one session. It is a **client-side act**: the agent's Hivemind integration (its MCP server entry / enabled flag) is disabled for that session, so the tools simply aren't available and the pool is untouched. **The server has no session registry and is unaware of off sessions.** The spec's only server-side commitment is that an absent client is indistinguishable from a quiet one. The agent plugin implements it (ADR 0035): `HIVEMIND_INCOGNITO=1` switches every reminder to "Hivemind is off; don't use or mention it", the `hivemind-incognito <harness>` launcher adds each harness's own switch so the tools never load, and local notes kept during the session are marked `[hivemind: incognito, never upload]` so no later session uploads them.

### 8.4 MCP runners (ADR 0009)

The MCP stdio surface ships **two runners**:

* **`hivemind-mcp`** — the **dev** path: in-memory store + local hash embedder + a hard-coded `dev` credential. Zero network, ephemeral, single identity; for exercising the ten `hive_*` tools with no dependencies.
* **`hivemind-mcp-pg`** — the **production** path: a DSN-backed `PgStore` + the operator-configured OpenAI-compatible embedder (ADR 0005), with the acting credential resolved by verifying `HIVEMIND_MCP_KEY` against the Postgres `credentials` table (ADR 0012).

**Unified multi-agent pool:** several agents each run their own `hivemind-mcp-pg` process with a distinct `HIVEMIND_MCP_KEY` (an **agent key**, ADR 0012). All of them read/write the **same** Postgres pool over a **unified** MCP interface (the identical ten `hive_*` tools) while every write carries that agent's *verified* provenance (its registered name, server-filled from the key). The key is **re-verified on every tool call** (ADR 0042, superseding ADR 0010's "verified once at start" consequence): revoking an agent's key, demoting its trust level or re-homing it applies to its very next call, no restart; a call made with a revoked key answers `unauthenticated`. The dev runner (`hivemind-mcp`) keeps its fixed `dev` identity and is the only path without a per-call credential. A deployment can **require stored keys** (`HIVEMIND_REQUIRE_STORED_KEYS`, ADR 0082): when set, any credential that is not an admin key, an org key, or an active agent key — a *dev-mode* credential — sits at trust level 0 on the data plane (§12.2: reads return empty, writes are permission denied). Off by default, so the keyless dev runner and local scripts are unchanged.

### 8.5 The hostable streamable-HTTP runner (ADR 0010)

`hivemind-mcp-http` is the **hostable, multi-agent** form of the Postgres-backed runner (ADR 0009): a long-lived streamable-HTTP process serving an **unlimited** number of agents over one shared pool, each authenticating **per request** with its own agent key (ADR 0012).

* **One shared pool per process, per-request auth.** A `hivemind-mcp-http` process owns one `PgStore` + one embedder + one `Authenticator` pool (the same DSN / embedder / credentials the REST API uses), shared by every agent it serves — never a process per agent. Each request presents its own key; a thin ASGI middleware verifies it against the `credentials` table (ADR 0012) and re-binds the shared, *stateless* services to that credential on every tool dispatch. The invariant is the shared pool, not the process count: production runs 2 interchangeable replicas of this runner for availability (§8.2, ADR 0026), and because the transport is stateless and the credential is per-request, either replica answers any request identically.
* **Immediate revocation.** Because the credential is resolved **per request** (as `hivemind-mcp-pg` now does per tool call, ADR 0042), admin revocation (`POST /v1/admin/agents/{name}/revoke`) takes effect on the very next request — no restart required.
* **Fails closed; no OAuth; Host checks (ADR 0042).** A dispatch with no resolvable credential answers `unauthenticated` and never falls back to a template identity. A 401 carries `WWW-Authenticate: Bearer realm="hivemind"`, and `/.well-known/oauth-*` answers 404 (there is no OAuth, so clients must not start a discovery flow). `HIVEMIND_MCP_ALLOWED_HOSTS` (comma-separated Host values, `name` or `name:*`) and `HIVEMIND_MCP_ALLOWED_ORIGINS` enable the MCP SDK's DNS-rebinding check; unset keeps the SDK default (check on only for a loopback bind), so an ingress-fronted `0.0.0.0` deployment is unchanged. The unauthenticated `/mcp/health/database` answer is cached for ~2 s so it cannot amplify load onto the pool.
* **Unauthenticated `GET /metrics` (ADR 0104), off unless `HIVEMIND_MCP_METRICS_ENABLED=true`** (the Kubernetes manifest sets it; off, it is a 404 before the key check). Answered before the key check, like the probes: this process's request counts and latencies by exact known path (`unmatched` otherwise), degraded searches and per-tool call counts and latencies. No agent, fleet, owner or key in any label, and none of the API's usage gauges; the 401 count is visible. The public Ingress routes only `/mcp`; leave it off, or block `/metrics`, where the MCP port is reachable some other way.
* **Per-request transport: stateless streamable-HTTP.** The server runs the SDK's stateless streamable-HTTP transport (one request = one self-contained exchange). The pool is stateless with respect to sessions, so this is a natural fit.
* **Deployment shape: a detached compose service.** `make mcp-http` ships the runner as a detached docker-compose service (one container built from the repo's Dockerfile), published on host port 8088 by default (override with `HIVEMIND_MCP_HTTP_PORT`). The container reads/writes the shared pool and embeds via the local vLLM on the compose network, so pool + embedder + runner come up with a single `docker compose up -d`. This is the dev shape; production runs the runner on Kubernetes (§8.2).

**MCP runners, at a glance** (three runners, one pool):

| runner | process | pool | credential | revocation |
|---|---|---|---|---|
| `hivemind-mcp` (dev, ADR 0009) | in-memory | in-memory (ephemeral) | hard-coded `dev` | n/a |
| `hivemind-mcp-pg` (per-agent, ADR 0009) | one per agent | shared Postgres | agent key (`HIVEMIND_MCP_KEY`, ADR 0012), verified at start and re-verified per tool call (ADR 0042) | immediate |
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
* **Pre-flight checks.** Once the lock is held and before any DDL, `migrate` checks the ADR 0015 dimension (a pool provisioned at a different embedding dimension than configured fails loudly; taking the lock first means two replicas at different dims on a cold pool cannot both pass), refuses pgvector < 0.8 and a dimension above 2000 (the HNSW limit), and after the chain fails on an INVALID HNSW index.

## 9. Non-goals (v1) — the explicit list

- No human-facing UI (agent-only) beyond two exceptions: the admin panel (ADR 0029), a browser front end over the admin REST surface, which since ADR 0080 includes a memory browser for the admin (read, search, withdraw, CSV export, ADR 0094); and the agent portal (ADR 0095), where a person signs in with one agent's key to see what that agent can read, wrote and rated, and withdraw its own entries. A memory UI for anyone else is still a non-goal
- No passive capture of transcripts/tool events (explicit writes only, ADR 0004)
- No knowledge graph, no auto-contradiction detection (the **facet slice** of entity extraction became a commitment in §13 — ADR 0016; *graph-expanded* retrieval stays a non-goal)
- No curation/verification workflow, no PII pipeline (trust levels are §12, not a non-goal — ADR 0011)
- No multi-tenant SaaS (single-org; self/fleet scoping is §12, ADR 0011)
- No binary/artifact storage (references only)
- No OAuth/SSO, no per-agent learned retrieval tuning
- No sharding, read replicas, or Redis-backed scale-out (two app-tier replicas per runner exist for availability only, §8.2)

## 10. Documented extensions (out of v1 scope, by design)

| Extension | Trigger to build it |
|---|---|
| **Passive capture** — ingest raw session transcripts, distill with an LLM (crash-safe, dedup'd) | "Agents forget to write" becomes the bottleneck |
| **Private staging + publish** — write to a personal scratch, *promote* it to the fleet | Analysts want to try analyses before sharing. *Partially satisfied: the `self` scope (ADR 0011) is the personal scratch; what remains is promotion — a curation story (see Curation workflow)* |
| **Namespaces / channels** — multi-fleet membership, per-fleet promotion, cross-fleet writes | The flat pool grows too noisy. *Partially satisfied: fleets + one home fleet per agent (ADR 0011) cover single-fleet needs; multi-fleet membership is the remaining extension* |
| **Binary artifacts** — S3-backed artifact store behind `sources` | Analysis references outgrow file/URL references |
| **Knowledge graph** — graph-expanded retrieval over a canonical entity registry | Cross-entry entity linking pays off in retrieval quality. *Partially pre-staged: the entity-extraction **facet** slice is shipped by ADR 0016 / SPEC §13 (a pre-staged §10 extension on scale ambition — the trigger has *not* fired); the graph half of this row (entity registry + multi-hop expansion) remains trigger-held* |
| **LLM-assisted contradiction detection** — at write time, surface entries above a similarity floor that the new entry may conflict with, as a *claim for the writer to judge* (never an automatic resolution — ADR 0001: a supersession is a claim, not an arbitration) | Explicit supersession can't keep up with contradictory writes. **Measurable trigger:** the first observed incident of a contradicting entry landing without a supersession, **or** pool size > 10k entries — whichever comes first. Until then the similarity floor cannot be tuned (there is no corpus to tune it against), and an untuned floor is the failure mode to avoid. *(Not to be confused with ADR 0052: every write already reports its 3 nearest entries with their similarity, with no floor and no verdict.)* |
| **Curation workflow** — verify/promote/retire roles | An org wants a "librarian" function |
| **Human read-only UI** — browse/search the pool in a browser | Analysts want to see the pool without an agent. *Partially satisfied: the admin panel lets the admin browse and search the pool (ADR 0080), and the agent portal lets a person see what one agent's key can read (ADR 0095); a UI without an agent key remains the extension* |
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
5. **Embedding prefix length** — resolved: a bounded **2000-prefix-token body prefix** default (`HIVEMIND_EMBEDDING_PREFIX_TOKENS`; a prefix token is a whitespace-delimited word, *not* a model tokenizer's token — ADR 0021). This supersedes the original ~2048-char (≈ 512-token) answer, which was both the wrong unit (chars per token varies 2–3x between prose and code) and, on the embedding side, an inert knob. The *unit and size* question is closed; **what** goes into the embedded text, and whether to chunk at all, stay open (ROADMAP §4.2).

## 12. Fleets, trust levels, and registration (v2 — ADRs 0011–0012)

This section supersedes the flat-pool commitment of §1 and the "no trust tiers" non-goal of §9. ADR 0011 owns the fleet/trust model; ADR 0012 owns the credential model. This section is the spec commitment; the ADRs own the *why*.

### 12.1 Fleets and scope

* A **fleet** is a named group of agents. The admin creates fleets (`POST /v1/admin/fleets`); **no fleet deletion in this increment** (ADR 0011). An admin can **rename** a fleet (`PATCH /v1/admin/fleets/{id}`: the id, entries, pins and agents are untouched, names stay unique, a merged fleet can be renamed; ADR 0109) **merge** a fleet into another, and **move** some of its agents to another fleet (below).
* Every active agent belongs to exactly **one home fleet** — admin-assigned, re-assignable at any time via `PATCH /v1/admin/agents/{name}`. One home fleet per agent in this increment; multi-fleet membership is a §10 extension.
* An entry is written into exactly one scope: `self` (the author agent only) or `fleet` (the home fleet it was written into). **An entry's fleet membership is fixed at write time**, when an agent moves fleets, its earlier entries stay in the old fleet (ADR 0011), **except that an admin fleet merge re-files a whole fleet** (ADR 0107) **and an admin fleet move re-files the movers' own `fleet` memories** (ADR 0112).
* Legacy `scope='org'` entries are **read-only for agent keys**: an agent key may not write `org` (the sanctioned `org` writers are the admin key; pre-v2 `user` keys no longer authenticate, ADR 0039); they remain readable at trust level 1+. The admin key writes `self` or `org` only: it has no fleet, so a `fleet` write from it is refused (ADR 0060).
* **Merging fleets (ADR 0107).** `POST /v1/admin/fleets/{source_id}/merge` re-files every entry of the source fleet (any state) to a target fleet, an existing one or a new one, moves the source's agents (trust levels unchanged; no key changes, since the home fleet is read on every request) and re-keys its pins, in one transaction that locks both fleet rows. A merge writes no memory. More than 10 pins across the two fleets need a `keep_pin_ids` choice of at most 10; the rest are unpinned. The source keeps its row, marked `merged_into` and `merged_at`: it is left out of the fleet list unless asked for, takes no new members or memories, and cannot be a merge source or target again. A fleet write (an entry, a pin, an activation or home-fleet change, `issue-agent`) that resolved the source before the merge committed is refused with 409 `fleet_merged` naming the target, and the client retries against the agent's new home fleet; a fleet write that waits more than 3 seconds for the merge's lock gets 503 `fleet_busy` with `Retry-After` (REST and MCP). `expected_pin_ids` (the pins the preview showed) makes the merge answer 409 `merge_refused` when the pins changed since. `dry_run: true` previews counts, the pins of both fleets and likely duplicate pairs (cosine 0.90 or more, looked up for at most 2,000 entries of the smaller fleet); nothing is superseded. `POST /v1/admin/fleet-merges/{merge_id}/undo` moves back what the merge moved and is still in the target, restores dropped pins where they fit and clears the mark; it is allowed only for the newest standing merge involving the target, while the target is not itself merged. Search counters stay summed.
* **Ownership and retiring an agent (ADR 0113).** An agent **owns** the memories whose `author` is its name, and those of every agent that was retired in its favour, directly or through a chain (`Credential.owned_authors`, resolved in the key check; `whoami` lists the others as `inherits_from`). Wherever §4 and §12 say "the author" for an access decision, they mean an owner: reading own `self` notes and own `fleet` memories wherever they sit (§12.2), withdrawing (§4.1, also from the portal), superseding (§4.1), and the refusal to rate (§4.2). `author` on a memory never changes (entries are immutable, ADR 0001). The admin retires an agent with `POST /v1/admin/agents/{name}/retire`: one transaction records the succession (`successions`: retired agent, successor, by, at) and revokes the agent, so its key stops working; its private notes, fleet memories and pins pass over, and nothing is rewritten. The successor must be active; its trust level and home fleet do not change; chains are allowed (A to B, then B to C: C owns both) and cycles are refused. Ownership grants exactly what authorship grants: a fleet memory in a fleet other than the successor's home fleet is read and withdrawn by the successor, but superseding it needs a replacement written into that fleet (§4.1), which the successor cannot do from another home fleet. Pins stay in their fleet and are never dropped; the pins of the agents handed down count toward the successor's cap of 3 (§12.2). `author`-filtered reads (`hive_list`, search, the portal) treat the caller's own name as everything it owns; any other name stays literal. Undo (`POST /v1/admin/successions/{retired}/undo`) deletes the succession; the retired agent stays revoked, and activating it needs a new key. Activation of an agent with a standing succession is refused (409 `agent_retired`) until it is undone.
* **Moving agents with their memories (ADR 0112).** `POST /v1/admin/fleets/{source_id}/move` takes `agents[]` (each must have the source as home fleet, else 409 `move_refused` naming those that do not) and a target, an existing fleet or a new one (`new_fleet_name`, created in the same transaction), and splits a fleet in this way: the agents' home fleet changes and the memories they wrote move, in one transaction that locks both fleet rows exactly as a merge does (a fleet write that waits more than 3 seconds for the lock gets 503 `fleet_busy`; a merged fleet is neither source nor target, 409 `fleet_merged`; the two fleets must differ, 409 `move_refused`; a lock that cannot be had in 5 seconds is also 409 `move_refused`, run it again). A memory moves when it is `fleet`-scoped in the source (any state), its author is one of the moved agents, **and the head of its supersession chain moves too** (the head is the entry at the end of the `superseded_by` links, or the entry itself). A mover's older version whose head belongs to someone who stays, or lies in another fleet, stays behind, so a version never leaves while the live one remains. `self` and `org` memories and ratings are not fleet-bound and are untouched; search counters stay with the source. Pins follow their memory to the target; the target's own pins plus those that followed are limited to 10 exactly as in a merge (`keep_pin_ids`, `expected_pin_ids`), the others are unpinned (audited `entry.unpin`, reason `fleet_move`). Agents that stay lose access to the memories that leave, and the preview reports how many leave and who stays. The source stays an ordinary fleet, even when every agent moved (it is not marked as merged). A write by a moved agent that resolved its old home fleet before the move committed is refused with `fleet_busy` (503, retry) instead of landing in the source. The move is recorded with the merges (`kind: move`, the entries and agents it moved, the pins it dropped) and undone with the same route: the recorded memories and agents still in the target go back, pins follow, dropped pins are restored where they fit (pins on returning memories count against the source's cap of 10 and 3 per agent like restored pins; those that do not fit are unpinned, audited `entry.unpin` with reason `fleet_move_undo`). It is allowed only for the newest standing merge or move involving the target or the source, while neither is merged away. Memories written after the move stay where they are.
* **Cleaning up after an agent (ADR 0111).** `POST /v1/admin/agents/{name}/cleanup` (admin key; the agent may be active or revoked) takes a time window and, in one transaction, withdraws the agent's **active** memories whose `created_at` is in the window (`since` inclusive, `until` exclusive, default now; `occurred_at` is not used, since the agent sets it), with `withdrawn_reason` `admin cleanup <id>`; sets aside the agent's ratings whose `updated_at` is in the window; removes the pins that show a withdrawn memory, on it or on an older version whose chain ends at it, except on memories being restored (audited `entry.unpin`); and puts back the `restore_ids` the admin chose. A restorable memory is `superseded`, is not one of the agent's own window memories, and its `superseded_by` chain passes only through the agent's own window memories and ends at one the cleanup withdraws, so junk over junk over a good memory offers the good one; the agent's intermediate junk stays superseded. A **set-aside** rating keeps its row and is skipped wherever a revoked agent's rating is skipped (§4.2, ADR 0067); the agent rating the same memory again clears the mark. `dry_run` is required, and an apply (`false`) must carry `expected_withdraw` and `expected_ratings` (422 otherwise): `true` answers the preview (the memories to withdraw cut at 200, the ratings by verdict, the pins, the restorable memories cut at 500, exact totals) and changes nothing. The apply sends `expected_withdraw` and `expected_ratings` from the preview and is refused with 409 `cleanup_refused` when they changed, or when a `restore_ids` entry is no longer restorable; it also answers 409 when it cannot get its locks within 5 seconds, and cleanups of one agent run one at a time (a second one that waits more than 5 seconds for the first is refused the same way). A window with nothing in it changes nothing and keeps no record. `also_revoke` (default false) revokes the agent after the cleanup as its own audited `agent.revoke`; if that revoke fails the cleanup stands and the answer carries `revoke_error`. `POST /v1/admin/agent-cleanups/{cleanup_id}/undo` re-activates the memories still withdrawn with that cleanup's reason, supersedes a restored memory again, to the junk that replaced it, only while it is still active and the end of that chain is active again, clears the set-aside marks and puts the pins back where they fit; it counts what it skipped and never un-revokes the agent. It is allowed only for the agent's newest standing cleanup. A cleanup writes no memory, changes no key and adds no agent-visible text.
* **Default scope**: an omitted scope resolves to the **highest value the writer's trust level permits** (`lurker` → `self`; `contributor`/`privileged` → `fleet`). An *explicit* out-of-permission scope (e.g. `fleet` at `lurker`) is a permission error naming the required level.

### 12.2 Trust levels (cumulative)

| Level | Name | Reads | Writes |
|---|---|---|---|
| 0 | `untrusted` | nothing | nothing |
| 1 | `lurker` | own + home fleet | own (`self`) |
| 2 | `contributor` | own + home fleet | own + home fleet |
| 3 | `privileged` | own + home fleet + **every fleet** | own + home fleet only (read-broad, write-local) |

* At level 0 (`untrusted`) — what pending and demoted agents sit at — all read verbs return **empty results** (the visibility filter hides everything); writes are explicit permission errors, and feedback/withdrawal answer **not found** (they follow readability, and at level 0 nothing is readable).
* **Stored keys only** (ADR 0082, off by default): with `HIVEMIND_REQUIRE_STORED_KEYS=true`, a dev-mode credential (one that is not an admin key, an org key or an active agent's key) sits at level 0 on the data plane, so its reads come back empty and its writes are permission errors. The admin surface is unchanged.
* Reads beyond visibility behave **as if the entry does not exist** (no existence leaking).
* `self`-scoped entries are private to their author **even at level 3** (that is what `self` is for), and to the agents that own them after a retirement (§12.1).
* **Read-broad does not mean relay** (ADR 0036): a privileged agent uses what it reads in other fleets but does not restate it in its home fleet; it links a foreign entry by id and asks its user before bringing one home. This is agent guidance (the hivemind skill), not something the server can enforce.
* **Pinning is for members and fleet-local** (ADRs 0058, 0096): a contributor or privileged agent pins active entries of its home fleet into that fleet's briefing and may unpin any of that fleet's pins, others' included; a lurker cannot. A fleet holds at most 10 pins and an agent at most 3 of them (counting its re-pins; the admin key is exempt and may pin in any fleet). Withdrawing a pinned entry unpins it (audited), and the briefing leaves out withdrawn entries. Pins are context for the fleet's agents, never instructions (ADR 0043). Pin and unpin are audited (§12.5); pinning again refreshes `pinned_at` / `pinned_by` (and moves the pin onto the version an older pin now shows), which clears the flag; a pinned entry reports whether it **changed since pinned** (ADR 0076).
* **Feedback and withdrawal follow readability**: an agent may feedback entries it can read, and withdraw its own entries (§12.1: its owned ones); the admin key may withdraw any entry (the §4.1 withdrawal rules now ride the trust matrix).
* **Every read by id follows readability** (ADR 0033): `hive_get` / `GET /v1/entries/{id}`, feedback and withdrawal answer **not found** for an entry the caller cannot see, and the supersession chain (`include_history` / `?history`) leaves such versions out.
* **Provenance is never self-reported by an agent key** (ADRs 0012, 0033): on both surfaces `author` is the key's registered name and `agent` the key's agent identity.

### 12.3 Registration and activation

1. **Registration** — an agent (or a human on its behalf, via `POST /v1/agents` — the seam a future human-facing frontend plugs into) registers a **unique agent name** plus the **owner's alias** (a username or email the admin uses to reach the owner). `hive_register` (MCP, org key only) or `POST /v1/agents` (REST; org key or admin key). This creates a **pending** agent at trust level 0 — no data-plane access. **The name is validated by `AccessService.register`, so both surfaces agree (ADR 0040):** 1–63 ASCII characters `[A-Za-z0-9][A-Za-z0-9._-]*`, taken as given (never normalised, so lookalikes are refused rather than mapped), and not a reserved name (`admin`, `org`, `dev`, `shared`) under a case-insensitive comparison; a violation is a 422 / `invalid_input`, distinct from the `name_conflict` of a taken name. Names are unique **ignoring case**: a name that differs from an existing agent's only in case (`Bob` / `bob`) is a `name_conflict` for every caller (ADR 0045); names that already coexist are left as they are. Fleet names are free-form but non-blank, ≤ 128 characters, with no control or zero-width characters. *The first registrant's owner alias is final (ADR 0039). The same name registered again by the **same** alias → success that reports the current status and what to do next (`already_registered`; pending → "still awaiting admin activation, do not register again"; active → "ask your admin for the agent key"; revoked → "rejected, ask your admin or pick another name"); by **any other** alias (or none) → `name_conflict` (409), one fixed text for every status that reveals nothing about the other registration.*
2. **Activation** — the admin activates via `POST /v1/admin/agents/{name}/activate`, setting the trust level (default `lurker`) and the home fleet (required; fleets are created first). The service generates the agent key and returns it **once** — the only moment a key is ever shown. The admin delivers the key **out-of-band** (chat/DM/email, using the owner alias); the service has **no notification channel**.
3. **Live traffic** — an active agent presents **only its agent key** on every request (one key per request, ADR 0031); per-agent / per-request verification (ADRs 0009–0010) applies unchanged. **Demotion** (to `untrusted`) and **revocation** are distinct verbs: demotion keeps the key valid but the agent can do nothing; revocation kills the key, sets the agent `revoked`, and the **name stays reserved** (ADR 0012). A revoked agent can be re-activated, which issues a fresh key (ADR 0028). A revoked agent's feedback stays stored but no longer counts, lists or flags while it is revoked (ADR 0067).
4. **Lifecycle** (ADR 0028) — `pending → active` (activate), `pending → revoked` (revoke: a rejected registration), `active → revoked` (revoke), `revoked → active` (activate, fresh key). Any other transition is **409**; activation never issues a second key to an agent that already holds one. An agent key authenticates only while its agent is `active`, status changes and key issue/replace/revoke are serialised per agent, and one live key per agent / one live org key are enforced by unique indexes (ADR 0039). The CLI `issue-agent` on a `pending`/`revoked` agent requires an explicit `--trust-level` and `--home-fleet` (it activates exactly as REST does). Pre-v2 `user` and name-less agent keys no longer authenticate.
5. **Replacing a key** (ADR 0108): an admin replaces an **active** agent's key with `POST /v1/admin/agents/{name}/key` (or the panel's "Replace key", or `hivemind-keys rotate-agent --name`). The old key row is deleted and the new one inserted in one transaction, so the old key fails on its next request; the new key is returned **once**. Status, trust level, home fleet, memories, ratings and pins are untouched. A `pending` or `revoked` agent is refused (409 `invalid_status`); a revoked agent is re-activated instead.

### 12.4 Admin surface

| Endpoint | Effect |
|---|---|
| `GET /v1/admin/agents` / `GET /v1/admin/fleets` | List agents (status, level, home fleet, alias, last use and idle, ADR 0110) / fleets |
| `POST /v1/admin/fleets` | Create a fleet (ADR 0011: no deletion in this increment) |
| `PATCH /v1/admin/fleets/{id}` | Rename a fleet (ADR 0109) |
| `POST /v1/admin/fleets/{source_id}/merge` | Merge a fleet into another or a new one, or preview it with `dry_run` (ADR 0107) |
| `POST /v1/admin/fleets/{source_id}/move` | Move agents and the fleet memories they wrote to another fleet, or preview it with `dry_run` (ADR 0112) |
| `GET /v1/admin/fleet-merges` / `POST /v1/admin/fleet-merges/{merge_id}/undo` | The recorded merges and moves / undo one (ADRs 0107, 0112) |
| `GET /v1/admin/agent-cleanups` / `POST /v1/admin/agent-cleanups/{cleanup_id}/undo` | The recorded cleanups / undo one (ADR 0111) |
| `POST /v1/admin/agents/{name}/activate` | Set level + home fleet; **returns the agent key once** (§12.3) |
| `PATCH /v1/admin/agents/{name}` | Change level / home fleet (demotion to `untrusted` = dormant) |
| `POST /v1/admin/agents/{name}/key` | Replace an active agent's key; **returns the new key once** (§12.3, ADR 0108) |
| `POST /v1/admin/agents/{name}/revoke` | Kill the key, status → `revoked` (name stays reserved; re-activation issues a *new* key). On a pending agent: reject the registration |
| `POST /v1/admin/org-key/rotate` | Rotate the org key — closes registration to every prior copy (ADR 0031) |
| `GET /v1/admin/entries`, `GET /v1/admin/entries/{id}`, `POST /v1/admin/search` | Read the memories: list, one entry in full, search (ADR 0080) |
| `POST /v1/admin/entries/{id}/withdraw` | Withdraw an entry (ADR 0080) |
| `GET /v1/admin/activity` | Daily writes and verdicts, per-agent and per-fleet totals (ADR 0080) |
| `GET /v1/admin/feedback` | Who gave which verdict on which entry, newest first (ADR 0090) |
| `GET /v1/admin/fleets/{fleet_id}/pins` | A fleet's pins and what changed since they were pinned (ADR 0092) |
| `GET /v1/admin/flagged` | The flag backlog, oldest flag first (ADR 0093) |

The single admin key gates this surface; admin-issued entries use the reserved name `admin` (ADR 0012). The admin panel (ADR 0029) is a browser front end over exactly this table plus the audit log and the usage counters; issuing and revoking admin keys stays with `hivemind-keys`.

### 12.5 Audit log

Every admin-surface action, every agent pin or unpin (ADR 0076) and every withdrawal through the agent portal (ADR 0095) is recorded in an append-only **audit log** (ADR 0027) — distinct from an entry's provenance and its supersession chain.

**What is recorded.** One row per successful action, with `occurred_at`, `actor_kind`, `actor`, `action`, `target` and a `detail` object:

| `action` | Recorded by | `target` | `detail` |
|---|---|---|---|
| `agent.register` | `POST /v1/agents`, `hive_register` — a **new** registration only (ADR 0046) | agent name | `{owner_alias}` when one was given (the registrant's unverified claim) |
| `agent.activate` | `POST /v1/admin/agents/{name}/activate` | agent name | `{trust_level, home_fleet_id}` |
| `agent.trust_level_set` | `PATCH /v1/admin/agents/{name}` | agent name | `{from, to}` |
| `agent.home_fleet_set` | `PATCH /v1/admin/agents/{name}` | agent name | `{from, to}` |
| `agent.revoke` | `POST /v1/admin/agents/{name}/revoke`, `hivemind-keys revoke`, and a retirement of an active agent | agent name | `{"from": "pending"\|"active"}`, plus `{"via": "retire", "successor"}` from a retirement (ADR 0028; a revoke from `pending` is a rejected registration) |
| `agent.retire` | `POST /v1/admin/agents/{name}/retire`, not a `dry_run` (ADR 0113); the revoke it makes is also recorded as `agent.revoke` | retired agent name | `{successor, was}` (`was` is the agent's status before) |
| `agent.retire_undo` | `POST /v1/admin/successions/{retired}/undo` (ADR 0113) | retired agent name | `{successor}` |
| `fleet.create` | `POST /v1/admin/fleets` | fleet id | `{name}` |
| `fleet.rename` | `PATCH /v1/admin/fleets/{id}`, only when the name changes (ADR 0109) | fleet id | `{from, to}` |
| `fleet.merge` | `POST /v1/admin/fleets/{source_id}/merge`, not a `dry_run` (ADR 0107); a new target is also recorded as `fleet.create` | source fleet id | `{merge_id, target_fleet_id, target_fleet_name, created_target, entries, agents, pins_kept, pins_dropped}` |
| `fleet.unmerge` | `POST /v1/admin/fleet-merges/{merge_id}/undo` (ADR 0107) | source fleet id | `{merge_id, target_fleet_id, entries, agents, pins_returned, pins_restored, pins_skipped}` (the skipped pins' entry ids) |
| `fleet.move_agents` | `POST /v1/admin/fleets/{source_id}/move`, not a `dry_run` (ADR 0112); a new target is also recorded as `fleet.create` | source fleet id | `{move_id, target_fleet_id, target_fleet_name, created_target, agents, entries, entries_held_back, pins_kept, pins_dropped}` |
| `fleet.move_undo` | `POST /v1/admin/fleet-merges/{merge_id}/undo` for a move (ADR 0112) | source fleet id | `{move_id, target_fleet_id, entries, agents, pins_returned, pins_restored, pins_skipped}` |
| `agent.cleanup` | `POST /v1/admin/agents/{name}/cleanup`, not a `dry_run`, and only when something was changed (ADR 0111); the pins it removed are also recorded as `entry.unpin`, and `also_revoke` as `agent.revoke` | agent name | `{cleanup_id, since, until, withdrawn, ratings_set_aside, restored, pins_removed, also_revoke}` |
| `agent.cleanup_undo` | `POST /v1/admin/agent-cleanups/{cleanup_id}/undo` (ADR 0111); the pins it put back are also recorded as `entry.pin` | agent name | `{cleanup_id, reactivated, reactivate_skipped, resuperseded, resupersede_skipped, ratings_cleared, pins_restored, pins_skipped}` |
| `org_key.rotate` | `POST /v1/admin/org-key/rotate`, `hivemind-keys rotate-org` | — | `{}` |
| `entry.withdraw` | `POST /v1/entries/{id}/withdraw` or `POST /v1/admin/entries/{id}/withdraw`, **only** when the admin key withdraws another agent's entry (§4.1); and every withdrawal through the agent portal, `POST /v1/portal/entries/{id}/withdraw` (ADR 0095) | entry id | `{author, reason}`, plus `"via": "portal"` from the portal |
| `admin_key.issue` | `hivemind-keys issue-admin` | key fingerprint | `{}` |
| `admin_key.revoke` | `hivemind-keys revoke-admin` (a successful revocation only) | key fingerprint | `{}` |
| `entry.pin` | `PUT /v1/entries/{id}/pin`, `hive_pin`, for every stored pin, a re-pin included (ADR 0076) | entry id | `{fleet_id}`, plus `from` (the replaced pin id, or a list) when a re-pin moved older pins, or `reason: "agent_cleanup_undo"` with the `cleanup_id` for a pin an undone cleanup put back (ADR 0111) |
| `entry.unpin` | `DELETE /v1/entries/{id}/pin`, `hive_pin` with `unpin`, or a withdrawal of a pinned entry; a pin actually removed (ADRs 0076, 0096) | entry id | `{fleet_id, pin_ids}` (the removed pins), plus `reason: "withdrawn"` when the withdrawal of the pinned entry removed them (ADR 0096; the withdrawer is the actor), `reason: "fleet_merge"` with the `merge_id` for a pin a fleet merge did not keep (ADR 0107), `reason: "fleet_move"` / `"fleet_move_undo"` with the `move_id` for a pin a fleet move did not keep, or a move's undo could not return (ADR 0112), or `reason: "agent_cleanup"` with the `cleanup_id` for a pin on a memory a cleanup withdrew (ADR 0111) |
| `agent_key.issue` | `hivemind-keys issue-agent` | agent name | `{}`, or `{"from", "trust_level", "home_fleet_id"}` when it activated a pending/revoked agent (ADR 0039) |
| `agent_key.replace` | `POST /v1/admin/agents/{name}/key`, `hivemind-keys rotate-agent` (ADR 0108) | agent name | `{old_key, new_key}`: the two key fingerprints (`old_key` null if the agent held none), never a key |

**Actors.** `actor_kind` is `admin_key` for the REST admin surface — `actor` is `admin:<fingerprint>` of the verified admin key — `agent_key` for a contributor's or privileged agent's pin or unpin (ADRs 0076, 0096) or a withdrawal through the agent portal (ADR 0095) — `actor` is the verified agent name — `org_key` for a registration made with the org key — `actor` is `org:<fingerprint>`, which identifies the key, not who used it (ADR 0046) — or `cli` for `hivemind-keys`, whose `actor` is the operator-supplied `--actor` (default: the OS user) and is **unverified**. A key **fingerprint** is the first 12 hex characters of the key's stored SHA-256 hash (what `hivemind-keys list` shows).

**Never recorded.** No raw API key appears in any column. Read-only calls, re-registrations (`already_registered`) and refused registrations are not audited; nor is an author withdrawing their own entry through `POST /v1/entries/{id}/withdraw` or `hive_withdraw` (the agent portal's withdrawal is audited, ADR 0095).

**Guarantees.** The CLI writes each row in the same transaction as its change. The REST surface writes the row **after** the change succeeds and not atomically with it: if the audit write fails the request fails (the change stands, unaudited), and a process crash between the two leaves the change unaudited.

**Reading it.** `GET /v1/admin/audit-log` (admin key) returns rows newest first; `actor` (exact), `action` (one of the vocabulary above), `since` (inclusive timestamp; naive = UTC), `before` (a row id: only rows strictly older than it, for paging back — ADR 0028) and `limit` (1–1000, default 100) filter it. There is no MCP verb — like `GET /v1/metrics`, it is an operator concern.

## 13. Entity extraction (v3 — ADR 0016)

This section supersedes the "no entity extraction" non-goal of §9 **for the facet slice only**. ADR 0016 owns the extractor; this section is the spec commitment; the ADR owns the *why*. The knowledge-graph half of the §10 row (entity registry + graph-expanded retrieval) stays trigger-held — facets are not a graph. This is a **pre-staged** §10 extension: the §10 trigger ("cross-entry entity linking pays off in retrieval quality") has *not* fired; the facet slice is shipped on scale ambition (300+ agents / multiple fleets) and is measured with the §1.1 harness.

### 13.1 What is extracted

* **At write time**, server-side, over the same text the embedder sees: `summary` + a bounded body prefix (`EMBEDDING_PREFIX_TOKENS` — whitespace-delimited words, ADR 0021). One budget feeds both consumers, so raising it raises the extractor's LLM input per write in step (§7).
* A fixed-prompt LLM call (`EXTRACTOR_MODEL` — a deploy-time decision, ADR 0016; e.g. a Qwen3-27B-class chat model on a *separate service* from the embeddings server).
* **All-or-nothing, schema-validated output:** `entities` is a list of `{name, kind}` where `name` is open vocabulary (trimmed, non-empty, ≤ 128 chars) and `kind` is a **closed** vocabulary (`person | organization | system | service | artifact | concept`); ≤ 10 entities per entry, deduped. Any malformed output fails the call — no partial salvage; the only tolerated wrapping is one leading `<think>…</think>` block and one markdown code fence around the JSON. C0/C1 control characters in names (NUL, newlines) are replaced by a space and whitespace collapsed before validation; format characters are kept; dedupe is on the lower-cased collapsed name.

### 13.2 Storage and immutability

* `entries.entities jsonb` + `entries.entities_model text` — symmetric with the `embedding` / `embedding_model` pair (ADR 0005): one machine writer, immutable after write (ADR 0001), provenance via `entities_model`.
* **No backfill in v1:** pre-existing entries keep `entities: []`; an operator-run backfill is a named maintenance procedure (the ADR 0005 re-embedding-migration pattern — the one sanctioned in-place exception to ADR 0001, for machine metadata only), not an online feature.

### 13.3 Query surface

* `EntryFilters.entities` — **AND-semantics over names, case-insensitive** (mirrors `tags`); exposed on REST `POST /v1/search` + `GET /v1/entries` and `hive_search` / `hive_list`.
* `kind` is stored + displayed only (not filterable in v1); entries expose an `entities` field in responses (MCP + REST).

### 13.4 Posture: optional + best-effort

* **Optional:** `EXTRACTOR_ENDPOINT` unset ⇒ extraction is off (entries land with `entities: []`, zero LLM cost) — the same dev-mode stance as "no authenticator".
* **Best-effort inline:** an extraction failure (timeout, retry exhaustion, schema mismatch) never blocks the write — the entry lands with `entities: []` and no `entities_model`. Entry-level write semantics are unchanged (ADR 0014); only the enrichment is lost.
* **Bounded retries** on transient failures (ADR 0014 pattern; `EXTRACTOR_RETRIES` default 2; `0` disables) within an overall deadline (`EXTRACTOR_DEADLINE`, derived when unset — ADR 0041).
* **Machine-only:** agents declare facets via `tags` (the existing channel); `entities` is a pure machine signal. Agent-supplied `entities` is a later extension (it would need a source flag).

### 13.5 Explicitly out of scope

* Graph expansion / multi-hop retrieval (stays a §10 / Tier 5 trigger; ADR 0006's rejection stands).
* A canonical entity registry / alias resolution (the next step after facets).
* Agent-supplied `entities` (needs a source flag; a later extension).
* Auto-contradiction detection (stays a §10 trigger).
