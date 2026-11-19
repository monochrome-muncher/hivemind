# Deploying Hivemind on Kubernetes

This guide deploys Hivemind to a Kubernetes cluster from GitLab CI/CD. You
get **2 replicas** (copies) each of the API, the MCP runner and the agent
portal, so a pod can restart without an outage, and one replica of the
admin panel. They all use **one** Postgres node (ADRs 0026 and 0007).
Without GitLab, you run the same steps by hand
([Without GitLab](#without-gitlab)). Running Hivemind on one machine with
Docker Compose is in [docs/ops-runbook.md](docs/ops-runbook.md), which
also has the step-by-step admin tasks (merging fleets, cleaning up after
an agent, offboarding) that apply here too.

Numbers like "ADR 0026" point to an architecture decision record in
[docs/adr/](docs/adr/README.md), which explains why a choice was made.

**Contents:** [1. Prerequisites](#1-prerequisites) ·
[2. Quickstart](#2-quickstart) ·
[3. Manual steps checklist](#3-manual-steps-checklist) ·
[4. Key and credential rotation](#4-key--credential-rotation-reference) ·
[5. Upgrades](#5-upgrades) ·
[6. Troubleshooting](#6-troubleshooting) ·
[7. RBAC](#7-rbac) ·
[8. Admin panel](#8-admin-panel-hivemind-admin) ·
[9. Agent portal](#9-agent-portal-hivemind-portal) ·
[10. Monitoring](#10-monitoring)

Layout:

- [`deploy/kubernetes/`](deploy/kubernetes/): the kustomize tree. It has
  the namespace, the ConfigMap, two 2-replica Deployments with a
  `maxSurge: 1` / `maxUnavailable: 0` rollout, two ClusterIP Services,
  two `minAvailable: 1` PodDisruptionBudgets, and `secret-template.yaml`,
  which documents the Secret that CI renders. Every pod runs as non-root
  with a read-only root filesystem and a locked-down security context.
  The subfolders are:
  - `optional/` is the opt-in nginx Ingress and cert-manager Certificate.
    Under it, `optional/networkpolicy/` holds the NetworkPolicies (§2
    step 6) and `optional/monitoring/` holds the Prometheus Operator
    ServiceMonitors, the vital-signs alerts and the Grafana dashboard
    (§10).
  - `admin/` and `portal/` are the admin panel and the agent portal. Each
    is its own kustomization (§8 and §9).

  (ADR 0026 explains the replica and PodDisruptionBudget choices.)
- [`.gitlab-ci.yml`](.gitlab-ci.yml): the pipeline (test, build, deploy).
  The first-run key bootstrap is
  [`scripts/ci-bootstrap-keys.sh`](scripts/ci-bootstrap-keys.sh) with
  [`deploy/kubernetes/bootstrap/`](deploy/kubernetes/bootstrap/) (ADR 0044).

---

## 1. Prerequisites

Your organization provides these. The app itself needs nothing else.

| Prerequisite | Detail |
|---|---|
| Postgres 16+ with the `vector` extension (pgvector **>= 0.8**) | A dedicated database and user. The credentials must allow `CREATE EXTENSION vector`: either the DB user is a superuser, or the extension is already installed. You need pgvector 0.8 or newer (it provides `hnsw.iterative_scan`), and `HIVEMIND_EMBEDDING_DIM` must be **<= 2000**, which is the limit of the HNSW index. Otherwise `hivemind-migrate` fails with a message that says what to do. On an older pgvector, upgrade the package or image, then run `ALTER EXTENSION vector UPDATE;`. (ADR 0025) |
| A vLLM (or any OpenAI-compatible) **embedding** endpoint reachable from the cluster | For example an in-cluster vLLM Service (`http://vllm:8000/v1`). Any OpenAI-compatible endpoint works. (ADR 0005) |
| (Optional) an OpenAI-compatible **chat** endpoint for entity extraction | Optional, and best effort. It is **off by default**: an empty `HIVEMIND_EXTRACTOR_ENDPOINT` and an empty key cost nothing, and entries land with empty `entities`. (ADR 0016) |
| GitLab: the **Kubernetes agent pre-configured** | The deploy job just runs `kubectl`. There is no kubeconfig wiring in `.gitlab-ci.yml`. |
| GitLab: the CI/CD variables below | See the table. |

**Required GitLab CI/CD variables:**

| Variable | Masked? / Protected? | Meaning |
|---|---|---|
| `K8S_IMAGE` | no / no | The registry image prefix, for example `registry.gitlab.com/<group>/<project>/hivemind`. The pipeline builds and pushes `$K8S_IMAGE:$CI_COMMIT_SHORT_SHA`, and the deploy job puts it in place of the `hivemind:0.0.0` placeholder in the manifests. |
| `K8S_SECRET_DATABASE_URL` | **yes / yes** | The Postgres DSN (connection string), for example `postgresql://hivemind:hivemind@pg.hivemind.svc:5432/hivemind`. |
| `K8S_SECRET_EMBEDDING_API_KEY` | **yes / yes** | The embedding credential. **It may be empty** (a self-hosted vLLM without auth). |
| `K8S_SECRET_EXTRACTOR_API_KEY` | **yes / yes** | The extractor credential. **It may be empty**, which turns extraction off and is safe. (ADR 0016) |
| `K8S_SECRET_MIGRATE_DATABASE_URL` | **yes / yes** (optional) | A direct Postgres DSN for `hivemind-migrate` only. You need it when `K8S_SECRET_DATABASE_URL` points at a transaction-mode PgBouncer, because the migration's session advisory lock cannot live there (see `docs/ops-runbook.md`). If you leave it unset, the migration runs through `K8S_SECRET_DATABASE_URL`. |

**Mark every `K8S_SECRET_*` variable _Protected_ as well as Masked,
and protect the default branch and the `production` environment.** An
unprotected variable is injected into every pipeline, including
merge-request pipelines from unprotected branches. A merge request author
controls `.gitlab-ci.yml` itself, so the `rules:` that keep `deploy` on the
default branch are no barrier, and masking is easy to evade. A protected
variable is only exposed to protected branches and tags. (The variables
that may be empty still need to exist, empty, for the same reason.)

The deploy job renders all the `K8S_SECRET_*` values (three, plus the
optional migrate DSN) into the `hivemind-secrets` Secret. The in-tree
[`deploy/kubernetes/secret-template.yaml`](deploy/kubernetes/secret-template.yaml)
documents its shape. It is deliberately not in the kustomization, so CI is
the only place that creates this Secret.

---

## 2. Quickstart

Before the first deploy, point
[`deploy/kubernetes/configmap.yaml`](deploy/kubernetes/configmap.yaml) at
your embedding service: `HIVEMIND_EMBEDDING_ENDPOINT`,
`HIVEMIND_EMBEDDING_MODEL` and `HIVEMIND_EMBEDDING_DIM`. The shipped values
assume an in-cluster vLLM Service named `vllm` that serves
Qwen3-Embedding-0.6B at 1024 dimensions. If you use entity extraction, also
set `HIVEMIND_EXTRACTOR_ENDPOINT` and `HIVEMIND_EXTRACTOR_MODEL`.
The first migration fixes the dimension (§5). The other values are the
ranking defaults. Leave them unless an ADR says otherwise.

The operator flow has eight steps. Steps 1 to 4 are required, and steps 5
to 8 are optional:

1. **Local dev.** Run `cp config/.env.example .env.local`. The master
   template is `config/.env.example`. The profile files (`.env.local`,
   `.env.staging`, `.env.production`, `.env.test`) are selected by the
   `ENVIRONMENT` env var, and real environment variables always win over
   file values (ADR 0017). Production on Kubernetes never reads a profile
   file: the values come from the ConfigMap and the Secret.
2. **Set the three masked GitLab variables** (`K8S_SECRET_DATABASE_URL`,
   `K8S_SECRET_EMBEDDING_API_KEY`, `K8S_SECRET_EXTRACTOR_API_KEY`) and the
   `K8S_IMAGE` variable. Behind a transaction-mode PgBouncer, also set
   `K8S_SECRET_MIGRATE_DATABASE_URL`. In GitLab, go to Settings > CI/CD >
   Variables.
3. **First `git push`.** The pipeline builds the image, deploys it and
   **boots the keys automatically**. The deploy job runs an idempotent
   first-run bootstrap, `scripts/ci-bootstrap-keys.sh`. It starts a
   one-shot Job under its own ServiceAccount
   (`deploy/kubernetes/bootstrap/rbac.yaml`: create Secrets, get and delete
   `hivemind-keys`). The Job runs `hivemind-keys bootstrap-secret`, which
   issues the admin key, rotates the org key and writes both straight into
   the `hivemind-keys` Secret. No key is printed, so the Job log and your
   log shipper never hold one. The keys are committed only after the Secret
   was created, so a failed run leaves nothing behind. Fix the cause (the
   script prints the Job log) and re-run the pipeline. There is one
   exception: if the Job log says the commit failed and the Secret could
   not be deleted, follow its steps and delete that Secret first.
   Otherwise the re-run sees the Secret, skips the bootstrap and goes green
   with keys that do not work. The script deletes the Job and the RBAC
   objects when it is done, so the deploy identity needs permission to
   create that Role and RoleBinding. If you bootstrapped before the
   bootstrap wrote the Secret directly, the old Job printed the keys. Treat
   your log store as key-bearing, or rotate both keys (§4). Why it works
   this way: ADR 0044.
4. **Fetch the generated keys** and hand them out. The raw keys live only
   in the Kubernetes Secret `hivemind-keys`. GitLab stays key-free.

   ```bash
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ADMIN_KEY}' | base64 -d
   kubectl -n hivemind get secret hivemind-keys -o jsonpath='{.data.ORG_KEY}'   | base64 -d
   ```

   Hand them out like this:

   - The **admin key** goes to operators (the admin surface and key
     management).
   - The **org key** goes to the registration surface (`hive_register` or
     `POST /v1/agents`).
   - **Agent keys** are issued per agent. You have three ways to do it.
     You can run `hivemind-keys issue-agent --name <agent>`, which gives
     you an active agent with no key; for a pending or revoked agent, add
     `--trust-level N --home-fleet <fleet-id>` (ADR 0039). You can call
     the admin REST endpoint `POST /v1/admin/agents/{name}/activate`, which
     returns the key **once**. Or you can use the **admin panel** (§8),
     which the same pipeline deploys.
   - For an owner who has not registered, the admin can create the agent
     directly. Use **New agent** on the panel's Agents page, or
     `POST /v1/admin/agents` (owner alias, agent name, trust level, home
     fleet). The key is shown **once** (ADR 0120).
5. **(Optional)** External reachability. Apply the opt-in Ingress:

   ```bash
   kubectl kustomize deploy/kubernetes/optional | kubectl -n hivemind apply -f -
   ```

   First set the real hostname and issuer in
   `deploy/kubernetes/optional/ingress.yaml`. The tree has a second
   Ingress, `hivemind-admin-api`. It keeps three things off the internet:
   the admin REST surface (`/v1/admin/*` and the admin-only
   `/v1/metrics`), the agent portal's routes (`/v1/portal/*`, which the
   portal calls in-cluster, §9) and the unauthenticated Prometheus target
   (`/metrics`; scrape the pods directly). It answers 403 to every source
   outside its `allowlist-source-range` (default `127.0.0.1/32`, which
   admits nobody). Operators use the admin panel (§8) or
   `kubectl port-forward`. To admit an operator network, put its CIDRs in
   both source-range annotations. The main Ingress rate-limits each client
   IP (20 req/s with a burst of 5x, and 100 open connections; nginx
   answers 503 above that). Raise the limits if many agents share one
   egress IP. (ADR 0050 covers the `/metrics` target.)
6. **(Optional)** Example NetworkPolicies, in a separate tree,
   `deploy/kubernetes/optional/networkpolicy/`. **Edit the namespace
   selector first.** It allows ingress only from the namespace
   `ingress-nginx`, so with any other controller namespace the policies
   cut external traffic off. You need a CNI (the cluster's network plugin)
   that enforces NetworkPolicy. Apply them with
   `kubectl kustomize deploy/kubernetes/optional/networkpolicy | kubectl -n hivemind apply -f -`.
   The stricter **default-deny egress** policy is its own tree,
   `deploy/kubernetes/optional/networkpolicy/egress/`. It lets the api,
   mcp, admin and portal pods and the re-extraction Job reach only DNS,
   Postgres, the embedding and extractor endpoints, and `hivemind-api`
   (the panel and the portal need it; the rule admits all four, which is
   harmless). Replace its placeholder `192.0.2.x` addresses with yours
   first. Unedited, it fails closed and the pods cannot reach Postgres.
7. **(Optional)** Prometheus scraping. The `hivemind-api` and
   `hivemind-mcp` pods serve `GET /metrics` with no key, and Prometheus
   must scrape them from inside the cluster. See [§10](#10-monitoring).
8. **(Optional)** The Grafana dashboard and the `hivemind-vital-signs`
   alerts, both shipped in `deploy/kubernetes/optional/monitoring/`. See
   [§10](#10-monitoring).

---

## 3. Manual steps checklist

**Only a person can do these** (everything else is automatic):

1. Provision the Postgres database and pgvector (one time).
2. Stand up the embedding endpoint (and, optionally, the chat endpoint
   for extraction) so the cluster can reach it. Put its address,
   model and dimension in `deploy/kubernetes/configmap.yaml` (§2).
3. Set the four GitLab CI/CD variables (§1).
4. Run the **first pipeline** (`git push`), then:
5. Fetch `ADMIN_KEY` and `ORG_KEY` from the `hivemind-keys` Secret and
   distribute them (§2 step 4).
6. (Optional) Apply the opt-in Ingress with the real hostname and issuer.
7. (Optional) Apply the NetworkPolicies (§2 step 6), and set up scraping,
   the Grafana dashboard and the alerts (§10).

**What is automatic** (never do these by hand):

- Schema migration. The entrypoint runs `hivemind-migrate` as a pre-step on
  every pod start, under an advisory lock, so concurrent replicas are safe.
  A dimension mismatch fails right away and says so (ADRs 0018, 0020 and
  0015).
- Image build and push (the build stage).
- Rendering of `hivemind-secrets` from the `K8S_SECRET_*` variables (the
  deploy job, on every deploy).
- First-run key bootstrap (the deploy job, **only** when `hivemind-keys`
  does not exist).
- Rollouts (`kubectl rollout status` on the api, mcp, admin and portal
  Deployments).

### Without GitLab

The deploy job in [`.gitlab-ci.yml`](.gitlab-ci.yml) is a short list of
`kubectl` commands, so any CI system, or a person with `kubectl` access
and the RBAC in §7, can run the same steps. Use the published image
instead of building one, and run the commands from a checkout of the same
release tag, so the manifests match the image:

```bash
git checkout v<version>
export IMAGE=ghcr.io/monochrome-muncher/hivemind:v<version>

# Once: the namespace (§7), then the ConfigMap you edited (§2).
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl -n hivemind apply -f deploy/kubernetes/configmap.yaml

# The app Secret. Keep the values in a private file outside the checkout
# (chmod 600), one KEY=value per line: HIVEMIND_DATABASE_URL, HIVEMIND_EMBEDDING_API_KEY,
# HIVEMIND_EXTRACTOR_API_KEY and HIVEMIND_MIGRATE_DATABASE_URL (the last
# three may be empty).
kubectl -n hivemind create secret generic hivemind-secrets \
  --from-env-file=$HOME/hivemind-secrets.env --dry-run=client -o yaml \
  | kubectl -n hivemind apply -f -

# First run only (it skips itself once the hivemind-keys Secret exists).
sh scripts/ci-bootstrap-keys.sh

# The app, the admin panel and the portal.
for dir in deploy/kubernetes deploy/kubernetes/admin deploy/kubernetes/portal; do
  kubectl kustomize "$dir" | sed "s|hivemind:0.0.0|$IMAGE|g" | kubectl -n hivemind apply -f -
done
```

Then fetch the keys as in §2 step 4. To upgrade, check out the new tag,
set `IMAGE` to it and run the last command again.

---

## 4. Key & credential rotation reference

Rotate **one credential at a time**. Verify each rotation before you start
the next. Two rotations at once make a failure impossible to diagnose.

Every `hivemind-keys` rotation below is recorded in the audit log, under
the name you pass as `--actor`. Put it before the subcommand. The default
is the OS user, which is `hivemind` in the image, so pass your own name:
`hivemind-keys --actor <you> rotate-org`. The value is unverified, which
is why those rows are marked `actor_kind = cli`. Check what happened with
`GET /v1/admin/audit-log` (admin key; `docs/ops-runbook.md` §4). The
first-run bootstrap Job records itself as `ci-bootstrap`. (ADR 0027,
SPEC §12.5.)

| Credential | Rotate via | When |
|---|---|---|
| Org key | `hivemind-keys rotate-org` | suspected org-key leak; periodic hardening |
| Admin key | `hivemind-keys issue-admin` + `hivemind-keys revoke-admin` | suspected admin-key leak; operator turnover |
| Agent key | `hivemind-keys rotate-agent --name <agent>` (replaces the key, the agent stays active) or `hivemind-keys revoke --name <agent>` | agent compromise; key leak; owner lost the key |
| DSN (DB password) | change the password at the Postgres provider | suspected DSN leak; provider policy |
| Embedding API key | rotate at the embedding provider | provider policy; suspected leak |
| Extractor API key | rotate at the chat provider | provider policy; suspected leak |

### 4.1 Org key

- **How:** run `hivemind-keys rotate-org`. It deletes and inserts in one
  step, and it closes registration to every earlier copy of the key. The
  new raw key is printed **once**. (ADR 0031)
- **Then:** store it. Update the Kubernetes Secret `hivemind-keys`
  (`kubectl -n hivemind patch secret hivemind-keys -p
  '{"stringData":{"ORG_KEY":"<new>"}}'`). Or delete the Secret and re-run
  the bootstrap (§2 step 3), which also issues a new admin key.
- **Blast radius:** only registration. Anyone still holding the old
  org key gets a 401 on their **next** registration attempt. Active agents
  authenticate with their own agent keys and are **not** affected. No
  data is affected.
- **Verify:** a registration call with the new org key succeeds, and the
  old key now gets a 401.

### 4.2 Admin key

- **How:** run `hivemind-keys issue-admin`, which prints the new key once.
  Distribute the new key. Then run `hivemind-keys --actor <you>
  revoke-admin --key <old raw key>` or `--hash <sha256 from hivemind-keys
  list>`. Exactly one of the two is required, and a unique fingerprint
  prefix of at least 12 hex characters is accepted. Always use the CLI: it
  records the revocation in the audit log, which raw SQL against
  `credentials` would bypass. (ADR 0027)
- **Blast radius:** only the admin surface (fleet and level management,
  key rotation) loses the old key. The data-plane verbs are unaffected.
- **Verify:** an admin call with the new key works, and the old key gets a
  401.

### 4.3 Agent keys

- **How, replacing the key:** run `hivemind-keys rotate-agent --name
  <agent>` (or use "Replace key" in the admin panel, or
  `POST /v1/admin/agents/{name}/key`). The old key stops working on the
  agent's next request, and the new one is shown **once**. The agent
  stays `active` with the same level, fleet and memories. Give the new
  key to the owner out of band. This works only for an `active` agent.
  (ADR 0108)
- **How, cutting the agent off:** run `hivemind-keys revoke --name
  <agent>`. The agent becomes `revoked` and the name **stays reserved**.
  Re-activation issues a NEW key: use the admin endpoint
  `POST /v1/admin/agents/{name}/activate` (or the admin panel).
  Activating an agent that is still `active` is refused (409), because
  each agent has one key, so revoke first. (ADRs 0012 and 0028)
- **Blast radius:** only that agent's verbs (its writes, reads and
  feedback).
- **Verify:** the agent's next request gets a 401 with the old key and
  works with the new one.

### 4.4 DSN (`HIVEMIND_DATABASE_URL`)

- **How:** change the DB password at the Postgres provider, then update
  `K8S_SECRET_DATABASE_URL`. The **next deploy** re-renders the Secret and
  restarts the pods one by one. Processes read their environment only at
  start, so the rollout is the step that applies the change.
- **Blast radius:** nothing works with the old DSN, and both Deployments
  roll.
- **Verify:** the rollout completes, `GET /v1/health` returns 200, and
  `hivemind-keys list` succeeds from a pod.
- **Failure mode:** a dead DSN is a **total outage** (all verbs).

### 4.5 Embedding API key / endpoint

- **How:** rotate the key at the provider, then update
  `K8S_SECRET_EMBEDDING_API_KEY`. The next deploy re-renders the Secret
  and restarts the pods one by one.
- **Failure mode:** a dead embedder **makes writes fail** once the retry
  budget is exhausted. You see an `EmbeddingError`, and the entry is **not**
  saved. Embeddings are not best effort, unlike extraction. Reads still
  work (keyword search plus the existing vectors). (ADR 0014)

### 4.6 Extractor API key / endpoint

- **How:** rotate the key at the chat provider, then update
  `K8S_SECRET_EXTRACTOR_API_KEY`. Or leave it empty and also leave the
  endpoint empty, which turns extraction off at zero cost. The next deploy
  re-renders the Secret and restarts the pods one by one.
- **Failure mode:** with a dead extractor, entries land **without entity
  facets**. Extraction is best effort, so the write still succeeds. The
  symptom you can see is **empty `entities`**, not an error. (ADR 0016)
- **Older entries:** after you change the extractor model or prompt, or
  after an outage, `hivemind-reextract` fills in or renames the entities of
  stored entries. See [docs/ops-runbook.md §14](docs/ops-runbook.md) (ADR
  0131).

---

## 5. Upgrades

- A normal deploy is just a new image tag. The entrypoint applies any
  outstanding migrations on every pod start, under a Postgres advisory
  lock, so many replicas may start at once and exactly one migrates. An
  image whose migration chain is **older** than the pool applies nothing
  and never rolls the schema back. (ADRs 0018 and 0020)
- **Does a release carry a migration?** Compare the chain between the two
  tags: `git diff --stat v<old> v<new> -- src/hivemind/store/migrations/`.
  No output means no schema change. Even when there is one, you have no
  manual step, because the entrypoint applies it. What each migration adds
  is in the `Server` column of the release's row in
  [plugins/hivemind/CHANGELOG.md](plugins/hivemind/CHANGELOG.md).
- **Agents pick up server-side changes on their own.** Tool descriptions,
  the MCP server instructions and `hive_whoami` come from the server, so
  upgrading the server updates what every agent sees. The agent plugin's
  skills and hook update separately, per harness (see "Update" in
  `plugins/hivemind/README.md`; what changed for agents in each release is
  in `plugins/hivemind/CHANGELOG.md`).
- **Deploy every runner project together whenever a migration lands.**
  The runners are separate deployments (and, under GitLab AutoDevOps,
  separate projects) that share one pool, so a migration from one runs
  against the other's still-deployed code. Releases that only change
  config need not be synchronised. Schema changes follow
  **expand-and-contract** (SPEC §8.6): additive within a release, and a
  removal split across two.
- **Removed settings are ignored, with a warning.** When a release
  removes a setting, a deployment that still sets it starts normally,
  even with `HIVEMIND_STRICT_ENV=true`, and logs `Removed HIVEMIND_*
  environment variable(s) are ignored` with the names. Delete them from
  the ConfigMap or Secret when convenient. Release 2.12.0 removes
  `HIVEMIND_RECENCY_FROM_CONFIRMATION` and `HIVEMIND_RECENCY_BY_KIND`. Both
  were off by default, so ranking does not change. (ADR 0103)
- **Derived images add layers only, never an `ENTRYPOINT`.** An
  air-gapped high-side image built `FROM` this one (for example to install
  custom CA certificates) inherits the migrations and the entrypoint.
  If it sets its own `ENTRYPOINT` or `CMD`, the migration pre-step is
  **skipped without any message**. This is the same trap as the
  key-bootstrap Job below, and you get no error until the first query hits
  a missing object. The image runs as UID 10001, so a layer that installs
  files system-wide must switch user and back:

  ```dockerfile
  FROM <registry>/hivemind:<sha>
  USER root
  COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
  RUN update-ca-certificates
  USER 10001:10001
  ```
- A **dimension mismatch stops the pod at startup.** The entrypoint's
  `hivemind-migrate` step exits non-zero and names both dimensions and both
  fixes. The pod never starts, so you cannot miss it. The check runs right
  after the advisory lock is taken and before any DDL, so two replicas
  configured with different dims cannot both pass on a fresh pool.
  (ADR 0015)
- Changing the embedding **model or dimension** is an operator migration
  (a new pool, or re-embedding), never a config flip. (ADR 0005)
- To **roll back** an incremental schema change, use the migration's
  `.rollback.sql` through `hivemind-migrate --rollback N`. It
  refuses to erase audit rows, revive revoked keys or unlock locked keys
  unless you pass `--discard-audit-rows`, `--revive-revoked-keys` or
  `--unlock-locked-keys`. Stop the app tier first (`docs/ops-runbook.md`
  §4). Rollbacks are defined for structural changes only. Reversing a
  populated column drop or a backfill is a restore from backup
  (`docs/ops-runbook.md`). Rolling back `0032` does not bring redacted text
  back. (ADRs 0020, 0119 and 0125)
- If the pool lags the deployed chain (for example, pods were added before
  a migration ran), run the migration one-off:

  ```bash
  kubectl -n hivemind run hivemind-migrate --rm -i --restart=Never \
    --image=<registry>/hivemind:<sha> \
    --overrides='{"spec":{"containers":[{"name":"hivemind-migrate","image":"<registry>/hivemind:<sha>","env":[{"name":"HIVEMIND_RUNNER","value":"migrate"}],"envFrom":[{"secretRef":{"name":"hivemind-secrets"}},{"configMapRef":{"name":"hivemind-config"}}]}]}}'
  ```

  (`kubectl run` has no `--env-from`, so the ConfigMap is injected too.
  That way the run sees the real `HIVEMIND_EMBEDDING_DIM`.) It is simpler
  when the Deployments are healthy:
  `kubectl -n hivemind exec deploy/hivemind-api -- hivemind-migrate`.

### Upgrading from 1.2.x to 2.0.0

This is history. Today's upgrades follow the bullets above, and each
release's row in [plugins/hivemind/CHANGELOG.md](plugins/hivemind/CHANGELOG.md)
(the `Server` column) names what operators must do. If you are still
upgrading from 1.2.x, do these before the first 2.0.0 pod starts:

- Find credentials that stop working, using the "Dead credentials" SQL in
  `docs/ops-runbook.md` §4. Pre-v2 `user` keys, name-less agent keys and
  keys of pending or revoked agents no longer authenticate (ADR 0039).
  Re-issue anything a live client still uses.
- Make sure you have pgvector 0.8+ and `HIVEMIND_EMBEDDING_DIM` <= 2000
  (§1).
- Mark the `K8S_SECRET_*` variables Protected and apply the RBAC in §7.

Rolling back to 1.2.x after migrations `0007` and `0008` ran is
schema-safe, but the duplicate keys `0007` deleted stay deleted, and the
1.2.x pods accept the dead credentials again.

### Graceful shutdown (`terminationGracePeriodSeconds`)

Both Deployments set **`terminationGracePeriodSeconds: 150`** plus a
**5-second `preStop` sleep**. With more than one replica, a rolling
deploy terminates pods routinely, so these settings matter on every
deploy, not only in rare cases.

uvicorn handles SIGTERM itself: it stops accepting requests, drains the
in-flight ones, then shuts down. Nothing bounds that drain, so the grace
period is the only real deadline. When it expires, the container is
SIGKILLed with whatever is still in flight. 150s covers the slowest
single request, a write (`hive_write` / `POST /v1/entries`), which
embeds and then extracts in sequence, each under its own bounded retry
budget (ADR 0014):

| leg | attempts | timeout | backoff | worst case |
|---|---|---|---|---|
| embedder | 1 + `HIVEMIND_EMBEDDING_RETRIES` (2) = 3 | `HIVEMIND_EMBEDDING_TIMEOUT` (10s) | 0.5s + 1.0s | 31.5s |
| extractor | 1 + `HIVEMIND_EXTRACTOR_RETRIES` (2) = 3 | `HIVEMIND_EXTRACTOR_TIMEOUT` (30s) | 0.5s + 1.0s | 91.5s |
| **worst-case write path** | | | | **123.0s** |

Add the 5s `preStop` sleep and two pooled Postgres round-trips, and round
up to **150s** for margin. The `preStop` sleep exists because, when a pod
is deleted, the SIGTERM and the EndpointSlice removal happen at the same
time. Until the removal reaches kube-proxy and the nginx ingress
controller (which routes straight to pod IPs), a terminating pod can still
be handed new requests that it would refuse.

**If you raise a retry budget or timeout, raise this too.** Each leg costs
`(retries + 1) x timeout + 0.5 x (2^retries - 1)` seconds. Sum the two
legs, add the 5s `preStop`, then round up. Make the change in *both*
`deploy/kubernetes/*-deployment.yaml`. For example,
`HIVEMIND_EXTRACTOR_RETRIES=4` makes the extractor leg 157.5s and the
write path 189s, so the grace period needs to go to **~240s**. If you
leave it stale, nothing crashes. Instead a SIGKILL drops a write, and the
caller is told nothing about it. Each leg also runs under
`HIVEMIND_EMBEDDING_DEADLINE` / `HIVEMIND_EXTRACTOR_DEADLINE`. Setting one
lower shortens the worst case, and a value below the matching `_TIMEOUT`
is a startup error. (ADR 0041)

### Probe endpoints

Five **unauthenticated** orchestrator endpoints are served outside the
auth middleware, because Kubernetes probes carry no credential (ADR 0019):

| Endpoint | Depth | Semantics |
|---|---|---|
| `GET /mcp/liveness` (mcp runner) | shallow | 200 whenever the process answers |
| `GET /mcp/health` (mcp runner) | shallow | 200 whenever the process answers. This is the readiness probe. It does **not** check the database: all replicas share it, so a DB outage would pull every pod out of service at once (ADR 0037) |
| `GET /mcp/health/database` (mcp runner) | deep | 200 when the Postgres pool answers within 5 s, otherwise 503. It is for people and monitoring, and no probe uses it. The pod log names the cause once per outage (`database health check failing: <exception>`) |
| `GET /v1/liveness` (REST API) | shallow | 200 whenever the process answers |
| `GET /v1/health` (REST API) | static | A static 200, public by design (SPEC §5.1). The REST surface intentionally has no deep DB probe in v1 |

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 on every agent call | An active agent: its agent key was revoked or mistyped. A not-yet-activated agent: a stale org key (after a rotation that was not distributed) | Active: re-activate it for a new key (§4.3). Pending: fetch `ORG_KEY` again from the `hivemind-keys` Secret (§4.1) |
| The app container crash-loops on the entrypoint's migrate pre-step | A dimension mismatch. The log names both dims and both fixes. (ADR 0015) | Do the equivalent of `make pg-reset` (a fresh pool), or set `HIVEMIND_EMBEDDING_DIM` to the pool's dim |
| `Unknown HIVEMIND_* environment variable(s)` WARNING at startup | Either a mistyped or stale `HIVEMIND_*` variable, for example a renamed knob (`_PREFIX_CHARS` became `_PREFIX_TOKENS`) still set under its old name, or a variable the platform injected (GitLab Auto DevOps, or Kubernetes service links such as `HIVEMIND_API_SERVICE_HOST`). (ADRs 0021 and 0024) | A typo: fix it as the message suggests. A platform variable: ignore it, or set `enableServiceLinks: false` on the pod to drop the service-link ones. Startup only **fails** on this when `HIVEMIND_STRICT_ENV=true` (ADR 0032) |
| `Removed HIVEMIND_* environment variable(s) are ignored` WARNING at startup | A setting this release removed is still set, for example `HIVEMIND_RECENCY_FROM_CONFIRMATION` or `HIVEMIND_RECENCY_BY_KIND`. It is harmless: the value is ignored and startup never fails on it. (ADR 0103) | Delete it from the ConfigMap or Secret (§5) |
| A scrape of `hivemind-mcp` fails with 401 or 404 | 401: an older image without `/metrics`, or a wrong path (for example through the Ingress's `/mcp` prefix, or a ServiceMonitor `path` other than `/metrics`). 404: `HIVEMIND_MCP_METRICS_ENABLED` is not `true` (it is off by default). (ADR 0104) | Roll the MCP pods to the current image. Set `HIVEMIND_MCP_METRICS_ENABLED=true` (the shipped Deployment does). Scrape the pod port 8088 at `/metrics` (§10, or `deploy/kubernetes/optional/monitoring/`). Do not give Prometheus a key |
| The MCP session drops after about 60s | The nginx default `proxy-read-timeout` kills the SSE stream | Apply the opt-in Ingress with the SSE annotations (`deploy/kubernetes/optional/ingress.yaml`) |
| Entries have empty `entities` | The extractor is off (an empty endpoint, which is by design), or the extractor is dead (it is best effort). (ADR 0016) | Check `HIVEMIND_EXTRACTOR_ENDPOINT` and `K8S_SECRET_EXTRACTOR_API_KEY`; see §4.6 |
| Writes fail with `EmbeddingError` | The embedder is dead and its retry budget is exhausted. (ADR 0014) | See §4.5 |
| A write dropped mid-flight during a deploy (the client sees a reset, no entry lands) | `terminationGracePeriodSeconds` no longer covers the retry budgets, so the pod was SIGKILLed while it was still draining | Recompute the sum in §5 and raise it in both Deployments |
| `hivemind-secrets` is missing values | A CI variable changed but there has been no redeploy yet | Re-run the pipeline (the deploy job re-renders the Secret on every deploy) |

**Two-Secret ownership rule (do not cross the boundary):** CI owns
`hivemind-secrets` (the `K8S_SECRET_*` values, re-rendered on
every deploy). The first-run bootstrap owns `hivemind-keys` (the
generated ADMIN_KEY and ORG_KEY). The bootstrap step
(`scripts/ci-bootstrap-keys.sh`) deliberately skips when `hivemind-keys`
exists, so re-rendering a secret never wipes the generated keys.

---

## 7. RBAC

This is a minimal ServiceAccount and Role for the GitLab Kubernetes
agent's deploy ServiceAccount. It grants the verbs `.gitlab-ci.yml`
actually uses. The org's Kubernetes agent must run as this ServiceAccount
(through its SA token or a mounted kubeconfig).

**An operator creates the namespace once**
(`kubectl apply -f deploy/kubernetes/namespace.yaml`). Namespaces are
cluster-scoped, so a namespaced Role cannot grant them. The pipeline's own
`kubectl apply -f namespace.yaml` is then a no-op that needs only `get` and
`patch` on that one namespace. The small ClusterRole below grants those
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
  # The bootstrap Role below grants delete on `hivemind-keys`; RBAC lets
  # the deployer create that Role only if it holds the same permission.
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["hivemind-keys"]
    verbs: ["delete"]
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

## 8. Admin panel (`hivemind-admin`)

The admin panel is a browser front end for the admin surface. With it you
can do the following.

- See a "Needs you" row on the Overview. (ADR 0121)
- Work the pending-agent queue, and activate an agent with its key shown
  once.
- Create an agent for its owner. (ADR 0120)
- Manage agents:
  - replace a key (ADR 0108);
  - see who is idle (ADR 0110);
  - clean up after a bad one (ADR 0111);
  - retire one in favour of a successor (ADR 0113);
  - correct the owner alias (ADR 0122);
  - read its history (ADR 0123);
  - rename it (ADR 0159);
  - offboard a person who leaves (ADR 0135).
- Manage fleets:
  - merge them (ADR 0107);
  - rename one;
  - move agents to split a team (ADR 0112);
  - set fleet phases and scouts (ADRs 0143 and 0144);
  - see each fleet's pins (ADR 0092).
- Browse memories, with search:
  - withdraw one (ADR 0080);
  - restore one (ADR 0124);
  - redact one (ADR 0125);
  - move a fleet memory to its author's `self` scope (ADR 0145);
  - search as one agent, with each hit's ranking explained (ADRs 0136 and
    0137).
- Read the feedback log. (ADR 0090)
- Work the flag backlog. (ADR 0093)
- Export lists as CSV. (ADR 0094)
- Read the audit log, rotate the org key, and see daily activity, stats
  and the fleet health metrics.

The root [README](README.md) describes it from a user's point of view, and
[docs/ops-runbook.md](docs/ops-runbook.md) sections 7 to 21 give the
steps for each task. Admin keys stay CLI-only. Why the panel is built this
way: ADR 0029.

**What it needs.** One env var: `HIVEMIND_ADMIN_API_URL`, the base URL of
the hivemind-api deployment (in-cluster: `http://hivemind-api:8000`). For
an https URL behind an internal CA, also set `HIVEMIND_ADMIN_API_CA_BUNDLE`
to a mounted PEM file. The panel holds **no DSN and no Secret**, never
talks to Postgres, and the entrypoint skips the migration step for it.

**Deploying it.** It is its own kustomization, so it works as a separate
GitLab project on the same image:

```bash
kubectl kustomize deploy/kubernetes/admin \
  | sed "s|hivemind:0.0.0|$IMAGE|g" | kubectl -n hivemind apply -f -
```

The in-repo `.gitlab-ci.yml` deploy job runs exactly this as step 5. It
runs one replica and has no PodDisruptionBudget, because the panel is
stateless and only operators use it.

**Reaching it.** It is an operator tool, so keep it off the public
internet:

```bash
kubectl -n hivemind port-forward svc/hivemind-admin 8080:8080
# then open http://localhost:8080 and sign in with the admin key
```

Or route an **internal-only** ingress host to `hivemind-admin:8080`.
The panel holds the admin key in the browser tab's `sessionStorage`
(cleared on sign-out or when the tab closes), so serve it over TLS
wherever it is not `localhost`.

**Locally:** run `make up` (panel on `:8089`) or `make api` plus
`make admin` (`:8080`). The Docker steps are in
[docs/ops-runbook.md](docs/ops-runbook.md) §1 and in the root
[README](README.md), "Try it on your computer".

**What it forwards.** The panel forwards the admin endpoints it uses (a
subset of `/v1/admin/*`: browsing memories, activity, the audit log, fleet
and agent management and key rotation, but never the data plane),
`GET /v1/metrics` and `GET /v1/health`. Anything else is a 404 from the
panel itself. It forwards `X-API-Key`, `Content-Type` and `Accept` and
nothing else, marks every forwarded response `Cache-Control: no-store`, and
never logs a header or a body. `GET /healthz` is its own probe and does not
call the API.


## 9. Agent portal (`hivemind-portal`)

The agent portal is a browser front end for the people behind the agents.
Someone signs in with one agent's key. They see:

- what that agent wrote (withdrawn and superseded entries included), the
  verdicts it gave and got, when the key was last used, and its home
  fleet's memories and pins;
- a "Needs attention" page of the agent's flagged memories (ADR 0115);
- a "Relied on" page of the memories it rated helpful that changed since
  (ADR 0138);
- a History page of what was done to and by the agent (ADR 0123);
- a search over everything the key can read.

They can also:

- withdraw the agent's own memories;
- move a fleet memory of the agent's to its `self` scope (ADR 0145);
- pin or unpin entries as the agent (same rights and limits as
  `hive_pin`);
- lock a leaked key;
- clean up the agent's own work over a time window, with undo (ADR 0118).

All of it is audited with `via: "portal"`. Admin and org keys are turned
away (§8 is for the admin key), and portal searches are left out of the
search counters. The root [README](README.md) describes it from a user's
point of view. Why the portal is built this way: ADR 0095.

**What it needs.** One env var: `HIVEMIND_PORTAL_API_URL`, the base URL of
the hivemind-api deployment (in-cluster: `http://hivemind-api:8000`). For
an https URL behind an internal CA, also set `HIVEMIND_PORTAL_API_CA_BUNDLE`
to a mounted PEM file. Like the panel, it holds **no DSN and no Secret**,
never talks to Postgres, and the entrypoint skips the migration step for
it. Upgrade `hivemind-api` first, or together: an older API answers 404 to
the portal's own routes.

**Deploying it.** It is its own kustomization, on the same image:

```bash
kubectl kustomize deploy/kubernetes/portal \
  | sed "s|hivemind:0.0.0|$IMAGE|g" | kubectl -n hivemind apply -f -
```

The in-repo `.gitlab-ci.yml` deploy job runs exactly this as step 6. It
runs two replicas with a `minAvailable: 1` PodDisruptionBudget and a
`maxUnavailable: 0` rollout, as for the API, because people other than the
operator use it (ADR 0026). It is stateless: the key lives in the
browser tab, so any replica serves any request.

**Reaching it.** Put it behind an **internal** ingress only, never a
public one. An agent key is a credential, and the portal shows everything
the key can read.
`deploy/kubernetes/portal/ingress-internal.example.yaml` is a
starting point that no kustomization applies. Before you apply it with
`kubectl -n hivemind apply -f`, set its `ingressClassName` to your
internal ingress class, replace the placeholder host and TLS secret, and
replace the `127.0.0.1/32` source range (which admits nobody) with your
internal network's ranges. Serve it over TLS, because the browser sends
the agent key on every request. With the example NetworkPolicies (§2
step 6), edit the `hivemind-portal` policy's namespace when the internal
controller runs somewhere other than `ingress-nginx`. For a quick look:

```bash
kubectl -n hivemind port-forward svc/hivemind-portal 8080:8080
# then open http://localhost:8080 and sign in with an agent key
```

**Which key.** Use the agent's own key, the one its harness uses (from
activation or `hivemind-keys issue-agent`). The portal keeps it in the
tab's `sessionStorage` under its own name, separate from the admin
panel's, and holds one key at a time. To look at another agent, sign out
and sign in with that agent's key.

**Locally:** run `make up` (portal on `:8090`) or `make api` plus
`make portal` (`:8081`). The Docker steps are in
[docs/ops-runbook.md](docs/ops-runbook.md) §1 and in the root
[README](README.md), "Try it on your computer".

**What it forwards.** `GET /v1/whoami`, `GET /v1/entries/{id}` (the
drawer, with `?history=true`), `GET /v1/pins`, `GET /v1/health` and the
portal's own routes: `GET /v1/portal/entries`, `POST /v1/portal/search`,
`GET /v1/portal/feedback`, `GET /v1/portal/flagged`, `GET /v1/portal/relied-on`,
`POST /v1/portal/entries/{id}/withdraw`,
`POST /v1/portal/entries/{id}/move-to-self`,
`PUT`/`DELETE /v1/portal/entries/{id}/pin`, `POST /v1/portal/cleanup`,
`GET /v1/portal/cleanups`, `POST /v1/portal/cleanups/{id}/undo`,
`GET /v1/portal/history` and
`POST /v1/portal/key/lock`. Anything else is a 404 from the portal itself,
including the data plane's writes and its counted search. Headers,
caching and logging are as for the panel: `X-API-Key`, `Content-Type`
and `Accept` only, `Cache-Control: no-store`, no header or body logged,
and `GET /healthz` as its own probe. The portal serves the panel's shared
front-end modules by exact name and none of its other files. (ADR 0091)

## 10. Monitoring

Monitoring is optional. It has two parts: Prometheus reads metrics from
the pods, and Grafana draws them. The files are in
`deploy/kubernetes/optional/monitoring/`. These are the steps that
[§2](#2-quickstart) lists as steps 7 and 8.

### What is scraped

`GET /metrics` is served with no key by two sets of pods:

- the `hivemind-api` pods, with usage gauges and request series;
- the `hivemind-mcp` pods, with their own request, tool-call and
  degraded-search series only.

Prometheus must scrape them in-cluster, never through the public Ingress,
which sends only `/mcp` to the MCP Service. The MCP endpoint is off unless
`HIVEMIND_MCP_METRICS_ENABLED=true` (the shipped manifest sets it).

The metrics reveal request volume and latency, the per-tool call mix and
error share, the count of 401s (an outsider could watch a key-guessing
run) and the degraded-search count. They never reveal an agent, fleet,
owner, key or argument. If the MCP port is reachable from outside some
other way (a LoadBalancer, or a compose host port behind a TLS proxy),
leave the endpoint off or block `/metrics` in the proxy. (ADRs 0050 and
0104)

### Plain Prometheus and the Prometheus Operator

Plain Prometheus with the usual `kubernetes-pods` job finds both sets of
pods by their `prometheus.io/*` annotations.

The Prometheus Operator ignores those annotations. Apply the
ServiceMonitors in `deploy/kubernetes/optional/monitoring/` (add your
Operator's release label first), or write your own that selects
`app.kubernetes.io/component: api` or `mcp` as well as the name label.
Every Hivemind Service carries `app.kubernetes.io/name: hivemind`, so a
selector on the name alone also scrapes the admin panel and the portal,
which have no `/metrics`.

`sum` the process counters across the two jobs. With the optional
NetworkPolicies, Prometheus must run in the `monitoring` namespace (or
edit the policies).

### The Grafana dashboard

`deploy/kubernetes/optional/monitoring/hivemind-dashboard.json` shows:

- search health: the share of searches finding nothing, per fleet;
  keyword-only searches; the explore share;
- traffic: requests, 5xx share, 401s, and p50 and p95 latency per route;
- MCP tool calls: rate, server failure share, refusals by error code, and
  p95 latency;
- the fleet health gauges over time;
- a "Vital signs" row: the echo rate, the grounding share, the age of open
  rivals and each fleet's 7-day writes and verdicts.

You can compare a release with the one before it by moving the time range.

The same tree ships the dashboard as a ConfigMap labelled
`grafana_dashboard: "1"`, which the Grafana sidecar of kube-prometheus-stack
loads on its own. Elsewhere, import the JSON (Dashboards > New > Import)
and pick the Prometheus data source. The search panels smooth over the
`Search window` variable (default 1h), because those counters refresh about
every 30 s. The MCP panels stay empty while `HIVEMIND_MCP_METRICS_ENABLED`
is off. (ADR 0142)

### Alerts

The same tree ships `alerts.yaml`, a `PrometheusRule` named
`hivemind-vital-signs`. It has four `info` alerts: a quiet fleet, a rising
echo rate, rivals lingering and a growing floating gap. They are hints for
the admin, not pages. Add your Operator's release label to it, as you did
for the ServiceMonitors.

The thresholds are starting points. The echo rate (0.2) and the rival age
(60 days) match the admin panel's "Vital signs" card. The quiet-fleet alert
(7 days without activity) and the floating-gap alert (larger than 28 days
earlier) have no counterpart there. (ADR 0142)

### What to watch after an upgrade

After an upgrade, open the dashboard and move the time range so the new
release sits beside the one before it. Four things are worth a look: the
share of searches finding nothing, keyword-only (degraded) searches, the
5xx share, and the MCP tool failure share. A jump in any of them right
after a release tells you where to start.
