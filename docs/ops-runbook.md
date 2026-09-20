# Hivemind — Ops Runbook (Tier 3.1)

> **What this document is:** the operational story for running Hivemind in
> production — deployment, backups, monitoring / health, and the key
> issuance / rotation story (defined by ADR 0012). It links to the ADRs /
> SPEC for the *reasoning*; it owns the *how*.

## 1. Deployment (single-node, docker-compose)

Hivemind is a **single-node** deployment (ADR 0007): one Postgres (with
pgvector) + the hostable streamable-HTTP MCP runner (+ an embedding
provider, ADR 0005). There is no orchestrator, no sharding, and no
multi-tenant isolation.

### Services (`docker-compose.yaml`)

| Service | Image / build | Purpose |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | the single Postgres + pgvector pool (host port 5432) |
| `vllm` | `vllm/vllm-openai-cpu` | local CPU embedding server (ADR 0005; `:8001`), fully-local path |
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

| Var | Meaning |
|---|---|
| `HIVEMIND_DATABASE_URL` | Postgres DSN (default `postgresql://hivemind:hivemind@localhost:5432/hivemind`) |
| `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` | the embedding provider (ADR 0005) |
| `HIVEMIND_EMBEDDING_DIM` | the embedding dimension (a deploy-time decision, default **1024** — ADR 0015; the dev Makefile exports 512 for fast local vLLM embedding — see §6) |
| `HIVEMIND_EMBEDDING_RETRIES` | retry budget for transient embedding failures (timeouts, connection errors, `429`, 5xx) — default 2; set `0` to disable (ADR 0014) |
| `HIVEMIND_EXTRACTOR_ENDPOINT` / `_MODEL` / `_API_KEY` | the entity-extraction extractor (ADR 0016, SPEC §13): **optional + best-effort** — unset = extraction off (entries land with empty `entities`, zero LLM cost); an extraction failure **never** blocks a write (the entry lands without facets). Dev/test: `http://localhost:8080/v1` (`qwen3.8-27b`, key `dummy`) |
| `HIVEMIND_EXTRACTOR_RETRIES` / `_TIMEOUT` | retry budget + call timeout for transient extractor failures (ADR 0014 pattern) — default 2 retries / 30 s; deterministic 4xx + schema-validation failures fail fast, no retry |
| `HIVEMIND_HOST` / `HIVEMIND_PORT` | the mcp-http bind host/port (ADR 0010) |

> **Embedding dimension is a deploy-time decision** (ADR 0005): the
> `vector(:dim)` column is created at migration time. Changing the
> dimension requires a fresh pool (`make pg-reset`), not an online
> change (ADR 0013).

## 2. Backups (single-node Postgres)

The single source of truth is the Postgres pool (ADR 0007). Backups are
plain Postgres backups — no Hivemind-specific tooling.

### Logical backup (portable, the primary story)

```sh
# A full logical dump (consistent, small; the pool is single-node).
docker compose exec postgres pg_dump -U hivemind -Fc hivemind \
  > backup-$(date +%Y%m%d-%H%M).pgdump
```

Schedule this (e.g. nightly) and ship it off-host. A logical dump is
portable across Postgres versions and is the safest restore path.

### Physical backup (fast, for scratch / small pools)

The `hivemind-pg-data` volume is the physical data directory. A volume
copy is a fast *point-in-time-ish* backup (for a small single-node pool;
not crash-consistent while the server writes — pair it with the logical
dump):

```sh
docker run --rm -v hivemind-hivemind-pg-data:/data -v "$(pwd)":/out \
  alpine tar -czf /out/pgdata.tar.gz -C /data .
```

### Restore

```sh
make pg-down
# wipe the volume, start fresh
make pg-reset
docker compose up -d postgres
# restore the logical dump
docker compose exec -i postgres pg_restore -U hivemind -d hivemind --clean --if-exists < backup-20260601-0400.pgdump
# re-apply any schema not already in the dump (idempotent; ADR 0013)
make migrate
```

> **Drift check:** after a restore, confirm the pool is on the expected
> schema generation (ADR 0013): `uv run python -c "import asyncio, asyncpg; from hivemind.store.migrate import current_schema_version; ..."`
> or read `schema_migrations` directly. If it lags, run `make migrate`
> (idempotent).

## 3. Monitoring / health

