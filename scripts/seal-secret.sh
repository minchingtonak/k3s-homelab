#!/usr/bin/env bash
# seal-secret.sh -- Encrypt a plain Kubernetes Secret into a SealedSecret.
#
# Usage:
#   ./scripts/seal-secret.sh <input-secret.yaml> <output-sealed-secret.yaml>
#
# Example:
#   ./scripts/seal-secret.sh /tmp/porkbun-secret.yaml \
#       infrastructure/cert-manager-config/porkbun-sealed-secret.yaml
#
# The sealed-secrets controller must be deployed in the cluster (flux-system
# namespace, release name sealed-secrets-controller). The public cert is fetched
# directly from the K8s API -- no pod connectivity required.

set -euo pipefail

KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/k3s-homelab}"
CONTROLLER_NS="flux-system"
CONTROLLER_LABEL="sealedsecrets.bitnami.com/sealed-secrets-key"
CERT_TMP="$(mktemp /tmp/sealed-secrets-cert.XXXXXX.pem)"

INPUT="${1:-}"
OUTPUT="${2:-}"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
  echo "Usage: $0 <input-secret.yaml> <output-sealed-secret.yaml>" >&2
  exit 1
fi

if [[ ! -f "$INPUT" ]]; then
  echo "Error: input file not found: $INPUT" >&2
  exit 1
fi

if ! command -v kubeseal &>/dev/null; then
  echo "Error: kubeseal not found in PATH" >&2
  exit 1
fi

echo "Fetching sealed-secrets public cert from cluster..."
KUBECONFIG="$KUBECONFIG" kubectl get secret -n "$CONTROLLER_NS" \
  -l "$CONTROLLER_LABEL" \
  -o jsonpath='{.items[0].data.tls\.crt}' \
  | base64 -d > "$CERT_TMP"

if [[ ! -s "$CERT_TMP" ]]; then
  echo "Error: failed to fetch cert -- is sealed-secrets-controller deployed?" >&2
  rm -f "$CERT_TMP"
  exit 1
fi

echo "Sealing $INPUT -> $OUTPUT ..."
kubeseal --cert "$CERT_TMP" --format=yaml < "$INPUT" > "$OUTPUT"

rm -f "$CERT_TMP"
echo "Done. Commit $OUTPUT to Git (do NOT commit $INPUT)."
