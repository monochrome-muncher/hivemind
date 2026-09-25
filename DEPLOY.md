# Hivemind — Production Deployment (Kubernetes + GitLab CI/CD)

Single source of truth for **deploying Hivemind to a Kubernetes cluster**
via GitLab CI/CD (ADR 0026: **2 replicas** of the API and 2 of the MCP
runner — for availability, not throughput — against **one** Postgres
node, which ADR 0007 decided and ADR 0026 leaves unchanged). The
single-node **docker** ops story lives in
[docs/ops-runbook.md](docs/ops-runbook.md) — this file is the k8s story
and the two do not overlap.

Layout:

- [`deploy/kubernetes/`](deploy/kubernetes/) — the kustomize tree
  (namespace, ConfigMap, two 2-replica Deployments with a
  `maxSurge: 1` / `maxUnavailable: 0` rollout, two ClusterIP Services,
  two `minAvailable: 1` PodDisruptionBudgets — ADR 0026;
  `secret-template.yaml` documents the CI-rendered Secret;
  `optional/` is the opt-in nginx Ingress + cert-manager Certificate)
- [`.gitlab-ci.yml`](.gitlab-ci.yml) — the pipeline (test → build → deploy)

---

## 1. Prerequisites

What the **org provides** (the app itself needs nothing else):

| Prerequisite | Detail |
|---|---|
| Postgres 16+ with the `vector` extension (pgvector) | A dedicated database + user whose credentials allow `CREATE EXTENSION vector` (DB user is a superuser, or the extension is pre-installed). |
| A vLLM (or any OpenAI-compatible) **embedding** endpoint reachable from the cluster | e.g. an in-cluster vLLM Service (`http://vllm:8000/v1`); any OpenAI-compatible endpoint works (ADR 0005). |
| (Optional) an OpenAI-compatible **chat** endpoint for entity extraction | ADR 0016 — optional + best-effort. **Off by default**: an empty `HIVEMIND_EXTRACTOR_ENDPOINT` + empty key = zero LLM cost, entries land with empty `entities`. |
| GitLab: the **Kubernetes agent pre-configured** | The deploy job just runs `kubectl` — no kubeconfig wiring in `.gitlab-ci.yml`. |
| GitLab: the CI/CD variables below | See the table. |

**Required GitLab CI/CD variables:**

| Variable | Masked? | Meaning |
|---|---|---|
| `K8S_IMAGE` | no | Registry image prefix, e.g. `registry.gitlab.com/<group>/<project>/hivemind`. The pipeline builds/pushes `$K8S_IMAGE:$CI_COMMIT_SHORT_SHA` and the deploy job substitutes it for the `hivemind:0.0.0` placeholder in the manifests. |
| `K8S_SECRET_DATABASE_URL` | **yes** | The Postgres DSN, e.g. `postgresql://hivemind:hivemind@pg.hivemind.svc:5432/hivemind`. |
| `K8S_SECRET_EMBEDDING_API_KEY` | **yes** | The embedding credential. **May be empty** (self-hosted vLLM without auth). |
| `K8S_SECRET_EXTRACTOR_API_KEY` | **yes** | The extractor credential. **May be empty** (extraction off, ADR 0016 — safe). |

All three `K8S_SECRET_*` values are rendered by the deploy job into the
`hivemind-secrets` Secret (the in-tree
[`deploy/kubernetes/secret-template.yaml`](deploy/kubernetes/secret-template.yaml)
documents the shape — it is deliberately NOT in the kustomization, so CI
is the single source of truth for secrets).

---

## 2. Quickstart

The 5-step operator flow:

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
   §2 of `.gitlab-ci.yml`).
4. **Fetch the generated keys** and hand them out (the raw keys live
   ONLY in the k8s Secret `hivemind-keys` — GitLab stays key-free):

   ```bash
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ADMIN_KEY}' | base64 -d
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ORG_KEY}'   | base64 -d
   ```

   Distribution: **admin key** → operators (admin surface + key
   management); **org key** → the registration surface (`hive_register`
   / `POST /v1/agents`); **agent keys** are issued per agent, either via
   `hivemind-keys issue-agent --name <agent>`, the admin REST surface
   `POST /v1/admin/agents/{name}/activate` (returns the key **once**), or
   the **admin panel** (§8), which the same pipeline deploys.
