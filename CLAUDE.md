# CLAUDE.md

You are a Kubernetes and K3s cluster expert assisting with this homelab infrastructure project. You have deep knowledge of K3s internals, Kubernetes primitives, Ansible, Linux networking, and GitOps tooling. Apply that expertise when making decisions, diagnosing issues, and suggesting improvements.

## Research Agent

Do not rely solely on built-in knowledge for questions about K3s, Kubernetes, Ansible, Flux, Helm, or related tooling — this ecosystem moves fast and your training data may be stale.

**Always use live research tools:**

- Use `WebSearch` to find current documentation, release notes, known issues, and best practices
- Use `WebFetch` to pull specific docs pages, GitHub issues, changelogs, or forum threads
- Spawn subagents (Explore, general-purpose) for deeper multi-step research tasks
- Prefer up-to-date sources over recalled knowledge, especially for K3s releases, Helm chart versions, and Flux APIs

## Project Overview

A K3s (lightweight Kubernetes) cluster running on baremetal nodes, bootstrapped with Flux for GitOps. Node provisioning and cluster bring-up are handled by Ansible in `ansible/`; everything running on the cluster is declared in `k8s/` and reconciled by Flux.

## Commands

Node provisioning and Flux bootstrap (run from `ansible/`, always pass an inventory explicitly):

```bash
cd ansible/

# Dry run / apply node + k3s provisioning
ansible-playbook -i inventory/minicluster site.yml --check --diff
ansible-playbook -i inventory/minicluster site.yml

# One-time Flux GitOps bootstrap (cluster-init server only)
ansible-playbook -i inventory/minicluster flux.yml
```

Never run a playbook against a live cluster without explicit approval from the user.

Repo tooling (run from the repo root):

```bash
make setup      # once per clone: install pinned tools + enable .githooks
make check      # formatting + manifest lint — the same commands CI runs
make fmt        # fix formatting
make doctor     # diagnose a broken toolchain
make help       # list targets
```

**Run `make check` before committing.** It reproduces the required CI jobs
exactly, so passing it predicts a passing pipeline. Prefer the `make` targets
over calling `dprint`, `uv` or `scripts/lint-k8s.py` directly — the targets stay
correct when the underlying tooling changes, which is how a hand-rolled
invocation ends up passing locally and failing in CI.

### Dependency management

