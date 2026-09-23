# Two app-tier replicas per runner, for availability — not throughput

**Supersedes [ADR 0007](0007-single-postgres-node-docker-compose-self-hosted.md)**
(single Postgres node, docker-compose, self-hosted per org). Most of
0007 is restated here unchanged; what changes is the *app tier*, which
0007 fixed at one process, and the *packaging*, which 0007 described as
docker-compose and which Tier 3.4 (Kubernetes + GitLab CI/CD) had
already moved without ever superseding that clause.

## Context

ADR 0007 decided the whole deployment in one sentence: one application
service plus one PostgreSQL node, via docker-compose, self-hosted per
organization. Two of its three clauses have drifted:

1. **The packaging clause is already false.** Tier 3.4 shipped a
   kustomize manifest tree (`deploy/kubernetes/`) and a GitLab pipeline
   that deploys it. docker-compose survives as the *dev* and
   single-node-ops story (`docs/ops-runbook.md`), not as the production
   shape. This was never written down as a decision, so ADR 0007 is
   still the citation of record for something nobody does.
2. **The one-process clause is the live question.** `replicas: 1` on
   both `hivemind-api` and `hivemind-mcp` means every deploy is an
   outage (the one pod terminates before its replacement is ready) and
   every node drain is an outage (the one pod is evicted with nowhere
   to land first). ROADMAP §3.5 explicitly deferred the replica bump as
   "a separate change — it drags in rollout strategy"; this is that
   change.

The third clause — **one Postgres node** — is not in question and is
restated below unchanged.

## Decision

**Run 2 replicas of each app-tier runner (`hivemind-api` and
`hivemind-mcp-http`), with an explicit `maxSurge: 1` /
`maxUnavailable: 0` rolling-update strategy and a `minAvailable: 1`
PodDisruptionBudget per Deployment. Everything else in ADR 0007 —
one Postgres node, no Redis, no sharding, one organization per
deployment — is unchanged.**

### The goal is availability, and only availability

Two replicas buy exactly two things:

* **Zero-downtime rolling deploys.** With `maxUnavailable: 0` the new
  pod must pass its readiness gate before the old one is asked to
  leave, so a release is a handover rather than a gap. Both Deployments
  already carry the machinery this assumes: a `startupProbe` that holds
  liveness off while the migration chain applies (ADR 0020), a 150s
  `terminationGracePeriodSeconds` sized to the worst-case write path,
  and a 5s `preStop` sleep that lets EndpointSlice deregistration win
  the race against SIGTERM. All of that was written for "more than one
  replica" and has been running against one.
* **Surviving a node drain or eviction.** A `minAvailable: 1` PDB is
  what actually delivers this: `maxUnavailable` on a Deployment governs
  *rollouts*, not **voluntary disruption**. `kubectl drain` consults the
  PDB and nothing else, so without one the eviction API will happily
  take the last pod of a 2-replica Deployment down.

### Three reasons that were considered and rejected

**Throughput — rejected on the shape of the workload.** The app tier is
I/O-bound, not CPU-bound: a request spends its time waiting on the
embedder, the extractor and Postgres. The per-request arithmetic is
already written down on both Deployments — a worst-case write is 31.5s
of embedder retries plus 91.5s of extractor retries plus two pooled
Postgres round-trips, essentially all of it blocked on something that
is not this process. Adding replicas adds *pressure* to those three
shared dependencies without adding *capacity* to any of them. If write
latency becomes the problem, the recorded answer is the async embedding
pipeline (the Postgres-outbox design, ROADMAP §10), not more pods.

**Availability-zone failure — rejected because it cannot be cashed.**
There is one Postgres node. An AZ failure that takes the database takes
the service, whatever the app tier is doing in the surviving zone.
Spreading app pods across AZs would be protection against a failure
mode that the storage tier does not survive either — real work, zero
delivered availability. If that changes, it changes because Postgres
becomes multi-node, and *that* is a different ADR with its own trigger.

**Three replicas — rejected as over-provisioning for the stated goal.**
Three buys surviving a *second* simultaneous failure (a node drain
while a rollout is mid-flight). Availability-only framing does not ask
for that, and every replica adds load to the one Postgres node. That
load used to be sequential-scan-shaped — ADR 0025 counted every
replica's vector query as another concurrent full-table scan — and the
HNSW index it shipped addresses that specific pressure, which is part
of why 2 is affordable now. It is not a reason to keep going.

### The connection budget, and why it is not the gating concern

More replicas means more Postgres connections, and there is one
Postgres node. This is bounded and tunable rather than load-bearing:

* A **transaction-mode PgBouncer** already sits in front of Postgres.
  The app tier's asyncpg pools disable their client-side
  prepared-statement cache unconditionally (`statement_cache_size=0`)
  because that cache is unsafe under transaction pooling, so the pooler
  is a supported topology, not a workaround.
* The pool size is an operator knob, not a hard-coded default:
  `HIVEMIND_POOL_MIN_SIZE` / `HIVEMIND_POOL_MAX_SIZE` reach `make_pool`
  from `Settings`, and `PgAuthenticator` shares them rather than
  falling back to bare defaults.