5. **(Optional)** external reachability — apply the opt-in Ingress:

   ```bash
   kubectl kustomize build deploy/kubernetes/optional | kubectl -n hivemind apply -f -
   ```

   (after setting the real hostname + issuer in
   `deploy/kubernetes/optional/ingress.yaml`).

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
- Rollouts (`kubectl rollout status` on both Deployments).

---

## 4. Key & credential rotation reference

Rotate **one credential at a time** (no double-rotation — verify each
rotation before starting the next; two simultaneous rotations make a
failure undiagnosable).

Every `hivemind-keys` rotation below is recorded in the audit log
(ADR 0027, SPEC §12.5) under the name you pass as `--actor` (before the
subcommand; default: the OS user — `root` in the image, so pass it):
`hivemind-keys --actor <you> rotate-org`. The value is unverified, which
is why those rows are marked `actor_kind = cli`. Check what happened with
`GET /v1/admin/audit-log` (admin key; `docs/ops-runbook.md` §4). The
first-run bootstrap Job records itself as `ci-bootstrap`.

| Credential | Rotate via | When |
|---|---|---|
| Org key | `hivemind-keys rotate-org` | suspected org-key leak; periodic hardening |
| Admin key | `hivemind-keys issue-admin` + `hivemind-keys revoke-admin` | suspected admin-key leak; operator turnover |
| Agent key | `hivemind-keys revoke --name <agent>` | agent compromise; key leak |
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
  distribute → `hivemind-keys revoke-admin` (the CLI subcommand shipped
  with this release; accepts the old raw key or its SHA-256 hash from
  `hivemind-keys list`). Until it is available in your image, the
  equivalent SQL is:
  `DELETE FROM credentials WHERE kind='admin' AND key_hash='<sha256>';`
- **Blast radius** — only the admin surface (fleet/level management,
  key rotation) loses the old key; data-plane verbs are unaffected.
- **Verify** — admin call with the new key works; the old key 401s.

### 4.3 Agent keys

