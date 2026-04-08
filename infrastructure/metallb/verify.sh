#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s-homelab}"
export KUBECONFIG

echo "==> Flux Kustomizations"
kubectl get kustomizations -n flux-system

echo ""
echo "==> HelmRelease"
kubectl get helmrelease -n metallb-system

echo ""
echo "==> MetalLB pods"
kubectl get pods -n metallb-system

echo ""
echo "==> IPAddressPools"
kubectl get ipaddresspools -n metallb-system

echo ""
echo "==> L2Advertisements"
kubectl get l2advertisements -n metallb-system

echo ""
echo "==> Smoke test: creating LoadBalancer service"
kubectl create service loadbalancer test-lb --tcp=80:80
echo "Waiting 5s for IP assignment..."
sleep 5
kubectl get svc test-lb
kubectl delete svc test-lb
echo "Smoke test complete."
