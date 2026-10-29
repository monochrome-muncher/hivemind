# Hivemind — Production Deployment (Kubernetes + GitLab CI/CD)

Single source of truth for **deploying Hivemind to a Kubernetes cluster**
via GitLab CI/CD: **2 replicas** of the API and 2 of the MCP runner
(ADR 0026, for availability) against **one** Postgres node (ADR 0007).
The single-node **docker** ops story lives in
[docs/ops-runbook.md](docs/ops-runbook.md) — this file is the k8s story
and the two do not overlap.

Layout:

- [`deploy/kubernetes/`](deploy/kubernetes/) — the kustomize tree:
  namespace, ConfigMap, two 2-replica Deployments with a
  `maxSurge: 1` / `maxUnavailable: 0` rollout, two ClusterIP Services,
  two `minAvailable: 1` PodDisruptionBudgets (ADR 0026), and
  `secret-template.yaml`, which documents the CI-rendered Secret.
  `optional/` is the opt-in nginx Ingress + cert-manager Certificate,
  with `optional/networkpolicy/` (§2 step 6) and `optional/monitoring/`
  (the Prometheus Operator ServiceMonitors and the Grafana dashboard,
  §2 steps 7 and 8) under it; `admin/` and `portal/` are the admin panel
  and the agent portal, each its own kustomization (§8 and §9). Every
  pod runs as non-root with a read-only root filesystem and a locked-down
  security context.
- [`.gitlab-ci.yml`](.gitlab-ci.yml) — the pipeline (test → build →
  deploy); the first-run key bootstrap is
  [`scripts/ci-bootstrap-keys.sh`](scripts/ci-bootstrap-keys.sh) +
  [`deploy/kubernetes/bootstrap/`](deploy/kubernetes/bootstrap/) (ADR 0044)

---

## 1. Prerequisites

What the **org provides** (the app itself needs nothing else):

