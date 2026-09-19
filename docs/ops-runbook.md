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
| `HIVEMIND_EMBEDDING_DIM` | the embedding dimension (a deploy-time decision — see §6) |
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
| MCP runner | `docker compose ps mcp-http` (ADR 0010); the streamable-HTTP endpoint `:8088` |

Watch for: the Postgres healthcheck failing, the `mcp-http` container
restarting, and the `schema_migrations` version lagging the deployed
`SCHEMA_VERSION` (drift → run `make migrate`).

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

## 6. Embedding dimension (deploy-time decision, ADR 0005)

The embedding dimension is baked into the `vector(:dim)` column at
migration time (ADR 0005). It is **not** an online setting: changing it
requires a fresh pool (a re-embed of every entry), not an in-place
change. Choose the dimension to match the embedding provider (ADR 0005:
self-hosted) at deploy time; the ROADMAP §4.2 tuning (prefix length /
dimension) uses the §1.1 eval harness to pick the value before it is
pinned.