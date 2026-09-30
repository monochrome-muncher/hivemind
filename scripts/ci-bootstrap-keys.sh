#!/bin/sh
# First-run key bootstrap for the GitLab deploy job (DEP-1, DEP-9).
#
# Idempotent: does nothing when the `hivemind-keys` Secret already exists.
# Otherwise runs deploy/kubernetes/bootstrap/keys-job.yaml (the image placeholder
# replaced by $IMAGE) in the cluster, reads the two raw keys the Job prints
# ONCE, and stores them in the `hivemind-keys` Secret. The raw keys are never
# echoed by this script; they live only in the Job's pod log (deleted with the
# Job) and then in the Secret. See DEPLOY.md §2/§4.
#
# Needs: kubectl (pointing at the cluster), IMAGE=<registry image:tag>.
# The Secret `hivemind-secrets` and ConfigMap `hivemind-config` must already exist.
set -eu

: "${IMAGE:?set IMAGE to the registry image to run}"
NS="${NAMESPACE:-hivemind}"
JOB=hivemind-keys-bootstrap
MANIFEST="${BOOTSTRAP_MANIFEST:-deploy/kubernetes/bootstrap/keys-job.yaml}"
TIMEOUT="${BOOTSTRAP_TIMEOUT_SECONDS:-300}"

if kubectl -n "$NS" get secret hivemind-keys >/dev/null 2>&1; then
  echo "hivemind-keys secret exists - skipping key bootstrap"
  exit 0
fi

kubectl -n "$NS" delete job "$JOB" --ignore-not-found=true
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
    echo "key bootstrap Job FAILED. Its log may hold a raw key, so it is not printed:" >&2
    echo "  kubectl -n $NS logs job/$JOB   (inspect by hand; see DEPLOY.md §2)" >&2
    exit 1
  fi
  if [ "$waited" -ge "$TIMEOUT" ]; then
    echo "key bootstrap Job did not finish within ${TIMEOUT}s" >&2
    exit 1
  fi
  sleep 3
  waited=$((waited + 3))
done

logs=$(kubectl -n "$NS" logs "job/$JOB")
admin_key=$(printf '%s\n' "$logs" | sed -n 's/^ADMIN_KEY=//p')
org_key=$(printf '%s\n' "$logs" | sed -n 's/^ORG_KEY=//p')
if [ -z "$admin_key" ] || [ -z "$org_key" ]; then
  echo "could not read both keys from the bootstrap Job log; not creating hivemind-keys" >&2
  exit 1
fi

manifest=$(kubectl -n "$NS" create secret generic hivemind-keys \
  --from-literal=ADMIN_KEY="$admin_key" \
  --from-literal=ORG_KEY="$org_key" \
  --dry-run=client -o yaml)
printf '%s\n' "$manifest" | kubectl -n "$NS" apply -f -
kubectl -n "$NS" delete job "$JOB"
echo "hivemind-keys secret created"
