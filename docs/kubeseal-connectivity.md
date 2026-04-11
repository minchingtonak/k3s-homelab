# kubeseal Connectivity Issue

## Symptom

Running `kubeseal --controller-name=sealed-secrets-controller --controller-namespace=flux-system --fetch-cert` (or any kubeseal invocation that probes the controller) fails with:

```
error: cannot fetch certificate: error trying to reach service: proxy error from 192.168.8.100:6443 while dialing 10.42.1.8:8080, code 502: 502 Bad Gateway
```

## Root Cause

The Flux Operator automatically installs a `NetworkPolicy` named `allow-egress` in the `flux-system` namespace that restricts ingress to all pods in that namespace:

```yaml
# flux-system/allow-egress (managed by Flux Operator, do not modify)
spec:
  podSelector: {}        # applies to ALL pods in flux-system
  policyTypes: [Ingress, Egress]
  ingress:
  - from:
    - podSelector: {}    # only allows traffic FROM pods within flux-system
  egress:
  - {}                   # egress unrestricted
```

When kubeseal probes the controller, it uses the Kubernetes API server proxy mechanism:

```
kubeseal → API server (192.168.8.100:6443) → proxies HTTP → pod (10.42.1.8:8080)
```

The API server's outbound connection arrives at the sealed-secrets pod with a source IP of `10.42.0.0` — the server node's flannel VXLAN VTEP address. This is **not** a pod IP, so it does not match `podSelector: {}` in the NetworkPolicy. kube-router enforces the policy by rejecting the packet with `icmp-port-unreachable`, which the API server surfaces as a 502.

A tcpdump on the server confirmed the VXLAN tunnel itself is healthy — packets reach the agent node and are decapsulated correctly, but kube-router drops them before they reach the pod:

```
192.168.8.100 → 192.168.8.101:8472 (VXLAN): 10.42.0.0 → 10.42.1.8 ICMP echo request
192.168.8.101 → 192.168.8.100:8472 (VXLAN): 10.42.1.0 → 10.42.0.0 ICMP unreachable
```

## Fix

Fetch the sealing certificate directly from the Kubernetes API (stored as a Secret in etcd) rather than connecting to the controller pod. This bypasses the network path entirely.

Use `scripts/seal-secret.sh`, which handles this automatically:

```bash
./scripts/seal-secret.sh /tmp/my-secret.yaml infrastructure/some-component/my-sealed-secret.yaml
```

The script fetches the cert with:
```bash
kubectl get secret -n flux-system -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o jsonpath='{.items[0].data.tls\.crt}' | base64 -d > /tmp/cert.pem
kubeseal --cert /tmp/cert.pem --format=yaml < input.yaml > output.yaml
```

## Why Not Fix the NetworkPolicy?

The `allow-egress` policy is managed by the Flux Operator (`kustomize.toolkit.fluxcd.io/ssa: Ignore`) and is intentional — it isolates Flux components from arbitrary cluster traffic. Patching it would require either modifying the FluxInstance configuration or adding a supplementary NetworkPolicy with an `ipBlock` for the cluster pod CIDR. The offline cert approach is simpler and has no downsides.

## Scope

This issue only affects `flux-system` pods. The `cert-manager` namespace (where the lego-webhook and cert-manager controllers run) has no NetworkPolicies, so API server webhook calls to those components work normally.