CLI tools are pinned in `aqua.yaml` and installed by [aqua](https://aquaproj.github.io/);
Python dependencies are declared inline per-script ([PEP 723](https://peps.python.org/pep-0723/))
and resolved by `uv`. Both are managed by Renovate.

Do not install tools by hand, add a `requirements.txt`, or build a virtualenv.
If a tool is missing or an import fails, that is a broken environment — say so
rather than working around it. A tool pinned in two places eventually disagrees
with itself, which is the failure mode this setup exists to prevent.

### Pre-commit hooks

`.githooks/pre-commit` (enabled by `make setup`) mirrors the CI jobs and **fails
closed** — a missing tool refuses the commit rather than skipping the check. It
also blocks committing a `*.sops.yaml` that is not actually encrypted. If a
commit is refused for a reason that looks like tooling rather than content, run
`make doctor`.

## Architecture

`ansible/` provisions Debian 13 (trixie) baremetal nodes and the k3s cluster on top:

- **Roles** — `common` (k3s user, packages, locale, timezone), `ssh_hardening` (key-only sshd), `sysctl_tuning` (TCP hardening, BBR + large buffers, swappiness/overcommit, high inotify limits for the Kubernetes watch API), `kernel_tuning` (transparent hugepages off via grub), `k3s` (server with `cluster-init` or HA-join / agent, selected by `k3s_role` + `k3s_cluster_init`).
- **Inventory** — `inventory/minicluster`: `minicluster-server-01` (`192.168.20.110`, `k3s_cluster_init: true`) plus agents `.111` and `.112`. Agents poll the server's `/healthz` before joining, so one run brings up the whole cluster. Per-cluster vars (`k3s_server_url`, SOPS-encrypted `k3s_token`) live under `inventory/<cluster>/group_vars/all/`; shared non-secret config lives in the playbook-level `group_vars/all/`.
- **Flux bootstrap** (`flux.yml`) — installs flux-operator via `helm install`, creates the `sops-age` Secret in `flux-system` from the local age key, and applies a bootstrap `FluxInstance` pointing at this repo. The bootstrap install is only for fresh-cluster bring-up: in steady state the operator is a first-class Flux-managed component (`k8s/infrastructure/flux-operator`, a HelmRelease Flux adopts via matching `releaseName`), so Renovate bumps upgrade the running operator. `helm install` (not `kubectl apply install.yaml`) is used precisely so a Helm release Secret exists for that adoption.

## GitOps Manifest Layout

Flux watches `k8s/clusters/minicluster` in this repo. The layout uses two layers:

- **`k8s/clusters/<cluster>/`** — Flux `Kustomization` resources (entry points). Each file points to a path under `k8s/infrastructure/` or `k8s/apps/` and controls ordering via `dependsOn`.
- **`k8s/infrastructure/<component>/` and `k8s/apps/<app>/`** — Actual Kubernetes manifests (HelmRepository, HelmRelease, Namespace, CRDs, config resources).

The two-phase pattern is used for operators that install CRDs (e.g. MetalLB): one `Kustomization` installs the operator and waits (`wait: true`), a second `Kustomization` with `dependsOn` applies CRD-backed config resources only after the CRDs exist.

`scratch/` holds WIP manifests not yet wired into Flux. `scripts/get-kubeconfig.sh` is a helper to pull kubeconfig from the server node.

`AGENTS.md` is the operating contract for the Hermes agent, which administers this cluster unattended from the ai LXC. It holds a read-only kubeconfig and proposes every change as a pull request. Keep it in sync when the workflow here changes — instructions only reach it once they are merged to `main`, so an edit left uncommitted has no effect.

## Secrets

Cluster secrets are SOPS-encrypted (age) and committed as `*.sops.yaml` files. Flux's kustomize-controller decrypts them at reconcile time using the `sops-age` Secret in `flux-system` (bootstrapped by `ansible/flux.yml`). See README "secrets (SOPS)" for the user-facing view/edit commands.

When creating a new secret:

1. **Always use `stringData:` for Opaque Secrets**, never `data:`. The lint check `secret-stringdata` in `scripts/lint-k8s.py` enforces this. Plaintext values are far easier to review and edit; Kubernetes base64-encodes them at apply time so the in-cluster Secret is identical.

2. **Ask the user for secret values directly** — do not pull them from `.env` files, other hosts, or guess. If a placeholder is needed (e.g. for an OAuth client_secret before the provider has been set up), use a clearly-marked `FIXME` value and tell the user what to replace.

3. **Encrypt before committing**: `sops -e -i path/to/new-secret.sops.yaml`. The `.sops.yaml` config at the repo root applies the age recipient automatically to any path matching `*.sops.yaml`.

4. **Never `kubectl apply` a Secret directly** — commit and let Flux reconcile (general project rule, not secret-specific).

### Sharing secrets across namespaces (substituteFrom pattern)

When a secret value needs to be referenced from a ConfigMap or other manifest (e.g. embedded in an app config file as a placeholder), use Flux `postBuild.substituteFrom` rather than mounting the Secret directly. This keeps the bulk of the config file diffable as plaintext while keeping the actual values encrypted.

Pattern (see `servarr` and `authentik` for live examples):

- Put `${SECRET_FOO}` placeholders inline in the ConfigMap manifest.
- Create a SOPS-encrypted Secret named `<app>-vars` in the `flux-system` namespace containing the actual `SECRET_FOO: <value>` pairs. It lives in a `vars/` subdirectory of the app's own manifest dir (`k8s/apps/<app>/vars/`), so it's obvious at a glance which apps have vars.
- Apply it via a dedicated `<app>-vars` Flux Kustomization with `decryption.provider: sops`, separate from the consuming Kustomization. Its entry-point file sits next to the app's own Kustomization in `k8s/clusters/<cluster>/apps/<app>/`.
- On the consuming Kustomization: add `dependsOn: [<app>-vars]` and `postBuild.substituteFrom: [{ kind: Secret, name: <app>-vars }]`.

The `flux-system` namespace placement is required: `postBuild.substituteFrom` only resolves sources in the Kustomization's own namespace (`flux-system`), not the app's namespace. The chicken-and-egg between secret creation and consumer reconciliation is solved by the separate `*-vars` Kustomization plus `dependsOn`.

### Owner-CR pruning cascades

Pruning a Kustomization that previously contained an owner CR will cascade-delete the resources it owned via Kubernetes garbage collection, even if Flux applies a replacement in the same reconcile. The replacement comes back on the next reconcile (1m later), but `wait: true` with a 5–10m timeout will fail health checks during the gap. Relevant during migrations; not a steady-state concern.

## Cluster Access

kubectl is configured locally and can be used directly — no need to SSH into nodes for cluster operations. To refresh the local kubeconfig from the server node:

```bash
./scripts/get-kubeconfig.sh
```

## Remote Debugging

### K3s nodes

Ansible connects as `root`; the same key works for interactive SSH.

```bash
# SSH into server node
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 root@192.168.20.110

# Run a remote command on the server
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 root@192.168.20.110 "<command>"

# Agent nodes follow the same pattern (IPs: 192.168.20.111, .112)
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 root@192.168.20.111

# Check K3s service status (k3s on the server, k3s-agent on agents)
ssh -i ~/.ssh/k3s_ed25519 root@192.168.20.110 "systemctl status k3s --no-pager"
```

### Proxmox host (192.168.20.89)

Not part of the cluster — it hosts the NAS storage and is the backup target used by the `dagu` backup DAGs.

```bash
ssh -i ~/.ssh/lxc_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 root@192.168.20.89
```

Do not run broad filesystem scans (e.g. `find /`) on it — that hammers the `/fast` and `/void` zpools.