- **How** — `hivemind-keys revoke --name <agent>` (the agent becomes
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
- **Deploy every runner project together whenever a migration lands.**
  The runners are separate deployments (and, under GitLab AutoDevOps,
  separate projects) sharing one pool, so a migration from one runs
  against the other's still-deployed code. Releases that only change
  config need not be synchronised. Schema changes follow
  **expand-and-contract** (SPEC §8.6): additive within a release, and a
  removal split across two.
- **Derived images add layers only — never an `ENTRYPOINT`.** An
  air-gapped high-side image built `FROM` this one (e.g. to install
  custom CA certificates) inherits the migrations and the entrypoint.
  Setting its own `ENTRYPOINT` or `CMD` **silently skips the migration
  pre-step** — the same trap as the key-bootstrap Job below, but with no
  error until the first query hits a missing object.
- A **dim mismatch is a LOUD failure at pod startup** (ADR 0015): the
  entrypoint's `hivemind-migrate` step exits non-zero naming both dims
  and both fixes — the pod never starts, never a silent no-op. The check
  runs *before* the advisory lock is taken.
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
    --env=HIVEMIND_RUNNER=migrate \
    --env-from=secret/hivemind-secrets
  ```

### Graceful shutdown (`terminationGracePeriodSeconds`)

Both Deployments set **`terminationGracePeriodSeconds: 150`** plus a
**5-second `preStop` sleep**. With more than one replica a rolling
deploy terminates pods routinely, so these are ordinary-path settings,
not edge-case insurance.

- **What the grace period actually governs.** uvicorn handles SIGTERM
  itself — stop accepting, drain the in-flight requests, *then* run the
  lifespan shutdown. Nothing sets uvicorn's
  `timeout_graceful_shutdown`, so its own wait is **unbounded**:
  `terminationGracePeriodSeconds` is the only real deadline, and when
  it expires the container is SIGKILLed with whatever is still in
  flight.
- **Where 150 comes from.** The slowest single request is a write
  (`hive_write` / `POST /v1/entries`): `WriteService.write` embeds and
  **then** extracts, in sequence, each with its own bounded retry
  budget (ADR 0014).

  | leg | attempts | timeout | backoff | worst case |
  |---|---|---|---|---|
  | embedder | 1 + `HIVEMIND_EMBEDDING_RETRIES` (2) = 3 | `HIVEMIND_EMBEDDING_TIMEOUT` (10s) | 0.5s + 1.0s | 31.5s |
  | extractor | 1 + `HIVEMIND_EXTRACTOR_RETRIES` (2) = 3 | `HIVEMIND_EXTRACTOR_TIMEOUT` (30s) | 0.5s + 1.0s | 91.5s |
  | **worst-case write path** | | | | **123.0s** |

  Plus the 5s `preStop` sleep and two pooled Postgres round-trips (auth
  verify + INSERT), rounded up to **150s** for margin — httpx applies
  its timeout **per I/O phase** (connect / read / write), so a single
  attempt can overshoot it. Reads (`hive_search`, `GET /v1/entries`)
  embed the query only and never extract, so they finish well inside
  the embedder's 31.5s.
- **If you raise a retry budget, raise this.** Each leg costs
  `(retries + 1) x timeout + 0.5 x (2^retries - 1)` seconds; sum the two
  legs, add the 5s `preStop`, then round up. Worked example: setting
  `HIVEMIND_EXTRACTOR_RETRIES=4` makes the extractor leg
  `5 x 30 + 0.5 x 15 = 157.5s` and the write path `189s`, so the grace
  period needs to go to **~240s** in *both*
  `deploy/kubernetes/*-deployment.yaml`. The same applies to
  `HIVEMIND_EXTRACTOR_TIMEOUT`, `HIVEMIND_EMBEDDING_RETRIES` and
  `HIVEMIND_EMBEDDING_TIMEOUT`. Leaving it stale is not a crash — it is
  a SIGKILL that drops a write the caller was told nothing about.
- **Why `preStop` as well.** On pod deletion the kubelet's SIGTERM and
  the EndpointSlice removal happen **concurrently**, so for as long as
  that removal takes to reach kube-proxy — and the nginx ingress
  controller, which routes straight to pod IPs, bypassing the Service —
  a terminating pod can still be handed **new** requests, which it would
  refuse because uvicorn has already closed its listener. The sleep lets
  deregistration win the race; the process serves normally throughout.
  It is `/bin/sleep` from the image's `python:3.14-slim` base, not the
  native `sleep` lifecycle handler (GA only in k8s 1.30+), so it works
  on older clusters.
- **What this deliberately does NOT do.** The MCP transport is
  **stateless** streamable-HTTP (ADR 0010, SPEC §8.5: one request = one
  self-contained exchange; the server holds no session registry), so a
  terminating pod's blast radius is at most **one in-flight tool call**,
  never a client session. There is no session-draining step and none is
  needed. The `PgStore` pool is likewise not closed on a shutdown path —
  it dies with the process, *after* uvicorn has drained — so an
  in-flight request can never outlive its own connection pool. Nothing
  is left half-written either: entries are immutable (ADR 0001), so a
  severed write simply does not land.
- **Dev compose gets a smaller version of the same setting.** The
  `mcp-http` compose service sets `stop_grace_period: 35s` (compose's
  default is 10s — shorter than a *single* embedder attempt). It runs
  with extraction off, so its worst case is the embedder leg alone.

### Probe endpoints (ADR 0019)

Four **unauthenticated** orchestrator endpoints (served outside the
auth middleware — k8s probes carry no credential):

| Endpoint | Depth | Semantics |
|---|---|---|
| `GET /mcp/liveness` (mcp runner) | shallow | 200 whenever the process answers |
| `GET /mcp/health` (mcp runner) | deep | 200 only when the Postgres pool answers; else 503 (a transient DB outage marks the pod NotReady without restarting it) |
| `GET /v1/liveness` (REST API) | shallow | 200 whenever the process answers |
| `GET /v1/health` (REST API) | static | unchanged — static 200, public by design (SPEC §5.1); the REST surface intentionally has no deep DB probe in v1 |

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 on every agent call | an active agent: its agent key was revoked or mistyped. A not-yet-activated agent: a stale org key (after a rotation that wasn't distributed) | active: re-activate it for a new key (§4.3); pending: re-fetch `ORG_KEY` from the `hivemind-keys` Secret (§4.1) |
| app container CrashLooping on the entrypoint's migrate pre-step | dim mismatch (ADR 0015) — the log names both dims + both fixes | `make pg-reset` equivalent (fresh pool) or set `HIVEMIND_EMBEDDING_DIM` to the pool's dim |
| container fails immediately with `Unknown HIVEMIND_* environment variable(s)` | a typo'd or stale `HIVEMIND_*` var (ADR 0024) — e.g. a renamed knob (ADR 0021's `_PREFIX_CHARS` → `_PREFIX_TOKENS`) left set under its old name | fix/remove the named variable per the error's suggestion, or add it to `_ALLOWED_EXTRA_ENV_VARS` in `src/hivemind/config.py` if it's genuinely read outside `Settings` |
| MCP session drops after ~60s | nginx default `proxy-read-timeout` killing the SSE stream (classic pitfall) | apply the opt-in Ingress with the SSE annotations (`deploy/kubernetes/optional/ingress.yaml`) |
| entries have empty `entities` | extractor off (empty endpoint — by design) OR extractor dead (best-effort, ADR 0016) | check `HIVEMIND_EXTRACTOR_ENDPOINT` + `K8S_SECRET_EXTRACTOR_API_KEY`; §4.6 |
| writes failing with `EmbeddingError` | embedder dead (ADR 0014 retry budget exhausted) | §4.5 |
| a write dropped mid-flight during a deploy (client sees a reset, no entry lands) | `terminationGracePeriodSeconds` no longer covers the retry budgets — the pod was SIGKILLed while still draining | recompute the sum in §5 and raise it in both Deployments |
| `hivemind-secrets` missing values | CI variable changed but no redeploy yet | re-run the pipeline (the deploy job re-renders the Secret every deploy) |

**Two-Secret ownership rule (do not cross the boundary):** CI owns
`hivemind-secrets` (the three `K8S_SECRET_*` values, re-rendered on
every deploy); the first-run bootstrap owns `hivemind-keys` (the
generated ADMIN_KEY / ORG_KEY). The bootstrap step in `.gitlab-ci.yml`
deliberately skips when `hivemind-keys` exists, so a secret re-render
never wipes the generated keys.

---

## 7. RBAC

Minimal ServiceAccount + Role for the GitLab Kubernetes agent's deploy
ServiceAccount (the permissions referenced by the comment in
`.gitlab-ci.yml`). The org's k8s agent must run as this ServiceAccount
(SA token or mounted kubeconfig).

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
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "patch", "apply"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "patch", "apply"]
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
```

## 8. Admin panel (`hivemind-admin`, ADR 0029)

A browser front end for the admin surface: the pending-agent queue
(grouped by owner alias), activation with the key shown once, agents,
fleets, the audit log, and org-key rotation. Admin keys stay CLI-only.

**What it needs.** One env var: `HIVEMIND_ADMIN_API_URL`, the base URL of
the hivemind-api deployment (in-cluster: `http://hivemind-api:8000`). For
an https URL behind an internal CA, also set `HIVEMIND_ADMIN_API_CA_BUNDLE`
to a mounted PEM file. It holds **no DSN and no Secret**, never talks to
Postgres, and the entrypoint skips the migration step for it.

**Deploying it.** It is its own kustomization, so it works as a separate
GitLab project on the same image:

```bash
kubectl kustomize build deploy/kubernetes/admin \
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

**Locally:** `make api` in one shell, `make admin` in another, then open
`http://localhost:8080`.

**What it forwards.** Only the admin endpoints, `GET /v1/metrics` and
`GET /v1/health`. Anything else is a 404 from the panel itself. It
forwards `X-API-Key`, `Content-Type` and `Accept` and nothing else, marks
every forwarded response `Cache-Control: no-store`, and never logs a
header or a body. `GET /healthz` is its own probe and does not call the
API.

