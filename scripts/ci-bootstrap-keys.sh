#!/bin/sh
# First-run key bootstrap for the GitLab deploy job (DEP-1, DEP-9, ADR 0044).
#
# Idempotent: does nothing when the `hivemind-keys` Secret already exists.
# Otherwise applies the Job's RBAC (deploy/kubernetes/bootstrap/rbac.yaml) and
# runs deploy/kubernetes/bootstrap/keys-job.yaml (the image placeholder
# replaced by $IMAGE). The Job writes the raw admin and org keys straight into
# the `hivemind-keys` Secret; no key ever passes through a log, this script or
# GitLab. The RBAC objects are removed again when the Job has finished. See
# DEPLOY.md §2/§4.
#
# Needs: kubectl (pointing at the cluster, allowed to manage Roles and
# Secrets in the namespace), IMAGE=<registry image:tag>.
# The Secret `hivemind-secrets` and ConfigMap `hivemind-config` must already exist.
set -eu

: "${IMAGE:?set IMAGE to the registry image to run}"
NS="${NAMESPACE:-hivemind}"
JOB=hivemind-keys-bootstrap
MANIFEST="${BOOTSTRAP_MANIFEST:-deploy/kubernetes/bootstrap/keys-job.yaml}"
RBAC="${BOOTSTRAP_RBAC:-deploy/kubernetes/bootstrap/rbac.yaml}"
TIMEOUT="${BOOTSTRAP_TIMEOUT_SECONDS:-300}"

# Fail CLOSED: `--ignore-not-found` makes "absent" an empty success, while an
# API/auth/network error aborts under `set -e`. (Treating every error as
# "absent" would start the bootstrap against a live Secret; the Job's own
# check and the create would still refuse, but nothing should get that far.)
existing=$(kubectl -n "$NS" get secret hivemind-keys -o name --ignore-not-found)
if [ -n "$existing" ]; then
  echo "hivemind-keys secret exists - skipping key bootstrap"
  exit 0
fi

remove_rbac() {
  sed "s|namespace: hivemind\$|namespace: $NS|" "$RBAC" \
    | kubectl -n "$NS" delete --ignore-not-found=true -f - >/dev/null || true
}

kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true
sed "s|namespace: hivemind\$|namespace: $NS|" "$RBAC" | kubectl -n "$NS" apply -f -
# `sed` is only the image substitution; its output is piped into apply (a
# here-document inside YAML cannot keep its terminator at column 0 - DEP-1).
sed "s|hivemind:0.0.0|$IMAGE|g" "$MANIFEST" | kubectl -n "$NS" apply -f -

# Poll instead of `kubectl wait --for=condition=complete`: that would sit
# out the whole timeout when the Job FAILS (backoffLimit 0).
waited=0
while :; do
  ok=$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}')
  bad=$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.failed}')
  if [ "${ok:-0}" -ge 1 ]; then
    break
  fi
  if [ "${bad:-0}" -ge 1 ]; then
    # The Job never prints a key (ADR 0044), so its log is safe to show.
    echo "key bootstrap Job FAILED; its log:" >&2
    kubectl -n "$NS" logs "job/$JOB" >&2 || true
    remove_rbac
    exit 1
  fi
  if [ "$waited" -ge "$TIMEOUT" ]; then
    echo "key bootstrap Job did not finish within ${TIMEOUT}s" >&2
    remove_rbac
    exit 1
  fi
  sleep 3
  waited=$((waited + 3))
done

remove_rbac
kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true
created=$(kubectl -n "$NS" get secret hivemind-keys -o name --ignore-not-found)
if [ -z "$created" ]; then
  echo "key bootstrap Job succeeded but the hivemind-keys Secret is missing" >&2
  exit 1
fi
echo "hivemind-keys secret created"
