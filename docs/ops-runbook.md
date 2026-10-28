# Hivemind — Ops Runbook (Tier 3.1)

> **What this document is:** the operational story for running Hivemind in
> production — deployment, backups, monitoring / health, and the key
> issuance / rotation story (defined by ADR 0012). It links to the ADRs /
> SPEC for the *reasoning*; it owns the *how*.

## 1. Deployment (single-node, docker-compose)

> **`docker-compose.yaml` is a dev / single-host convenience, not a hardened
> production recipe.** It ships default credentials (`hivemind`/`hivemind`),
> binds Postgres (5432) and the unauthenticated vLLM server (8001) to
> **127.0.0.1 only**, and publishes the MCP runner (8088) on all interfaces.
> For anything real: change the Postgres password (and the DSN) before first
> start, keep 5432/8001 off every reachable interface (Docker publishes
> around `ufw`/`iptables INPUT` rules), and put 8088 behind a TLS-terminating
> proxy. The Kubernetes path in [DEPLOY.md](../DEPLOY.md) is the production
> story.

Hivemind is a **single-node** deployment (ADR 0007): one Postgres (with
pgvector) + the hostable streamable-HTTP MCP runner (+ an embedding
provider, ADR 0005). There is no orchestrator, no sharding, and no
multi-tenant isolation.

> **Kubernetes deployment?** This runbook is the *single-node*
> story. The production story (Kubernetes + GitLab CI/CD — image build,
> manifests, key bootstrap, key rotation) lives in [DEPLOY.md](../DEPLOY.md).

### Services (`docker-compose.yaml`)

| Service | Image / build | Purpose |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | the single Postgres + pgvector pool (host port 5432, loopback only) |
| `vllm` | `vllm/vllm-openai-cpu` | local CPU embedding server (ADR 0005; `:8001`, loopback only), fully-local path |
| `api` | this repo (`Dockerfile`) | the REST API (SPEC §5.1; `127.0.0.1:8000`, loopback only) |
| `admin` | this repo (`Dockerfile`) | the admin panel (ADR 0029; `127.0.0.1:8089`, loopback only), proxying to `api` |
| `portal` | this repo (`Dockerfile`) | the agent portal (ADR 0095; `127.0.0.1:8090`, loopback only), proxying to `api` |
| `mcp-http` | this repo (`Dockerfile`) | the hostable, multi-agent streamable-HTTP MCP runner (ADR 0010; host port 8088) |

### Bring up

```sh
make install        # uv sync (runtime + dev deps)
make pg             # start Postgres + pgvector (:5432)
make migrate        # apply the idempotent schema (hivemind-migrate)
make vllm           # optional: the local CPU embedding server (:8001)
make mcp-http       # start the hostable MCP runner as a detached service (:8088)
```

### Configuration (env vars, `src/hivemind/config.py`)

The most-used variables are below. [`config/.env.example`](../config/.env.example)
lists every one with its default, and `src/hivemind/config.py` is the source.