| Signal | How |
|---|---|
| Liveness / readiness | `GET /v1/health` (public) — `{"status":"ok"}` |
| Usage / counters | `GET /v1/metrics` (admin-gated) — entries / fleets / agents counters (ROADMAP §3.3) |
| Schema drift | `schema_migrations.version` (ADR 0013) — the applied schema generation |
| Postgres health | the `postgres` service healthcheck (`pg_isready`); `docker compose ps` |
| Embedder health | writes failing with `EmbeddingError` after the retry budget (ADR 0014) — check the embedding endpoint (`HIVEMIND_EMBEDDING_ENDPOINT`) and the provider process |
| Extractor health | entries landing with **empty `entities`** while extraction is expected (ADR 0016) — check `HIVEMIND_EXTRACTOR_ENDPOINT` / model reachability; note this is *by design* silent (best-effort: the write never fails over extraction), so watch for the symptom, not an error. All-or-nothing validation: if the model wraps its JSON in a code fence (or any schema violation — >10 entities, bad kind, name >128 chars) the whole extraction fails and the entry lands without facets — monitor facet coverage in the dogfood |
| MCP runner | `docker compose ps mcp-http` (ADR 0010); the streamable-HTTP endpoint `:8088` |

Watch for: the Postgres healthcheck failing, the `mcp-http` container
restarting, the `schema_migrations` version lagging the deployed
`SCHEMA_VERSION` (drift → run `make migrate`), and writes failing with
`EmbeddingError` after the retry budget (ADR 0014) — a dead embedder
fails writes once its `HIVEMIND_EMBEDDING_RETRIES` budget is
exhausted; a transient blip is retried automatically and needs no
action. (The extractor, ADR 0016, is deliberately the opposite: its
failures are best-effort and never block a write — the observable
symptom is an entry with empty `entities`, not a failed write.)

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
```

The admin REST surface does the same (`POST /v1/admin/agents/{name}/activate`
returns the generated agent key **once** — the only moment a key is ever
shown). Raw keys are random 32-byte secrets shown once; only the SHA-256
hash is stored (a leaked database never leaks usable keys, SPEC §8.1).

### Rotation / revocation

| Action | Command / endpoint | Effect |
|---|---|---|
| Rotate the org key (cluster kill switch) | `hivemind-keys rotate-org` / `POST /v1/admin/org-key/rotate` | all prior org keys stop working on the next request |
| Revoke an agent key | `hivemind-keys revoke --name alice` / `POST /v1/admin/agents/alice/revoke` | the agent's key is dead; the **name stays reserved** (ADR 0012) |
| Demote an agent | `PATCH /v1/admin/agents/{name}` (trust level → 0) | the agent is *dormant* (key still valid, no access) — distinct from revocation |

> **The org-key rotation is the cluster-wide kill switch** (ADR 0012):
> rotating it invalidates every org-key request (registration, health)
> immediately. Use it to cut the org off the cluster (e.g. a compromised
> shared key).

## 5. Disaster recovery (full)

1. Stop the services: `make mcp-http-down`, `make pg-down`.
2. Restore Postgres (see §2): wipe the volume, start fresh, `pg_restore`
   the latest logical dump, `make migrate` (idempotent catch-up, ADR
   0013).
3. Bring the services back up: `make pg`, `make mcp-http`.
4. Verify: `GET /v1/health`, `GET /v1/metrics`, and the
   `schema_migrations` version.

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
   now 1024, ADR 0015; the dev Makefile: `export HIVEMIND_EMBEDDING_DIM ?= 512`,
   override per deploy) + the matching `HIVEMIND_EMBEDDING_MODEL` /
   endpoint. A wrong value is a loud error at `hivemind-migrate` time
   (ADR 0015), so it can't slip through silently.
4. **Reset the pool** — `make pg-reset` (dev) / recreate the vector
   column (prod), then `make migrate` — the `vector(:dim)` column is
   recreated at the new width (forward migration, ADR 0013).
5. **Re-embed** — re-import the prior entries through the write path
   (entries embed at write time, ADR 0005; a stored 512-dim vector
   cannot be re-used in a 1024-dim pool).
6. **Restart the services** and verify: `GET /v1/health`, then run the
   §1.1 eval harness (golden set) at the new dim before declaring the
   switch a success.