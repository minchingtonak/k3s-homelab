# Runbook: Migrating Flux to a new git remote

## Symptom

After changing the git remote URL (e.g. moving from a self-hosted Forgejo instance to
GitHub), all Flux Kustomizations stall on an old commit and the `GitRepository` shows:

```
NAME          READY   STATUS
flux-system   False   failed to get secret 'flux-system/flux-git-credentials': secrets "flux-git-credentials" not found
```

or a similar auth/connectivity error against the old remote.

## Root cause

The same bootstrapping deadlock that affects path migrations. The `GitRepository`
resource in the cluster still points at the old remote. Flux can't fetch from the new
remote to pick up the `flux-instance.yaml` change that would tell it about the new
remote. It must be patched manually.

**Important:** The `GitRepository` is owned by the flux-operator via `FluxInstance`.
Patching the `GitRepository` directly will be overwritten on the next reconciliation.
Patch the `FluxInstance` instead — the operator will propagate the change to the
`GitRepository` immediately.

## Resolution

### 1. Push to both remotes before making the switch

The new remote must already have the updated content (including the changed
`flux-instance.yaml`) before Flux is pointed at it:

```bash
# Add old remote under a separate name if you haven't already
git remote add old-remote <old-remote-url>

# Push to both
git push origin main          # new remote (e.g. GitHub)
git push old-remote main      # old remote (e.g. Forgejo)
```

### 2. Patch the FluxInstance sync URL

```bash
kubectl patch fluxinstance flux -n flux-system \
  --type=merge \
  -p '{"spec":{"sync":{"url":"https://github.com/<owner>/<repo>.git"}}}'
```

### 3. Remove pullSecret if switching to a public HTTPS remote

If the old remote required authentication, the `FluxInstance` may have a `pullSecret`
field that no longer applies. Remove it with a JSON patch:

```bash
kubectl patch fluxinstance flux -n flux-system \
  --type=json \
  -p '[{"op":"remove","path":"/spec/sync/pullSecret"}]'
```

If the field doesn't exist, this will error — that's fine, skip it.

If you're switching to a **private** repo over HTTPS, create a credentials secret and
set `pullSecret` to point to it instead of removing it:

```bash
kubectl create secret generic flux-git-credentials \
  -n flux-system \
  --from-literal=username=git \
  --from-literal=password=<github-pat>

kubectl patch fluxinstance flux -n flux-system \
  --type=merge \
  -p '{"spec":{"sync":{"pullSecret":"flux-git-credentials"}}}'
```

### 4. Verify the source is fetching

```bash
kubectl get gitrepository flux-system -n flux-system
```

Expected output:

```
NAME          READY   STATUS
flux-system   True    stored artifact for revision 'refs/heads/main@sha1:...'
```

If it's still `False`, check:

```bash
kubectl describe gitrepository flux-system -n flux-system
```

### 5. Force reconciliation

```bash
flux reconcile source git flux-system
flux reconcile kustomization flux-system
```

The dependency chain will propagate automatically. If any Kustomization stalls
(showing `dependency 'flux-system/x' is not ready` even after `x` is green), a second
`flux reconcile source git flux-system` clears it.

## Prevention

Update `flux-instance.yaml` (the in-repo manifest) and the Pulumi `pulumi/index.ts`
bootstrap config at the same time, then push to both remotes before Flux loses access
to the old one. This ensures the new remote already has the correct config waiting when
Flux switches over.