| Var | Meaning |
|---|---|
| `HIVEMIND_DATABASE_URL` | Postgres DSN (default `postgresql://hivemind:hivemind@localhost:5432/hivemind`) |
| `HIVEMIND_MIGRATE_DATABASE_URL` | optional direct (non-pooled) Postgres DSN used only by `hivemind-migrate`; set it when `HIVEMIND_DATABASE_URL` goes through a transaction-mode PgBouncer (see the note below). Empty = migrate through `HIVEMIND_DATABASE_URL` |
| `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` | the embedding provider (ADR 0005) |
| `HIVEMIND_EMBEDDING_DIM` | the embedding dimension (a deploy-time decision, default **1024** — ADR 0015; the dev Makefile exports 512 for fast local vLLM embedding — see §6) |
| `HIVEMIND_EMBEDDING_RETRIES` | retry budget for transient embedding failures (timeouts, connection errors, `429`, 5xx) — default 2; set `0` to disable (ADR 0014) |
| `HIVEMIND_EMBEDDING_DEADLINE` / `HIVEMIND_EXTRACTOR_DEADLINE` | overall per-call budget in seconds, retries and backoff included (ADR 0041). Unset (default) = derived: `(retries+1) x timeout + 0.5 x (2^retries - 1)`, i.e. the DEPLOY.md §5 worst case; set lower to cap a write's wait; must be >= the matching `_TIMEOUT` |
| `HIVEMIND_EXTRACTOR_ENDPOINT` / `_MODEL` / `_API_KEY` | the entity-extraction extractor (ADR 0016, SPEC §13): **optional + best-effort** — unset = extraction off (entries land with empty `entities`, zero LLM cost); an extraction failure **never** blocks a write (the entry lands without facets). Dev/test: `http://localhost:8080/v1` (`qwen3.8-27b`, key `dummy`) |
| `HIVEMIND_EXTRACTOR_RETRIES` / `_TIMEOUT` | retry budget + call timeout for transient extractor failures (ADR 0014 pattern) — default 2 retries / 30 s; deterministic 4xx + schema-validation failures fail fast, no retry |
| `HIVEMIND_HOST` / `HIVEMIND_PORT` | the mcp-http bind host/port (ADR 0010) |
| `HIVEMIND_EXPLORE_LAMBDA` | relevance weight of an opt-in `explore: true` search's MMR re-ordering, `(0, 1]`, default 0.7; 1 = relevance order (ADR 0078). Searches without `explore` ignore it |
| `HIVEMIND_RECENCY_FLOOR` | lower bound on the SPEC §6.4 recency factor, `(0, 1]`, default 0.8 (ADR 0022) — set it to an empty value or `none` (case-insensitive) to revert to the pre-ADR-0022 unbounded behaviour (ADR 0023), not a negligible number |
| `HIVEMIND_RECENCY_HINT` | on by default (ADR 0101): a search may carry a `recency` hint (`prefer_recent`, `ignore_age`; ADR 0073) and it changes that search's ranking; a search with no hint ranks as before. Set `false` to accept and ignore the hint |
| `HIVEMIND_CONFIRMATION_HALVES_FLAGS` | on by default (ADR 0106): once a noted helpful report is newer than every stale or wrong report on an entry, those flags count at half weight in its quality multiplier, so a rebutted flag weighs less in ranking. Counts, the flag backlog and the shown correction are unchanged. Set `false` to count every flag in full |
| `HIVEMIND_VECTOR_MIN_SIMILARITY` | the search similarity threshold (ADRs 0062, 0100), `[0, 1)`, 0.45 when unset (measured for the Qwen3-Embedding family; empty or `none` turns it off). The vector stream keeps only entries above it, so a search with nothing relevant can come back empty; the keyword stream is not cut. Depends on the embedding model and dimension: with another embedder, measure it with `make measure-threshold` ([retrieval experiments](retrieval-experiments.md#similarity-threshold)) and re-measure after changing either |
| `HIVEMIND_QUERY_INSTRUCTION` | a one-line task instruction for an instruction-aware embedder such as Qwen3-Embedding (ADR 0097), at most 500 characters. On by default with `Given an agent's task or question, retrieve shared memory entries that answer it`; set it empty to turn it off. Only the search query sent to the vector stream is embedded as `Instruct: {instruction}\nQuery:{query}`; entries are never embedded with it and the keyword stream keeps the raw query. The format is Qwen3's, so with a different embedder run `make measure-query-instruction` and turn it off if it does not help. It moves query vectors: re-check `HIVEMIND_VECTOR_MIN_SIMILARITY` after changing it |
| `HIVEMIND_VERSION_GROUP_MIN_SIMILARITY` | read-time grouping of unlinked versions of one fact (ADR 0074), `(0, 1)`, off by default (unset, empty or `none`). When set, two active search candidates whose stored vectors are at least this similar (same fleet, or both org-scoped) are shown next to each other, newest first, each naming the other in `similar`; nothing is hidden or re-scored. Depends on the embedding model: measure with `make measure-versions` ([retrieval experiments](retrieval-experiments.md#version-grouping)) before setting it |
| `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` | seconds the usage report reuses its one grouped scan of `entries` and the fleet health aggregates (ADRs 0069, 0075), default 300, `0` = scan on every report. Shared by `GET /v1/metrics` and `GET /metrics`, so the entry counts (and writes per fleet) can be up to this old; agents, fleets and searches are read on every report |
| `HIVEMIND_MCP_METRICS_ENABLED` | off by default; when `true`, `hivemind-mcp-http` serves an unauthenticated `GET /metrics` with its own request, tool-call and degraded-search series (ADR 0104). When off, `/metrics` is a 404 and nothing is collected. The Kubernetes Deployment sets it `true`; leave it off in the compose files, which publish the MCP port on every interface |
| `HIVEMIND_REQUIRE_STORED_KEYS` | off by default; when `true`, a dev-mode credential (not an admin, org or active agent key) is treated as trust level 0 on the data plane: reads come back empty and writes are refused (ADR 0082, SPEC §12.2). The Kubernetes ConfigMap sets it `true` |
| `HIVEMIND_IDLE_AGENT_DAYS` | default 30, a positive whole number: days without a request before the admin agent list marks an active agent idle (ADR 0110, [section 9](#9-finding-idle-agents-adr-0110)). A list only; nothing is revoked on it |
| `HIVEMIND_POOL_MIN_SIZE` / `HIVEMIND_POOL_MAX_SIZE` | the asyncpg connection pool's min/max size, per process (default 1 / 10) — the operator knob for per-pod concurrency |

> **Behind a transaction-mode PgBouncer (or any transaction-pooling
> proxy):** Hivemind's asyncpg pools disable their client-side prepared-
> statement cache (`statement_cache_size=0`) unconditionally, because
> that cache is unsafe once consecutive queries from one connection can
> land on different physical Postgres connections between calls — the
> documented asyncpg/transaction-mode incompatibility. `HIVEMIND_POOL_MAX_SIZE`
> sizes **both** pools a pod holds: `PgStore`'s data-plane pool and
> `PgAuthenticator`'s separate pool (auth is checked on every
> authenticated request, on its own connection lane) — one pod's total
> footprint is therefore up to `2 x pool_max_size`. An org whose
> Postgres `max_connections` is constrained should size it (times 2,
> times replica count, plus PgBouncer's own pool) to stay under that
> ceiling rather than relying on the default.
>
> **Migrations need a direct connection.** `hivemind-migrate` (run by every
> pod's entrypoint, ADR 0018) serialises replicas with a *session* advisory
> lock (ADR 0020), which a transaction-mode pooler cannot hold: the lock
> would be taken on one server connection and checked on another. Set
> `HIVEMIND_MIGRATE_DATABASE_URL` to a DSN that reaches Postgres directly
> (on Kubernetes: the optional `K8S_SECRET_MIGRATE_DATABASE_URL` CI
> variable); `hivemind-migrate` then uses it and the app keeps using
> `HIVEMIND_DATABASE_URL`. Unset, migrations use `HIVEMIND_DATABASE_URL`.

> **An unrecognized `HIVEMIND_*` variable is logged at startup**
> (ADRs 0024, 0032): `load_settings()` reports any `HIVEMIND_*` name
> that is neither a `Settings` field nor on the small exemption list in
> `src/hivemind/config.py` (`_ALLOWED_EXTRA_ENV_VARS` — currently
> `HIVEMIND_RUNNER`/`HOST`/`PORT`/`MCP_KEY`/`MCP_HTTP_PORT`, all read
> directly from `os.environ` outside `Settings`). The WARNING names each
> variable and, where the name is close to a real one, suggests it —
> this is what a stale or renamed variable (e.g. the ADR 0021
> `_PREFIX_CHARS` → `_PREFIX_TOKENS` rename) looks like. By default the
> process still starts, because platforms such as GitLab Auto DevOps and
> Kubernetes service links inject `HIVEMIND_*` variables of their own
> (`HIVEMIND_API_SERVICE_HOST` and the like). Set
> `HIVEMIND_STRICT_ENV=true` to make it a startup failure instead (the
> dev Makefile does, unless the profile file sets it).
> A setting a release removed (2.12.0: `HIVEMIND_RECENCY_FROM_CONFIRMATION`
> and `HIVEMIND_RECENCY_BY_KIND`, ADR 0103) is not treated as unknown: it is
> ignored and logged once as `Removed HIVEMIND_* environment variable(s)`,
> even under `HIVEMIND_STRICT_ENV=true`. Delete it from the env file.

> **Embedding dimension is a deploy-time decision** (ADR 0005): the
> `vector(:dim)` column is created at migration time. Changing the
> dimension requires a fresh pool (`make pg-reset`), not an online
> change (ADR 0005).

## 2. Backups (single-node Postgres)

The single source of truth is the Postgres pool (ADR 0007). Backups are
plain Postgres backups — no Hivemind-specific tooling.

### Logical backup (portable, the primary story)

```sh
# A full logical dump (consistent, small; the pool is single-node).
f=backup-$(date +%Y%m%d-%H%M).pgdump
docker compose exec -T postgres pg_dump -U hivemind -Fc hivemind > "$f"
# Verify the dump is readable before trusting it:
docker compose exec -T postgres pg_restore --list < "$f" | head
```

Schedule this (e.g. nightly) and ship it off-host. A logical dump is
portable across Postgres versions and is the safest restore path.

### Physical backup (fast, for scratch / small pools)

The `hivemind-pg-data` volume is the physical data directory. A volume
copy is a fast *point-in-time-ish* backup (for a small single-node pool;
not crash-consistent while the server writes — pair it with the logical
dump):

```sh
# --volumes-from reuses whatever volume the running container mounts, so the
# name (which depends on the compose project / checkout directory) cannot be
# wrong. A mistyped `-v <name>:/data` silently creates an EMPTY volume and
# "backs up" nothing.
docker run --rm --volumes-from "$(docker compose ps -q postgres)" -v "$(pwd)":/out \
  alpine tar -czf /out/pgdata.tar.gz -C /var/lib/postgresql/data .
# Sanity check: a real data directory is far larger than a few KB.
ls -l pgdata.tar.gz
```

### Restore

```sh
make pg-down
# wipe the volume, start fresh
make pg-reset
docker compose up -d postgres
# restore the logical dump
docker compose exec -i postgres pg_restore -U hivemind -d hivemind --clean --if-exists --no-owner < backup-20260601-0400.pgdump
# apply any migrations not already in the dump (ADR 0020)
make migrate
```

> **Drift check:** after a restore, confirm the pool is on the expected
> migration (ADR 0020): `uv run python -c "import asyncio; from hivemind.store.migrate import current_schema_version; ..."`
> or read `_yoyo_migration` directly. If it lags, run `make migrate`
> (safe to re-run; an up-to-date pool is a no-op).

## 3. Monitoring / health

| Signal | How |
|---|---|
| Liveness / readiness | Unauthenticated probe endpoints (ADR 0019), all public: `GET /v1/liveness` (shallow — 200 whenever the process answers), `GET /v1/health` (static 200, public by design — SPEC §5.1), `GET /mcp/liveness` (shallow), `GET /mcp/health` (shallow, the readiness probe — ADR 0037), `GET /mcp/health/database` (deep — 200 only when the Postgres pool answers within 5 s, else 503; the pod log names the cause: `database health check failing: …`) |
| Usage / counters | `GET /v1/metrics` (admin-gated) — entries / fleets / agents / searches / feedback counters and the `health` section (ROADMAP §3.3, ADRs 0056, 0068, 0075) |
| Prometheus | `GET /metrics` on each `hivemind-api` pod (no key; ADR 0050; each `hivemind-mcp` pod serves its own per-process series too when `HIVEMIND_MCP_METRICS_ENABLED=true`, as the Kubernetes manifest sets (ADR 0104): requests, `hivemind_search_degraded_total`, `hivemind_mcp_tool_calls_total{tool,outcome}`, `hivemind_mcp_tool_duration_seconds{tool}`, no usage gauges, so `sum` the process counters across the api and mcp jobs; Operator users: `deploy/kubernetes/optional/monitoring/`, DEPLOY.md §2 step 7): usage gauges (`max` across replicas), `hivemind_http_requests_total`, `hivemind_http_request_duration_seconds`, `hivemind_search_degraded_total` (alert on its rate: the embedder is down), `hivemind_searches_total` / `hivemind_searches_empty_total` / `hivemind_searches_explore_total` per fleet (every runner, read from the pool, `max` across replicas; ADRs 0056, 0078), `hivemind_feedback_flags` / `hivemind_feedback_flags_confirmed` per agent (ADR 0068), `hivemind_health_*` fleet health gauges (admin-facing observations, never an agent signal; a ratio with no denominator has no series; per-fleet ones carry `fleet`; `hivemind_health_success` is 0 while a refresh fails and the gauges are stale; ADR 0075) |
| Grafana | `deploy/kubernetes/optional/monitoring/hivemind-dashboard.json` (DEPLOY.md §2 step 8): search health, traffic, MCP tools and fleet health over time, from the series above |
| Schema drift | the latest id in `_yoyo_migration` (ADR 0020) — the applied migration |
| Postgres health | the `postgres` service healthcheck (`pg_isready`); `docker compose ps` |
| Embedder health | writes failing with `EmbeddingError` after the retry budget (ADR 0014) — check the embedding endpoint (`HIVEMIND_EMBEDDING_ENDPOINT`) and the provider process |
| Extractor health | entries landing with **empty `entities`** while extraction is expected (ADR 0016) — check `HIVEMIND_EXTRACTOR_ENDPOINT` / model reachability; note this is *by design* silent (best-effort: the write never fails over extraction), so watch for the symptom, not an error. All-or-nothing validation: if the model wraps its JSON in a code fence (or any schema violation — >10 entities, bad kind, name >128 chars) the whole extraction fails and the entry lands without facets — monitor facet coverage in the dogfood |
| MCP runner | `docker compose ps mcp-http` (ADR 0010); the streamable-HTTP endpoint `:8088` |

Watch for: the Postgres healthcheck failing, the `mcp-http` container
restarting, the pool's applied migration lagging the deployed chain
(drift → run `make migrate`), and writes failing with
`EmbeddingError` after the retry budget (ADR 0014) — a dead embedder
fails writes once its `HIVEMIND_EMBEDDING_RETRIES` budget is
exhausted; a transient blip is retried automatically and needs no
action. (The extractor, ADR 0016, is deliberately the opposite: its
failures are best-effort and never block a write — the observable
symptom is an entry with empty `entities`, not a failed write.)

> **A crashed `CREATE INDEX CONCURRENTLY` leaves an INVALID index**
> (ADR 0025, migrations `0004`, `0008` and `0013`). This is inherent to
> `CONCURRENTLY` — if the build is interrupted (pod killed, connection
> dropped mid-build), Postgres leaves the index catalogued but unusable,
> and the migration's `IF NOT EXISTS` means a plain re-run of
> `make migrate` will silently skip it rather than repair it. It applies
> to every index the chain builds concurrently:
> `entries_embedding_hnsw_idx` (0004), `entries_superseded_by_idx`,
> `entries_created_at_id_idx` and `entries_author_created_idx` (0008),
> `entry_links_to_created_idx` and `entries_agent_created_idx` (0013). Check for any:
>
> ```sql
> SELECT indexrelid::regclass, indisvalid
> FROM pg_index WHERE NOT indisvalid;
> ```
>
> For each row returned, either rebuild it in place
> (`REINDEX INDEX CONCURRENTLY <name>;` — takes no write lock on
> `entries`, safe under live traffic) or drop and let the next
> `make migrate` recreate it (`DROP INDEX CONCURRENTLY IF EXISTS <name>;`
> then re-run). Until repaired Postgres won't plan around the index:
> vector search falls back to the exact sequential scan (slower, never
> wrong), `?history` falls back to a scan of `entries` for predecessors
> and the default / per-author lists re-sort (all correct, O(pool)).
> `hivemind-migrate` itself fails with this remedy only for the HNSW
> index; check the others by hand with the query above.

## 4. Key issuance + rotation (ADR 0012)

The key model is **three kinds** (ADR 0012): one shared **org key**,
admin-issued **agent keys** (one per registered agent, carrying its
trust level + home fleet), and the single **admin key**. `user` keys are
retired (agents are first-class; their key is issued at activation).

### Issuing keys

```sh
# Admin key (the single credential for the admin surface).
uv run hivemind-keys issue-admin

# Agent key (issued at activation; one per registered agent, ADR 0012).
uv run hivemind-keys issue-agent --name alice
# A pending or revoked agent needs an explicit level and fleet (it is
# activated exactly as REST activate does; ADR 0039):
uv run hivemind-keys issue-agent --name alice --trust-level 1 --home-fleet <fleet-id>

# Replace an active agent's key (a leak, or the owner lost it; ADR 0108).
# The old key stops working at once; the new one is printed once:
uv run hivemind-keys rotate-agent --name alice

# Dead credentials (ADR 0039): pre-v2 `user` keys, name-less agent keys, and
# agent keys of agents that are missing, pending or revoked never
# authenticate. `hivemind-keys list` marks them [dead] and `migrate` logs a
# warning with their count. They are inert, but they would authenticate
# again after a rollback to an older release, so delete them:
#   SELECT c.key_hash, c.kind, c.user_id, c.agent_name FROM credentials c
#    WHERE c.kind = 'user'
#       OR (c.kind = 'agent' AND (c.agent_name IS NULL OR NOT EXISTS
#            (SELECT 1 FROM agents a WHERE a.name = c.agent_name AND a.status = 'active')));
#   -- review, then DELETE FROM credentials c WHERE <same predicate>;
```

The admin REST surface does the same (`POST /v1/admin/agents/{name}/activate`
returns the generated agent key **once** — the only moment a key is ever
shown). Raw keys are random 32-byte secrets shown once; only the SHA-256
hash is stored (a leaked database never leaks usable keys, SPEC §8.1).

### Rotation / revocation

| Action | Command / endpoint | Effect |
|---|---|---|
| Rotate the org key (close registration) | `hivemind-keys rotate-org` / `POST /v1/admin/org-key/rotate` | all prior org keys stop working on the next request; active agents are unaffected |
| Replace an agent's key | `hivemind-keys rotate-agent --name alice` / `POST /v1/admin/agents/alice/key` / the admin panel's "Replace key" | the old key stops working on the agent's next request and the new key is shown **once**; status, level, fleet and memories stay as they are; only for an `active` agent (a revoked one is re-activated instead; ADR 0108) |
| Revoke an agent key | `hivemind-keys revoke --name alice` / `POST /v1/admin/agents/alice/revoke` | the agent's key is dead and its status is `revoked`; the **name stays reserved**; on a `pending` agent this rejects the registration (ADRs 0012, 0028) |
| Demote an agent | `PATCH /v1/admin/agents/{name}` (trust level → 0) | the agent is *dormant* (key still valid, no access) — distinct from revocation |

> **Rotating the org key closes registration** (ADR 0031): every prior
> org key stops working immediately, so a leaked copy can no longer
> register agents. It does **not** cut off active agents — they
> authenticate with their own agent keys. To cut agents off, revoke
> them (or demote them to `untrusted`).

### Audit trail of key and admin actions (ADR 0027)

Every mutating `hivemind-keys` command (`issue-admin`, `revoke-admin`,
`rotate-org`, `rotate-agent`, `issue-agent`, `revoke`; not `list`) and every admin REST
action writes a row to the audit log (SPEC §12.5). Read it with the
admin key:

```sh
curl -s -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/audit-log?action=org_key.rotate&limit=20"
```

- **Name yourself on the CLI.** Pass `--actor` before the subcommand —
  `uv run hivemind-keys --actor alice.ops rotate-org`. It defaults to the
  OS user, which inside a container is usually `root` and says nothing.
  The value is **unverified** (anyone with database access can type
  anything); CLI rows are marked `actor_kind = cli` for that reason.
- **Admin keys appear by fingerprint**, the 12 hex characters
  `hivemind-keys list` shows. REST rows read `admin:<fingerprint>`, so
  after an admin-key rotation you can tell the old key's actions from
  the new one's. No raw key is ever written to the log.
- **Pins are audited too** (ADR 0076, migration `0014`): `entry.pin` and
  `entry.unpin` rows, with `actor_kind = agent_key` and the agent's name
  when a contributor or privileged agent pinned, `admin:<fingerprint>` for the admin key.
  Rolling the image back past `0014` without `make rollback` breaks the audit
  view on those rows (the rollback deletes them).
- **Portal pins and unpins are audited too** (ADR 0116): a pin or unpin
  made from the agent portal writes the usual `entry.pin` or
  `entry.unpin` row with `actor_kind = agent_key`, the agent's name as
  actor and `via: "portal"` in `detail`. Pins made through the API or
  `hive_pin` carry no `via`.
- **Portal withdrawals are audited too** (ADR 0095): an agent's own
  memory withdrawn from the agent portal writes an `entry.withdraw` row
  with `actor_kind = agent_key`, the agent's name as actor and
  `via: "portal"` in `detail`. An agent withdrawing its own entry through
  the API or `hive_withdraw` is not audited.
- **Raw SQL bypasses it.** A hand-run `DELETE FROM credentials …` is not
  audited; prefer the CLI even for break-glass work.

### Entries written with a self-reported author (before 1.0.0rc5)

Before ADR 0033, MCP `hive_write` accepted an `author` argument, and the
author decides who may read a `self` entry. An agent that passed another
name wrote entries readable by the agent registered under that name, and
unreadable by itself. From 1.0.0rc5 the author always comes from the key,
but existing rows are not rewritten: the true writer cannot be recovered,
because `agent` was self-reportable too. Review the suspects:

```sql
-- self entries whose author is not a registered agent, or differs from
-- the agent column (an agent key normally writes author = agent = its name)
SELECT e.id, e.author, e.agent, e.created_at, left(e.summary, 80) AS summary
FROM entries e
LEFT JOIN agents a ON a.name = e.author
WHERE e.scope = 'self'
  AND e.state = 'active'
  AND (a.name IS NULL OR e.author <> e.agent)
ORDER BY e.created_at;
```

For each row that should not be readable by its current author, withdraw
it with the admin key (`POST /v1/entries/{id}/withdraw` with a `reason`);
the withdrawal is recorded in the audit log.

## 5. Disaster recovery (full)

1. Stop the services: `make mcp-http-down`, `make pg-down`.
2. Restore Postgres (see §2): wipe the volume, start fresh, `pg_restore`
   the latest logical dump, `make migrate` (applies any outstanding
   migrations, ADR 0020).
3. Bring the services back up: `make pg`, `make mcp-http`.
4. Verify: `GET /v1/health`, `GET /v1/metrics`, and the applied
   migration (`_yoyo_migration`).

## 6. Embedding dimension (deploy-time decision, ADR 0005 + ADR 0015)

The embedding dimension is baked into the `vector(:dim)` column at
migration time (ADR 0005). It is **not** an online setting: changing it
requires a fresh pool (a re-embed of every entry), not an in-place
change. The **default dim is 1024** (ADR 0015 — we never assume 1536;
that is one provider's native dim), and the dev Makefile pins **512**
for fast local vLLM embedding. Choose the dimension to match the
embedding provider (ADR 0005: self-hosted) at deploy time; the
ROADMAP §4.2 tuning (prefix length / dimension) uses the §1.1 eval
harness to pick the value before it is pinned.

**A dim mismatch is loud, never silent (ADR 0015):** `hivemind-migrate`
(checks `current_embedding_dim` before applying anything) and the
integration suite fail at **migrate time** with an actionable error when
the pool's dim differs from the configured dim — naming both dims and
offering both fixes (reset the pool, or point `HIVEMIND_EMBEDDING_DIM`
at the pool's dim). You can no longer get a silent no-op followed by a
confusing `DataError` on the first vector write.

### 512 → 1024 switch (the production scenario)

The Qwen3-Embedding-0.6B model serves both dimensions (Matryoshka), so
the switch is a pool change, not a model change:

1. **Stop writes** — drain the `mcp-http` / API replicas so no new
   entries land at the old dim.
2. **Back up** the data (see §3) — the old-dim pool is your source of
   truth until the new one is verified.
3. **Set the dim** — `HIVEMIND_EMBEDDING_DIM=1024` (the code default is
   now 1024, ADR 0015; the dev Makefile defaults to 512 unless the profile file sets it,
   override per deploy) + the matching `HIVEMIND_EMBEDDING_MODEL` /
   endpoint. A wrong value is a loud error at `hivemind-migrate` time
   (ADR 0015), so it can't slip through silently.
4. **Reset the pool** — `make pg-reset` (dev) / recreate the vector
   column (prod), then `make migrate` — the `vector(:dim)` column is
   recreated at the new width (a pool reset, not a migration — ADR 0005).
5. **Re-embed** — re-import the prior entries through the write path
   (entries embed at write time, ADR 0005; a stored 512-dim vector
   cannot be re-used in a 1024-dim pool).
6. **Restart the services** and verify: `GET /v1/health`, then run the
   §1.1 eval harness (golden set) at the new dim before declaring the
   switch a success.

## 7. Merging fleets (ADR 0107)

Two fleets that grew into the same work can be merged into one. The source
fleet's memories (active, superseded and withdrawn), its agents and the
pins you choose move to the target fleet. No key changes: an agent's home
fleet is read from the database on every request, so its next request
already lands in the target and it keeps reading everything it wrote. A
merge writes no memory and supersedes nothing. It needs the admin key.

**In the admin panel.** Open **Fleets** and press **Merge into…** on the
source fleet.

1. Pick the target: an existing fleet, or a new name (to merge two fleets
   into a new one, merge the first into the new fleet, then the second
   into it).
2. Read the preview. It shows both fleets' agents, memories by state and
   pins, which tells you how long the merge will take. Likely duplicate
   pairs (the nearest memory of the other fleet at 0.90 similarity or more)
   are listed for your information only; nothing is superseded, and agents
   do that as they go. For a very large fleet only the newest 2,000
   memories of the smaller fleet are checked, and the preview says so.
3. Choose the pins. A fleet keeps at most 10. When the two fleets hold more
   than 10 together, the newest 10 are ticked and you must tick no more than
   10. The pins you leave out are unpinned and audited.
4. Type the source fleet's name and press **Merge fleets**.

**With the API.** The same steps, with the admin key:

```sh
# Preview: changes nothing.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"target_fleet_id": "<target-id>", "dry_run": true}' \
  "https://hivemind.example/v1/admin/fleets/<source-id>/merge"
# Merge (use "new_fleet_name": "..." instead of a target id for a new fleet).
# keep_pin_ids is needed when the two fleets hold more than 10 pins.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"target_fleet_id": "<target-id>", "keep_pin_ids": ["<entry-id>"], "dry_run": false}' \
  "https://hivemind.example/v1/admin/fleets/<source-id>/merge"
# The recorded merges, newest first, each with "undoable".
curl -s -H "X-API-Key: $ADMIN_KEY" "https://hivemind.example/v1/admin/fleet-merges"
# Undo one.
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/fleet-merges/<merge-id>/undo"
```

**What to expect.**

- **Writes to the two fleets wait briefly.** The merge locks both fleet
  rows while it moves the memories, a few seconds per 10,000 memories, so
  pick a quiet time. A merge and an undo run without the usual 30 second
  database command limit, so a large fleet is not cut off halfway (the
  work is one transaction, so a failure leaves nothing changed). A write
  that was already on its way to the source fleet is refused with
  `409 fleet_merged` (naming the target) and the agent retries; its next
  attempt lands in the target. A write that has to wait more than 3 seconds
  for the merge gets `503 fleet_busy` ("fleet is being merged or moved, retry")
  with a `Retry-After` header, on REST and in the MCP tools, and retrying
  works once the merge commits. If the merge cannot get its locks within
  5 seconds, or deadlocks with a concurrent change, it stops with
  `409 merge_refused` and changes nothing; run it again.
- **Do not merge during a rollout.** An older server pod does not know
  about merged fleets. It skips the lock, so a write or pin it handles can
  still land in the source fleet, and an agent it assigns to a merged
  fleet gets `409 fleet_merged` until you move it. Wait until every pod
  runs the new release.
- **Pins changed since the preview.** The panel sends the pins it showed.
  If someone pinned or unpinned in either fleet meanwhile, the merge
  answers `409 merge_refused` ("preview again"). Preview again and
  continue.
- **A failed audit write.** The merge commits first and is audited after.
  If the audit write fails the call answers 500 although the merge
  happened. Look at the merges list (or `GET /v1/admin/fleet-merges`)
  before you run it again.
- **The source fleet stays**, marked as merged. It is hidden from the fleet
  list unless you ask (`?include_merged=true`, or the **Show merged
  fleets** box in the panel), cannot be picked as a home fleet and cannot
  be merged again.
- **Monitoring.** Grafana and Prometheus series labelled with the source
  fleet stop at the merge and continue under the target. The entry counts
  are cached for `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` (300 s), so the old
  split can show for up to five minutes. The replica that ran the merge
  or undo drops its cached counts at once; other API replicas keep theirs
  until the TTL runs out. Search counters of the source are
  added to the target's.
- **Audit.** `fleet.merge` (and `fleet.create` for a new target), one
  `entry.unpin` with reason `fleet_merge` for each pin left out, and
  `fleet.unmerge` for an undo (SPEC §12.5).

**Undoing a merge.** Press **Undo** in the panel's **Merges and moves** list, or call
the endpoint above. The memories and agents the merge moved go back to the
source if they are still in the target. Memories written since stay in the
target, and an agent that was moved elsewhere in the meantime stays where
it is. Pins on the returning memories go back with them, and the pins that
were left out are restored where they fit under the limit of 10 (the oldest
are skipped, and the answer lists them). Search counters stay summed. A restored pin also has to fit the
limit of 3 pins per agent (admin pins are exempt). Undoing a merge into a
new fleet leaves that fleet in place, empty or holding what was written
after the merge; it is a normal fleet, so remove nothing by hand. An
undo is allowed only for the newest merge that involves the target fleet,
and only while the target has not itself been merged into another fleet.
If the fleet was merged again since, undo that merge first; otherwise the
endpoint answers `409 merge_refused` and says which.

**Rolling back the release.** `make rollback` past migration `0017` deletes
the merge record and the `fleet.merge` and `fleet.unmerge` audit rows. The
memories and agents stay where the merge put them, and merged fleets become
ordinary empty fleets again.

## 8. Renaming a fleet (ADR 0109)

A fleet's name is a label. Memories, pins, agents and keys all point at the
fleet's id, so a rename changes nothing for agents except the name they
see in `hive_whoami`. It needs the admin key.

In the panel, open **Fleets** and press **Rename** on the fleet's row
(merged fleets too, with **Show merged fleets** on). Or call the API:

```sh
curl -s -X PATCH -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "data-platform"}' \
  "https://hivemind.example/v1/admin/fleets/<fleet-id>"
```

- Names are unique and compared exactly. If another fleet already has the
  name, the answer is `409 fleet_exists` and nothing changes (the same
  answer as creating a fleet with a taken name).
- A bad name (blank, too long, hidden characters) is `422`, an unknown
  fleet is `404`.
- Renaming a fleet to the name it already has answers `200` and writes
  nothing.
- A merged fleet can be renamed, for example to `old-ops (merged)`.
- **Audit.** One `fleet.rename` row with the fleet id as target and
  `{from, to}` as detail (SPEC §12.5). The panel's audit log shows the new
  name.

**Rolling back the release.** `make rollback` past migration `0019` deletes
the `fleet.rename` audit rows. Renamed fleets keep their new names.
## 9. Finding idle agents (ADR 0110)

Each key records when it last worked, at most once an hour. The admin
agent list shows that as **Last active**, and marks an active agent
**idle** when its key has been quiet for `HIVEMIND_IDLE_AGENT_DAYS` days
(30 unless you set it). Use it to find keys nobody uses any more and
revoke them. Nothing is revoked for you, and nothing ranks or limits an
agent on it.

**In the admin panel.** Open **Agents**, choose **Idle 30+ days** (the
number follows your setting) in the activity filter, and press **Revoke**
on the rows you no longer want. Revoking kills the key on its next request
and keeps the name reserved; activating the agent again issues a new key.

**With the API.** Each row of `GET /v1/admin/agents` has `last_used_at`
(null if never), `idle` and `idle_after_days`:

```sh
curl -s -H "X-API-Key: $ADMIN_KEY" "https://hivemind.example/v1/admin/agents" \
  | jq -r '.[] | select(.idle) | "\(.name)\t\(.last_used_at // "never")"'
# Then, for each agent you decide to drop:
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/agents/<name>/revoke"
```

**What to expect.**

- **Existing agents start from the upgrade time.** Use before the release
  that added this was not recorded, so the upgrade stamps every active
  agent's key with its own time. An agent that went quiet earlier shows as
  idle `HIVEMIND_IDLE_AGENT_DAYS` days after the upgrade, not before. A key
  issued later and never used (a new agent, or a replaced key) is counted
  from the day it was issued.
- **Rolling back: image first, then the migration.** The server reads the
  new column on every key check. Run `make rollback` only after the old
  image is serving; the other order makes every key check fail.
- **The time is accurate to the hour.** A busy key is written once an hour,
  not on every request, so the check adds no database write to a normal
  request. The stamp is best effort: if the write fails, the request still
  succeeds and the next one tries again.
- **It covers every route.** The REST API, the MCP server and the agent
  portal all check keys the same way. Admin and org keys are stamped too,
  but only agents are listed.
- **Pending and revoked agents are never idle**; they have no live key.
  Re-activating an agent starts its last-use time over.

## 10. Cleaning up after a bad agent (ADR 0111)

An agent that went wrong for a while leaves memories behind that other
agents read, ratings that shape the quality score, and, if it had replaced
good memories with bad ones, good memories that are now marked superseded. A
cleanup puts that right for one agent and one time window. It needs the
admin key, and it can be undone.

**Order of work in an incident.**

1. **Stop the agent first.** Replace its key (**Replace key** on the agent's page, ADR 0108, section 4) or revoke it. While its key is live it
   can write more bad memories, and a cleanup only covers what exists when
   it runs.
2. **Find the window.** Open the agent's page in the admin panel and look at
   what it wrote and when. The window is on the time a memory was written to
   the pool (`created_at`), not on the date the agent claimed for it, and on
   the time a rating was last written. `since` counts, `until` does not.
   Leave `until` empty to mean now.
3. **Preview.** In **Agents**, press **Clean up…** on the agent (it works
   for active and revoked agents). Enter the window and press **Preview**.
   You see the memories that will be withdrawn, the number of ratings that
   will be set aside by verdict, the pins that will come off, and the
   memories the agent had replaced, which will become active again. Those
   are ticked: untick any you do not want back. Long lists are cut and say
   so, but the counts are exact.
4. **Apply.** Type the agent's name and press the button. The panel sends
   the counts it showed. If a memory or rating arrived in the window since,
   the answer is `409 cleanup_refused` ("preview again"); preview again and
   continue.
5. **Revoke, if you have not.** The **also revoke** box is off by default.
   Tick it to revoke the agent in the same request, after the cleanup. It is
   audited as its own `agent.revoke`. If the agent is already revoked, nothing
   more happens.

**What a cleanup does and does not do.**

- Withdraws only the agent's active memories in the window. Memories already
  superseded or withdrawn are left as they are.
- Sets ratings aside, it does not delete them. A set-aside rating no longer
  counts in quality, the flagged list or the reports on a memory. If the
  agent later rates the same memory again, the new rating counts.
- Offers to restore a memory that the agent's own memories replaced, even
  through a chain of its own replacements (junk over junk over a good
  memory brings back the good one; the junk stays superseded). A memory
  replaced by another author since is not offered.
- Removes pins that show a withdrawn memory, including a pin on an older
  version of it (not on a memory you restore). Each is audited as `entry.unpin`
  with reason `agent_cleanup`.
- Writes no memory and changes no key. Agents see withdrawn memories
  disappear and restored ones come back.
- Goes by who wrote a memory, not who owns it now (ADR 0113). Cleaning up
  a retired agent still withdraws what it wrote in the window, even though
  its successor owns those memories. Cleaning up a successor leaves the
  memories it inherited alone, because it did not write them.

**With the API.**

```sh
# Preview: changes nothing.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"since": "2026-10-27T08:00:00Z", "dry_run": true}' \
  "https://hivemind.example/v1/admin/agents/<name>/cleanup"
# Apply. Send back the counts from the preview, and the ids to restore.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"since": "2026-10-27T08:00:00Z", "until": "<until from the preview>", "dry_run": false,
       "expected_withdraw": 14, "expected_ratings": 3,
       "restore_ids": ["<entry-id>"], "also_revoke": false}' \
  "https://hivemind.example/v1/admin/agents/<name>/cleanup"
# The recorded cleanups, newest first, each with "undoable".
curl -s -H "X-API-Key: $ADMIN_KEY" "https://hivemind.example/v1/admin/agent-cleanups"
# Undo one.
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/agent-cleanups/<cleanup-id>/undo"
```

**What to expect.**

- **It takes locks.** A cleanup locks the agent's memories in the window and
  the pin locks of their fleets, so writes to those memories wait for it.
  A second cleanup of the same agent waits for the first, and gets `409 cleanup_refused` if it has waited 5 seconds. It runs without the
  usual 30 second database command limit. If it cannot get its locks within
  5 seconds it answers `409 cleanup_refused` and changes nothing; run it
  again.
- **Do not clean up during a rollout.** An older server pod does not know
  about set-aside ratings, so it still counts them.
- **Nothing to clean.** A window with nothing in it changes nothing, keeps no
  record and says so.
- **The revoke is a second step.** If revoking fails after the cleanup
  committed, the cleanup stands. Revoke the agent from the agents list.
- **A failed audit write.** The cleanup commits first and is audited after.
  If the audit write fails, the call answers 500 although the cleanup
  happened. Look at the cleanups list before you run it again.
- **Audit.** `agent.cleanup`, an `entry.unpin` for each pin removed, and
  `agent.cleanup_undo` for an undo (SPEC §12.5).

**Undoing a cleanup.** Press **Undo** in the **Cleanups** card, or call the
endpoint above. Memories still withdrawn by that cleanup become active
again. Memories it restored are replaced again by the same memory where that
is still possible. The ratings it set aside count again. Pins go back where
they fit under the limits of 10 per fleet and 3 per pinning agent (the admin
is exempt); the rest are skipped and listed. Anything changed after the
cleanup, such as a memory an admin withdrew again by hand, is left alone and
counted as skipped. An undo never un-revokes the agent; activate it again
separately if that is what you want. Only the newest cleanup of an agent that
has not been undone can be undone, so undo in the order they were made.
Otherwise the endpoint answers `409 cleanup_refused` and says which cleanup
comes first.

**Rolling back the release.** `make rollback` past migration `0022` deletes
the cleanup records and the `agent.cleanup` and `agent.cleanup_undo` audit
rows. Withdrawn memories stay withdrawn. The ratings are kept and count again. Memories still carry `admin cleanup <id>` as their withdrawal reason, naming a record that no longer exists, and a roll-forward starts with no cleanups to undo.


## 11. Moving agents to another fleet / splitting a fleet (ADR 0112)

When one team becomes two, or a few agents change teams, move those agents
and the memories they wrote to another fleet. The agents' home fleet changes
and the `fleet` memories they wrote in the source fleet move with them, in
one transaction. No key changes (the home fleet is read from the database on
every request), a move writes no memory and supersedes nothing, and the
source fleet stays a normal fleet. It needs the admin key.

**What moves, exactly.** A memory moves when it is a fleet memory of the
source (active, superseded or withdrawn), one of the moving agents wrote it,
and the newest version in its chain moves too. So a moving agent's old
version never leaves while the live version, written by an agent that
stays, remains behind. The reverse also holds: what a staying agent wrote
stays, even when a moving agent replaced it. Private notes (`self`), `org`
memories and ratings are not tied to a fleet and are not touched. Pins on
the moving memories go with them. Search counters stay with the source.

**The agents that stay lose access** to every memory that leaves, because
agents read their own home fleet. The preview says how many memories leave,
how many of the moving agents' memories stay behind for the chain reason, and
which agents stay. Read it before you apply.

**In the admin panel.** Open **Fleets** and press **Move agents…** on the
source fleet.

1. Tick the agents to move.
2. Pick the target, an existing fleet or a new name (it is created in the same
   step; this is how you split a fleet), and press **Preview**.
3. Read the counts. If the target's pins plus the pins that move are more
   than 10, tick the 10 to keep; the newest 10 are ticked. The others are
   unpinned and audited.
4. Type the source fleet's name and press **Move agents**.

**With the API.**

```sh
# Preview: changes nothing.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"agents": ["amy", "ann"], "target_fleet_id": "<target-id>", "dry_run": true}' \
  "https://hivemind.example/v1/admin/fleets/<source-id>/move"
# Move (use "new_fleet_name": "..." instead of a target id for a new fleet).
# keep_pin_ids is needed when the pins in play are more than 10.
curl -s -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"agents": ["amy", "ann"], "new_fleet_name": "team-b", "dry_run": false}' \
  "https://hivemind.example/v1/admin/fleets/<source-id>/move"
# The merges and moves, newest first, each with "kind" and "undoable".
curl -s -H "X-API-Key: $ADMIN_KEY" "https://hivemind.example/v1/admin/fleet-merges"
# Undo one (the same route as a merge).
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/fleet-merges/<id>/undo"
```

**What to expect.**

- **Every named agent must have the source as home fleet.** Otherwise the
  answer is `409 move_refused` and names the agents that do not (an unknown
  name is listed too). The target must be another fleet that has not been
  merged away (`409 fleet_merged`).
- **Writes to the two fleets wait briefly**, as for a merge (section 7): a
  few seconds per 10,000 memories, `503 fleet_busy` for a write that waits
  more than 3 seconds, and `409 move_refused` with no change when the move
  cannot get its locks within 5 seconds or deadlocks. Run it again. A write
  by a moving agent that was already on its way to the old fleet when the
  move committed is answered `503 fleet_busy` too ("home fleet changed,
  retry"), and the retry lands in the new fleet.
- **Do not move agents during a rollout.** An older pod does not do that
  home-fleet check, so such a write can land in the old fleet.
- **Pins changed since the preview** answers `409 move_refused` ("preview
  again"), as for a merge.
- **A failed audit write.** The move commits first and is audited after. If
  the audit write fails the call answers 500 although the move happened;
  check the list of merges and moves before you run it again.
- **Moving every agent** is allowed. The source fleet is not marked as merged
  and stays usable, empty of those agents.
- **Audit.** `fleet.move_agents` (and `fleet.create` for a new target), one
  `entry.unpin` with reason `fleet_move` for each pin left out, and
  `fleet.move_undo` for an undo (SPEC §12.5).

**Undoing a move.** Press **Undo** in the panel's **Merges and moves** list,
or call the endpoint above. The memories and agents the move recorded go back
to the source when they are still in the target; memories written since stay
where they are, and an agent moved elsewhere in the meantime stays there.
Pins follow their memories back, and the pins that were left out are restored
where they fit under the limit of 10 per fleet and 3 per pinning agent: the
pins on returning memories count too, so a source that refilled its briefing
after the move may not take them all back (the ones that do not fit are
unpinned, audited as `fleet_move_undo`, and listed in the answer). An undo is
allowed only for the newest merge or move that involves the target or the
source, and while neither fleet has been merged away; otherwise the answer is
`409 move_refused` and says which.

**Rolling back the release.** `make rollback` past migration `0023` deletes
the move records and the `fleet.move_agents` and `fleet.move_undo` audit rows.
The memories and agents stay where the move put them. Merge records are kept.
After that a move can no longer be undone, and an older merge's undo no longer
sees the moves that followed it, so undo any moves you want to reverse first.

## 12. Retiring an agent with a successor (ADR 0113)

When an agent is replaced (a new model, a new machine), its memories should
not be stranded. Retiring it hands every memory it wrote to a successor and
revokes the old agent in the same step. The successor reads, supersedes and
withdraws them, sees them in its agent portal, and cannot rate them (they
count as its own). Nothing is rewritten: `author` stays the old agent's
name. It needs the admin key.

In the panel, open **Agents** and press **Retire…** on an active or revoked
agent's row. Pick the successor (it must be active), read the preview (the private
notes, fleet memories and pins that pass over), type the agent's name and
confirm. The **Retired agents** list on the same page has an **Undo** per
row. Or call the API:

```sh
# Preview: counts only, nothing changes.
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"successor": "new-bot", "dry_run": true}' \
  "https://hivemind.example/v1/admin/agents/old-bot/retire"
# Retire.
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"successor": "new-bot", "dry_run": false}' \
  "https://hivemind.example/v1/admin/agents/old-bot/retire"
# The standing retirements, and an undo.
curl -s -H "X-API-Key: $ADMIN_KEY" "https://hivemind.example/v1/admin/successions"
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/successions/old-bot/undo"
```

- **What the successor gets.** Exactly what authorship gave: it reads the old
  agent's `self` notes and fleet memories (in whatever fleet they sit), and
  withdraws them. It supersedes a fleet memory only by writing into that same
  fleet, so a successor in another fleet can read and withdraw such a memory
  but not supersede it; the preview counts these. Its trust level and home
  fleet do not change.
- **Pins** stay where they are and none is dropped. In the successor's fleet
  they count toward its limit of 3 pins.
- **Chains** work: retire A to B, later B to C, and C owns both. A cycle is
  refused (`409 succession_refused`), as is retiring an agent twice. The
  successor must be active (`409 invalid_status`); the agent to retire may be
  active or already revoked, not pending.
- **Undo** ends the ownership at the next request. It does not bring the old
  agent back: it stays revoked, and activating it needs a new key. While a
  retirement stands, activating the old agent (API or `hivemind-keys
  issue-agent`) is refused with `409 agent_retired`.
- **Audit.** A successor's withdrawal of an inherited memory is audited as
  `entry.withdraw` with `via: "succession"`. `agent.retire` (target the old agent, detail `{successor, was}`)
  and, for an active agent, the usual `agent.revoke` with `via: "retire"`.
  An undo is `agent.retire_undo` (SPEC §12.5).
- **Cleanups go by author.** A cleanup of the old agent (section 10) still
  withdraws what it wrote, even after the hand-over. A cleanup of the
  successor covers only what the successor wrote itself.
- **Do not retire during a rollout.** An older pod ignores the new table: the
  successor reads less there until it is replaced, and it would not refuse
  activating the retired agent.

**Rolling back the release.** Deploy the previous release first, then run
`make rollback`: this release reads `successions` in every key check, so
rolling back the migration under it would fail every agent request. The
rollback past migration `0024` drops the `successions` table and the two
audit actions' rows. Retired agents stay revoked and successors lose the
memories they inherited.
