# The first-run key bootstrap writes the Kubernetes Secret itself

## Context

On a fresh deployment the GitLab deploy job bootstraps the first admin key
and the org key (DEPLOY.md §2). Until now a one-shot Job ran
`hivemind-keys issue-admin` and `rotate-org`, printed both raw keys to its
pod log as `ADMIN_KEY=...` / `ORG_KEY=...`, and `scripts/ci-bootstrap-keys.sh`
scraped the log into the `hivemind-keys` Secret.

The pod log is not a private channel. A cluster log shipper (Fluent Bit,
Promtail, a cloud provider's agent) copies container stdout into a store
that is usually readable by far more people than the Secret and keeps it
for weeks, so the admin key, which grants every admin act, ended up there.
`ttlSecondsAfterFinished` only removes the copy on the node. The 2.0.0
review recorded this as an open follow-up (ROADMAP §3.13) and noted that
removing it needs a ServiceAccount allowed to create Secrets.

The two CLI calls were also not atomic with the Secret: a failure after
`issue-admin` left an admin key nobody held, and one after `rotate-org`
had already invalidated the previous org key.

## Decision

1. **New subcommand `hivemind-keys bootstrap-secret [--secret NAME]`.** It
   runs inside the cluster and talks to the Kubernetes API with the pod's
   own ServiceAccount token (stdlib HTTP, `store/kube_secret.py`; no client
   library in the image). If the Secret exists it does nothing. Otherwise it
   issues an admin key and rotates the org key in **one** database
   transaction, creates the Secret with both values **before** committing,
   and commits only after the create succeeded. A failed create rolls the
   keys back; a failed commit deletes the Secret again (best effort). It
   never prints a key. Its audit rows are the usual `cli` rows
   (`admin_key.issue`, `org_key.rotate`) under `--actor ci-bootstrap`.

2. **The bootstrap Job gets its own narrow identity**
   (`deploy/kubernetes/bootstrap/rbac.yaml`): a ServiceAccount, a Role with
   `create` on Secrets (Kubernetes cannot narrow `create` by name) plus
   `get` / `delete` on `hivemind-keys` only, and a RoleBinding. The Job is
   the only workload that mounts a token; every Deployment keeps
   `automountServiceAccountToken: false`.

3. **The CI script no longer touches keys.** It applies the RBAC and the
   Job, waits, checks that the Secret now exists, and deletes the Job and
   the RBAC objects again, so the Secret-creating identity exists only while
   a bootstrap runs. Because the Job log holds no key, the script prints it
   when the Job fails.

## Consequences

- No raw key reaches a log, CI output or GitLab on the bootstrap path. The
  key-bearing places are the database (hashes only) and the Secret.
- A failed bootstrap leaves no orphan admin key and no rotated org key; it
  can be re-run as is. `backoffLimit` stays 0 so a failure is visible in CI.
- The deploy identity must be allowed to create the Role and RoleBinding,
  which Kubernetes permits only if it already holds the Secret permissions
  it grants (it already creates `hivemind-secrets`).
- While the Job runs, its token can create any Secret in the namespace.
  The window is the Job's runtime; the binding is deleted afterwards.
- Operators who bootstrapped with the old Job should still treat their
  log store as key-bearing, or rotate both keys (DEPLOY.md §4).
