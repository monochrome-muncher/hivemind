# Hivemind — Production Deployment (Kubernetes + GitLab CI/CD)

Single source of truth for **deploying Hivemind to a Kubernetes cluster**
via GitLab CI/CD (ADR 0007 single-node shape: one API process, one MCP
runner, one Postgres node). The single-node **docker** ops story lives in
[docs/ops-runbook.md](docs/ops-runbook.md) — this file is the k8s story
and the two do not overlap.

Layout:

- [`deploy/kubernetes/`](deploy/kubernetes/) — the kustomize tree
  (namespace, ConfigMap, two 1-replica Deployments, two ClusterIP
  Services; `secret-template.yaml` documents the CI-rendered Secret;
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

1. **Local dev** — `cp .env.example .env` (the app reads `.env` via
   pydantic-settings `env_file`; see the `.env.example` file for every
   `HIVEMIND_*` knob). Production ignores `.env` — values come from the
   ConfigMap + Secret.
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
   `hivemind-keys issue-agent --name <agent>` or the admin REST surface
   `POST /v1/admin/agents/{name}/activate` (returns the key **once**).
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

- Schema migration — the `migrate` **initContainer** on every pod start
  (idempotent, ADR 0013; a dim mismatch fails LOUDLY, ADR 0015).
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

| Credential | Rotate via | When |
|---|---|---|
| Org key | `hivemind-keys rotate-org` | suspected org-key leak; periodic hardening |
| Admin key | `hivemind-keys issue-admin` + `hivemind-keys revoke-admin` | suspected admin-key leak; operator turnover |
| Agent key | `hivemind-keys revoke --name <agent>` | agent compromise; key leak |
| DSN (DB password) | change the password at the Postgres provider | suspected DSN leak; provider policy |
| Embedding API key | rotate at the embedding provider | provider policy; suspected leak |
| Extractor API key | rotate at the chat provider | provider policy; suspected leak |

### 4.1 Org key

- **How** — `hivemind-keys rotate-org` (atomic delete + insert, the
  cluster kill-switch, ADR 0012). The new raw key is printed **once**.
- **Then** — store it: update the k8s Secret `hivemind-keys`
  (`kubectl -n hivemind patch secret hivemind-keys -p
  '{"stringData":{"ORG_KEY":"<new>"}}'`) — or delete the Secret and re-run
  the bootstrap (§2 step 4).
- **Blast radius** — agents carrying the old org key fail at the **next
  request** (401). No data impact.
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

- **How** — `hivemind-keys revoke --name <agent>` (the name **stays
  reserved**; re-activation issues a NEW key — ADR 0012). Re-issue via
  the admin surface `POST /v1/admin/agents/{name}/activate`.
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

- Deploys are **forward-only + idempotent** (ADR 0013); the migrate
  initContainer runs on every pod start — a normal deploy is just a new
  image tag.
- A **dim mismatch is a LOUD failure at pod startup** (ADR 0015): the
  initContainer's `hivemind-migrate` exits non-zero naming both dims and
  both fixes — the pod never starts, never a silent no-op.
- Changing the embedding **model/dim** is an operator migration (new
  pool or re-embedding — ADR 0005), never a config flip.
- If `schema_migrations` lags the deployed `SCHEMA_VERSION` (e.g. pods
  were added before a migration ran), run the migration one-off:

  ```bash
  kubectl -n hivemind run hivemind-migrate --rm -i --restart=Never \
    --image=<registry>/hivemind:<sha> \
    --env=HIVEMIND_RUNNER=migrate \
    --env-from=secret/hivemind-secrets
  ```

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 401 on every agent call | wrong/stale org key (e.g. after a rotation that wasn't distributed) | re-fetch `ORG_KEY` from the `hivemind-keys` Secret; §4.1 |
| `migrate` initContainer CrashLooping | dim mismatch (ADR 0015) — the log names both dims + both fixes | `make pg-reset` equivalent (fresh pool) or set `HIVEMIND_EMBEDDING_DIM` to the pool's dim |
| MCP session drops after ~60s | nginx default `proxy-read-timeout` killing the SSE stream (classic pitfall) | apply the opt-in Ingress with the SSE annotations (`deploy/kubernetes/optional/ingress.yaml`) |
| entries have empty `entities` | extractor off (empty endpoint — by design) OR extractor dead (best-effort, ADR 0016) | check `HIVEMIND_EXTRACTOR_ENDPOINT` + `K8S_SECRET_EXTRACTOR_API_KEY`; §4.6 |
| writes failing with `EmbeddingError` | embedder dead (ADR 0014 retry budget exhausted) | §4.5 |
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