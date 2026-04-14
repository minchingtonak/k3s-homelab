# Runbook: Flux fails after git repo restructure

## Symptom

After moving the `clusters/homelab` directory to a new path in git (e.g. into
`k8s/clusters/homelab`), all Flux Kustomizations fail with errors like:

```
flux-system   False   kustomization path not found: stat /tmp/.../clusters/homelab: no such file or directory
metallb       False   kustomization path not found: stat /tmp/.../infrastructure/metallb: no such file or directory
cert-manager  False   dependency 'flux-system/traefik' is not ready
```

## Root cause

This is a bootstrapping deadlock. The `flux-system` Kustomization is responsible
for applying the `FluxInstance` manifest from git — but the `FluxInstance` is also
what tells `flux-system` *where in git to look*. When the path changes:

1. `flux-system` tries to fetch content from the **old** path (`clusters/homelab`)
2. That path no longer exists in git → reconciliation fails
3. Because reconciliation fails, it never applies the updated `flux-instance.yaml`
   that would point it to the new path

Flux can't use itself to update the config that controls itself. The in-cluster
`FluxInstance` must be patched manually to break the deadlock.

The individual Kustomization path errors (`infrastructure/metallb`, etc.) are a
cascading failure: those Kustomization CRDs still reference the old paths until
`flux-system` successfully reconciles and overwrites them from the new location.

## Resolution

### 1. Patch the FluxInstance sync path directly

```bash
kubectl patch fluxinstance -n flux-system flux \
  --type=merge \
  -p '{"spec":{"sync":{"path":"k8s/clusters/homelab"}}}'
```

Replace `k8s/clusters/homelab` with whatever the new path is.

### 2. Force a git source reconciliation

```bash
flux reconcile source git flux-system
```

This fetches the latest git commit and applies the new `k8s/clusters/homelab`
content. `flux-system` and the Layer 0 Kustomizations (metallb, sealed-secrets,
nfs-provisioner, reloader) should turn green immediately.

### 3. Wait for the dependency chain to propagate

The remaining Kustomizations will unblock in order as their dependencies become
ready:

```
Layer 0: metallb, sealed-secrets, nfs-provisioner, reloader
Layer 1: metallb-config, traefik
Layer 2: traefik-config, cert-manager, external-dns
Layer 3: cert-manager-config, headlamp, homepage, kube-prometheus-stack, seerr, flux-operator
```

If any layer stalls (Flux's 1h poll interval delays re-evaluation), force it:

```bash
flux reconcile source git flux-system
```

Running this once is usually enough to unblock the full chain.

## Prevention

The bootstrap `FluxInstance` in `index.ts` uses `ignoreChanges: ['spec']` so
Pulumi won't fight with the flux-operator over ownership. However, this also means
Pulumi won't auto-update `sync.path` when the repo is restructured — the manual
patch above is always required when the root path changes.
