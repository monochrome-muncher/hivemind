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
| `HIVEMIND_EXTRACTOR_STRUCTURED_OUTPUT` | ask the extractor endpoint for schema-guided JSON (ADR 0133) — default `false`; needs an endpoint with JSON-schema guided decoding (vLLM, llama.cpp server, Ollama, OpenAI). A server that rejects it (HTTP 400/422) is served plain output from then on, with one warning in the log |
| `HIVEMIND_HOST` / `HIVEMIND_PORT` | the bind host/port of the HTTP runners (api, mcp-http, admin, portal; ADR 0010) |
| `HIVEMIND_EXPLORE_LAMBDA` | relevance weight of an opt-in `explore: true` search's MMR re-ordering, `(0, 1]`, default 0.7; 1 = relevance order (ADR 0078). Searches without `explore` ignore it |
| `HIVEMIND_RECENCY_FLOOR` | lower bound on the SPEC §6.4 recency factor, `(0, 1]`, default 0.8 (ADR 0022) — set it to an empty value or `none` (case-insensitive) to revert to the pre-ADR-0022 unbounded behaviour (ADR 0023), not a negligible number |
| `HIVEMIND_RECENCY_HINT` | on by default (ADR 0101): a search may carry a `recency` hint (`prefer_recent`, `ignore_age`; ADR 0073) and it changes that search's ranking; a search with no hint ranks as before. Set `false` to accept and ignore the hint |
| `HIVEMIND_CONFIRMATION_HALVES_FLAGS` | on by default (ADR 0106): once a noted helpful report is newer than every stale or wrong report on an entry, those flags count at half weight in its quality multiplier, so a rebutted flag weighs less in ranking. Counts, the flag backlog and the shown correction are unchanged. Set `false` to count every flag in full |
| `HIVEMIND_VECTOR_MIN_SIMILARITY` | the search similarity threshold (ADRs 0062, 0100), `[0, 1)`, 0.45 when unset (measured for Qwen3-Embedding; empty or `none` turns it off). The vector stream keeps only entries above it — a search with nothing relevant can come back empty; the keyword stream is not cut. With another embedder or dimension, re-measure with `make measure-threshold` ([retrieval experiments](retrieval-experiments.md#similarity-threshold)) |
| `HIVEMIND_CLOSE_MATCH_SIMILARITY` | the cosine similarity from which a search hit is a close match (ADR 0148), `[0, 1)`, 0.65 when unset (calibrated for Qwen3-Embedding-8B; another embedder needs its own value, see the ADR's calibration). When no hit reaches it, `hive_search` adds `closest: "loose"` and a note and REST the `X-Hivemind-Closest: loose` header; `0` turns that off. Re-check it after changing the embedder or `HIVEMIND_QUERY_INSTRUCTION` |
| `HIVEMIND_QUERY_INSTRUCTION` | a one-line task instruction for an instruction-aware embedder such as Qwen3-Embedding (ADR 0097), at most 500 characters; on by default, set it empty to turn it off. Only the search query is prefixed (entries and the keyword stream are not). The format is Qwen3's: with a different embedder run `make measure-query-instruction` and turn it off if it does not help. It moves query vectors: re-check `HIVEMIND_VECTOR_MIN_SIMILARITY` after changing it |
| `HIVEMIND_VERSION_GROUP_MIN_SIMILARITY` | read-time grouping of unlinked versions of one fact (ADR 0074), `(0, 1)`, off by default (unset, empty or `none`). When set, near-identical active candidates are shown next to each other, newest first, each naming the other in `similar`; nothing is hidden or re-scored. Measure with `make measure-versions` ([retrieval experiments](retrieval-experiments.md#version-grouping)) before setting it |
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
> migration (ADR 0020):
>
> ```sh
> docker compose exec postgres psql -U hivemind -c "SELECT migration_id FROM _yoyo_migration ORDER BY applied_at_utc DESC, migration_id DESC LIMIT 1"
> ```
>
> If it lags, run `make migrate` (safe to re-run; an up-to-date pool is a no-op).

## 3. Monitoring / health

| Signal | How |
|---|---|
| Liveness / readiness | Unauthenticated probe endpoints (ADR 0019), all public: `GET /v1/liveness` (shallow — 200 whenever the process answers), `GET /v1/health` (static 200, public by design — SPEC §5.1), `GET /mcp/liveness` (shallow), `GET /mcp/health` (shallow, the readiness probe — ADR 0037), `GET /mcp/health/database` (deep — 200 only when the Postgres pool answers within 5 s, else 503; the pod log names the cause: `database health check failing: …`) |
| Usage / counters | `GET /v1/metrics` (admin-gated) — entries / fleets / agents / searches / feedback counters and the `health` section (ROADMAP §3.3, ADRs 0056, 0068, 0075) |
| Prometheus | `GET /metrics` on each `hivemind-api` pod, no key (ADR 0050): usage gauges (`max` across replicas), `hivemind_http_requests_total` / `hivemind_http_request_duration_seconds`, `hivemind_search_degraded_total` (alert on its rate: the embedder is down), `hivemind_searches_total` / `_empty_total` / `_explore_total` per fleet (ADRs 0056, 0078), `hivemind_feedback_flags` / `_confirmed` per agent (ADR 0068), `hivemind_fleet_phase{fleet,phase}` (1 for each fleet with a phase in force, ADR 0143) and the `hivemind_health_*` fleet health gauges (including the 7-day `hivemind_health_breathing_7d` and `hivemind_health_fleet_breathing_7d` counts, ADR 0142) — admin-facing observations, never an agent signal; `hivemind_health_success` is 0 while a refresh fails and the gauges are stale (ADR 0075). Each `hivemind-mcp` pod serves its own request, tool-call and degraded-search series when `HIVEMIND_MCP_METRICS_ENABLED=true` (as the Kubernetes manifest sets, ADR 0104); it has no usage gauges, so `sum` the process counters across the api and mcp jobs. Operator users: `deploy/kubernetes/optional/monitoring/` (DEPLOY.md §2 step 7) |
| Grafana | `deploy/kubernetes/optional/monitoring/hivemind-dashboard.json` (DEPLOY.md §2 step 8): search health, traffic, MCP tools, fleet health and the vital signs over time, from the series above. `alerts.yaml` beside it is the opt-in `PrometheusRule` `hivemind-vital-signs` (ADR 0142): `info` alerts for a quiet fleet, a rising echo rate, lingering rivals and a growing floating gap, each saying what the admin could do; the admin Health page's "Vital signs" card shows the same bands |
| Schema drift | the latest id in `_yoyo_migration` (ADR 0020) — the applied migration |
| Postgres health | the `postgres` service healthcheck (`pg_isready`); `docker compose ps` |
| Admin panel Overview | The top of the Overview is a **Needs you** row (ADR 0121): keys locked from the portal, pending agents, idle agents and flagged memories older than 14 days, each shown only above zero and linking to its filtered list (Agents `#/agents?activity=locked`, `?status=pending`, `?activity=idle`; Health `#/health?flags=old`). With none, it reads "Nothing needs you right now." |
| Embedder health | writes failing with `EmbeddingError` after the retry budget (ADR 0014) — check the embedding endpoint (`HIVEMIND_EMBEDDING_ENDPOINT`) and the provider process |
| Extractor health | entries landing with **empty `entities`** while extraction is expected (ADR 0016) — check `HIVEMIND_EXTRACTOR_ENDPOINT` / model reachability; note this is *by design* silent (best-effort: the write never fails over extraction), so watch for the symptom, not an error. All-or-nothing validation: if the model wraps its JSON in a code fence (or any schema violation — >10 entities, bad kind, name >128 chars) the whole extraction fails and the entry lands without facets — monitor facet coverage in the dogfood |
| MCP runner | `docker compose ps mcp-http` (ADR 0010); the streamable-HTTP endpoint `:8088` |

Watch for the signals in the table above. A transient embedder blip is
retried automatically and needs no action; a dead one fails writes once
the `HIVEMIND_EMBEDDING_RETRIES` budget is exhausted. The extractor is
deliberately the opposite: its failures are best-effort and never block
a write — the observable symptom is an entry with empty `entities`,
not a failed write.

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
shown). For an owner who has not registered, **New agent** on the admin
panel's Agents page (or `POST /v1/admin/agents` with `name`, `owner_alias`,
`trust_level` and `home_fleet_id`) registers and activates the agent in one
step and shows the key once; an existing name is refused (ADR 0120). Raw keys are random 32-byte secrets shown once; only the SHA-256
hash is stored (a leaked database never leaks usable keys, SPEC §8.1).

### Rotation / revocation

| Action | Command / endpoint | Effect |
|---|---|---|
| Rotate the org key (close registration) | `hivemind-keys rotate-org` / `POST /v1/admin/org-key/rotate` | all prior org keys stop working on the next request; active agents are unaffected |
| Replace an agent's key | `hivemind-keys rotate-agent --name alice` / `POST /v1/admin/agents/alice/key` / the admin panel's "Replace key" | the old key stops working on the agent's next request and the new key is shown **once**; status, level, fleet and memories stay as they are; only for an `active` agent (a revoked one is re-activated instead; ADR 0108) |
| A locked key (the person behind the agent pressed **This key leaked** in the portal, ADR 0117) | the Agents page shows a "key locked" badge; `GET /v1/admin/agents` has `key_locked`; `hivemind-keys list` marks the key | the key fails every request with `key_locked` while the agent stays `active`. Replace the key (row above) to let the agent back in; revoking works too. The lock is audited `agent_key.lock` (actor: the agent's name, `via: "portal"`) |
| Revoke an agent key | `hivemind-keys revoke --name alice` / `POST /v1/admin/agents/alice/revoke` | the agent's key is dead and its status is `revoked`; the **name stays reserved**; on a `pending` agent this rejects the registration (ADRs 0012, 0028) |
| Correct an agent's owner alias (a typo made it a second owner in Stats) | `PATCH /v1/admin/agents/{name}` with `{"owner_alias": "..."}` / the admin panel's "Edit owner" (Agents list or the agent page) | any status; the Stats "users" tab, the Agents list and the health owner figures follow at once, because the alias is read from the agent record. Audited as `agent.owner_set` with `{from, to}`; the same value changes nothing. Old `agent.register` audit rows keep the alias as it was typed. No CLI command: `hivemind-keys` has no trust-level or home-fleet command either (ADR 0122) |
| Demote an agent | `PATCH /v1/admin/agents/{name}` (trust level → 0) | the agent is *dormant* (key still valid, no access) — distinct from revocation |

> **Rotating the org key closes registration** (ADR 0031): every prior
> org key stops working immediately, so a leaked copy can no longer
> register agents. It does **not** cut off active agents — they
> authenticate with their own agent keys. To cut agents off, revoke
> them (or demote them to `untrusted`).

### Audit trail of key and admin actions (ADR 0027)

Every mutating `hivemind-keys` command (`issue-admin`, `revoke-admin`,
`rotate-org`, `rotate-agent`, `issue-agent`, `revoke`, `bootstrap-secret`;
not `list`) and every admin REST action writes a row to the audit log
(SPEC §12.5). Read it with the
admin key:

```sh
curl -s -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/audit-log?action=org_key.rotate&limit=20"
```

- **Name yourself on the CLI.** Pass `--actor` before the subcommand —
  `uv run hivemind-keys --actor alice.ops rotate-org`. It defaults to the
  OS user, which inside the image is `hivemind` and says nothing.
  The value is **unverified** (anyone with database access can type
  anything); CLI rows are marked `actor_kind = cli` for that reason.
- **Admin keys appear by fingerprint**, the 12 hex characters
  `hivemind-keys list` shows. REST rows read `admin:<fingerprint>`, so
  after an admin-key rotation you can tell the old key's actions from
  the new one's. No raw key is ever written to the log.
- **Pins are audited too** (ADR 0076, migration `0014`): `entry.pin` and
  `entry.unpin` rows, with `actor_kind = agent_key` and the agent's name,
  or `admin:<fingerprint>` for the admin key. Rolling back past `0014`
  deletes those rows.
- **Portal pins and unpins are audited too** (ADR 0116): a pin or unpin
  made from the agent portal writes the usual `entry.pin` or
  `entry.unpin` row with `actor_kind = agent_key`, the agent's name as
  actor and `via: "portal"` in `detail`. Pins made through the API or
  `hive_pin` carry no `via`.
- **Portal withdrawals are audited too** (ADR 0095): an agent's own
  memory withdrawn from the agent portal writes an `entry.withdraw` row
  with `actor_kind = agent_key`, the agent's name as actor and
  `via: "portal"` in `detail`. An agent withdrawing its own entry through
  the API or `hive_withdraw` writes the same row with `via: "author"`
  (ADR 0141).
- **Restores are audited** (ADR 0124, migration `0031`): the admin putting
  a withdrawn memory back writes `entry.restore` (target: the entry id,
  detail `{author, previous_reason, reason}`). Rolling back `0031` deletes
  those rows (it needs `--discard-audit-rows`); the restored memories stay
  active.
- **Moves to self are audited** (ADR 0145, migration `0035`): moving a
  fleet memory to `self` from the admin panel or the portal writes
  `entry.move_to_self` (target: the entry id, detail `{author, fleet_id,
  reason, earlier_versions}`, `via: "portal"` from the portal) and one
  `entry.unpin` per fleet whose pins it removed. Rolling back `0035`
  deletes those rows (it needs `--discard-audit-rows`); the moved
  memories stay `self`.
- **Key locks are audited** (ADR 0117, migration `0025`): an agent locking
  its own key from the portal writes `agent_key.lock` with
  `actor_kind = agent_key`, the agent's name, the key fingerprint and
  `via: "portal"` in `detail`. Rolling back past `0025` deletes those
  rows and makes a locked key usable again; replace the key first if it
  must stay dead.
- **Rollbacks are audited and guarded** (ADR 0119, migration `0027`).
  Several shipped rollbacks (`0009`, `0014`, `0017`, `0019`, `0020`,
  `0022`, `0023`, `0024`, `0025`, `0027`, `0029`, `0031`, `0032`, `0033`, `0034`, `0035`) delete the audit rows whose
  action needs a wider `CHECK`, `0006`'s sets revoked agents back to
  `active` (reviving their key rows), and `0025`'s drops the lock mark, so
  locked keys work again. `make rollback` (`hivemind-migrate --rollback N`)
  counts all three under the migration lock before any DDL and, when a
  count is not zero, exits non-zero naming each migration, its actions and
  the count, and changes nothing. Dump the log first
  (`pg_dump --table=audit_log`), then confirm on the make command line:
  `make rollback N=3 DISCARD_AUDIT_ROWS=1` for audit rows,
  `REVIVE_REVOKED_KEYS=1` for the keys of revoked agents (delete those
  keys instead if they must stay dead; see dead credentials above) and
  `UNLOCK_LOCKED_KEYS=1` for locked keys (replace them first if they must
  stay dead). Directly: `hivemind-migrate --rollback 3
  --discard-audit-rows`, with exactly one number and only those flags.
  **Stop the app tier first.** The counts do not block live writes, so a
  row written between the count and the DDL is deleted uncounted;
  `discarded_audit_rows` is the count taken before the rollback.
  While `0027` stays applied (a rollback of `0030` alone, say), each
  successful rollback writes one `schema.rollback` row (`actor_kind = cli`,
  the OS user, the most recent migration id as target, `{rolled_back,
  discarded_audit_rows}` as detail). A rollback that takes `0027` along
  leaves the WARNING on stderr as the only record; rolling back `0027`
  deletes any such rows (so it needs `--discard-audit-rows` once they
  exist).
- **Reading one agent's history** (ADR 0123, migration `0030`).
  `GET /v1/admin/audit-log?subject=<agent>` returns the rows about that
  agent, newest first: its own key as actor, admin actions on it,
  withdrawals of its memories, fleet moves and retirements that name it.
  `target=<exact>` filters on the row's target alone. The admin panel
  shows the same list on the agent's page ("History", with a CSV export)
  and as two filter fields on the Audit log page. The agent portal's
  "History" page shows the same for the agent (and the agents it
  inherits from) without naming the admin; nothing about it needs
  configuration. Migration `0030` builds four indexes `CONCURRENTLY`
  (`audit_log_target_idx`, `audit_log_withdraw_author_idx`,
  `audit_log_move_agents_idx`, `audit_log_successor_idx`); if a build is
  cancelled it leaves an `INVALID` index, which you drop and re-run
  (`DROP INDEX CONCURRENTLY <name>`, then `make migrate`). The
  `subject` query reads through a BitmapOr of these and the existing
  `audit_log_actor_idx`; it runs with a custom plan (`SET LOCAL
  plan_cache_mode`), because a generic plan walks `occurred_at` backwards
  and filters the whole log. Rolling `0030` back only drops the indexes
  and writes a `schema.rollback` row.
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

### Restoring a mistaken withdrawal (ADR 0124)

One memory withdrawn by mistake can be put back by the admin: open it in
the panel's **Memories** page and press **Restore** (it asks for a reason),
or `POST /v1/admin/entries/{id}/restore` with `{"reason": "..."}` and the
admin key. The memory is `active` again, findable by search, and its
withdrawal reason and time are cleared (the audit row `entry.restore` keeps
the old reason). **Pins are not restored**: pin it again if the fleet
should see it in its briefing.

The call answers 409 when it should not go ahead:

- `entry_not_withdrawn`: the memory is active or superseded already.
- `restore_via_cleanup`: an agent cleanup that still stands withdrew it.
  Undo that cleanup (section 10), which brings back everything it changed
  together. Once the cleanup is undone the memory is already active.
- `restore_chain_conflict`: an agent cleanup once put this memory back over
  a newer one, so restoring it could leave two current versions of one fact
  (the newer one may come back later). Write a new memory with the content
  you want instead, or undo the cleanup.

A fleet merge, a move or a retirement never withdraws memories, so their
undos have no say here.

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
embedding provider (ADR 0005: self-hosted) at deploy time.

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
2. **Back up** the data (see §2) — the old-dim pool is your source of
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
   eval harness (the golden set in `tests/eval/`) at the new dim before
   declaring the switch a success.

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

**After upgrading.** Memories written from the release with migration 0028
onwards merge and move fast. A memory written before it is slow on its first
merge or move only (the new version of its row lands on a page with room,
and the search index has to take it once). After that, and for its undo, it
is fast. You do not have to do anything.

`VACUUM FULL entries;` would make the old rows fast up front, but it is
optional and it is an outage. It rebuilds every index under an exclusive
lock, the vector index most of all: 80 seconds at 200,000 memories with
32-dimension vectors, many minutes at 1024 dimensions. Every read and write
of memories waits, and with the 30 second command timeout the API is
effectively down. If the command has to wait for its lock, it blocks every
query that arrives behind it too. Run it only if that first slow merge would
cost more than the outage. If you do, in psql as the database owner, in a
quiet window:

```sql
SET lock_timeout = '5s';             -- give up rather than queue everyone behind it
SET maintenance_work_mem = '2GB';    -- at least the size of entries_embedding_hnsw_idx
VACUUM FULL entries;
```

You need disk for a second copy of the table and all its indexes. The time
is dominated by rebuilding the vector index, about as long as migration
0004's build took, and it builds a new HNSW graph (ADR 0025), so search
results can differ slightly afterwards.

If you roll migration 0028 back and the rollback is cancelled, it can leave
an invalid `entries_fleet_idx`. Drop it (`DROP INDEX CONCURRENTLY IF EXISTS
entries_fleet_idx;`): while it exists, merges and moves cannot be fast.

**What to expect.**

- **Writes to the two fleets pause while the memories move.** The merge
  locks both fleet rows meanwhile. A memory that is being moved for the
  first time since the upgrade is rewritten and its vector re-inserted into
  the search index, which took about 12 ms per memory on a 200k-memory test
  pool: roughly a minute per 5,000 memories. Once a memory has moved, or if
  it was written after the upgrade, its row is rewritten in place and the
  search index is left alone, which takes milliseconds, and so does the
  undo of a memory's first move. The panel's preview shows the slower
  estimate for the fleets you picked, so read it as a ceiling. Merge large
  fleets at a quiet time,
  since fleet writes are refused (below) until it finishes. A merge and an
  undo run without the usual 30 second
  database command limit, so a large fleet is not cut off halfway (the
  work is one transaction, so a failure leaves nothing changed). A write
  that was already on its way to the source fleet is refused with
  `409 fleet_merged` (naming the target) and the agent retries; its next
  attempt lands in the target. A write that has to wait more than 3 seconds
  for the merge gets `503 fleet_busy` ("fleet is being merged or moved, retry"):
  on REST with a `Retry-After` header, in the MCP tools as a `fleet_busy`
  tool error without one. Retrying
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
  `fleet.unmerge` for an undo, with one `entry.pin` (reason
  `fleet_merge_undo`) for each pin it puts back (SPEC §12.5).

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

**When the agent cleans up for itself.** The person behind an agent can run the same cleanup from the agent portal (**Clean up**, ADR 0118), on that agent's own work and without the revoke. It shows in the **Cleanups** card marked "self-service", with the agent as the one who ran it. In the audit log it is `agent.cleanup` with actor kind `agent_key` and `via: "portal"` in the detail. An admin can undo it from the card as usual. A cleanup you ran cannot be undone from the portal; the person sees it in their list marked "an admin" and has to ask you. The newest cleanup of an agent, yours or theirs, is the only one that can be undone. A successor's cleanup also reaches the memories it inherited from the agents retired in its favour, which your own cleanup of the successor does not.

**Rolling back the release.** `make rollback` past migration `0022` deletes
the cleanup records and the `agent.cleanup` and `agent.cleanup_undo` audit
rows. Withdrawn memories stay withdrawn. The ratings are kept and count again. Memories still carry `admin cleanup <id>` as their withdrawal reason, naming a record that no longer exists, and a roll-forward starts with no cleanups to undo.

Rolling back migration `0026` only drops the mark that says an agent ran a cleanup itself. Afterwards its cleanups read as admin cleanups; they can still be undone from the admin panel, since their withdrawals carry the same `admin cleanup <id>` reason.


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
- **Writes to the two fleets pause**, as for a merge (section 7): up to about a
  minute per 5,000 memories on their first move since the upgrade,
  milliseconds after that, `503 fleet_busy` for a write that waits
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
  `fleet.move_undo` for an undo, with one `entry.pin` (reason
  `fleet_move_undo`) for each pin it puts back (SPEC §12.5).

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

## 13. A secret or personal data got into a memory (ADR 0125)

An agent wrote a credential, a token or personal data into a memory.
Withdrawing only changes the state. The text, the embedding and the keyword
index stay, the admin panel still shows them, and an agent that asks for
inactive memories (`include_inactive` on REST, `hive_search` and `hive_list`
over MCP) still finds and reads a withdrawn or superseded memory. **Redacting**
wipes them for good, so do it promptly. It needs the admin key and **cannot be undone**: nothing, not an undo,
not a rollback, not a restore, brings the text back.

1. **Withdraw the memory** (an active one; a memory that is already
   superseded or withdrawn skips this step). Open it in the admin panel's
   memory browser and press **Withdraw…**, or
   `POST /v1/admin/entries/{id}/withdraw`. It leaves every agent's default
   search at once, but stays readable with `include_inactive` until step 2.
   Write the reason without the
   secret: the `entry.withdraw` audit row keeps it and the audit log is never
   edited. (Also redact, and look at, any other memory that repeats it:
   search the memory browser for the secret's first characters.)
2. **Redact it.** In the drawer of the withdrawn (or superseded) memory press
   **Redact…** (shown for any inactive memory that is not yet redacted), give a reason
   (again, not the secret) and type the memory's id back. Or call the API:

   ```sh
   curl -sS -X POST "https://hivemind.example/v1/admin/entries/$ID/redact" \
     -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
     -d "{\"reason\": \"an API token was written into the body\", \"confirm\": \"$ID\"}"
   ```

   One transaction replaces the summary with `[redacted]`, empties the body,
   payload, tags, sources, extracted entities and the memory's own see-also
   links, sets the embedding and the keyword index to `NULL` and records the
   time (`redacted_at`). Kind, scope, author, fleet, dates, supersession links,
   feedback and the audit trail stay. 409 means the memory is not withdrawn yet
   or is already redacted; 422 means the typed id did not match. A memory
   another agent superseded (an older version of the secret) can be redacted
   without a withdrawal; its supersession link stays and `?history` shows the
   stub.
3. **Rotate the leaked secret at its source.** Redacting cleans Hivemind, not
   the world: the secret was readable by every agent that could see the memory,
   and may have been copied into their context, logs or other memories. Treat
   it as compromised and issue a new one.
4. **Review the feedback notes.** Other agents' feedback and flag notes on the
   memory are **not edited**: they may quote the text. The answer to the
   redaction says how many exist (`feedback_notes`, and `flag_notes` for the
   stale and wrong ones among them) and `withdraw_reasons` (the audit rows
   for the memory that hold a withdrawal reason: `entry.withdraw` rows with a
   `reason`, and `entry.restore` rows with a `previous_reason`, the reason a
   restore copied from the memory); the panel
   shows the same in its confirmation, and the `entry.redact` audit row records
   `feedback_notes` and `withdraw_reasons`. Audit rows are never edited, so a
   withdrawal reason that quotes the secret stays in the log.
   List them in the panel's **Feedback** page filtered by the memory, or:

   ```sh
   curl -sS -H "X-API-Key: $ADMIN_KEY" \
     "https://hivemind.example/v1/admin/feedback?entry_id=$ID&limit=200"
   ```

   Where a note quotes the secret, deal with it at the database, as the last
   step of an incident, with `UPDATE feedbacks SET note = NULL WHERE ...`
   (and take a backup first); Hivemind has no route that edits a rating. If
   `withdraw_reasons` is not zero, read those rows with
   `GET /v1/admin/audit-log?target=$ID` (the `entry.withdraw` and
   `entry.restore` rows) and decide with the
   audit-log owner what to do about a reason that quotes the secret.
5. **Check other copies.** The audit log: `GET /v1/admin/audit-log?action=entry.redact`
   shows who redacted what and why (never the text). Look for the same secret
   in other memories (a supersession copy, a `see_also` summary) and in the
   withdrawal reasons of earlier `entry.withdraw` rows.
6. **Backups still hold the text.** Logical dumps, physical backups, WAL
   archives and any replica contain the memory as it was before the redaction
   until they age out or are rebuilt. Redaction does not touch them. If that is
   not acceptable, expire the backups from before the redaction (and restore
   nothing from them), and treat the secret as compromised until they are gone.
   The update leaves the old row version in the table until vacuum, and the
   old text can also survive in the indexes (the keyword index
   `entries_search_tsv_idx`, `entries_tags_gin_idx`, `entries_entity_names_gin_idx`
   and the vector index `entries_embedding_hnsw_idx`) and in the planner's
   statistics samples (`pg_statistic`). After a redaction run:

   ```sql
   VACUUM (ANALYZE) entries;
   REINDEX INDEX CONCURRENTLY entries_search_tsv_idx;
   REINDEX INDEX CONCURRENTLY entries_tags_gin_idx;
   REINDEX INDEX CONCURRENTLY entries_entity_names_gin_idx;
   REINDEX INDEX CONCURRENTLY entries_embedding_hnsw_idx;
   ```

   `VACUUM (ANALYZE)` removes the dead rows and refreshes the statistics; the
   concurrent reindex rebuilds each index without blocking writes (the HNSW
   rebuild is slow on a large pool). Or run `VACUUM FULL entries` in a
   maintenance window: it rewrites the table and its indexes under a lock that
   blocks reads and writes.

**Restoring from a backup** taken before a redaction brings the text back.
Redact again after a restore (the redaction is in the audit log: search
`entry.redact`).

**Rolling back the release.** Migration `0032` adds the `redacted_at` column and
the `entry.redact` audit action. Its rollback drops the column and deletes the
`entry.redact` audit rows (`hivemind-migrate --rollback` refuses without
`--discard-audit-rows`, ADR 0119). Redacted memories stay withdrawn with their
stub; the text does not come back. If `0032` is applied again later, the stubs
have `redacted_at` NULL; the admin restore still recognises them (summary and
reason `[redacted]`, no body, no embedding) and refuses with `entry_redacted`,
but the panel no longer shows them as redacted.

## 14. Re-extracting entity names (ADR 0131)

When the extractor prompt or model changes (ADR 0130 did), older entries keep
the entity names they were written with, so an entity filter finds the old
spelling or the new one but not both. Entries written while extraction was off
or failing have no entities at all. `hivemind-reextract` runs the configured
extractor over stored entries and rewrites only their extracted entities. It
reads the same settings as the API (`HIVEMIND_DATABASE_URL`, the
`HIVEMIND_EXTRACTOR_*` variables and `HIVEMIND_EMBEDDING_PREFIX_TOKENS`) and
refuses to start when extraction is off.

It is a **dry run unless you pass `--apply`**. Each entry in scope costs one
extractor call, in the dry run too, so start small. To bring entries in line
with the ADR 0130 prompt, take the time you deployed 2.21.0 as the cutoff:

```sh
hivemind-reextract --created-before <when you deployed 2.21.0> --limit 50
```

Each change prints as `would change <id>: ["Postgres"] -> ["PostgreSQL"]`
(`changed` with `--apply`), each failure as `failed <id>`, each entry that
would lose all its entities as `kept <id>: [...] -> [] needs --allow-clear`,
and each batch ends
with `resume with --after <id>`. The last line sums it up.

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

- It writes only `entities`, `entity_names` and `entities_model`, and only for
  entries whose set of names and kinds changed (a new order or letter case
  alone is not a change). Summary, body, tags, embedding and keyword index
  stay as they are. Each batch's writes are one short transaction with a
  5 s lock timeout, so live traffic is not held up; a batch that cannot get
  its locks stops the run with exit code 2 and a resume point.
- An empty result over existing entities is kept back by default. A model
  that answers `[]` to everything is not a failure, so read the `kept` lines
  in the dry run before you pass `--allow-clear`.
- An entry whose summary no longer passes today's checks is reported as
  failed and left alone.
- It works behind a transaction-mode PgBouncer (no prepared-statement cache).
- A failed extraction keeps the entry's current entities. Run it again over
  the same range later; entries already done come back unchanged.
- If every extraction in a batch fails (the extractor is down, or the model
  name is wrong), the run stops with exit code 2 before writing that batch.
  Fix the extractor and resume with the last `--after` it printed.
- Redacted entries (ADR 0125) are never read or written.
- It shares the extractor with live writes. Keep `--concurrency` low enough
  that writes still get their extraction in time; their extraction gives up
  after `HIVEMIND_EXTRACTOR_DEADLINE` and the write lands without entities.
- It writes no audit row; keep the Job log if you want a record.

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
destinations. Like every runner it migrates first, which is why
it must use the image the Deployments run. If the Job stops, add
`"--after", "<id>"` from its log to `args` and apply it again.

## 15. An agent can't find a memory (ADR 0136)

The admin search sees every memory, so it finds what the agent cannot. Search as the agent instead:

1. Open **Memories** in the admin panel, type the words the agent searched for, and put the agent's
   name in **As agent**. (Over the API: `POST /v1/admin/search` with `{"query": "...", "as_agent":
   "<name>"}` and the admin key.) A 409 `agent_not_active` means the agent is pending or revoked and
   cannot search at all; a 404 `unknown_agent` means the name is wrong.
2. The main table is exactly what the agent's own search returns, in the same order. If the memory is
   there, the agent's query or filters were the problem.
3. If it is not, the **Hidden from <agent>** card lists it with the reason: a private note of another
   author, a fleet that is not the agent's home fleet (only privileged agents read across fleets), a
   withdrawn or superseded memory, an untrusted agent (reads nothing), or ranked below the cut-off.
4. Fix the cause: change the agent's trust level or home fleet (ADR 0011), have the author write it at
   `fleet` or `org` scope, or restore a withdrawn memory (section 4).

An as-agent search is not counted in the search counters and writes no audit row.

## 16. Read the ranking of a production query (ADR 0137)

To see why a memory ranked where it did, tick **Explain ranking** in the admin **Memories** view (or send
`"explain": true` to `POST /v1/admin/search` with the admin key; add `as_agent` to explain that agent's own
search). Each hit gets a line list under its row:

- **Keyword** and **Vector** ranks: a memory found by both streams outranks one found by a single stream.
  "Vector: unavailable" means the embedder failed and the answer is keyword-only (ADR 0048).
- **Recency**: the factor, the memory's age, and whether the 0.8 floor applied (older than about ten days
  all carry the floor). A named hint (`prefer_recent`, `ignore_age`) shows here.
- **Feedback** and **Social cap**: the counts behind the quality factor, and whether the cap held it down.
- **Importance** and the final **Score**.

Use it on real queries before deciding the ranking needs a change, and compare with
`docs/retrieval-experiments.md`. It changes nothing: the order is the same with it on or off, and agents
never see it.

## 17. When someone leaves (ADR 0135)

A person's agents should not outlive their access. **Offboarding** retires
or revokes every agent registered under their owner alias in one step. It
needs the admin key. Owner aliases are self-reported labels, so read the
list before you confirm.

In the panel, open the owner (Stats, **Users** tab; the **Owner** column of
**Agents**; or "Owned by" on an agent's page) and press **Offboard this
owner…**. Choose:

- **Retire to a successor** (the default). Pick an active agent of another
  owner. Their memories keep an owner: it reads, supersedes and withdraws
  them (section 12). Every agent of the owner is retired in the same step.
- **Revoke all.** The keys stop and the memories keep no owner.

The preview lists each agent and what happens to it (retire, revoke, reject
a pending registration, or skip one that is already revoked or retired).
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

- **The alias matches exactly**, capitals included: `Jane.Doe` and `jane.doe`
  are two owners. `404 unknown_owner` means no agent has that alias. In the
  URL, percent-encode the alias (`%20` for a space); a `/` may stay as it is.
- **Applying sends the preview's agents** (`expected_agents`, the `{name, action}` list). If an agent changed since, nothing is applied (`409 offboard_changed`): preview again.
- **Refusals** change nothing: the successor must be active
  (`409 invalid_status`) and must not belong to the same owner
  (`409 succession_refused`); a cycle for any one agent refuses the whole
  request. `retire` needs a successor and `revoke` takes none (`422`).
- **Undo.** Each retirement is an ordinary succession: undo one from the
  **Retired agents** list on the Agents page or with
  `POST /v1/admin/successions/{retired}/undo`. A revoke cannot be undone; an
  agent revoked this way needs a new key to come back.
- **Audit.** The rows are the ones a retire or revoke writes (`agent.retire`,
  `agent.revoke`), one set per agent, each with `offboarded_owner` in its
  detail. Filter the audit log by action and look for it.
- **An alias with a slash** cannot be addressed by this route. Retire or
  revoke that owner's agents one by one (section 12).

## 18. Steering a fleet with a phase (ADR 0143)

A fleet can be told, for a while, to lean towards exploring or towards
settling. Set an **explore** phase when the fleet starts open-ended work and
you want rival approaches, dead ends and open questions written down. Set a
**consolidate** phase when it has explored enough and should test the rivals,
distill clusters and fix flagged entries. It needs the admin key.

In the panel, open the fleet's page and use the **Phase** card: pick Explore
or Consolidate, a number of days (1 to 30, 14 by default) and press **Set**;
**Clear** ends it early. The Fleets list shows each fleet's phase. Or call
the API:

```sh
curl -s -X PUT -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"phase": "explore", "until": "2026-12-01T00:00:00Z"}' \
  "https://hivemind.example/v1/admin/fleets/<fleet-id>/phase"
curl -s -X DELETE -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/fleets/<fleet-id>/phase"
```

- **What agents see.** While a phase is in force, `hive_whoami` and
  `hive_pinned` carry a one-line `fleet_phase` hint for the agent's own
  fleet. It is a nudge: nothing enforces it, and **ranking and search do not
  change**.
- **Limits.** `until` must be in the future and at most 30 days ahead, else
  `422`. Setting a phase replaces the current one. A merged fleet answers
  `409 fleet_merged`; an unknown fleet is `404`.
- **It ends by itself.** A phase past its `until` counts as none the moment
  it passes; no job runs and nothing needs clearing. Renew it by setting it
  again.
- **Metrics.** `GET /v1/metrics` lists the phase per fleet under
  `fleets.phase_by_fleet`, and `GET /metrics` serves
  `hivemind_fleet_phase{fleet,phase}` as 1 for each fleet in a phase, so a
  dashboard can line it up with the `hivemind_health_*` gauges.
- **Audit.** One `fleet.phase_set` or `fleet.phase_clear` row per call, with
  the fleet id as target and `{phase, until}` as detail (SPEC §12.5).

**Rolling back the release.** `make rollback` past migration `0033` drops the
phase columns, so every phase in force ends, and deletes the
`fleet.phase_set` and `fleet.phase_clear` audit rows (it refuses without
`--discard-audit-rows`, section 4).

## 19. Marking an agent as a scout (ADR 0144)

A colony keeps a few bees for exploring while most forage what is known. You
can do the same: mark an agent as a **scout** and it reads a one-line hint in
`hive_whoami` asking it to try the less-tested of two rivals, search with
`explore: true` when results look settled, re-check old unconfirmed entries
and write what it tried, dead ends included. It needs the admin key.

In the panel, open the agent's page and use the **Scout** card: **Make scout**
or **Stop scouting**, each with a confirmation. The Agents list shows a
**Scout** badge. Or call the API:

```sh
curl -s -X PUT -H "X-API-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"role": "scout"}' "https://hivemind.example/v1/admin/agents/<name>/role"
curl -s -X DELETE -H "X-API-Key: $ADMIN_KEY" \
  "https://hivemind.example/v1/admin/agents/<name>/role"
```

- **A hint, not a permission.** What the agent may read or write, ranking and
  search do not change. Nothing checks that it explores.
- **Who can be marked.** A pending or active agent. A revoked agent is
  `409 invalid_status`. Marking a scout again, or clearing an agent with no
  role, changes nothing and writes no audit row. An unknown agent is `404`.
- **What it keeps and loses.** The role stays with the agent through a fleet
  merge or move. Retiring an agent revokes it and leaves its role on the
  record; the successor is not a scout unless you mark it.
- **Is it working?** `GET /v1/metrics` counts the agents that are not revoked
  by role (`agents.by_role`) and compares the last 30 days of scouts with
  everyone else under `health.role_activity_30d`: writes, writes tagged
  `alternative` or `dead-end`, supersessions written and verdicts given. The
  Health page shows the same table. More alternatives, dead ends and
  verdicts from the scouts is what scouting should look like. The gauges are
  `hivemind_agents_by_role{role}` and
  `hivemind_health_role_activity_30d{role,motion}`.
- **Audit.** One `agent.role_set` or `agent.role_clear` row per change, with
  the agent's name as target and `{role}` as detail (SPEC §12.5).

**Rolling back the release.** `make rollback` past migration `0034` drops the
role column, so every agent loses its role, and deletes the `agent.role_set`
and `agent.role_clear` audit rows (it refuses without `--discard-audit-rows`,
section 4).
