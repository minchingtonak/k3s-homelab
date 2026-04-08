# infrastructure/

This directory exists to work around a CRD ordering problem with Flux's auto-discovery.

## The Problem

The Flux Operator creates a single `Kustomization` that recursively discovers and applies
**all** YAML files under `clusters/homelab/`. There is no way to express ordering within
that single reconciliation pass — everything gets applied at once.

Some resources depend on CRDs that are only registered after a HelmRelease finishes
installing. For example, `IPAddressPool` and `L2Advertisement` are MetalLB-specific
resource types that don't exist in the cluster until the MetalLB Helm chart runs.
If those manifests land in `clusters/homelab/`, Flux tries to apply them in the same
pass as the HelmRelease and fails with:

```
no matches for kind "IPAddressPool" in version "metallb.io/v1beta1"
```

## The Solution

Split the deployment into two explicit Flux `Kustomization` resources with `dependsOn`:

1. A child Kustomization points at `infrastructure/<app>/` and installs the Helm chart
   (`wait: true` blocks until all pods are healthy and CRDs are registered).
2. A second child Kustomization points at `infrastructure/<app>-config/` with
   `dependsOn` referencing the first — it only runs once the chart is healthy.

Only the two Kustomization pointer files live in `clusters/homelab/<app>/` (where
auto-discovery picks them up). The actual manifests live here, outside the
auto-discovered tree.

## Structure

```
clusters/homelab/<app>/
  kustomization-<app>.yaml         # pointer → infrastructure/<app>/
  kustomization-<app>-config.yaml  # pointer → infrastructure/<app>-config/, dependsOn above

infrastructure/<app>/
  namespace.yaml
  helmrepository.yaml
  helmrelease.yaml

infrastructure/<app>-config/
  <crds-that-need-chart-installed-first>.yaml
```
