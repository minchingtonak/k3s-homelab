# Runbook: Debugging failing Flux reconciliation

## Quick triage

```bash
# Get a snapshot of all Kustomizations
kubectl get kustomizations -A

# Get all HelmReleases
kubectl get helmreleases -A
```

Look for `READY=False`. The `STATUS` column usually contains the error directly. A blocked dependency chain looks like:

```
flux-system   nfs-provisioner         False   health check failed ... HelmRelease/nfs-provisioner/... status: 'Failed'
flux-system   kube-prometheus-stack   False   dependency 'flux-system/nfs-provisioner' is not ready
flux-system   seerr                   False   dependency 'flux-system/nfs-provisioner' is not ready
```

Start at the root failure (`nfs-provisioner`) — fixing it unblocks the chain.

---

## Common failure patterns

### 1. HelmRelease stalled — retries exceeded

**Symptom:** HelmRelease `READY=False`, status `Helm rollback ... succeeded` or `Failed to upgrade after N attempt(s)`.

```bash
kubectl describe helmrelease <name> -n <namespace>
# Scroll to Events and "Last Helm logs" for the exact error
```

**Resolution:** fix the underlying cause (see patterns below), delete any immutable resource if needed, then reset the stalled release:

```bash
flux reconcile helmrelease <name> -n <namespace> --reset
```

`--reset` clears the `RetriesExceeded` stall so Flux will attempt the upgrade again.

---

### 2. StorageClass parameter change rejected

**Symptom:**

```
StorageClass "nfs-zfs" is invalid: parameters: Forbidden: updates to parameters are forbidden.
```

**Root cause:** StorageClass `parameters` are **immutable** in Kubernetes. Any change to `parameters` in a HelmRelease's values (e.g. `archiveOnDelete`, `pathPattern`) causes every Helm upgrade to fail because the API server rejects the patch.

**Resolution:**

1. Check what's currently live vs. what Helm is trying to apply:

   ```bash
   kubectl get storageclass nfs-zfs -o yaml
   ```

2. Delete the StorageClass. Existing PVs and their data are unaffected — they retain their bindings:

   ```bash
   kubectl delete storageclass nfs-zfs
   ```

3. Reset the stalled HelmRelease:

   ```bash
   flux reconcile helmrelease nfs-subdir-external-provisioner -n nfs-provisioner --reset
   ```

   Helm will recreate the StorageClass with the new parameters.

4. Reconcile any Kustomizations that were blocked:
   ```bash
   flux reconcile kustomization nfs-provisioner -n flux-system
   flux reconcile kustomization kube-prometheus-stack -n flux-system
   flux reconcile kustomization seerr -n flux-system
   ```

**Note:** the same pattern applies to any other immutable resource (e.g. certain fields on `Job`, `PersistentVolume`). The fix is always: delete the resource, let Helm recreate it.

---

### 3. Dependency not ready

**Symptom:**

```
dependency 'flux-system/<name>' is not ready
```

This is a cascading failure — not the root cause. Fix the dependency it's waiting on and the blocked Kustomizations will self-resolve within Flux's poll interval (1h). To unblock immediately:

```bash
flux reconcile kustomization <blocked-name> -n flux-system
```

---

### 4. HelmRelease — chart fetch failed

**Symptom:** `HelmRepository` or chart version not found.

```bash
kubectl get helmrepositories -A
kubectl describe helmrepository <name> -n <namespace>
```

Common causes:

- Chart version pinned in `helmrelease.yaml` doesn't exist in the repo
- HelmRepository URL is unreachable (check DNS / network from within the cluster)

Force a repository refresh:

```bash
flux reconcile source helm <repo-name> -n <namespace>
```

---

### 5. Git source not pulling

**Symptom:** Kustomizations are stuck on an old git SHA, or `flux get sources git` shows an error.

```bash
flux get sources git -A
```

Force a fetch:

```bash
flux reconcile source git flux-system
```

If it fails, check the Forgejo repo is reachable and the credentials in the `flux-system` secret are valid.

---

## Forcing a full reconciliation

If many resources are stuck after a fix, reconcile the git source first — it cascades down:

```bash
flux reconcile source git flux-system
```

To reconcile every Kustomization at once:

```bash
kubectl get kustomizations -n flux-system -o name \
  | xargs -I{} flux reconcile {} -n flux-system
```

---
