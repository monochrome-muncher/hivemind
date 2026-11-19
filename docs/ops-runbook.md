# Hivemind ops runbook

This runbook is for the people who run Hivemind. Sections 1, 2, 3 and 5
cover running it on one machine with Docker Compose: setup, backups,
monitoring and recovery. Kubernetes has its own guide,
[DEPLOY.md](../DEPLOY.md). The rest applies to any deployment: keys (4),
the embedding dimension (6) and the admin tasks (7 to 21). Those use the
admin panel, the `hivemind-keys` command or the REST API with the admin
key, which work the same on Kubernetes (reach the API through
`kubectl port-forward` or the admin Ingress, DEPLOY.md §2 step 5). The
ADRs (architecture decision records) and SPEC.md hold the reasons; this
runbook holds the steps. An ADR number in parentheses, like (ADR 0007),
tells you where to read more.

**Running it:** [1. Deployment](#1-deployment-single-node-docker-compose) ·
[2. Backups](#2-backups-single-node-postgres) ·
[3. Monitoring](#3-monitoring-and-health) ·
[4. Keys](#4-issuing-and-rotating-keys) ·
[5. Disaster recovery](#5-disaster-recovery) ·
[6. Embedding dimension](#6-embedding-dimension)

**Admin tasks:** [7. Merge fleets](#7-merging-fleets) ·
[8. Rename a fleet](#8-renaming-a-fleet) ·
[9. Find idle agents](#9-finding-idle-agents) ·
[10. Clean up after a bad agent](#10-cleaning-up-after-a-bad-agent) ·
[11. Move agents or split a fleet](#11-moving-agents-to-another-fleet-or-splitting-a-fleet) ·
[12. Retire an agent](#12-retiring-an-agent-with-a-successor) ·
[13. A secret got into a memory](#13-a-secret-or-personal-data-got-into-a-memory) ·
[14. Re-extract entity names](#14-re-extracting-entity-names) ·
[15. An agent can't find a memory](#15-an-agent-cant-find-a-memory) ·
[16. Read a query's ranking](#16-read-the-ranking-of-a-production-query) ·
[17. When someone leaves](#17-when-someone-leaves) ·
[18. Fleet phases](#18-steering-a-fleet-with-a-phase) ·
[19. Scouts](#19-marking-an-agent-as-a-scout) ·
[20. Move a fleet memory to self](#20-a-fleet-memory-that-should-have-been-private) ·
[21. Rename an agent](#21-renaming-an-agent)

## 1. Deployment (single-node, docker-compose)

> **`docker-compose.yaml` is a convenience for development and single
> hosts. It is not a hardened production recipe.** It ships default
> credentials (`hivemind`/`hivemind`). It binds Postgres (5432) and the
> unauthenticated vLLM server (8001) to **127.0.0.1 only**, and it
> publishes the MCP runner (8088) on all interfaces. For anything real,
> change the Postgres password (and the DSN) before first start, keep
> 5432 and 8001 off every reachable interface (Docker publishes around
> `ufw` and `iptables INPUT` rules), and put 8088 behind a proxy that
> terminates TLS. For production, use the Kubernetes path in
> [DEPLOY.md](../DEPLOY.md).

Hivemind runs as a single node: one Postgres (with pgvector), the
hostable streamable-HTTP MCP runner and an embedding provider. There is
no orchestrator, no sharding and no multi-tenant isolation. (ADR 0007;
the embedding provider is ADR 0005.)

> **Running on Kubernetes?** This runbook covers the single-node setup.
> The production setup (Kubernetes and GitLab CI/CD: image build,
> manifests, key bootstrap, key rotation) is in [DEPLOY.md](../DEPLOY.md).

### Services (`docker-compose.yaml`)

| Service | Image / build | Purpose |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | the one Postgres + pgvector pool (host port 5432, loopback only) |
| `vllm` | `vllm/vllm-openai-cpu` | local CPU embedding server, the fully local path (`:8001`, loopback only; ADR 0005) |
| `api` | this repo (`Dockerfile`) | the REST API (`127.0.0.1:8000`, loopback only; SPEC §5.1) |
| `admin` | this repo (`Dockerfile`) | the admin panel, proxying to `api` (`127.0.0.1:8089`, loopback only; ADR 0029) |
| `portal` | this repo (`Dockerfile`) | the agent portal, proxying to `api` (`127.0.0.1:8090`, loopback only; ADR 0095) |
| `mcp-http` | this repo (`Dockerfile`) | the hostable streamable-HTTP MCP runner for many agents (host port 8088; ADR 0010) |

### Bring up

```sh
make install        # uv sync (runtime + dev deps)
make pg             # start Postgres + pgvector (:5432)
make migrate        # apply the idempotent schema (hivemind-migrate)
make vllm           # optional: the local CPU embedding server (:8001)
make mcp-http       # start the hostable MCP runner as a detached service (:8088)
```

### Configuration (env vars, `src/hivemind/config.py`)

Below are the variables you will use most. [`config/.env.example`](../config/.env.example)
lists every one with its default, and `src/hivemind/config.py` is the
authority. Long explanations sit in the notes under the table.

| Var | Meaning |
|---|---|
| `HIVEMIND_DATABASE_URL` | Postgres DSN (default `postgresql://hivemind:hivemind@localhost:5432/hivemind`) |
| `HIVEMIND_MIGRATE_DATABASE_URL` | optional direct (non-pooled) Postgres DSN, used only by `hivemind-migrate`. Empty means migrate through `HIVEMIND_DATABASE_URL`. See the PgBouncer note below |
| `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` | the embedding provider (ADR 0005) |
| `HIVEMIND_EMBEDDING_DIM` | the embedding dimension, a deploy-time decision. Default **1024** (ADR 0015). The dev Makefile exports 512 for fast local vLLM embedding. See §6 |
| `HIVEMIND_EMBEDDING_RETRIES` | retry budget for transient embedding failures (timeouts, connection errors, `429`, 5xx). Default 2; `0` turns retries off (ADR 0014) |
| `HIVEMIND_EMBEDDING_DEADLINE` / `HIVEMIND_EXTRACTOR_DEADLINE` | overall per-call budget in seconds, retries and backoff included (ADR 0041). See the deadline note |
| `HIVEMIND_EXTRACTOR_ENDPOINT` / `_MODEL` / `_API_KEY` | the entity extractor (ADR 0016, SPEC §13). Optional and best effort. See the extractor note |
| `HIVEMIND_EXTRACTOR_RETRIES` / `_TIMEOUT` | retry budget and call timeout for transient extractor failures (same pattern as ADR 0014). Default 2 retries and 30 s. Deterministic 4xx errors and schema-validation failures fail fast, with no retry |
| `HIVEMIND_EXTRACTOR_STRUCTURED_OUTPUT` | ask the extractor endpoint for schema-guided JSON (ADR 0133). Default `false`. See the structured output note |
| `HIVEMIND_HOST` / `HIVEMIND_PORT` | the bind host and port of the HTTP runners: api, mcp-http, admin and portal (ADR 0010) |
| `HIVEMIND_EXPLORE_LAMBDA` | relevance weight of the MMR re-ordering in a search that opts in with `explore: true`. Range `(0, 1]`, default 0.7; 1 means relevance order. A search without `explore` ignores it (ADR 0078) |
| `HIVEMIND_RECENCY_FLOOR` | lower bound on the SPEC §6.4 recency factor. Range `(0, 1]`, default 0.8 (ADR 0022). See the recency floor note |
| `HIVEMIND_RECENCY_HINT` | on by default (ADR 0101). A search may carry a `recency` hint (`prefer_recent`, `ignore_age`; ADR 0073) and the hint changes that search's ranking. A search with no hint ranks as before. Set `false` to accept the hint and ignore it |
| `HIVEMIND_CONFIRMATION_HALVES_FLAGS` | on by default (ADR 0106). Once a noted helpful report is newer than every stale or wrong report on an entry, those flags count at half weight in its quality multiplier, so a rebutted flag weighs less in ranking. Counts, the flag backlog and the shown correction do not change. Set `false` to count every flag in full |
| `HIVEMIND_VECTOR_MIN_SIMILARITY` | the search similarity threshold. Range `[0, 1)`, 0.45 when unset; empty or `none` turns it off (ADRs 0062, 0100). See the similarity threshold note |
| `HIVEMIND_CLOSE_MATCH_SIMILARITY` | the cosine similarity from which a search hit counts as a close match. Range `[0, 1)`, 0.65 when unset; `0` turns the feature off (ADR 0148). See the close match note |
| `HIVEMIND_QUERY_INSTRUCTION` | a one-line task instruction for an instruction-aware embedder such as Qwen3-Embedding, at most 500 characters. On by default; set it empty to turn it off (ADR 0097). See the query instruction note |
| `HIVEMIND_VERSION_GROUP_MIN_SIMILARITY` | read-time grouping of unlinked versions of one fact. Range `(0, 1)`, off by default (unset, empty or `none`) (ADR 0074). See the version grouping note |
| `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` | seconds the usage report reuses its one grouped scan of `entries` and the fleet health aggregates. Default 300; `0` scans on every report (ADRs 0069, 0075). See the entry counts note |
| `HIVEMIND_MCP_METRICS_ENABLED` | off by default. When `true`, `hivemind-mcp-http` serves an unauthenticated `GET /metrics` (ADR 0104). See the MCP metrics note |
| `HIVEMIND_REQUIRE_STORED_KEYS` | off by default. When `true`, a dev-mode credential is treated as trust level 0 on the data plane (ADR 0082, SPEC §12.2). See the stored keys note |
| `HIVEMIND_IDLE_AGENT_DAYS` | default 30, a positive whole number: the days without a request before the admin agent list marks an active agent idle. It only changes the list; nothing is revoked (ADR 0110; see [section 9](#9-finding-idle-agents)) |
| `HIVEMIND_POOL_MIN_SIZE` / `HIVEMIND_POOL_MAX_SIZE` | the asyncpg connection pool's minimum and maximum size, per process (default 1 / 10). This is the knob for per-pod concurrency |

**Notes on the table.**

- **Deadlines** (`HIVEMIND_EMBEDDING_DEADLINE` / `HIVEMIND_EXTRACTOR_DEADLINE`).
  Unset (the default), the deadline is derived:
  `(retries+1) x timeout + 0.5 x (2^retries - 1)`, which is the worst case
  in DEPLOY.md §5. Set it lower to cap how long a write waits. It must be
  at least the matching `_TIMEOUT`.
- **Extractor** (`HIVEMIND_EXTRACTOR_ENDPOINT` / `_MODEL` / `_API_KEY`).
  The extractor is **optional and best effort**. Unset, extraction is off:
  entries land with empty `entities` and there is no LLM cost. A failed
  extraction **never** blocks a write; the entry lands without facets. For
  dev and test use `http://localhost:8080/v1` (`qwen3.8-27b`, key `dummy`).
- **Structured output** (`HIVEMIND_EXTRACTOR_STRUCTURED_OUTPUT`). It needs
  an endpoint with JSON-schema guided decoding (vLLM, llama.cpp server,
  Ollama, OpenAI). A server that rejects it (HTTP 400 or 422) gets plain
  output from then on, with one warning in the log.
- **Recency floor** (`HIVEMIND_RECENCY_FLOOR`). Set it to an empty value or
  to `none` (case-insensitive) to go back to the unbounded behavior from
  before ADR 0022 (ADR 0023). Do not use a tiny number instead.
- **Similarity threshold** (`HIVEMIND_VECTOR_MIN_SIMILARITY`). The default
  0.45 was measured for Qwen3-Embedding. The vector stream keeps only
  entries above the threshold, so a search with nothing relevant can come
  back empty. The keyword stream is not cut. With another embedder or
  dimension, measure it again with `make measure-threshold` ([retrieval
  experiments](retrieval-experiments.md#similarity-threshold)).
- **Close match** (`HIVEMIND_CLOSE_MATCH_SIMILARITY`). The default 0.65 is
  calibrated for Qwen3-Embedding-8B. Another embedder needs its own value
  (see the calibration in the ADR). When no hit reaches the value,
  `hive_search` adds `closest: "loose"` and a note, and REST adds the
  `X-Hivemind-Closest: loose` header. Check the value again after you
  change the embedder or `HIVEMIND_QUERY_INSTRUCTION`.
- **Query instruction** (`HIVEMIND_QUERY_INSTRUCTION`). Only the search
  query gets the prefix; entries and the keyword stream do not. The format
  is Qwen3's. With a different embedder, run
  `make measure-query-instruction` and turn the setting off if it does not
  help. It moves query vectors, so check `HIVEMIND_VECTOR_MIN_SIMILARITY`
  again after you change it.
- **Version grouping** (`HIVEMIND_VERSION_GROUP_MIN_SIMILARITY`). When set,
  near-identical active candidates are shown next to each other, newest
  first, and each names the other in `similar`. Nothing is hidden or
  scored again. Before you set it, measure with `make measure-versions`
  ([retrieval experiments](retrieval-experiments.md#version-grouping)).
- **Entry counts** (`HIVEMIND_METRICS_ENTRY_COUNTS_TTL`). `GET /v1/metrics`
  and `GET /metrics` share this setting, so the entry counts (and the
  writes per fleet) can be as old as the TTL. Agents, fleets and searches
  are read on every report.
- **MCP metrics** (`HIVEMIND_MCP_METRICS_ENABLED`). When `true`, the
  endpoint carries the runner's own request, tool-call and degraded-search
  series. When off, `/metrics` is a 404 and nothing is collected. The
  Kubernetes Deployment sets it `true`. Leave it off in the compose files,
  which publish the MCP port on every interface.
- **Stored keys** (`HIVEMIND_REQUIRE_STORED_KEYS`). A dev-mode credential is
  one that is not an admin, org or active agent key. With the setting on,
  reads from it come back empty and writes are refused. The Kubernetes
  ConfigMap sets it `true`.

> **Behind a transaction-mode PgBouncer (or any transaction-pooling
> proxy):** Hivemind's asyncpg pools always turn off their client-side
> prepared-statement cache (`statement_cache_size=0`). That cache is
> unsafe when consecutive queries from one connection can land on
> different physical Postgres connections, which is the known
> incompatibility between asyncpg and transaction mode.
> `HIVEMIND_POOL_MAX_SIZE` sizes **both** pools a pod holds: the
> data-plane pool of `PgStore` and the separate pool of `PgAuthenticator`
> (auth is checked on every authenticated request, on its own connection
> lane). One pod can therefore use up to `2 x pool_max_size` connections.
> If your Postgres `max_connections` is tight, size the pool so that the
> total (times 2, times the replica count, plus PgBouncer's own pool)
> stays under that ceiling. Do not rely on the default.
>
> **Migrations need a direct connection.** `hivemind-migrate` runs in
> every pod's entrypoint (ADR 0018). It keeps replicas from migrating at
> the same time with a *session* advisory lock, which a transaction-mode
> pooler cannot hold, because the lock would be taken on one server
> connection and checked on another (ADR 0020). Set
> `HIVEMIND_MIGRATE_DATABASE_URL` to a DSN that reaches Postgres directly
> (on Kubernetes, the optional `K8S_SECRET_MIGRATE_DATABASE_URL` CI
> variable). `hivemind-migrate` then uses it and the app keeps using
> `HIVEMIND_DATABASE_URL`. If you leave it unset, migrations use
> `HIVEMIND_DATABASE_URL`.

> **Hivemind logs any unrecognized `HIVEMIND_*` variable at startup.**
> `load_settings()` reports every `HIVEMIND_*` name that is neither a
> `Settings` field nor on the short exemption list in
> `src/hivemind/config.py` (`_ALLOWED_EXTRA_ENV_VARS`, currently
> `HIVEMIND_RUNNER`/`HOST`/`PORT`/`MCP_KEY`/`MCP_HTTP_PORT`, all read
> directly from `os.environ` outside `Settings`). The warning names each
> variable and, where the name is close to a real one, suggests it. This
> is what a stale or renamed variable looks like, for example the rename
> of `_PREFIX_CHARS` to `_PREFIX_TOKENS` in ADR 0021. By default the
> process still starts, because platforms such as GitLab Auto DevOps and
> Kubernetes service links inject `HIVEMIND_*` variables of their own
> (`HIVEMIND_API_SERVICE_HOST` and the like). Set `HIVEMIND_STRICT_ENV=true`
> to make it a startup failure instead (the dev Makefile does, unless the
> profile file sets it). (ADRs 0024, 0032.)
>
> A setting that a release removed is not treated as unknown. The 2.12.0
> release removed `HIVEMIND_RECENCY_FROM_CONFIRMATION` and
> `HIVEMIND_RECENCY_BY_KIND` (ADR 0103). Such a variable is ignored and
> logged once as `Removed HIVEMIND_* environment variable(s)`, even under
> `HIVEMIND_STRICT_ENV=true`. Delete it from the env file.

> **The embedding dimension is a deploy-time decision.** The
> `vector(:dim)` column is created at migration time. To change the
> dimension you need a fresh pool (`make pg-reset`); it cannot be changed
> online (ADR 0005).

## 2. Backups (single-node Postgres)

Postgres holds all of Hivemind's data, so a backup is a plain Postgres
backup. You need no Hivemind-specific tooling (ADR 0007).

### Logical backup (portable, the one to rely on)

```sh
# A full logical dump (consistent, small; the pool is single-node).
f=backup-$(date +%Y%m%d-%H%M).pgdump
docker compose exec -T postgres pg_dump -U hivemind -Fc hivemind > "$f"
# Verify the dump is readable before trusting it:
docker compose exec -T postgres pg_restore --list < "$f" | head
```

Schedule this (for example nightly) and ship the file off the host. A
logical dump is portable across Postgres versions and is the safest way to
restore.

### Physical backup (fast, for scratch or small pools)

The `hivemind-pg-data` volume is the physical data directory. Copying the
volume is a fast backup, but only roughly a snapshot of one moment. It is
meant for a small single-node pool, and it is not crash-consistent while
the server is writing, so pair it with the logical dump:

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

> **Drift check:** after a restore, confirm the pool is on the migration
> you expect (ADR 0020):
>
> ```sh
> docker compose exec postgres psql -U hivemind -c "SELECT migration_id FROM _yoyo_migration ORDER BY applied_at_utc DESC, migration_id DESC LIMIT 1"
> ```
>
> If it lags, run `make migrate`. It is safe to run again, and an
> up-to-date pool does nothing.

## 3. Monitoring and health

Each row says what to watch and where.

| Signal | How |
|---|---|
| Liveness and readiness | Unauthenticated probe endpoints, all public (ADR 0019). `GET /v1/liveness` is shallow: it answers 200 whenever the process answers. `GET /v1/health` is a static 200, public by design (SPEC §5.1). `GET /mcp/liveness` is shallow. `GET /mcp/health` is shallow and is the readiness probe (ADR 0037). `GET /mcp/health/database` is deep: it answers 200 only when the Postgres pool answers within 5 s, else 503, and the pod log names the cause: `database health check failing: …` |
| Usage and counters | `GET /v1/metrics` (admin only): counters for entries, fleets, agents, searches and feedback, plus the `health` section (ROADMAP §3.3, ADRs 0056, 0068, 0075) |
| Prometheus | `GET /metrics` on each `hivemind-api` pod, no key (ADR 0050). See the Prometheus note below the table |
| Grafana | `deploy/kubernetes/optional/monitoring/hivemind-dashboard.json` (DEPLOY.md §10): search health, traffic, MCP tools, fleet health (including each fleet's unverified memories, ADR 0156) and the vital signs over time, drawn from the series above. `alerts.yaml` beside it is the opt-in `PrometheusRule` `hivemind-vital-signs` (ADR 0142). It raises `info` alerts for a quiet fleet, a rising echo rate, lingering rivals and a growing floating gap, and each alert says what the admin could do. The admin Health page's "Vital signs" card shows the same bands |
| Schema drift | the latest id in `_yoyo_migration`, which is the applied migration (ADR 0020) |
| Postgres health | the `postgres` service healthcheck (`pg_isready`); `docker compose ps` |
| Admin panel Overview | The top of the Overview is a **Needs you** row (ADR 0121). It shows keys locked from the portal, pending agents, idle agents and flagged memories older than 14 days. Each appears only above zero and links to its filtered list (Agents `#/agents?activity=locked`, `?status=pending`, `?activity=idle`; Health `#/health?flags=old`). With none, it reads "Nothing needs you right now." |
| Embedder health | writes failing with `EmbeddingError` after the retry budget (ADR 0014). Check the embedding endpoint (`HIVEMIND_EMBEDDING_ENDPOINT`) and the provider process |
| Extractor health | entries landing with **empty `entities`** while you expect extraction (ADR 0016). Check that `HIVEMIND_EXTRACTOR_ENDPOINT` and the model are reachable. See the extractor note below the table |
| MCP runner | `docker compose ps mcp-http`; the streamable-HTTP endpoint `:8088` (ADR 0010) |

**The Prometheus row in full.** `GET /metrics` on each `hivemind-api` pod
serves these series:

- Usage gauges (`max` across replicas).
- `hivemind_http_requests_total` and `hivemind_http_request_duration_seconds`.
- `hivemind_search_degraded_total`. Alert on its rate: it means the
  embedder is down.
- `hivemind_searches_total`, `_empty_total` and `_explore_total`, per fleet
  (ADRs 0056, 0078).
- `hivemind_feedback_flags` and `_confirmed`, per agent (ADR 0068).
- `hivemind_fleet_phase{fleet,phase}`, which is 1 for each fleet with a
  phase in force (ADR 0143).
- The `hivemind_health_*` fleet health gauges, including the 7-day
  `hivemind_health_breathing_7d` and `hivemind_health_fleet_breathing_7d`
  counts (ADR 0142). These are observations for admins, never a signal to
  an agent. `hivemind_health_success` is 0 while a refresh fails and the
  gauges are stale (ADR 0075).

Each `hivemind-mcp` pod serves its own request, tool-call and
degraded-search series when `HIVEMIND_MCP_METRICS_ENABLED=true`, as the
Kubernetes manifest sets it (ADR 0104). The tool-call series is
`hivemind_mcp_tool_calls_total{tool,outcome,code}`, where `code` is
`none`, `raised` or the reply's error code (ADR 0161). An mcp pod has no
usage gauges, so `sum` the process counters across the api and mcp jobs.
If you run the Kubernetes setup, see `deploy/kubernetes/optional/monitoring/`
(DEPLOY.md §10).

**The extractor row in full.** The extractor is silent by design. It is
best effort, and a write never fails over extraction, so you watch for the
symptom (empty `entities`), not for an error. Validation is all or
nothing. If the model wraps its JSON in a code fence, or breaks the schema
in any way (more than 10 entities, a bad kind, a name over 128
characters), the whole extraction fails and the entry lands without
facets. Monitor facet coverage in the dogfood.

Watch for the signals in the table above. A short embedder blip is retried
automatically and needs no action. A dead embedder fails writes once the
`HIVEMIND_EMBEDDING_RETRIES` budget runs out. The extractor is the
opposite: its failures are best effort and never block a write. The
symptom is an entry with empty `entities`, not a failed write.

> **A crashed `CREATE INDEX CONCURRENTLY` leaves an INVALID index**
> (migrations `0004`, `0008` and `0013`). This comes with `CONCURRENTLY`.
> If the build is interrupted (a pod is killed, a connection drops
> mid-build), Postgres leaves the index catalogued but unusable. The
> migration uses `IF NOT EXISTS`, so running `make migrate` again quietly
> skips the index instead of repairing it. This applies to every index the
> migration chain builds concurrently: `entries_embedding_hnsw_idx` (0004),
> `entries_superseded_by_idx`, `entries_created_at_id_idx` and
> `entries_author_created_idx` (0008), and `entry_links_to_created_idx`
> and `entries_agent_created_idx` (0013). Check for any of them:
>
> ```sql
> SELECT indexrelid::regclass, indisvalid
> FROM pg_index WHERE NOT indisvalid;
> ```
>
> For each row returned, either rebuild the index in place
> (`REINDEX INDEX CONCURRENTLY <name>;`, which takes no write lock on
> `entries` and is safe under live traffic), or drop it and let the next
> `make migrate` create it again (`DROP INDEX CONCURRENTLY IF EXISTS <name>;`
> and then run `make migrate`). Until you repair it, Postgres will not
> plan around the index. Vector search falls back to the exact sequential
> scan (slower, never wrong). `?history` falls back to a scan of `entries`
> for predecessors. The default and per-author lists sort again. All of
> these stay correct, but cost grows with the size of the pool.
> `hivemind-migrate` itself fails with this remedy only for the HNSW
> index, so check the others by hand with the query above. (ADR 0025.)

## 4. Issuing and rotating keys

Hivemind has **three kinds** of key: one shared **org key**, the
admin-issued **agent keys** (one per registered agent, carrying its trust
level and home fleet) and the single **admin key**. `user` keys are
retired: agents are first-class, and an agent's key is issued when you
activate it. (ADR 0012.)

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

The admin REST surface does the same. `POST /v1/admin/agents/{name}/activate`
returns the generated agent key **once**, and that is the only time a key
is ever shown. For an owner who has not registered, use **New agent** on
the admin panel's Agents page (or `POST /v1/admin/agents` with `name`,
`owner_alias`, `trust_level` and `home_fleet_id`). It registers and
activates the agent in one step and shows the key once. A name that
exists already is refused (ADR 0120). Raw keys are random 32-byte secrets
shown once, and only the SHA-256 hash is stored, so a leaked database
never leaks usable keys (SPEC §8.1).

### Rotation and revocation

| Action | Command / endpoint | Effect |
|---|---|---|
| Rotate the org key (close registration) | `hivemind-keys rotate-org` / `POST /v1/admin/org-key/rotate` | all prior org keys stop working on the next request; active agents are unaffected |
| Replace an agent's key | `hivemind-keys rotate-agent --name alice` / `POST /v1/admin/agents/alice/key` / the admin panel's "Replace key" | the old key stops working on the agent's next request and the new key is shown **once**; status, level, fleet and memories stay as they are; works only for an `active` agent (re-activate a revoked one instead; ADR 0108) |
| A locked key (the person behind the agent pressed **This key leaked** in the portal; ADR 0117) | the Agents page shows a "key locked" badge; `GET /v1/admin/agents` has `key_locked`; `hivemind-keys list` marks the key | the key fails every request with `key_locked` while the agent stays `active`. Replace the key (the row above) to let the agent back in; revoking works too. The lock is audited as `agent_key.lock` (actor: the agent's name, `via: "portal"`) |
| Revoke an agent key | `hivemind-keys revoke --name alice` / `POST /v1/admin/agents/alice/revoke` | the agent's key is dead and its status is `revoked`; the **name stays reserved**; on a `pending` agent this rejects the registration (ADRs 0012, 0028) |
| Correct an agent's owner alias (a typo made it a second owner in Stats) | `PATCH /v1/admin/agents/{name}` with `{"owner_alias": "..."}` / the admin panel's "Edit…" (Agents list or the agent page; the Owner alias field) | works at any status. The Stats "users" tab, the Agents list and the health owner figures follow at once, because the alias is read from the agent record. Audited as `agent.owner_set` with `{from, to}`; the same value changes nothing. Old `agent.register` audit rows keep the alias as it was typed. There is no CLI command, and `hivemind-keys` has no trust-level or home-fleet command either (ADR 0122) |
| Rename an agent (a typo, a reorganisation) | `POST /v1/admin/agents/{name}/rename` with `{"name": "..."}` / the admin panel's "Edit…" (the Name field) / `hivemind-keys rename-agent OLD NEW` | works at any status; the key keeps working and the memories, ratings and pins follow. See section 21 |
| Demote an agent | `PATCH /v1/admin/agents/{name}` (set the trust level to 0) | the agent is *dormant*: its key is still valid but it has no access. This is different from revocation |

> **Rotating the org key closes registration.** Every prior org key stops
> working at once, so a leaked copy can no longer register agents. It does
> **not** cut off active agents, because they sign in with their own agent
> keys. To cut agents off, revoke them or demote them to `untrusted`.
> (ADR 0031.)

### Audit trail of key and admin actions

Every mutating `hivemind-keys` command writes a row to the audit log
(SPEC §12.5). That covers `issue-admin`, `revoke-admin`, `rotate-org`,
`rotate-agent`, `rename-agent`, `issue-agent`, `revoke` and
`bootstrap-secret`, but not `list`. Every admin REST action writes a row
too (ADR 0027). Read the log with the admin key:

```sh
curl -s -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/audit-log?action=org_key.rotate&limit=20"
```

- **Name yourself on the CLI.** Pass `--actor` before the subcommand, for
  example `uv run hivemind-keys --actor alice.ops rotate-org`. It defaults
  to the OS user, which inside the image is `hivemind` and tells you
  nothing. The value is **unverified** (anyone with database access can
  type anything), so CLI rows are marked `actor_kind = cli`.
- **Admin keys appear by fingerprint**, the 12 hex characters
  `hivemind-keys list` shows. REST rows read `admin:<fingerprint>`, so
  after an admin-key rotation you can tell the old key's actions from the
  new one's. No raw key is ever written to the log.
- **Pins are audited too.** You get `entry.pin` and `entry.unpin` rows,
  with `actor_kind = agent_key` and the agent's name, or
  `admin:<fingerprint>` for the admin key. Rolling back past `0014`
  deletes those rows. (ADR 0076, migration `0014`.)
- **Portal pins and unpins are audited too.** A pin or unpin made from the
  agent portal writes the usual `entry.pin` or `entry.unpin` row with
  `actor_kind = agent_key`, the agent's name as actor and `via: "portal"`
  in `detail`. Pins made through the API or `hive_pin` carry no `via`.
  (ADR 0116.)
- **Portal withdrawals are audited too.** When an agent's own memory is
  withdrawn from the agent portal, an `entry.withdraw` row is written with
  `actor_kind = agent_key`, the agent's name as actor and `via: "portal"`
  in `detail`. An agent withdrawing its own entry through the API or
  `hive_withdraw` writes the same row with `via: "author"` (ADR 0141).
  (ADR 0095 for the portal.)
- **Restores are audited.** When the admin puts a withdrawn memory back,
  `entry.restore` is written (target: the entry id, detail `{author,
  previous_reason, reason}`). Rolling back `0031` deletes those rows (it
  needs `--discard-audit-rows`). The restored memories stay active.
  (ADR 0124, migration `0031`.)
- **Moves to self are audited.** Moving a fleet memory to `self` from the
  admin panel or the portal writes `entry.move_to_self` (target: the entry
  id, detail `{author, fleet_id, reason, earlier_versions}`, with
  `via: "portal"` from the portal) and one `entry.unpin` for each fleet
  whose pins it removed. Rolling back `0035` deletes those rows (it needs
  `--discard-audit-rows`). The moved memories stay `self`. (ADR 0145,
  migration `0035`.)
- **Key locks are audited.** When an agent locks its own key from the
  portal, `agent_key.lock` is written with `actor_kind = agent_key`, the
  agent's name, the key fingerprint and `via: "portal"` in `detail`.
  Rolling back past `0025` deletes those rows and makes a locked key
  usable again, so replace the key first if it must stay dead. (ADR 0117,
  migration `0025`.)
- **Rollbacks are audited and guarded.** Several shipped rollbacks
  (`0009`, `0014`, `0017`, `0019`, `0020`, `0022`, `0023`, `0024`, `0025`,
  `0027`, `0029`, `0031`, `0032`, `0033`, `0034`, `0035`) delete the audit
  rows whose action needs a wider `CHECK`. The rollback of `0006` sets
  revoked agents back to `active`, which revives their key rows. The
  rollback of `0025` drops the lock mark, so locked keys work again.
  `make rollback` (`hivemind-migrate --rollback N`) counts all three under
  the migration lock before any DDL. When a count is not zero, it exits
  non-zero, names each migration, its actions and the count, and changes
  nothing. Dump the log first (`pg_dump --table=audit_log`), then confirm
  on the make command line: `make rollback N=3 DISCARD_AUDIT_ROWS=1` for
  audit rows, `REVIVE_REVOKED_KEYS=1` for the keys of revoked agents
  (delete those keys instead if they must stay dead; see the dead
  credentials comment under Issuing keys) and `UNLOCK_LOCKED_KEYS=1` for
  locked keys (replace them first if they must stay dead). Directly, run
  `hivemind-migrate --rollback 3 --discard-audit-rows`, with exactly one
  number and only those flags. **Stop the app tier first.** The counts do
  not block live writes, so a row written between the count and the DDL is
  deleted without being counted. `discarded_audit_rows` is the count taken
  before the rollback. While `0027` stays applied (for example after a
  rollback of `0030` alone), each successful rollback writes one
  `schema.rollback` row (`actor_kind = cli`, the OS user, the most recent
  migration id as target, `{rolled_back, discarded_audit_rows}` as
  detail). A rollback that takes `0027` along leaves the warning on stderr
  as the only record. Rolling back `0027` deletes any such rows, so once
  they exist it needs `--discard-audit-rows`. (ADR 0119, migration
  `0027`.)
- **Reading one agent's history.** `GET /v1/admin/audit-log?subject=<agent>`
  returns the rows about that agent, newest first: its own key as actor,
  admin actions on it, withdrawals of its memories, and fleet moves and
  retirements that name it. `target=<exact>` filters on the row's target
  alone. The admin panel shows the same list on the agent's page
  ("History", with a CSV export) and as two filter fields on the Audit log
  page. The agent portal's "History" page shows the same for the agent
  (and the agents it inherits from) without naming the admin. None of this
  needs configuration. Migration `0030` builds four indexes
  `CONCURRENTLY` (`audit_log_target_idx`, `audit_log_withdraw_author_idx`,
  `audit_log_move_agents_idx`, `audit_log_successor_idx`). If a build is
  cancelled it leaves an `INVALID` index. Drop it and run the migration
  again (`DROP INDEX CONCURRENTLY <name>`, then `make migrate`). The
  `subject` query reads through a BitmapOr of these indexes and the
  existing `audit_log_actor_idx`. It runs with a custom plan (`SET LOCAL
  plan_cache_mode`), because a generic plan walks `occurred_at` backwards
  and filters the whole log. Rolling `0030` back only drops the indexes
  and writes a `schema.rollback` row. (ADR 0123, migration `0030`.)
- **Raw SQL bypasses the audit log.** A hand-run `DELETE FROM credentials …`
  is not audited. Use the CLI even for break-glass work.

### Entries written with a self-reported author (before 1.0.0rc5)

Before 1.0.0rc5, MCP `hive_write` accepted an `author` argument, and the
author decides who may read a `self` entry. An agent that passed another
name wrote entries that the agent registered under that name could read
and that the writer itself could not. From 1.0.0rc5 the author always
comes from the key (ADR 0033). Existing rows were not rewritten, because
the true writer cannot be recovered: `agent` could be self-reported too.
Review the suspects:

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
it with the admin key (`POST /v1/entries/{id}/withdraw` with a `reason`).
The withdrawal is recorded in the audit log.

### Restoring a mistaken withdrawal

The admin can put back one memory that was withdrawn by mistake (ADR 0124).
Open it in the panel's **Memories** page and press **Restore** (it asks
for a reason), or call `POST /v1/admin/entries/{id}/restore` with
`{"reason": "..."}` and the admin key. The memory is `active` again and
search can find it. Its withdrawal reason and time are cleared (the audit
row `entry.restore` keeps the old reason). **Pins are not restored.** Pin
the memory again if the fleet should see it in its briefing.

The call answers 409 when it should not go ahead:

- `entry_not_withdrawn`: the memory is already active or superseded.
- `restore_via_cleanup`: an agent cleanup that still stands withdrew it.
  Undo that cleanup (section 10), which brings back everything it changed
  together. Once the cleanup is undone the memory is already active.
- `restore_chain_conflict`: an agent cleanup once put this memory back over
  a newer one, so restoring it could leave two current versions of one
  fact (the newer one may come back later). Write a new memory with the
  content you want instead, or undo the cleanup.

A fleet merge, a move or a retirement never withdraws memories, so their
undos have no say here.

## 5. Disaster recovery

1. Stop the services: `make mcp-http-down`, `make pg-down`.
2. Restore Postgres (see §2): wipe the volume, start fresh, run
   `pg_restore` with the latest logical dump, then run `make migrate`. It
   applies any outstanding migrations (ADR 0020).
3. Bring the services back up: `make pg`, `make mcp-http`.
4. Check that it works: `GET /v1/health`, `GET /v1/metrics` and the
   applied migration (`_yoyo_migration`).

## 6. Embedding dimension

The embedding dimension is built into the `vector(:dim)` column when the
migration runs (ADR 0005). It is **not** an online setting. To change it
you need a fresh pool, which means re-embedding every entry. You cannot
change it in place. The **default dimension is 1024** (ADR 0015: we never
assume 1536, because that is one provider's native dimension), and the dev
Makefile pins **512** for fast local vLLM embedding. At deploy time, choose
the dimension that matches your embedding provider (self-hosted; ADR 0005).

**A dimension mismatch fails loudly, never silently.** `hivemind-migrate`
checks `current_embedding_dim` before it applies anything, and the
integration suite checks it too. Both fail at **migrate time** with a
clear error when the pool's dimension differs from the configured one. The
error names both dimensions and offers both fixes: reset the pool, or
point `HIVEMIND_EMBEDDING_DIM` at the pool's dimension. You can no longer
get a silent no-op followed by a confusing `DataError` on the first vector
write. (ADR 0015.)

### Switching from 512 to 1024 (the production scenario)

The Qwen3-Embedding-0.6B model serves both dimensions (Matryoshka), so
the switch changes the pool, not the model:

1. **Stop writes.** Drain the `mcp-http` and API replicas so that no new
   entries land at the old dimension.
2. **Back up** the data (see §2). The old-dimension pool is your only copy
   until you have checked the new one.
3. **Set the dimension.** Set `HIVEMIND_EMBEDDING_DIM=1024`, plus the
   matching `HIVEMIND_EMBEDDING_MODEL` and endpoint. The code default is
   now 1024 (ADR 0015). The dev Makefile defaults to 512 unless the profile
   file sets it, so override it per deploy. A wrong value is a loud error
   at `hivemind-migrate` time, so it cannot slip through.
4. **Reset the pool.** Run `make pg-reset` (dev) or recreate the vector
   column (prod), then run `make migrate`. The `vector(:dim)` column is
   created again at the new width. This is a pool reset, not a migration
   (ADR 0005).
5. **Re-embed.** Import the earlier entries again through the write path.
   Entries are embedded when they are written (ADR 0005), and a stored
   512-dimension vector cannot be reused in a 1024-dimension pool.
6. **Restart the services** and check: `GET /v1/health`, then run the eval
   harness (the golden set in `tests/eval/`) at the new dimension before
   you call the switch a success.

## 7. Merging fleets

A fleet is a group of agents that share memories. Two fleets that grew into
the same work can be merged into one. The source fleet's memories (active,
superseded and withdrawn), its agents and the pins you choose move to the
target fleet. No keys change: an agent's home fleet is read from the
database on every request, so its next request already lands in the target
and it keeps reading everything it wrote. A merge writes no memory and
supersedes nothing. It needs the admin key. (ADR 0107.)

**In the admin panel.** Open **Fleets** and press **Merge into…** on the
source fleet.

1. Pick the target: an existing fleet, or a new name. To merge two fleets
   into a new one, merge the first into the new fleet, then merge the
   second into it.
2. Read the preview. It shows both fleets' agents, memories by state and
   pins, which tells you how long the merge will take. Likely duplicate
   pairs (the nearest memory of the other fleet at 0.90 similarity or more)
   are listed for your information only. Nothing is superseded; agents do
   that as they go. For a very large fleet only the newest 2,000 memories
   of the smaller fleet are checked, and the preview says so.
3. Choose the pins. A fleet keeps at most 10. When the two fleets hold more
   than 10 together, the newest 10 are ticked, and you must tick no more
   than 10. The pins you leave out are unpinned and audited.
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

**After upgrading.** Memories written from the release with migration 0028
onwards merge and move fast. A memory written before it is slow on its
first merge or move only: the new version of its row lands on a page with
room, and the search index has to take it once. After that, and for its
undo, it is fast. You do not have to do anything.

`VACUUM FULL entries;` would make the old rows fast up front, but it is
optional and it is an outage. It rebuilds every index under an exclusive
lock, the vector index most of all: 80 seconds at 200,000 memories with
32-dimension vectors, and many minutes at 1024 dimensions. Every read and
write of memories waits, and with the 30 second command timeout the API is
effectively down. If the command has to wait for its lock, it blocks every
query that arrives behind it too. Run it only if that first slow merge
would cost more than the outage. If you do, run it in psql as the database
owner, in a quiet window:

```sql
SET lock_timeout = '5s';             -- give up rather than queue everyone behind it
SET maintenance_work_mem = '2GB';    -- at least the size of entries_embedding_hnsw_idx
VACUUM FULL entries;
```

You need disk for a second copy of the table and all its indexes. The time
is dominated by rebuilding the vector index, about as long as the build in
migration 0004 took. It builds a new HNSW graph, so search results can
differ slightly afterwards (ADR 0025).

If you roll migration 0028 back and the rollback is cancelled, it can leave
an invalid `entries_fleet_idx`. Drop it (`DROP INDEX CONCURRENTLY IF EXISTS
entries_fleet_idx;`). While it exists, merges and moves cannot be fast.

**What to expect.**

- **Writes to the two fleets pause while the memories move.** The merge
  locks both fleet rows meanwhile. A memory that is moving for the first
  time since the upgrade is rewritten and its vector is inserted again
  into the search index. That took about 12 ms per memory on a
  200k-memory test pool, roughly a minute per 5,000 memories. Once a
  memory has moved, or if it was written after the upgrade, its row is
  rewritten in place and the search index is left alone. That takes
  milliseconds, and so does the undo of a memory's first move. The panel's
  preview shows the slower estimate for the fleets you picked, so read it
  as a ceiling. Merge large fleets at a quiet time, since fleet writes are
  refused (see below) until it finishes. A merge and an undo run without
  the usual 30 second database command limit, so a large fleet is not cut
  off halfway (the work is one transaction, so a failure leaves nothing
  changed). A write that was already on its way to the source fleet is
  refused with `409 fleet_merged` (naming the target), and the agent
  retries. Its next attempt lands in the target. A write that has to wait
  more than 3 seconds for the merge gets `503 fleet_busy` ("fleet is being
  merged or moved, retry"). On REST it carries a `Retry-After` header; in
  the MCP tools it is a `fleet_busy` tool error without one. Retrying works
  once the merge commits. If the merge cannot get its locks within
  5 seconds, or deadlocks with a concurrent change, it stops with
  `409 merge_refused` and changes nothing. Run it again.
- **Do not merge during a rollout.** An older server pod does not know about
  merged fleets. It skips the lock, so a write or pin it handles can still
  land in the source fleet, and an agent it assigns to a merged fleet gets
  `409 fleet_merged` until you move it. Wait until every pod runs the new
  release.
- **Pins changed since the preview.** The panel sends the pins it showed.
  If someone pinned or unpinned in either fleet meanwhile, the merge
  answers `409 merge_refused` ("preview again"). Preview again and
  continue.
- **A failed audit write.** The merge commits first and is audited after.
  If the audit write fails, the call answers 500 although the merge
  happened. Look at the merges list (or `GET /v1/admin/fleet-merges`)
  before you run it again.
- **The source fleet stays**, marked as merged. It is hidden from the fleet
  list unless you ask (`?include_merged=true`, or the **Show merged
  fleets** box in the panel). It cannot be picked as a home fleet and
  cannot be merged again.
- **Monitoring.** Grafana and Prometheus series labelled with the source
  fleet stop at the merge and continue under the target. The entry counts
  are cached for `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` (300 s), so the old
  split can show for up to five minutes. The replica that ran the merge or
  undo drops its cached counts at once; other API replicas keep theirs
  until the TTL runs out. The source's search counters are added to the
  target's.
- **Audit.** `fleet.merge` (and `fleet.create` for a new target), one
  `entry.unpin` with reason `fleet_merge` for each pin left out, and
  `fleet.unmerge` for an undo, with one `entry.pin` (reason
  `fleet_merge_undo`) for each pin it puts back (SPEC §12.5).

**Undoing a merge.** Press **Undo** in the panel's **Merges and moves**
list, or call the endpoint above. The memories and agents the merge moved
go back to the source if they are still in the target. Memories written
since stay in the target, and an agent that was moved elsewhere in the
meantime stays where it is. Pins on the returning memories go back with
them. The pins that were left out are restored where they fit under the
limit of 10 (the oldest are skipped, and the answer lists them). A
restored pin also has to fit the limit of 3 pins per agent (admin pins are
exempt). Search counters stay summed. Undoing a merge into a new fleet
leaves that fleet in place, empty or holding what was written after the
merge. It is a normal fleet, so do not remove anything by hand. You can
undo only the newest merge that involves the target fleet, and only while
the target has not itself been merged into another fleet. If the fleet was
merged again since, undo that merge first. Otherwise the endpoint answers
`409 merge_refused` and says which merge comes first.

**Rolling back the release.** `make rollback` past migration `0017` deletes
the merge record and the `fleet.merge` and `fleet.unmerge` audit rows. The
memories and agents stay where the merge put them, and merged fleets become
ordinary empty fleets again.

## 8. Renaming a fleet

A fleet's name is a label. Memories, pins, agents and keys all point at the
fleet's id, so a rename changes nothing for agents except the name they see
in `hive_whoami`. It needs the admin key. (ADR 0109.)

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
- A bad name (blank, too long, hidden characters) answers `422`, and an
  unknown fleet answers `404`.
- Renaming a fleet to the name it already has answers `200` and writes
  nothing.
- A merged fleet can be renamed, for example to `old-ops (merged)`.
- **Audit.** One `fleet.rename` row with the fleet id as target and
  `{from, to}` as detail (SPEC §12.5). The panel's audit log shows the new
  name.

**Rolling back the release.** `make rollback` past migration `0019` deletes
the `fleet.rename` audit rows. Renamed fleets keep their new names.

## 9. Finding idle agents

Each key records when it last worked, at most once an hour. The admin agent
list shows that as **Last active**, and marks an active agent **idle** when
its key has been quiet for `HIVEMIND_IDLE_AGENT_DAYS` days (30 unless you
set it). Use it to find keys nobody uses any more and revoke them. Nothing
is revoked for you, and nothing ranks or limits an agent on it. (ADR 0110.)

**In the admin panel.** Open **Agents**, choose **Idle 30+ days** (the
number follows your setting) in the activity filter, and press **Revoke**
on the rows you no longer want. Revoking kills the key on its next request
and keeps the name reserved. Activating the agent again issues a new key.

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
  image is serving. The other order makes every key check fail.
- **The time is accurate to the hour.** A busy key is written once an hour,
  not on every request, so the check adds no database write to a normal
  request. The stamp is best effort: if the write fails, the request still
  succeeds and the next one tries again.
- **It covers every route.** The REST API, the MCP server and the agent
  portal all check keys the same way. Admin and org keys are stamped too,
  but only agents are listed.
- **Pending and revoked agents are never idle**, because they have no live
  key. Re-activating an agent starts its last-use time over.

## 10. Cleaning up after a bad agent

An agent that went wrong for a while leaves memories behind that other
agents read. It leaves ratings that shape the quality score. If it had
replaced good memories with bad ones, it also leaves good memories that
are now marked superseded. A cleanup puts that right for one agent and one
time window. It needs the admin key, and it can be undone. (ADR 0111.)

**Order of work in an incident.**

1. **Stop the agent first.** Replace its key (**Replace key** on the
   agent's page, section 4) or revoke it (ADR 0108). While its key is live
   it can write more bad memories, and a cleanup only covers what exists
   when it runs.
2. **Find the window.** Open the agent's page in the admin panel and look at
   what it wrote and when. The window is on the time a memory was written
   to the pool (`created_at`), not on the date the agent claimed for it, and
   on the time a rating was last written. `since` counts, `until` does not.
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
   the answer is `409 cleanup_refused` ("preview again"). Preview again and
   continue.
5. **Revoke, if you have not.** The **also revoke** box is off by default.
   Tick it to revoke the agent in the same request, after the cleanup. It
   is audited as its own `agent.revoke`. If the agent is already revoked,
   nothing more happens.

**What a cleanup does and does not do.**

- It withdraws only the agent's active memories in the window. Memories
  already superseded or withdrawn are left as they are.
- It sets ratings aside and does not delete them. A set-aside rating no
  longer counts in quality, the flagged list or the reports on a memory. If
  the agent later rates the same memory again, the new rating counts.
- It offers to restore a memory that the agent's own memories replaced,
  even through a chain of its own replacements (junk over junk over a good
  memory brings back the good one, and the junk stays superseded). A memory
  replaced by another author since is not offered.
- It removes pins that show a withdrawn memory, including a pin on an older
  version of it (but not a pin on a memory you restore). Each is audited as
  `entry.unpin` with reason `agent_cleanup`.
- It writes no memory and changes no key. Agents see withdrawn memories
  disappear and restored ones come back.
- It goes by who wrote a memory, not who owns it now. Cleaning up a retired
  agent still withdraws what it wrote in the window, even though its
  successor owns those memories. Cleaning up a successor leaves the
  memories it inherited alone, because it did not write them. (ADR 0113.)

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
  the pin locks of their fleets, so writes to those memories wait for it. A
  second cleanup of the same agent waits for the first, and gets
  `409 cleanup_refused` if it has waited 5 seconds. A cleanup runs without
  the usual 30 second database command limit. If it cannot get its locks
  within 5 seconds, it answers `409 cleanup_refused` and changes nothing.
  Run it again.
- **Do not clean up during a rollout.** An older server pod does not know
  about set-aside ratings, so it still counts them.
- **Nothing to clean.** A window with nothing in it changes nothing, keeps
  no record and says so.
- **The revoke is a second step.** If revoking fails after the cleanup
  committed, the cleanup stands. Revoke the agent from the agents list.
- **A failed audit write.** The cleanup commits first and is audited after.
  If the audit write fails, the call answers 500 although the cleanup
  happened. Look at the cleanups list before you run it again.
- **Audit.** `agent.cleanup`, an `entry.unpin` for each pin removed, and
  `agent.cleanup_undo` for an undo (SPEC §12.5).

**Undoing a cleanup.** Press **Undo** in the **Cleanups** card, or call the
endpoint above. Memories still withdrawn by that cleanup become active
again. Memories it restored are replaced again by the same memory where
that is still possible. The ratings it set aside count again. Pins go back
where they fit under the limits of 10 per fleet and 3 per pinning agent
(the admin is exempt), and the rest are skipped and listed. Anything
changed after the cleanup, such as a memory an admin withdrew again by
hand, is left alone and counted as skipped. An undo never un-revokes the
agent. If you want that, activate the agent again separately. You can undo
only the newest cleanup of an agent that has not been undone, so undo in
the order they were made. Otherwise the endpoint answers
`409 cleanup_refused` and says which cleanup comes first.

**When the agent cleans up for itself.** The person behind an agent can run
the same cleanup from the agent portal (**Clean up**), on that agent's own
work and without the revoke (ADR 0118). It shows in the **Cleanups** card
marked "self-service", with the agent as the one who ran it. In the audit
log it is `agent.cleanup` with actor kind `agent_key` and `via: "portal"`
in the detail. An admin can undo it from the card as usual. A cleanup you
ran cannot be undone from the portal: the person sees it in their list
marked "an admin" and has to ask you. The newest cleanup of an agent, yours
or theirs, is the only one that can be undone. A successor's cleanup also
reaches the memories it inherited from the agents retired in its favour,
which your own cleanup of the successor does not.

**Rolling back the release.** `make rollback` past migration `0022` deletes
the cleanup records and the `agent.cleanup` and `agent.cleanup_undo` audit
rows. Withdrawn memories stay withdrawn. The ratings are kept and count
again. Memories still carry `admin cleanup <id>` as their withdrawal
reason, naming a record that no longer exists, and a roll-forward starts
with no cleanups to undo.

Rolling back migration `0026` only drops the mark that says an agent ran a
cleanup itself. Afterwards its cleanups read as admin cleanups. They can
still be undone from the admin panel, since their withdrawals carry the
same `admin cleanup <id>` reason.

## 11. Moving agents to another fleet or splitting a fleet

When one team becomes two, or a few agents change teams, move those agents
and the memories they wrote to another fleet. The agents' home fleet
changes, and the `fleet` memories they wrote in the source fleet move with
them, in one transaction. No key changes (the home fleet is read from the
database on every request), a move writes no memory and supersedes
nothing, and the source fleet stays a normal fleet. It needs the admin key.
(ADR 0112.)

**What moves, exactly.** A memory moves when it is a fleet memory of the
source (active, superseded or withdrawn), one of the moving agents wrote
it, and the newest version in its chain moves too. So a moving agent's old
version never leaves while the live version, written by an agent that
stays, remains behind. The reverse also holds: what a staying agent wrote
stays, even when a moving agent replaced it. Private notes (`self`), `org`
memories and ratings are not tied to a fleet and are not touched. Pins on
the moving memories go with them. Search counters stay with the source.

**The agents that stay lose access** to every memory that leaves, because
agents read their own home fleet. The preview says how many memories leave,
how many of the moving agents' memories stay behind for the chain reason,
and which agents stay. Read it before you apply.

**In the admin panel.** Open **Fleets** and press **Move agents…** on the
source fleet.

1. Tick the agents to move.
2. Pick the target, an existing fleet or a new name (it is created in the
   same step; this is how you split a fleet), and press **Preview**.
3. Read the counts. If the target's pins plus the pins that move are more
   than 10, tick the 10 to keep. The newest 10 are ticked. The others are
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
- **Writes to the two fleets pause**, as for a merge (section 7). A first
  move since the upgrade takes up to about a minute per 5,000 memories, and
  later moves take milliseconds. A write that waits more than 3 seconds
  gets `503 fleet_busy`. If the move cannot get its locks within 5 seconds,
  or deadlocks, it answers `409 move_refused` with no change. Run it again.
  A write by a moving agent that was already on its way to the old fleet
  when the move committed is answered `503 fleet_busy` too ("home fleet
  changed, retry"), and the retry lands in the new fleet.
- **Do not move agents during a rollout.** An older pod does not do that
  home-fleet check, so such a write can land in the old fleet.
- **Pins changed since the preview** answers `409 move_refused` ("preview
  again"), as for a merge.
- **A failed audit write.** The move commits first and is audited after. If
  the audit write fails, the call answers 500 although the move happened.
  Check the list of merges and moves before you run it again.
- **Moving every agent** is allowed. The source fleet is not marked as
  merged and stays usable, empty of those agents.
- **Audit.** `fleet.move_agents` (and `fleet.create` for a new target), one
  `entry.unpin` with reason `fleet_move` for each pin left out, and
  `fleet.move_undo` for an undo, with one `entry.pin` (reason
  `fleet_move_undo`) for each pin it puts back (SPEC §12.5).

**Undoing a move.** Press **Undo** in the panel's **Merges and moves** list,
or call the endpoint above. The memories and agents the move recorded go
back to the source when they are still in the target. Memories written
since stay where they are, and an agent moved elsewhere in the meantime
stays there. Pins follow their memories back. The pins that were left out
are restored where they fit under the limit of 10 per fleet and 3 per
pinning agent. The pins on returning memories count too, so a source that
refilled its briefing after the move may not take them all back. The ones
that do not fit are unpinned, audited as `fleet_move_undo`, and listed in
the answer. You can undo only the newest merge or move that involves the
target or the source, and only while neither fleet has been merged away.
Otherwise the answer is `409 move_refused` and says which.

**Rolling back the release.** `make rollback` past migration `0023` deletes
the move records and the `fleet.move_agents` and `fleet.move_undo` audit
rows. The memories and agents stay where the move put them. Merge records
are kept. After that a move can no longer be undone, and an older merge's
undo no longer sees the moves that followed it, so undo any moves you want
to reverse first.

## 12. Retiring an agent with a successor

When an agent is replaced (a new model, a new machine), its memories should
not be stranded. Retiring it hands every memory it wrote to a successor and
revokes the old agent in the same step. The successor reads, supersedes and
withdraws them, sees them in its agent portal, and cannot rate them (they
count as its own). Nothing is rewritten: `author` stays the old agent's
name. It needs the admin key. (ADR 0113.)

In the panel, open **Agents** and press **Retire…** on an active or revoked
agent's row. Pick the successor (it must be active), read the preview (the
private notes, fleet memories and pins that pass over), type the agent's
name and confirm. The **Retired agents** list on the same page has an
**Undo** per row. Or call the API:

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

- **What the successor gets.** Exactly what authorship gave: it reads the
  old agent's `self` notes and fleet memories (in whatever fleet they sit),
  and withdraws them. It supersedes a fleet memory only by writing into
  that same fleet, so a successor in another fleet can read and withdraw
  such a memory but not supersede it. The preview counts these. Its trust
  level and home fleet do not change.
- **Pins** stay where they are, and none is dropped. In the successor's
  fleet they count toward its limit of 3 pins.
- **Chains** work: retire A to B, later B to C, and C owns both. A cycle is
  refused (`409 succession_refused`), and so is retiring an agent twice.
  The successor must be active (`409 invalid_status`). The agent to retire
  may be active or already revoked, but not pending.
- **Undo** ends the ownership at the next request. It does not bring the
  old agent back: it stays revoked, and activating it needs a new key.
  While a retirement stands, activating the old agent (through the API or
  `hivemind-keys issue-agent`) is refused with `409 agent_retired`.
- **Audit.** A successor's withdrawal of an inherited memory is audited as
  `entry.withdraw` with `via: "succession"`. A retirement writes
  `agent.retire` (target the old agent, detail `{successor, was}`) and, for
  an active agent, the usual `agent.revoke` with `via: "retire"`. An undo
  is `agent.retire_undo` (SPEC §12.5).
- **Cleanups go by author.** A cleanup of the old agent (section 10) still
  withdraws what it wrote, even after the hand-over. A cleanup of the
  successor covers only what the successor wrote itself.
- **Do not retire during a rollout.** An older pod ignores the new table.
  There the successor reads less until the pod is replaced, and the pod
  would not refuse to activate the retired agent.

**Rolling back the release.** Deploy the previous release first, then run
`make rollback`. This release reads `successions` in every key check, so
rolling back the migration under it would fail every agent request. The
rollback past migration `0024` drops the `successions` table and the rows
of the two audit actions. Retired agents stay revoked, and successors lose
the memories they inherited.

## 13. A secret or personal data got into a memory

An agent wrote a credential, a token or personal data into a memory.
Withdrawing it only changes its state. The text, the embedding and the
keyword index stay, the admin panel still shows them, and an agent that
asks for inactive memories (`include_inactive` on REST, `hive_search` and
`hive_list` over MCP) can still find and read a withdrawn or superseded
memory. **Redacting** wipes them for good, so do it promptly. It needs the
admin key and **cannot be undone**: nothing brings the text back, not an
undo, not a rollback and not a restore. (ADR 0125.)

1. **Withdraw the memory.** Do this for an active memory; one that is
   already superseded or withdrawn skips this step. Open it in the admin
   panel's memory browser and press **Withdraw…**, or call
   `POST /v1/admin/entries/{id}/withdraw`. It leaves every agent's default
   search at once, but stays readable with `include_inactive` until step 2.
   Write the reason without the secret: the `entry.withdraw` audit row
   keeps it, and the audit log is never edited. Also redact, and look at,
   any other memory that repeats the secret. Search the memory browser for
   the secret's first characters.
2. **Redact it.** In the drawer of the withdrawn (or superseded) memory,
   press **Redact…** (it is shown for any inactive memory that is not yet
   redacted). Give a reason (again, not the secret) and type the memory's
   id back. Or call the API:

   ```sh
   curl -sS -X POST "https://hivemind.example/v1/admin/entries/$ID/redact" \
     -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
     -d "{\"reason\": \"an API token was written into the body\", \"confirm\": \"$ID\"}"
   ```

   One transaction replaces the summary with `[redacted]`, empties the
   body, payload, tags, sources, extracted entities and the memory's own
   see-also links, sets the embedding and the keyword index to `NULL`, and
   records the time (`redacted_at`). Kind, scope, author, fleet, dates,
   supersession links, feedback and the audit trail stay. A 409 means the
   memory is not withdrawn yet or is already redacted. A 422 means the
   typed id did not match. A memory that another agent superseded (an
   older version of the secret) can be redacted without a withdrawal. Its
   supersession link stays, and `?history` shows the stub.
3. **Rotate the leaked secret at its source.** Redacting cleans Hivemind,
   not the world. Every agent that could see the memory could read the
   secret, and may have copied it into its context, its logs or other
   memories. Treat the secret as compromised and issue a new one.
4. **Review the feedback notes.** Other agents' feedback and flag notes on
   the memory are **not edited**, and they may quote the text. The answer
   to the redaction tells you how many exist: `feedback_notes`, and
   `flag_notes` for the stale and wrong ones among them. It also gives
   `withdraw_reasons`, the audit rows for the memory that hold a
   withdrawal reason: `entry.withdraw` rows with a `reason`, and
   `entry.restore` rows with a `previous_reason` (the reason a restore
   copied from the memory). The panel shows the same in its confirmation,
   and the `entry.redact` audit row records `feedback_notes` and
   `withdraw_reasons`. Audit rows are never edited, so a withdrawal reason
   that quotes the secret stays in the log. List the notes in the panel's
   **Feedback** page filtered by the memory, or:

   ```sh
   curl -sS -H "X-API-Key: $ADMIN_KEY" \
     "https://hivemind.example/v1/admin/feedback?entry_id=$ID&limit=200"
   ```

   Where a note quotes the secret, deal with it at the database, as the
   last step of an incident, with `UPDATE feedbacks SET note = NULL WHERE ...`
   (and take a backup first). Hivemind has no route that edits a rating. If
   `withdraw_reasons` is not zero, read those rows with
   `GET /v1/admin/audit-log?target=$ID` (the `entry.withdraw` and
   `entry.restore` rows), and decide with the owner of the audit log what to
   do about a reason that quotes the secret.
5. **Check other copies.** `GET /v1/admin/audit-log?action=entry.redact`
   shows who redacted what and why (never the text). Look for the same
   secret in other memories (a supersession copy, a `see_also` summary) and
   in the withdrawal reasons of earlier `entry.withdraw` rows.
6. **Backups still hold the text.** Logical dumps, physical backups, WAL
   archives and any replica contain the memory as it was before the
   redaction, until they age out or are rebuilt. Redaction does not touch
   them. If that is not acceptable, expire the backups from before the
   redaction (and restore nothing from them), and treat the secret as
   compromised until they are gone. The update also leaves the old row
   version in the table until vacuum. The old text can survive in the
   indexes too (the keyword index `entries_search_tsv_idx`,
   `entries_tags_gin_idx`, `entries_entity_names_gin_idx` and the vector
   index `entries_embedding_hnsw_idx`) and in the planner's statistics
   samples (`pg_statistic`). After a redaction, run:

   ```sql
   VACUUM (ANALYZE) entries;
   REINDEX INDEX CONCURRENTLY entries_search_tsv_idx;
   REINDEX INDEX CONCURRENTLY entries_tags_gin_idx;
   REINDEX INDEX CONCURRENTLY entries_entity_names_gin_idx;
   REINDEX INDEX CONCURRENTLY entries_embedding_hnsw_idx;
   ```

   `VACUUM (ANALYZE)` removes the dead rows and refreshes the statistics.
   The concurrent reindex rebuilds each index without blocking writes (the
   HNSW rebuild is slow on a large pool). Or run `VACUUM FULL entries` in a
   maintenance window: it rewrites the table and its indexes under a lock
   that blocks reads and writes.

**Restoring from a backup** taken before a redaction brings the text back.
Redact again after a restore (the redaction is in the audit log: search
`entry.redact`).

**Rolling back the release.** Migration `0032` adds the `redacted_at`
column and the `entry.redact` audit action. Its rollback drops the column
and deletes the `entry.redact` audit rows (`hivemind-migrate --rollback`
refuses without `--discard-audit-rows`, ADR 0119). Redacted memories stay
withdrawn with their stub, and the text does not come back. If `0032` is
applied again later, the stubs have `redacted_at` NULL. The admin restore
still recognises them (summary and reason `[redacted]`, no body, no
embedding) and refuses with `entry_redacted`, but the panel no longer shows
them as redacted.

## 14. Re-extracting entity names

When the extractor prompt or model changes (ADR 0130 did), older entries
keep the entity names they were written with, so an entity filter finds the
old spelling or the new one but not both. Entries written while extraction
was off or failing have no entities at all. `hivemind-reextract` runs the
configured extractor over stored entries and rewrites only their extracted
entities. It reads the same settings as the API (`HIVEMIND_DATABASE_URL`,
the `HIVEMIND_EXTRACTOR_*` variables and `HIVEMIND_EMBEDDING_PREFIX_TOKENS`)
and refuses to start when extraction is off. (ADR 0131.)

It is a **dry run unless you pass `--apply`**. Each entry in scope costs one
extractor call, in the dry run too, so start small. To bring entries in
line with the ADR 0130 prompt, take the time you deployed 2.21.0 as the
cutoff:

```sh
hivemind-reextract --created-before <when you deployed 2.21.0> --limit 50
```

Each change prints as `would change <id>: ["Postgres"] -> ["PostgreSQL"]`
(`changed` with `--apply`). Each failure prints as `failed <id>`, and each
entry that would lose all its entities prints as
`kept <id>: [...] -> [] needs --allow-clear`. Each batch ends with
`resume with --after <id>`. The last line sums up the run.

| Option | Meaning |
|---|---|
| `--apply` | write the changes |
| `--created-before T`, `--created-after T` | only entries created in that range (ISO 8601; a time without a zone is UTC) |
| `--fleet ID` | only entries in that fleet |
| `--include-inactive` | also superseded and withdrawn entries (default: active only) |
| `--after ID` | resume after that entry |
| `--limit N` | stop after N entries |
| `--allow-clear` | also write an empty result over existing entities |
| `--batch-size N` | entries per batch (default 50) |
| `--concurrency N` | extractor calls in flight at once (default 4) |

What it does and does not do:

- It writes only `entities`, `entity_names` and `entities_model`, and only
  for entries whose set of names and kinds changed (a new order or letter
  case alone is not a change). Summary, body, tags, embedding and keyword
  index stay as they are. Each batch's writes are one short transaction
  with a 5 s lock timeout, so live traffic is not held up. A batch that
  cannot get its locks stops the run with exit code 2 and a resume point.
- It keeps back an empty result over existing entities by default. A model
  that answers `[]` to everything is not a failure, so read the `kept`
  lines in the dry run before you pass `--allow-clear`.
- An entry whose summary no longer passes today's checks is reported as
  failed and left alone.
- It works behind a transaction-mode PgBouncer (no prepared-statement
  cache).
- A failed extraction keeps the entry's current entities. Run it again over
  the same range later. Entries already done come back unchanged.
- If every extraction in a batch fails (the extractor is down, or the model
  name is wrong), the run stops with exit code 2 before writing that batch.
  Fix the extractor and resume with the last `--after` it printed.
- It never reads or writes redacted entries (ADR 0125).
- It shares the extractor with live writes. Keep `--concurrency` low enough
  that writes still get their extraction in time. Their extraction gives up
  after `HIVEMIND_EXTRACTOR_DEADLINE`, and the write lands without
  entities.
- It writes no audit row. Keep the Job log if you want a record.

**On Kubernetes**, copy
[`deploy/kubernetes/maintenance/reextract-job.yaml`](../deploy/kubernetes/maintenance/reextract-job.yaml),
set `image` to the tag your Deployments run, and set `args`:

```sh
kubectl -n hivemind apply -f reextract-job.yaml        # dry run
kubectl -n hivemind logs -f job/hivemind-reextract
kubectl -n hivemind delete job hivemind-reextract
# add "--apply" to args, then
kubectl -n hivemind apply -f reextract-job.yaml
```

The Job uses the API's ConfigMap and Secret and sets
`HIVEMIND_RUNNER=reextract`. The opt-in egress policy
(`deploy/kubernetes/optional/networkpolicy/egress/`) gives it the API's
destinations. Like every runner it migrates first, which is why it must use
the image the Deployments run. If the Job stops, add `"--after", "<id>"`
from its log to `args` and apply it again.

## 15. An agent can't find a memory

The admin search sees every memory, so it finds what the agent cannot.
Search as the agent instead. (ADR 0136.)

1. Open **Memories** in the admin panel, type the words the agent searched
   for, and put the agent's name in **As agent**. (Over the API, send
   `POST /v1/admin/search` with `{"query": "...", "as_agent": "<name>"}`
   and the admin key.) A 409 `agent_not_active` means the agent is pending
   or revoked and cannot search at all. A 404 `unknown_agent` means the
   name is wrong.
2. The main table is exactly what the agent's own search returns, in the
   same order. If the memory is there, the agent's query or filters were
   the problem.
3. If it is not there, the **Hidden from <agent>** card lists it with the
   reason. The reason is one of these: a private note of another author, a
   fleet that is not the agent's home fleet (only privileged agents read
   across fleets), a withdrawn or superseded memory, an untrusted agent
   (it reads nothing), or ranked below the cut-off.
4. Fix the cause. Change the agent's trust level or home fleet (ADR 0011),
   have the author write the memory at `fleet` or `org` scope, or restore
   a withdrawn memory (section 4).

An as-agent search is not counted in the search counters and writes no
audit row.

## 16. Read the ranking of a production query

To see why a memory ranked where it did, tick **Explain ranking** in the
admin **Memories** view. Or send `"explain": true` to
`POST /v1/admin/search` with the admin key, and add `as_agent` to explain
that agent's own search. (ADR 0137.) Each hit gets a list of lines under its
row:

- **Keyword** and **Vector** ranks. A memory found by both streams
  outranks one found by a single stream. "Vector: unavailable" means the
  embedder failed and the answer is keyword-only (ADR 0048).
- **Recency**: the factor, the memory's age, and whether the 0.8 floor
  applied (a memory older than about ten days always carries the floor). A
  named hint (`prefer_recent`, `ignore_age`) shows here.
- **Feedback** and **Social cap**: the counts behind the quality factor,
  and whether the cap held it down.
- **Importance** and the final **Score**.

Use it on real queries before you decide the ranking needs a change, and
compare with `docs/retrieval-experiments.md`. It changes nothing: the order
is the same with it on or off, and agents never see it.

## 17. When someone leaves

A person's agents should not outlive their access. **Offboarding** retires
or revokes every agent registered under their owner alias in one step. It
needs the admin key. Owner aliases are labels that agents report about
themselves, so read the list before you confirm. (ADR 0135.)

In the panel, open the owner (Stats, **Users** tab; the **Owner** column of
**Agents**; or "Owned by" on an agent's page) and press **Offboard this
owner…**. Choose:

- **Retire to a successor** (the default). Pick an active agent of another
  owner. That agent reads, supersedes and withdraws the memories, so they
  keep an owner (section 12). Every agent of the owner is retired in the
  same step.
- **Revoke all.** The keys stop and the memories keep no owner.

The preview lists each agent and what happens to it: retire, revoke, reject
a pending registration, or skip one that is already revoked or retired.
Type the alias to apply. Or call the API:

```sh
# Preview: nothing changes.
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"mode": "retire", "successor": "team-bot", "dry_run": true}' \
  "https://hivemind.example/v1/admin/owners/jane.doe/offboard"
# Apply (all agents or none).
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"mode": "retire", "successor": "team-bot", "dry_run": false,
       "expected_agents": [{"name": "jane-bot", "action": "retire"}]}' \
  "https://hivemind.example/v1/admin/owners/jane.doe/offboard"
```

- **The alias matches exactly**, capitals included: `Jane.Doe` and
  `jane.doe` are two owners. `404 unknown_owner` means no agent has that
  alias. In the URL, percent-encode the alias (`%20` for a space); a `/`
  may stay as it is.
- **Applying sends the preview's agents** (`expected_agents`, the
  `{name, action}` list). If an agent changed since, nothing is applied
  (`409 offboard_changed`). Preview again.
- **Refusals** change nothing. The successor must be active
  (`409 invalid_status`) and must not belong to the same owner
  (`409 succession_refused`). A cycle for any one agent refuses the whole
  request. `retire` needs a successor, and `revoke` takes none (`422`).
- **Undo.** Each retirement is an ordinary succession. Undo one from the
  **Retired agents** list on the Agents page or with
  `POST /v1/admin/successions/{retired}/undo`. A revoke cannot be undone: an
  agent revoked this way needs a new key to come back.
- **Audit.** The rows are the ones a retire or revoke writes
  (`agent.retire`, `agent.revoke`), one set per agent, each with
  `offboarded_owner` in its detail. Filter the audit log by action and look
  for it.
- **An alias with a slash** cannot be addressed by this route. Retire or
  revoke that owner's agents one by one (section 12).

## 18. Steering a fleet with a phase

A fleet can be told, for a while, to lean towards exploring or towards
settling. Set an **explore** phase when the fleet starts open-ended work
and you want rival approaches, dead ends and open questions written down.
Set a **consolidate** phase when it has explored enough and should test the
rivals, distill clusters and fix flagged entries. It needs the admin key.
(ADR 0143.)

In the panel, open the fleet's page and use the **Phase** card. Pick
Explore or Consolidate, a number of days (1 to 30, 14 by default) and press
**Set**. **Clear** ends it early. The Fleets list shows each fleet's phase.
Or call the API:

```sh
curl -s -X PUT -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"phase": "explore", "until": "2026-12-01T00:00:00Z"}' \
  "https://hivemind.example/v1/admin/fleets/<fleet-id>/phase"
curl -s -X DELETE -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/fleets/<fleet-id>/phase"
```

- **What agents see.** While a phase is in force, `hive_whoami` and
  `hive_pinned` carry a one-line `fleet_phase` hint for the agent's own
  fleet. It is a nudge. Nothing enforces it, and **ranking and search do
  not change**.
- **Limits.** `until` must be in the future and at most 30 days ahead, or
  you get `422`. Setting a phase replaces the current one. A merged fleet
  answers `409 fleet_merged`, and an unknown fleet answers `404`.
- **It ends by itself.** A phase past its `until` counts as none the moment
  it passes. No job runs and nothing needs clearing. Renew it by setting it
  again.
- **Metrics.** `GET /v1/metrics` lists the phase per fleet under
  `fleets.phase_by_fleet`, and `GET /metrics` serves
  `hivemind_fleet_phase{fleet,phase}` as 1 for each fleet in a phase, so a
  dashboard can line it up with the `hivemind_health_*` gauges.
- **Audit.** One `fleet.phase_set` or `fleet.phase_clear` row per call, with
  the fleet id as target and `{phase, until}` as detail (SPEC §12.5).

**Rolling back the release.** `make rollback` past migration `0033` drops
the phase columns, so every phase in force ends. It also deletes the
`fleet.phase_set` and `fleet.phase_clear` audit rows (it refuses without
`--discard-audit-rows`, section 4).

## 19. Marking an agent as a scout

A colony keeps a few bees for exploring while most forage what is known.
You can do the same: mark an agent as a **scout**, and it reads a one-line
hint in `hive_whoami`. The hint asks it to try the less-tested of two
rivals, search with `explore: true` when results look settled, re-check old
unconfirmed entries and write what it tried, dead ends included. It needs
the admin key. (ADR 0144.)

In the panel, open the agent's page and use the **Scout** card: **Make
scout** or **Stop scouting**, each with a confirmation. The Agents list
shows a **Scout** badge. Or call the API:

```sh
curl -s -X PUT -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"role": "scout"}' "https://hivemind.example/v1/admin/agents/<name>/role"
curl -s -X DELETE -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/agents/<name>/role"
```

- **A hint, not a permission.** What the agent may read or write does not
  change, and neither do ranking and search. Nothing checks that it
  explores.
- **Who can be marked.** A pending or active agent. A revoked agent gets
  `409 invalid_status`. Marking a scout again, or clearing an agent with no
  role, changes nothing and writes no audit row. An unknown agent is `404`.
- **What it keeps and loses.** The role stays with the agent through a
  fleet merge or move. Retiring an agent revokes it and leaves its role on
  the record. The successor is not a scout unless you mark it.
- **Is it working?** `GET /v1/metrics` counts the agents that are not
  revoked by role (`agents.by_role`). It also compares the last 30 days of
  scouts with everyone else under `health.role_activity_30d`: writes,
  writes tagged `alternative` or `dead-end`, supersessions written and
  verdicts given. The Health page shows the same table. Scouts should show
  more alternatives, dead ends and verdicts. The gauges are
  `hivemind_agents_by_role{role}` and
  `hivemind_health_role_activity_30d{role,motion}`.
- **Audit.** One `agent.role_set` or `agent.role_clear` row per change, with
  the agent's name as target and `{role}` as detail (SPEC §12.5).

**Rolling back the release.** `make rollback` past migration `0034` drops
the role column, so every agent loses its role. It also deletes the
`agent.role_set` and `agent.role_clear` audit rows (it refuses without
`--discard-audit-rows`, section 4).

## 20. A fleet memory that should have been private

An agent sometimes writes to its fleet what was meant for itself: a
brainstorm, a plan nobody tried, its user's preference. If the memory is
not wrong, only misplaced, **move it to self** instead of withdrawing it.
The author keeps finding it, every other agent stops seeing it, and the
text, feedback and supersession links stay. It needs the admin key, or the
agent's own key in the agent portal. (ADR 0145.)

In the admin panel, open the memory and use **Move to self…** in the
drawer, with a reason. The person behind the agent can do the same from the
portal's **Its memories** page. Or call the API:

```sh
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "a brainstorm, not a finding"}' \
  "https://hivemind.example/v1/admin/entries/<entry-id>/move-to-self"
```

- **What moves.** The memory, and the earlier `fleet` versions in its line
  that were written by its author or by an agent the author inherited from
  (ADR 0149). Every pin on a moved version is removed. The answer lists the
  earlier versions and the removed pins.
- **When it is refused.** You get `409 conflict` and nothing changes when
  the memory is not an active fleet memory, or when it or one of those
  versions replaced a fleet memory by another agent (moving it would take
  that memory away from the fleet too). Withdraw it instead.
- **No undo.** To share it again, the author supersedes it with a fleet
  entry. Pins are not put back.
- **Audit.** One `entry.move_to_self` row with the author, fleet, reason and
  earlier versions (`via: "portal"` from the portal), and one `entry.unpin`
  row per fleet that lost pins, with `reason: "moved_to_self"` (SPEC §12.5).

**Rolling back the release.** `make rollback` past migration `0035` deletes
the `entry.move_to_self` audit rows (it refuses without
`--discard-audit-rows`, section 4). The moved memories stay `self`.

## 21. Renaming an agent

The name is the agent's identity: its memories, ratings, pins and key all
carry it. A rename rewrites every one of those copies in a single
transaction, and the agent's key keeps working, so the person behind the
agent changes nothing. The agent sees the new name in `hive_whoami` on its
next call. It needs the admin key. (ADR 0159.)

In the panel, press **Edit…** on the agent's row or page and change the
Name. Or call the API, or run the break-glass command on the database host:

```sh
curl -s -X POST -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"name": "robert"}' "https://hivemind.example/v1/admin/agents/bob/rename"

hivemind-keys rename-agent bob robert
```

- **Any status**, retired agents included. The new name is checked as a
  registration's is: `422` for a bad or reserved name, `409 name_conflict`
  when another agent holds it in any letter case or left it in a rename,
  and `404` for an unknown agent. A change of case (`bob` to `Bob`) is a
  rename. The name the agent already has answers `200` and writes nothing.
- **The old name stays reserved for good.** Nobody can register it or be
  renamed to it, because the audit log and the history keep the old name.
  The agent itself can take it back with another rename.
- **What stays.** Audit rows keep the old name (the history of the agent
  follows it), and so do memory texts that mention it.
- **Big agents.** The rename rewrites each memory the agent wrote, so an
  agent with very many memories takes longer. It runs in one transaction.
- **Audit.** One `agent.rename` row, target the new name, detail
  `{from, to}` (a `cli` row from the command; SPEC §12.5).
- **Do not rename during a rolling release.** A pod from before this
  release does not know the reservation.

**Rolling back the release.** `make rollback` past migration `0036` drops
the reserved former names, so those names can be registered again. It also
deletes the `agent.rename` audit rows (it refuses without
`--discard-audit-rows`, section 4). The agents keep their new names.