| Prerequisite | Detail |
|---|---|
| Postgres 16+ with the `vector` extension (pgvector **>= 0.8**) | A dedicated database + user whose credentials allow `CREATE EXTENSION vector` (DB user is a superuser, or the extension is pre-installed). pgvector 0.8+ is required (`hnsw.iterative_scan`, ADR 0025) and `HIVEMIND_EMBEDDING_DIM` must be **<= 2000** (HNSW's limit); `hivemind-migrate` fails with an actionable message otherwise. On an older pgvector upgrade the package/image, then `ALTER EXTENSION vector UPDATE;`. |
| A vLLM (or any OpenAI-compatible) **embedding** endpoint reachable from the cluster | e.g. an in-cluster vLLM Service (`http://vllm:8000/v1`); any OpenAI-compatible endpoint works (ADR 0005). |
| (Optional) an OpenAI-compatible **chat** endpoint for entity extraction | ADR 0016 — optional + best-effort. **Off by default**: an empty `HIVEMIND_EXTRACTOR_ENDPOINT` + empty key = zero LLM cost, entries land with empty `entities`. |
| GitLab: the **Kubernetes agent pre-configured** | The deploy job just runs `kubectl` — no kubeconfig wiring in `.gitlab-ci.yml`. |
| GitLab: the CI/CD variables below | See the table. |

**Required GitLab CI/CD variables:**

| Variable | Masked? / Protected? | Meaning |
|---|---|---|
| `K8S_IMAGE` | no / no | Registry image prefix, e.g. `registry.gitlab.com/<group>/<project>/hivemind`. The pipeline builds/pushes `$K8S_IMAGE:$CI_COMMIT_SHORT_SHA` and the deploy job substitutes it for the `hivemind:0.0.0` placeholder in the manifests. |
| `K8S_SECRET_DATABASE_URL` | **yes / yes** | The Postgres DSN, e.g. `postgresql://hivemind:hivemind@pg.hivemind.svc:5432/hivemind`. |
| `K8S_SECRET_EMBEDDING_API_KEY` | **yes / yes** | The embedding credential. **May be empty** (self-hosted vLLM without auth). |
| `K8S_SECRET_EXTRACTOR_API_KEY` | **yes / yes** | The extractor credential. **May be empty** (extraction off, ADR 0016 — safe). |
| `K8S_SECRET_MIGRATE_DATABASE_URL` | **yes / yes** (optional) | A direct Postgres DSN for `hivemind-migrate` only, needed when `K8S_SECRET_DATABASE_URL` points at a transaction-mode PgBouncer (the migration's session advisory lock cannot live there; `docs/ops-runbook.md`). Unset = migrate through `K8S_SECRET_DATABASE_URL`. |

**Mark all three `K8S_SECRET_*` variables _Protected_ as well as Masked,
and protect the default branch and the `production` environment.** An
unprotected variable is injected into every pipeline, including merge-request
pipelines from unprotected branches — and an MR author controls
`.gitlab-ci.yml` itself, so the `rules:` that keep `deploy` on the default
branch are no barrier; masking is easy to evade. A protected variable is only
exposed to protected branches/tags. (The empty-allowed variables still need
to exist, empty, for the same reason.)

All three `K8S_SECRET_*` values are rendered by the deploy job into the
`hivemind-secrets` Secret (the in-tree
[`deploy/kubernetes/secret-template.yaml`](deploy/kubernetes/secret-template.yaml)
documents the shape — it is deliberately NOT in the kustomization, so CI
is the single source of truth for secrets).

---

## 2. Quickstart

The operator flow (steps 1–4 are required, 5–8 optional):

1. **Local dev** — `cp config/.env.example .env.local` (the master
   template is `config/.env.example`; the profile files — `.env.local`,
   `.env.staging`, `.env.production`, `.env.test` — are selected by the
   `ENVIRONMENT` env var (ADR 0017), and real environment variables always
   win over file values). Production/k8s never reads a profile file —
   values come from the ConfigMap + Secret.
2. **Set the three masked GitLab variables** (`K8S_SECRET_DATABASE_URL`,
   `K8S_SECRET_EMBEDDING_API_KEY`, `K8S_SECRET_EXTRACTOR_API_KEY`) + the
   `K8S_IMAGE` variable (GitLab → Settings → CI/CD → Variables).
3. **First `git push`** — the pipeline builds + deploys + **boots the
   keys automatically** (the deploy job's idempotent first-run bootstrap,
   `scripts/ci-bootstrap-keys.sh`, ADR 0044). A one-shot Job, under its own
   ServiceAccount (`deploy/kubernetes/bootstrap/rbac.yaml`: create Secrets,
   get/delete `hivemind-keys`), runs `hivemind-keys bootstrap-secret`, which
   issues the admin key, rotates the org key and writes both straight into
   the `hivemind-keys` Secret. No key is printed, so the Job log and your
   log shipper never hold one. The keys are committed only after the Secret
   was created, so a failed run leaves nothing behind: fix the cause (the
   script prints the Job log) and re-run the pipeline. One exception: if the
   Job log says the commit failed and the Secret could not be deleted, follow
   its steps and delete that Secret first. Otherwise the re-run sees the
   Secret, skips the bootstrap and goes green with keys that do not work. The script deletes
   the Job and the RBAC objects when it is done; the deploy identity needs
   permission to create that Role and RoleBinding. If you bootstrapped
   before ADR 0044, the old Job printed the keys: treat your log store as
   key-bearing, or rotate both keys (§4).
4. **Fetch the generated keys** and hand them out (the raw keys live
   ONLY in the k8s Secret `hivemind-keys` — GitLab stays key-free):

   ```bash
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ADMIN_KEY}' | base64 -d
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ORG_KEY}'   | base64 -d
   ```

   Distribution: **admin key** → operators (admin surface + key
   management); **org key** → the registration surface (`hive_register`
   / `POST /v1/agents`); **agent keys** are issued per agent, either via
   `hivemind-keys issue-agent --name <agent>` (an active agent with no key; for a pending or revoked one add `--trust-level N --home-fleet <fleet-id>`, ADR 0039), the admin REST surface
   `POST /v1/admin/agents/{name}/activate` (returns the key **once**), or
   the **admin panel** (§8), which the same pipeline deploys.
5. **(Optional)** external reachability — apply the opt-in Ingress:

   ```bash
   kubectl kustomize deploy/kubernetes/optional | kubectl -n hivemind apply -f -
   ```

   (after setting the real hostname + issuer in
   `deploy/kubernetes/optional/ingress.yaml`). The tree has a second
   Ingress, `hivemind-admin-api`, that keeps the admin REST surface
   (`/v1/admin/*` and the admin-only `/v1/metrics`), the agent portal's routes (`/v1/portal/*`, which the portal calls in-cluster, §9) and the unauthenticated Prometheus target (`/metrics`, ADR 0050; scrape the pods directly) off the internet: it answers 403 to every source outside
   its `allowlist-source-range` (default `127.0.0.1/32`, nobody). Operators
   use the admin panel (§8) or `kubectl port-forward`; to admit an operator
   network, put its CIDRs in both source-range annotations. The main
   Ingress rate-limits each client IP (20 req/s with a burst of 5x, 100 open
   connections; nginx answers 503 above that); raise the limits if many
   agents share one egress IP.
6. **(Optional)** example NetworkPolicies — a separate tree,
   `deploy/kubernetes/optional/networkpolicy/`. **Edit the namespace
   selector first**: it allows ingress only from the namespace
   `ingress-nginx`; with any other controller namespace the policies cut
   external traffic off. Needs a CNI that enforces NetworkPolicy; apply with
   `kubectl kustomize deploy/kubernetes/optional/networkpolicy | kubectl -n hivemind apply -f -`.
   The stricter **default-deny egress** policy is its own tree,
   `deploy/kubernetes/optional/networkpolicy/egress/`: it lets the api, mcp,
   admin and portal pods reach only DNS, Postgres, the embedding/extractor
   endpoints and (for the panel and the portal) `hivemind-api`. Replace its placeholder
   `192.0.2.x` addresses with yours first; unedited, it fails closed and the
   pods cannot reach Postgres.
7. **(Optional)** Prometheus scraping (ADRs 0050, 0104). `GET /metrics` is
   served with no key by the `hivemind-api` pods (usage gauges and request
   series) and by the `hivemind-mcp` pods (their own request, tool-call and
   degraded-search series only), so Prometheus must scrape them in-cluster
   (never through the public Ingress, which sends only `/mcp` to the MCP
   Service). The MCP endpoint is off unless `HIVEMIND_MCP_METRICS_ENABLED=true`
   (the shipped manifest sets it). It reveals request volume and latency,
   per-tool call mix and error share, the count of 401s (an outsider could
   watch a key-guessing run) and the degraded-search count, never an agent,
   fleet, owner, key or argument. If the MCP port is reachable from outside
   some other way (a LoadBalancer, a compose host port behind a TLS proxy),
   leave it off or block `/metrics` in the proxy.
   Plain Prometheus with the usual `kubernetes-pods` job finds both by their
   `prometheus.io/*` annotations. The Prometheus Operator ignores those:
   apply the ServiceMonitors in `deploy/kubernetes/optional/monitoring/`
   (add your Operator's release label first), or write your own that
   selects `app.kubernetes.io/component: api` or `mcp` as well as the name
   label. Every Hivemind Service carries `app.kubernetes.io/name: hivemind`,
   so a selector on the name alone also scrapes the admin panel and the
   portal, which have no `/metrics`. `sum` the process counters across the
   two jobs. With the optional NetworkPolicies, Prometheus must run in the
   `monitoring` namespace (or edit the policies).
8. **(Optional)** Grafana dashboard. `deploy/kubernetes/optional/monitoring/hivemind-dashboard.json`
   shows search health (the share of searches finding nothing, per fleet;
   keyword-only searches; explore share), traffic (requests, 5xx share,
   401s, p50/p95 latency per route), MCP tool calls (rate, error share, p95
   latency) and the fleet health gauges over time, so a release can be
   compared with the one before it by moving the time range. The same tree
   ships it as a ConfigMap labelled `grafana_dashboard: "1"`, which the
   Grafana sidecar of kube-prometheus-stack loads on its own; elsewhere,
   import the JSON (Dashboards > New > Import) and pick the Prometheus data
   source. The search panels smooth over the `Search window` variable
   (default 1h): those counters refresh about every 30 s. The MCP panels stay
   empty while `HIVEMIND_MCP_METRICS_ENABLED` is off.

---

## 3. Manual steps checklist

**Human-only actions** (everything else is automatic):

1. Provision the Postgres database + pgvector (one-time).
2. Stand up the embedding endpoint (and, optionally, the chat endpoint
   for extraction) reachable from the cluster.
3. Set the four GitLab CI/CD variables (§1).
4. **First pipeline** (`git push`) — then:
5. Fetch `ADMIN_KEY` / `ORG_KEY` from the `hivemind-keys` Secret and
   distribute them (§2 step 4).
6. (Optional) apply the opt-in Ingress with the real hostname/issuer.
7. (Optional) apply the NetworkPolicies, and set up scraping and the
   Grafana dashboard (§2 steps 6 to 8).

**What is automatic** (never do these by hand):

- Schema migration — the **entrypoint's** `hivemind-migrate` pre-step on
  every pod start (ADR 0018 — single source of truth; advisory-locked so
  concurrent replicas are safe, ADR 0020; a dim mismatch fails LOUDLY,
  ADR 0015).
- Image build + push (build stage).
- Rendering of `hivemind-secrets` from the `K8S_SECRET_*` variables
  (deploy job, every deploy).
- First-run key bootstrap (deploy job, **only** when `hivemind-keys`
  does not exist).
- Rollouts (`kubectl rollout status` on the api, mcp, admin and portal Deployments).

---

## 4. Key & credential rotation reference

Rotate **one credential at a time** (no double-rotation — verify each
rotation before starting the next; two simultaneous rotations make a
failure undiagnosable).

Every `hivemind-keys` rotation below is recorded in the audit log
(ADR 0027, SPEC §12.5) under the name you pass as `--actor` (before the
subcommand; default: the OS user — `hivemind` in the image, so pass it):
`hivemind-keys --actor <you> rotate-org`. The value is unverified, which
is why those rows are marked `actor_kind = cli`. Check what happened with
`GET /v1/admin/audit-log` (admin key; `docs/ops-runbook.md` §4). The
first-run bootstrap Job records itself as `ci-bootstrap`.

| Credential | Rotate via | When |
|---|---|---|
| Org key | `hivemind-keys rotate-org` | suspected org-key leak; periodic hardening |
| Admin key | `hivemind-keys issue-admin` + `hivemind-keys revoke-admin` | suspected admin-key leak; operator turnover |
| Agent key | `hivemind-keys rotate-agent --name <agent>` (replaces the key, the agent stays active) or `hivemind-keys revoke --name <agent>` | agent compromise; key leak; owner lost the key |
| DSN (DB password) | change the password at the Postgres provider | suspected DSN leak; provider policy |
| Embedding API key | rotate at the embedding provider | provider policy; suspected leak |
| Extractor API key | rotate at the chat provider | provider policy; suspected leak |

### 4.1 Org key

- **How** — `hivemind-keys rotate-org` (atomic delete + insert; it
  closes registration to every prior copy, ADR 0031). The new raw key is
  printed **once**.
- **Then** — store it: update the k8s Secret `hivemind-keys`
  (`kubectl -n hivemind patch secret hivemind-keys -p
  '{"stringData":{"ORG_KEY":"<new>"}}'`) — or delete the Secret and re-run
  the bootstrap (§2 step 4).
- **Blast radius** — only registration: anyone still holding the old
  org key gets a 401 on their **next** registration attempt. Active agents
  authenticate with their own agent keys and are **not** affected. No
  data impact.
- **Verify** — a registration call with the new org key succeeds; the old
  key now 401s.

### 4.2 Admin key

- **How** — `hivemind-keys issue-admin` (prints the new key once) →
  distribute → `hivemind-keys --actor <you> revoke-admin --key <old raw
  key>` or `--hash <sha256 from hivemind-keys list>` (exactly one of the
  two is required; a unique >=12-hex fingerprint prefix is accepted).
  Always use the CLI: it records the revocation in the
  audit log (ADR 0027), which raw SQL against `credentials` would bypass.
- **Blast radius** — only the admin surface (fleet/level management,
  key rotation) loses the old key; data-plane verbs are unaffected.
- **Verify** — admin call with the new key works; the old key 401s.

### 4.3 Agent keys

- **How, replacing the key**: `hivemind-keys rotate-agent --name <agent>`
  (or "Replace key" in the admin panel, or
  `POST /v1/admin/agents/{name}/key`). The old key stops working on the
  agent's next request, the new one is shown **once**, and the agent
  stays `active` with the same level, fleet and memories (ADR 0108). Give
  the new key to the owner out of band. Only for an `active` agent.
- **How, cutting the agent off**: `hivemind-keys revoke --name <agent>` (the agent becomes
  `revoked` and the name **stays reserved**; re-activation issues a NEW
  key — ADRs 0012, 0028). Re-issue via the admin surface
  `POST /v1/admin/agents/{name}/activate` (or the admin panel). Activating
  an agent that is still `active` is refused (409): one key per agent,
  so revoke first.
- **Blast radius** — only that agent's verbs (its writes/reads/feedback).
- **Verify** — the agent's next request 401s with the old key and works
  with the new one.

### 4.4 DSN (`HIVEMIND_DATABASE_URL`)

- **How** — change the DB password at the Postgres provider → update
  `K8S_SECRET_DATABASE_URL` → **next deploy** re-renders the Secret +
  rolling restart (env is read at process start, so the rollout IS the
  apply step).
- **Blast radius** — nothing works with the old DSN; both Deployments
  roll.
- **Verify** — rollout completes, `GET /v1/health` 200, `hivemind-keys
  list` succeeds from a pod.
- **Failure mode** — a dead DSN = **total outage** (all verbs).

### 4.5 Embedding API key / endpoint

- **How** — rotate at the provider → update `K8S_SECRET_EMBEDDING_API_KEY`
  → next deploy re-renders the Secret + rolling restart.
- **Failure mode** — a dead embedder **FAILS WRITES** once the ADR 0014
  retry budget is exhausted (`EmbeddingError`; the entry is NOT
  persisted — embeddings are NOT best-effort, unlike extraction). Reads
  still work (keyword + existing vectors).

### 4.6 Extractor API key / endpoint

- **How** — rotate at the chat provider → update
  `K8S_SECRET_EXTRACTOR_API_KEY` (or leave it empty + empty endpoint =
  extraction off, zero cost). Next deploy re-renders the Secret + rolling
  restart.
- **Failure mode** — a dead extractor = entries land **WITHOUT entity
  facets** (best-effort, ADR 0016). The write still succeeds; the
  observable symptom is **empty `entities`**, not an error.

---

## 5. Upgrades

- A normal deploy is just a new image tag: the entrypoint applies any
  outstanding migrations on every pod start (ADR 0018, ADR 0020), under
  a Postgres advisory lock, so many replicas may start at once and
  exactly one migrates. An image whose migration chain is **older** than
  the pool applies nothing and never rolls the schema back.
- **Does a release carry a migration?** Compare the chain between the two
  tags: `git diff --stat v<old> v<new> -- src/hivemind/store/migrations/`.
  No output means no schema change. Even when there is one, there is no
  manual step: the entrypoint applies it. What each migration adds is in
  the `Server` column of the release's row in
  [plugins/hivemind/CHANGELOG.md](plugins/hivemind/CHANGELOG.md).
- **Agents pick up server-side changes on their own.** Tool descriptions,
  the MCP server instructions and `hive_whoami` come from the server, so
  upgrading the server updates what every agent sees. The agent plugin's
  skills and hook update separately, per harness
  (`plugins/hivemind/README.md`, "Update"; what changed for agents in
  each release is in `plugins/hivemind/CHANGELOG.md`).
- **Deploy every runner project together whenever a migration lands.**
  The runners are separate deployments (and, under GitLab AutoDevOps,
  separate projects) sharing one pool, so a migration from one runs
  against the other's still-deployed code. Releases that only change
  config need not be synchronised. Schema changes follow
  **expand-and-contract** (SPEC §8.6): additive within a release, and a
  removal split across two.
- **Removed settings are ignored, with a warning.** When a release
  removes a setting, a deployment that still sets it starts normally,
  even with `HIVEMIND_STRICT_ENV=true`, and logs `Removed HIVEMIND_*
  environment variable(s) are ignored` with the names. Delete them from
  the ConfigMap or Secret when convenient. 2.12.0 removes
  `HIVEMIND_RECENCY_FROM_CONFIRMATION` and `HIVEMIND_RECENCY_BY_KIND`
  (ADR 0103); both were off by default, so ranking does not change.
- **Derived images add layers only — never an `ENTRYPOINT`.** An
  air-gapped high-side image built `FROM` this one (e.g. to install
  custom CA certificates) inherits the migrations and the entrypoint.
  Setting its own `ENTRYPOINT` or `CMD` **silently skips the migration
  pre-step** — the same trap as the key-bootstrap Job below, but with no
  error until the first query hits a missing object. The image runs as
  UID 10001, so a layer that installs files system-wide must switch user
  and back:

  ```dockerfile
  FROM <registry>/hivemind:<sha>
  USER root
  COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
  RUN update-ca-certificates
  USER 10001:10001
  ```
- A **dim mismatch is a LOUD failure at pod startup** (ADR 0015): the
  entrypoint's `hivemind-migrate` step exits non-zero naming both dims
  and both fixes — the pod never starts, never a silent no-op. The check
  runs right after the advisory lock is taken and before any DDL, so two
  replicas configured with different dims cannot both pass on a fresh pool.
- Changing the embedding **model/dim** is an operator migration (new
  pool or re-embedding — ADR 0005), never a config flip.
- To **roll back** an incremental schema change, use the migration's
  `.rollback.sql` (ADR 0020). Rollbacks are defined for structural
  changes only; reversing a populated column drop or a backfill is a
  restore from backup (`docs/ops-runbook.md`).
- If the pool lags the deployed chain (e.g. pods were added before a
  migration ran), run the migration one-off:

  ```bash
  kubectl -n hivemind run hivemind-migrate --rm -i --restart=Never \
    --image=<registry>/hivemind:<sha> \
    --overrides='{"spec":{"containers":[{"name":"hivemind-migrate","image":"<registry>/hivemind:<sha>","env":[{"name":"HIVEMIND_RUNNER","value":"migrate"}],"envFrom":[{"secretRef":{"name":"hivemind-secrets"}},{"configMapRef":{"name":"hivemind-config"}}]}]}}'
  ```

  (`kubectl run` has no `--env-from`; the ConfigMap is injected too so the
  run sees the real `HIVEMIND_EMBEDDING_DIM`.) Simpler when the Deployments
  are healthy: `kubectl -n hivemind exec deploy/hivemind-api -- hivemind-migrate`.

### Upgrading from 1.2.x to 2.0.0

Historical — today's upgrades follow the bullets above, and each
release's row in [plugins/hivemind/CHANGELOG.md](plugins/hivemind/CHANGELOG.md)
(the `Server` column) names what operators must do. If you are still
upgrading from 1.2.x, do these before the first 2.0.0 pod starts: find
credentials that stop working with the "Dead credentials" SQL in
`docs/ops-runbook.md` §4 (pre-v2 `user` keys, name-less agent keys and
keys of pending or revoked agents no longer authenticate, ADR 0039) and
re-issue anything a live client still uses; ensure pgvector 0.8+ and
`HIVEMIND_EMBEDDING_DIM` ≤ 2000 (§1); mark the `K8S_SECRET_*` variables
Protected and apply the RBAC in §7. Rolling back to 1.2.x after
migrations `0007`/`0008` ran is schema-safe, but the duplicate keys
`0007` deleted stay deleted, and the 1.2.x pods accept the dead
credentials again.

### Graceful shutdown (`terminationGracePeriodSeconds`)

Both Deployments set **`terminationGracePeriodSeconds: 150`** plus a
**5-second `preStop` sleep**. With more than one replica a rolling
deploy terminates pods routinely, so these are ordinary-path settings,
not edge-case insurance.

uvicorn handles SIGTERM itself — stop accepting, drain the in-flight
requests, then shut down — and nothing bounds that drain, so the grace
period is the only real deadline: when it expires the container is
SIGKILLed with whatever is still in flight. 150s covers the slowest
single request, a write (`hive_write` / `POST /v1/entries`), which
embeds and then extracts in sequence, each under its own bounded retry
budget (ADR 0014):

| leg | attempts | timeout | backoff | worst case |
|---|---|---|---|---|
| embedder | 1 + `HIVEMIND_EMBEDDING_RETRIES` (2) = 3 | `HIVEMIND_EMBEDDING_TIMEOUT` (10s) | 0.5s + 1.0s | 31.5s |
| extractor | 1 + `HIVEMIND_EXTRACTOR_RETRIES` (2) = 3 | `HIVEMIND_EXTRACTOR_TIMEOUT` (30s) | 0.5s + 1.0s | 91.5s |
| **worst-case write path** | | | | **123.0s** |

Plus the 5s `preStop` sleep and two pooled Postgres round-trips,
rounded up to **150s** for margin. The `preStop` sleep exists because on
pod deletion the SIGTERM and the EndpointSlice removal happen
concurrently: until deregistration reaches kube-proxy — and the nginx
ingress controller, which routes straight to pod IPs — a terminating pod
can still be handed new requests it would refuse.

**If you raise a retry budget or timeout, raise this.** Each leg costs
`(retries + 1) x timeout + 0.5 x (2^retries - 1)` seconds; sum the two
legs, add the 5s `preStop`, then round up — in *both*
`deploy/kubernetes/*-deployment.yaml`. Worked example:
`HIVEMIND_EXTRACTOR_RETRIES=4` makes the extractor leg 157.5s and the
write path 189s, so the grace period needs to go to **~240s**. Leaving
it stale is not a crash — it is a SIGKILL that drops a write the caller
was told nothing about. (Each leg also runs under
`HIVEMIND_EMBEDDING_DEADLINE` / `HIVEMIND_EXTRACTOR_DEADLINE`, ADR 0041;
setting one lower shortens the worst case, and a value below the
matching `_TIMEOUT` is a startup error.)

### Probe endpoints (ADR 0019)

Four **unauthenticated** orchestrator endpoints (served outside the
auth middleware — k8s probes carry no credential):

| Endpoint | Depth | Semantics |
|---|---|---|
| `GET /mcp/liveness` (mcp runner) | shallow | 200 whenever the process answers |
| `GET /mcp/health` (mcp runner) | shallow | 200 whenever the process answers — the readiness probe. It does **not** check the database: all replicas share it, so a DB outage would pull every pod out of service at once (ADR 0037) |
| `GET /mcp/health/database` (mcp runner) | deep | 200 when the Postgres pool answers within 5 s; else 503. For people and monitoring; no probe uses it. The pod log names the cause once per outage (`database health check failing: <exception>`) |
| `GET /v1/liveness` (REST API) | shallow | 200 whenever the process answers |
| `GET /v1/health` (REST API) | static | unchanged — static 200, public by design (SPEC §5.1); the REST surface intentionally has no deep DB probe in v1 |

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 on every agent call | an active agent: its agent key was revoked or mistyped. A not-yet-activated agent: a stale org key (after a rotation that wasn't distributed) | active: re-activate it for a new key (§4.3); pending: re-fetch `ORG_KEY` from the `hivemind-keys` Secret (§4.1) |
| app container CrashLooping on the entrypoint's migrate pre-step | dim mismatch (ADR 0015) — the log names both dims + both fixes | `make pg-reset` equivalent (fresh pool) or set `HIVEMIND_EMBEDDING_DIM` to the pool's dim |
| `Unknown HIVEMIND_* environment variable(s)` WARNING at startup | either a typo'd or stale `HIVEMIND_*` var (ADR 0024) — e.g. a renamed knob (ADR 0021's `_PREFIX_CHARS` → `_PREFIX_TOKENS`) left set under its old name — or a variable the platform injected (GitLab Auto DevOps; Kubernetes service links such as `HIVEMIND_API_SERVICE_HOST`) | a typo: fix it per the suggestion. A platform variable: ignore it, or set `enableServiceLinks: false` on the pod to drop the service-link ones. Startup only **fails** on this when `HIVEMIND_STRICT_ENV=true` (ADR 0032) |
| `Removed HIVEMIND_* environment variable(s) are ignored` WARNING at startup | a setting this release removed is still set, e.g. `HIVEMIND_RECENCY_FROM_CONFIRMATION` or `HIVEMIND_RECENCY_BY_KIND` (ADR 0103). Harmless: the value is ignored and startup never fails on it | delete it from the ConfigMap or Secret (§5) |
| A scrape of `hivemind-mcp` fails with 401 or 404 | 401: an older image without `/metrics` (ADR 0104), or a wrong path (e.g. through the Ingress's `/mcp` prefix, or a ServiceMonitor `path` other than `/metrics`). 404: `HIVEMIND_MCP_METRICS_ENABLED` is not `true` (it is off by default) | roll the MCP pods to the current image; set `HIVEMIND_MCP_METRICS_ENABLED=true` (the shipped Deployment does); scrape the pod port 8088 at `/metrics` (§2 step 7, or `deploy/kubernetes/optional/monitoring/`). Do not give Prometheus a key |
| MCP session drops after ~60s | nginx default `proxy-read-timeout` killing the SSE stream (classic pitfall) | apply the opt-in Ingress with the SSE annotations (`deploy/kubernetes/optional/ingress.yaml`) |
| entries have empty `entities` | extractor off (empty endpoint — by design) OR extractor dead (best-effort, ADR 0016) | check `HIVEMIND_EXTRACTOR_ENDPOINT` + `K8S_SECRET_EXTRACTOR_API_KEY`; §4.6 |
| writes failing with `EmbeddingError` | embedder dead (ADR 0014 retry budget exhausted) | §4.5 |
| a write dropped mid-flight during a deploy (client sees a reset, no entry lands) | `terminationGracePeriodSeconds` no longer covers the retry budgets — the pod was SIGKILLed while still draining | recompute the sum in §5 and raise it in both Deployments |
| `hivemind-secrets` missing values | CI variable changed but no redeploy yet | re-run the pipeline (the deploy job re-renders the Secret every deploy) |

**Two-Secret ownership rule (do not cross the boundary):** CI owns
`hivemind-secrets` (the three `K8S_SECRET_*` values, re-rendered on
every deploy); the first-run bootstrap owns `hivemind-keys` (the
generated ADMIN_KEY / ORG_KEY). The bootstrap step (`scripts/ci-bootstrap-keys.sh`)
deliberately skips when `hivemind-keys` exists, so a secret re-render
never wipes the generated keys.

---

## 7. RBAC

Minimal ServiceAccount + Role for the GitLab Kubernetes agent's deploy
ServiceAccount (the verbs `.gitlab-ci.yml` actually uses). The org's k8s agent must run as this ServiceAccount
(SA token or mounted kubeconfig).

**The namespace is created once by an operator** (`kubectl apply -f
deploy/kubernetes/namespace.yaml`): `namespaces` are cluster-scoped, so a
namespaced Role cannot grant them. The pipeline's own
`kubectl apply -f namespace.yaml` is then a no-op that needs only `get` +
`patch` on that one namespace, granted by the small ClusterRole below
(`resourceNames` limits it to `hivemind`).

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hivemind-deployer
  namespace: hivemind
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: hivemind-deployer
  namespace: hivemind
rules:
  # `apply` is not an RBAC verb: server-side/client-side apply needs
  # get + create + patch. The rollout waits need list + watch.
  - apiGroups: [""]
    resources: ["configmaps", "services"]
    verbs: ["get", "list", "watch", "create", "patch"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "patch"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "create", "patch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["get", "list", "watch", "create", "patch"]
  # The first-run bootstrap applies, and later deletes, its own
  # ServiceAccount/Role/RoleBinding (deploy/kubernetes/bootstrap/rbac.yaml):
  - apiGroups: [""]
    resources: ["serviceaccounts"]
    verbs: ["get", "create", "patch", "delete"]
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings"]
    verbs: ["get", "create", "patch", "delete"]
  # Only if you apply the optional/ trees (Ingress, example NetworkPolicies)
  # with the same ServiceAccount:
  # - apiGroups: ["networking.k8s.io"]
  #   resources: ["ingresses", "networkpolicies"]
  #   verbs: ["get", "list", "watch", "create", "patch"]
  # Only if you apply optional/monitoring/ with it (the dashboard ConfigMap
  # is covered by the configmaps rule above):
  # - apiGroups: ["monitoring.coreos.com"]
  #   resources: ["servicemonitors"]
  #   verbs: ["get", "list", "watch", "create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: hivemind-deployer
  namespace: hivemind
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: hivemind-deployer
subjects:
  - kind: ServiceAccount
    name: hivemind-deployer
    namespace: hivemind
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: hivemind-namespace-apply
rules:
  - apiGroups: [""]
    resources: ["namespaces"]
    resourceNames: ["hivemind"]
    verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hivemind-namespace-apply
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: hivemind-namespace-apply
subjects:
  - kind: ServiceAccount
    name: hivemind-deployer
    namespace: hivemind
```

## 8. Admin panel (`hivemind-admin`, ADR 0029)

A browser front end for the admin surface: the pending-agent queue,
activation with the key shown once, agents (replace a key, ADR 0108; see
who is idle, ADR 0110; clean up after a bad one, ADR 0111; retire one in
favour of a successor, ADR 0113), fleets (merge, ADR 0107; rename; move
agents to split a team, ADR 0112), the audit log and org-key rotation,
plus daily activity and stats, the fleet health metrics, a memory browser
with search and withdrawal (ADR 0080), the feedback log (ADR 0090), each
fleet's pins (ADR 0092), the flag backlog (ADR 0093) and CSV export
(ADR 0094). The root [README](README.md) describes it from a user's point
of view. Admin keys stay CLI-only.

**What it needs.** One env var: `HIVEMIND_ADMIN_API_URL`, the base URL of
the hivemind-api deployment (in-cluster: `http://hivemind-api:8000`). For
an https URL behind an internal CA, also set `HIVEMIND_ADMIN_API_CA_BUNDLE`
to a mounted PEM file. It holds **no DSN and no Secret**, never talks to
Postgres, and the entrypoint skips the migration step for it.

**Deploying it.** It is its own kustomization, so it works as a separate
GitLab project on the same image:

```bash
kubectl kustomize deploy/kubernetes/admin \
  | sed "s|hivemind:0.0.0|$IMAGE|g" | kubectl -n hivemind apply -f -
```

The in-repo `.gitlab-ci.yml` deploy job runs exactly this as step 5. One
replica, no PDB: it is stateless and only operators use it.

**Reaching it.** It is an operator tool, so keep it off the public
internet:

```bash
kubectl -n hivemind port-forward svc/hivemind-admin 8080:8080
# then open http://localhost:8080 and sign in with the admin key
```

or route an **internal-only** ingress host to `hivemind-admin:8080`.
The panel holds the admin key in the browser tab's `sessionStorage`
(cleared on sign-out or when the tab closes), so serve it over TLS
wherever it is not `localhost`.

**Locally:** `make up` (panel on `:8089`) or `make api` + `make admin`
(`:8080`) — the docker story is in [docs/ops-runbook.md](docs/ops-runbook.md)
§1 and the root [README](README.md), "Try it on your computer".

**What it forwards.** The admin endpoints the panel uses (a subset of
`/v1/admin/*` — browsing memories, activity, the audit log, fleet and
agent management, key rotation; never the data plane), `GET /v1/metrics`
and `GET /v1/health`. Anything else is a 404 from the panel itself. It
forwards `X-API-Key`, `Content-Type` and `Accept` and nothing else, marks
every forwarded response `Cache-Control: no-store`, and never logs a
header or a body. `GET /healthz` is its own probe and does not call the
API.


## 9. Agent portal (`hivemind-portal`, ADR 0095)

A browser front end for the people behind the agents. Someone signs in
with one agent's key and sees what that agent wrote (withdrawn and
superseded included), the verdicts it gave and got, when the key was last
used, its home fleet's memories and pins, a "Needs attention" page of the
agent's flagged memories (ADR 0115), and a search over everything the key
can read. They can withdraw the agent's own memories, pin or unpin
entries as the agent (same rights and limits as `hive_pin`), lock a
leaked key, and clean up the agent's own work over a time window with
undo (ADR 0118); all of it is audited with `via: "portal"`. Admin and
org keys are turned away (§8 is for the admin key), and portal searches
are left out of the search counters. The root [README](README.md)
describes it from a user's point of view.

**What it needs.** One env var: `HIVEMIND_PORTAL_API_URL`, the base URL of
the hivemind-api deployment (in-cluster: `http://hivemind-api:8000`). For
an https URL behind an internal CA, also set `HIVEMIND_PORTAL_API_CA_BUNDLE`
to a mounted PEM file. Like the panel it holds **no DSN and no Secret**,
never talks to Postgres, and the entrypoint skips the migration step for
it. Upgrade `hivemind-api` first, or together: an older API answers 404 to
the portal's own routes.

**Deploying it.** Its own kustomization, on the same image:

```bash
kubectl kustomize deploy/kubernetes/portal \
  | sed "s|hivemind:0.0.0|$IMAGE|g" | kubectl -n hivemind apply -f -
```

The in-repo `.gitlab-ci.yml` deploy job runs exactly this as step 6. Two
replicas with a `minAvailable: 1` PodDisruptionBudget and a
`maxUnavailable: 0` rollout, as for the API (ADR 0026), because people
other than the operator use it. It is stateless: the key lives in the
browser tab, so any replica serves any request.

**Reaching it.** Behind an **internal** ingress only, never a public one:
an agent key is a credential, and the portal shows everything the key can
read. `deploy/kubernetes/portal/ingress-internal.example.yaml` is a
starting point that no kustomization applies. Before applying it with
`kubectl -n hivemind apply -f`, set its `ingressClassName` to your
internal ingress class, replace the placeholder host and TLS secret, and
replace the `127.0.0.1/32` source range (which admits nobody) with your
internal network's ranges. Serve it over TLS: the browser sends the agent
key on every request. With the example NetworkPolicies (§2 step 6), edit
the `hivemind-portal` policy's namespace when the internal controller
runs somewhere other than `ingress-nginx`. For a quick look:

```bash
kubectl -n hivemind port-forward svc/hivemind-portal 8080:8080
# then open http://localhost:8080 and sign in with an agent key
```

**Which key.** The agent's own key, the one its harness uses (from
activation or `hivemind-keys issue-agent`). The portal keeps it in the
tab's `sessionStorage` under its own name, separate from the admin
panel's, and holds one key at a time: to look at another agent, sign out
and sign in with that agent's key.

**Locally:** `make up` (portal on `:8090`) or `make api` + `make portal`
(`:8081`) — the docker story is in [docs/ops-runbook.md](docs/ops-runbook.md)
§1 and the root [README](README.md), "Try it on your computer".

**What it forwards.** `GET /v1/whoami`, `GET /v1/entries/{id}` (the
drawer, with `?history=true`), `GET /v1/pins`, `GET /v1/health` and the
portal's own routes: `GET /v1/portal/entries`, `POST /v1/portal/search`,
`GET /v1/portal/feedback`, `GET /v1/portal/flagged`,
`POST /v1/portal/entries/{id}/withdraw`,
`PUT`/`DELETE /v1/portal/entries/{id}/pin`, `POST /v1/portal/cleanup`,
`GET /v1/portal/cleanups`, `POST /v1/portal/cleanups/{id}/undo` and
`POST /v1/portal/key/lock`. Anything else, the data plane's writes and
its counted search included, is a 404 from the portal itself. Headers,
caching and logging are as for the panel: `X-API-Key`, `Content-Type`
and `Accept` only, `Cache-Control: no-store`, no header or body logged,
and `GET /healthz` as its own probe. The portal serves the panel's shared
front-end modules (ADR 0091) by exact name and none of its other files.
