#!/usr/bin/env bash
#
# Builds a read-only kubeconfig for the Hermes agent and installs it on the ai
# LXC at /docker/hermes/secrets/kubeconfig, where the hermes containers mount it
# as /secrets/kubeconfig.
#
# Requires that the hermes-rbac Kustomization has already reconciled, since the
# ServiceAccount token Secret it reads is created by Flux. Run this from a
# machine whose kubectl already has admin access to the cluster.
#
# Safe to re-run: it overwrites the kubeconfig in place.
set -euo pipefail

NAMESPACE="hermes"
SA_SECRET="hermes-readonly-token"
CLUSTER_ENDPOINT="https://192.168.20.110:6443"
CLUSTER_NAME="minicluster"

LXC_HOST="root@192.168.20.208"
LXC_SSH_KEY="${HOME}/.ssh/lxc_ed25519"
REMOTE_PATH="/docker/hermes/secrets/kubeconfig"

echo "Reading ServiceAccount token from ${NAMESPACE}/${SA_SECRET}..."
if ! kubectl -n "$NAMESPACE" get secret "$SA_SECRET" >/dev/null 2>&1; then
  echo "ERROR: secret ${NAMESPACE}/${SA_SECRET} not found." >&2
  echo "       Has the hermes-rbac Kustomization reconciled yet? Check with:" >&2
  echo "       flux get kustomization hermes-rbac" >&2
  exit 1
fi

TOKEN="$(kubectl -n "$NAMESPACE" get secret "$SA_SECRET" -o jsonpath='{.data.token}' | base64 -d)"
CA_CRT="$(kubectl -n "$NAMESPACE" get secret "$SA_SECRET" -o jsonpath='{.data.ca\.crt}')"

if [[ -z "$TOKEN" || -z "$CA_CRT" ]]; then
  echo "ERROR: token or ca.crt is empty; the API server may not have populated" >&2
  echo "       the Secret yet. Wait a moment and re-run." >&2
  exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

cat >"$TMP" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${CLUSTER_ENDPOINT}
      certificate-authority-data: ${CA_CRT}
users:
  - name: hermes-readonly
    user:
      token: ${TOKEN}
contexts:
  - name: ${CLUSTER_NAME}
    context:
      cluster: ${CLUSTER_NAME}
      user: hermes-readonly
current-context: ${CLUSTER_NAME}
EOF

echo "Verifying the generated credentials are read-only..."
if kubectl --kubeconfig="$TMP" auth can-i list nodes >/dev/null 2>&1; then
  echo "  reads: OK"
else
  echo "ERROR: generated kubeconfig cannot list nodes" >&2
  exit 1
fi

# These must all come back "no". A yes means the ClusterRole granted more than
# intended and the agent could mutate the cluster directly, bypassing GitOps.
#
# Secret reads are deliberately NOT checked here: the role grants blanket
# get/list/watch, so the agent can read Secrets. Only writes are forbidden.
fail=0
for check in "create deployments" "delete pods" "patch configmaps"; do
  # shellcheck disable=SC2086
  if kubectl --kubeconfig="$TMP" auth can-i $check >/dev/null 2>&1; then
    echo "ERROR: credentials can '${check}' — expected denial" >&2
    fail=1
  else
    echo "  denied '${check}': OK"
  fi
done
[[ $fail -eq 0 ]] || exit 1

echo "Installing to ${LXC_HOST}:${REMOTE_PATH}..."
scp -q -i "$LXC_SSH_KEY" "$TMP" "${LXC_HOST}:${REMOTE_PATH}"
ssh -i "$LXC_SSH_KEY" "$LXC_HOST" "chown 10000:10000 ${REMOTE_PATH} && chmod 0600 ${REMOTE_PATH}"

echo "Done. The agent will pick it up via KUBECONFIG=/secrets/kubeconfig."
