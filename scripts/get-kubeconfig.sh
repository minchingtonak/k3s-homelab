#!/usr/bin/env bash
set -euo pipefail

SERVER_IP="192.168.8.100"
REMOTE_USER="k3s"
REMOTE_KUBECONFIG="/home/k3s/.kube/config"
LOCAL_KUBECONFIG="${HOME}/.kube/config"

mkdir -p "$(dirname "$LOCAL_KUBECONFIG")"

SSH_KEY="${HOME}/.ssh/k3s_ed25519"

echo "Copying kubeconfig from ${REMOTE_USER}@${SERVER_IP}..."
scp -i "$SSH_KEY" "${REMOTE_USER}@${SERVER_IP}:${REMOTE_KUBECONFIG}" /tmp/k3s-kubeconfig.yaml

# Replace the cluster IP (k3s writes 127.0.0.1 by default)
sed "s|https://127.0.0.1:6443|https://${SERVER_IP}:6443|g" /tmp/k3s-kubeconfig.yaml > /tmp/k3s-kubeconfig-patched.yaml
rm /tmp/k3s-kubeconfig.yaml

if [[ -f "$LOCAL_KUBECONFIG" ]]; then
  echo "Merging into existing kubeconfig at ${LOCAL_KUBECONFIG}..."
  # Remove stale entries for this cluster before merging so the new CA/creds win
  kubectl --kubeconfig="$LOCAL_KUBECONFIG" config delete-cluster default 2>/dev/null || true
  kubectl --kubeconfig="$LOCAL_KUBECONFIG" config delete-context default 2>/dev/null || true
  kubectl --kubeconfig="$LOCAL_KUBECONFIG" config delete-user default 2>/dev/null || true
  KUBECONFIG="${LOCAL_KUBECONFIG}:/tmp/k3s-kubeconfig-patched.yaml" \
    kubectl config view --flatten > /tmp/k3s-merged.yaml
  mv /tmp/k3s-merged.yaml "$LOCAL_KUBECONFIG"
  rm /tmp/k3s-kubeconfig-patched.yaml
else
  mv /tmp/k3s-kubeconfig-patched.yaml "$LOCAL_KUBECONFIG"
fi

chmod 600 "$LOCAL_KUBECONFIG"
echo "Done. Test with: kubectl --context=default get nodes"
