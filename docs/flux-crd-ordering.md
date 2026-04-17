# flux crd ordering

Flux validates all resources in a kustomization with a dry-run before applying
any of them. If a kustomization contains both a HelmRelease (which installs CRDs)
and resources that use those CRDs, the dry-run fails on a fresh cluster because
the CRDs don't exist yet.

This is a first-bootstrap-only problem — on an existing cluster the CRDs are
already present, so it silently works. It only surfaces when standing up from
scratch.

## the two-phase pattern

Split the kustomization into two:

- **phase 1** — HelmRelease only, with `wait: true` so Flux blocks until the
  Helm install completes and CRDs are registered
- **phase 2** — CRD-backed resources only, with `dependsOn: [phase-1]` so it
  only runs after the CRDs exist

```
clusters/homelab/infrastructure/metallb/
  kustomization-metallb.yaml         # phase 1: HelmRelease, wait: true
  kustomization-metallb-config.yaml  # phase 2: IPAddressPool, L2Advertisement

clusters/homelab/infrastructure/kube-prometheus-stack/
  kustomization-kube-prometheus-stack.yaml         # phase 1: HelmRelease, wait: true
  kustomization-kube-prometheus-stack-config.yaml  # phase 2: ServiceMonitor, PrometheusRule
```

Phase 1 kustomization:

```yaml
spec:
  path: k8s/infrastructure/metallb
  wait: true      # blocks until HelmRelease is Ready and CRDs are registered
  timeout: 5m
```

Phase 2 kustomization:

```yaml
spec:
  path: k8s/infrastructure/metallb-config
  dependsOn:
    - name: metallb   # guarantees phase 1 (and its CRDs) are ready first
```

## what needs the two-phase pattern

Any resource kind backed by a CRD that is installed by a HelmRelease in the
same kustomization. Common cases in this repo:

| CRD group | installed by | affected kinds |
|---|---|---|
| `metallb.io/v1beta1` | metallb HelmRelease | `IPAddressPool`, `L2Advertisement` |
| `monitoring.coreos.com/v1` | kube-prometheus-stack HelmRelease | `ServiceMonitor`, `PrometheusRule`, `PodMonitor` |

Resources using CRDs from a *different*, already-deployed kustomization are
fine — e.g. `IngressRoute` (traefik CRDs) inside kube-prometheus-stack is safe
because traefik deploys before kube-prometheus-stack and its CRDs already exist
at dry-run time.

## what does not need it

- Resources using only core Kubernetes kinds (`Deployment`, `Service`,
  `ConfigMap`, `Secret`, etc.) — these are always available
- `cert-manager.io/v1` resources inside `cert-manager-config` — safe because
  `cert-manager-config` has `dependsOn: [cert-manager]` and cert-manager's
  HelmRelease is in a separate kustomization that runs first