* The arithmetic is therefore explicit: one pod holds up to
  `2 x pool_max_size` connections (the `PgStore` data-plane pool plus
  `PgAuthenticator`'s separate lane), so the app tier's footprint is
  `2 x pool_max_size x replicas`, plus PgBouncer's own server pool.
  `docs/ops-runbook.md` already carries this and says to size it
  against `max_connections` rather than trusting the defaults.

Doubling the replica count doubles a number the operator already
controls and can already see. Without a pooler this would be the
decision's gating constraint; with one it is a sizing note.

## Explicit non-goals

This ADR is **narrowly an app-tier availability decision**. It must not
be cited as authority for any of the following, none of which it
decides, weakens or opens:

* **Multi-tenancy.** One deployment still serves exactly one
  organization (SPEC §9 non-goal; the §10 trigger is unfired).
* **Horizontal storage scaling.** One Postgres node, unchanged.
* **Sharding.** Not in v1, unchanged (SPEC §9).
* **Read replicas.** No read/write split; every replica reads and
  writes the same primary through the same pooler.
* **Redis.** Still bought by nothing — its ROADMAP §10 trigger reads
  "API replicas > 1, **or** distinct orgs > 1, **or** a cross-process
  per-agent rate-limit ceiling is needed". The replica clause of that
  trigger is now literally satisfied, and it still buys nothing,
  because Hivemind has no distributed rate limiting, no fan-out and no
  distributed locks: the only cross-process coordination in the system
  is the migration advisory lock, which is in Postgres (ADR 0020). If
  Redis is ever wanted it needs its own ADR arguing its own case, not
  a pointer at this replica count.
* **Throughput-motivated scaling.** Rejected above, with reasons. A
  later decision to scale for load is a different decision and needs a
  different ADR; it does not inherit this one's conclusion.

## What still holds from ADR 0007, and what changes

| ADR 0007 clause | Status |
|---|---|
| One Postgres node (with pgvector) per deployment | **Unchanged** |
| No Redis | **Unchanged** |
| No sharding, no separate storage service | **Unchanged** |
| No multi-tenant SaaS; self-hosted per organization | **Unchanged** |
| A second instance = a second organization | **Unchanged** (and see the glossary: a second *replica* is still one instance, still one organization) |
| Scale target (~50 users/agents, ~500 sessions/day, ~10k entries/day) | **Unchanged as the spec target**; ROADMAP Tier 5 already records the org's ambition at 300+ agents, which is a trigger, not a commitment |
| One application service (one process) | **Changed**: 2 replicas per runner |
| Deployed via docker-compose | **Changed**: Kubernetes (`deploy/kubernetes/`, Tier 3.4) is the production shape; docker-compose is the dev + single-node-ops story (`docs/ops-runbook.md`) |

## Consequences

* Both Deployments move to `replicas: 2` with an explicit
  `strategy.rollingUpdate` of `maxSurge: 1` / `maxUnavailable: 0`. The
  strategy is written down even though Kubernetes' own 25% defaults
  round to the same numbers at `replicas: 2` — at 3 or 4 they do not,
  so the guarantee would silently weaken the next time someone edits
  one line.
* Two `PodDisruptionBudget` resources (`minAvailable: 1`), one per
  Deployment, in the kustomize tree. Note the corollary: with
  `minAvailable: 1` at `replicas: 2`, draining *both* nodes that host a
  Deployment's pods requires the first drain to complete (a replacement
  pod Ready elsewhere) before the second can start. That is the point,
  but it makes a whole-cluster drain a sequenced operation rather than
  a simultaneous one.
* Rolling deploys are now routine pod terminations rather than a
  deliberate outage. Nothing new is required to make that safe: the
  REST surface is plain request/response with no server-side session
  state (SPEC §5.1), the MCP transport is stateless streamable-HTTP —
  one request is one self-contained exchange and the server holds no
  session registry (ADR 0010, SPEC §8.5) — and credentials are resolved
  per request, so a request landing on either replica behaves
  identically.
* Concurrent startup is already safe: `migrate` runs from the image
  entrypoint on every pod (ADR 0018) under a Postgres advisory lock, so
  two pods starting at once means exactly one migrates and the other
  waits (ADR 0020). This was designed for replicas and is now actually
  exercised by them.
* **Expand-and-contract stops being theoretical.** During a rolling
  deploy, release N and release N+1 run against one pool
  simultaneously — ADR 0020's rule was written for exactly this and is
  now enforced by the deployment shape, not only by CI.
* The app tier's Postgres connection footprint doubles, bounded as
  above. An operator with a constrained `max_connections` sizes
  `HIVEMIND_POOL_MAX_SIZE` accordingly (`docs/ops-runbook.md`).
* ADR 0007 keeps its text verbatim with a superseded-by header; its
  citations elsewhere in the repo that assert something still true
  (one Postgres node per org, the scale target, the backup story) are
  deliberately left pointing at it.
