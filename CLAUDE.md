# CLAUDE.md

You are a Kubernetes and K3s cluster expert assisting with this homelab infrastructure project. You have deep knowledge of K3s internals, Kubernetes primitives, Proxmox VE, cloud-init, Linux networking, and GitOps tooling. Apply that expertise when making decisions, diagnosing issues, and suggesting improvements.

## Research Agent

Do not rely solely on built-in knowledge for questions about K3s, Kubernetes, Proxmox, Flux, Helm, or related tooling — this ecosystem moves fast and your training data may be stale.

**Always use live research tools:**

- Use `WebSearch` to find current documentation, release notes, known issues, and best practices
- Use `WebFetch` to pull specific docs pages, GitHub issues, changelogs, or forum threads
- Spawn subagents (Explore, general-purpose) for deeper multi-step research tasks
- Prefer up-to-date sources over recalled knowledge, especially for K3s releases, Helm chart versions, Flux APIs, and Proxmox behavior

## Project Overview

TypeScript + Pulumi Infrastructure-as-Code project that provisions a K3s (lightweight Kubernetes) cluster on Proxmox VE, bootstrapped with Flux for GitOps. All infrastructure is defined in `pulumi/index.ts`.

## Commands

Pulumi commands must be run from the `pulumi/` directory:

```bash
cd pulumi/

# Preview changes before applying
pulumi preview

# Deploy/update infrastructure
pulumi up

# Tear down infrastructure
pulumi destroy
# OR
pulumi down

# Manage stacks
pulumi stack ls
pulumi stack select dev

# TypeScript type-check (no separate build step — Pulumi compiles on deploy)
pnpm typecheck
```

## Architecture

All infrastructure is defined in a single `pulumi/index.ts` with these sections:

1. **Network bridge** — Creates `vmbr1`, a Linux bridge for inter-node cluster traffic (layer 2, no IP). Nodes use `vmbr0` (host LAN) + `vmbr1` (cluster-internal).

2. **Cloud image** — Downloads Debian 13 genericcloud QCOW2 from the official Debian repository into Proxmox local ISO storage.

3. **Cloud-init snippets** — Shared sysctl configs applied to every node:
   - TCP hardening (disables IPv6, enables syncookies, RFC 3704 filtering)
   - Network optimizations (BBR, TCP Fast Open, 16MB buffers)
   - Memory optimizations (swappiness=2, overcommit for K3s)
   - Kernel optimizations (high inotify limits for Kubernetes watch API)
   - SSH hardening (key-only auth, no root login)
   - fail2ban jail (3 retries, 24h ban)

4. **Base template VM** (vmId 1000) — 2 CPU / 2GB RAM / 10GB disk, cloned for server and agents.

5. **K3s server node** (vmId 100) — Control plane (`k3s-server-01`, IP `192.168.20.100`). 2 CPU / 4GB RAM / 10GB disk. Installs K3s in cluster-init mode with Traefik and ServiceLB disabled.

6. **K3s agent nodes** (vmId 101+) — Worker nodes. 4 CPU / 8GB RAM / 50GB disk. Health-check loop waits for server at `192.168.20.100:6443` before joining. Currently `agentCount = 1`.

7. **Flux bootstrap** — Fetches kubeconfig from the server node via SSH (replacing `127.0.0.1` with the external IP), writes it to `~/.kube/k3s-homelab`, installs the Flux Operator via Helm, then applies a `FluxInstance` CRD that bootstraps Flux against the git repository. Flux manages `clusters/homelab` path. The bootstrap operator install is only for fresh-cluster bring-up: in steady state the operator is a first-class Flux-managed component (`infrastructure/flux-operator`, a HelmRelease Flux adopts via matching `releaseName`), so Renovate bumps actually upgrade the running operator. The minicluster's ansible bootstrap uses `helm install` (not `kubectl apply install.yaml`) so a Helm release Secret exists for that adoption.

## GitOps Manifest Layout

Flux watches `clusters/homelab` in this repo. The layout uses two layers:

- **`clusters/homelab/`** — Flux `Kustomization` resources (entry points). Each file here points to a path in `infrastructure/` and controls ordering via `dependsOn`.
- **`infrastructure/<component>/`** — Actual Kubernetes manifests (HelmRepository, HelmRelease, Namespace, CRDs, config resources).

The two-phase pattern is used for operators that install CRDs (e.g. MetalLB): one `Kustomization` installs the operator and waits (`wait: true`), a second `Kustomization` with `dependsOn` applies CRD-backed config resources only after the CRDs exist.

`scratch/` holds WIP manifests not yet wired into Flux. `scripts/get-kubeconfig.sh` is a helper to pull kubeconfig from the server node.

## Stack Configuration

Config and secrets in `pulumi/Pulumi.dev.yaml` are Pulumi-encrypted:

- `k3s-homelab:proxmoxEndpoint` — Proxmox API URL (`https://192.168.20.89:8006`)
- `k3s-homelab:proxmoxPassword` — Proxmox API password
- `k3s-homelab:k3sToken` — Shared K3s cluster token
- `k3s-homelab:sshPublicKey` — SSH public key injected into all VMs via cloud-init
- `k3s-homelab:sshPrivateKey` — SSH private key used to fetch kubeconfig from the server node
- `k3s-homelab:gitRepo` — Git repository HTTPS URL for Flux GitOps

To set/update secrets: `pulumi config set --secret k3s-homelab:<key> <value>`

## Secrets

Cluster secrets are SOPS-encrypted (age) and committed as `*.sops.yaml` files. Flux's kustomize-controller decrypts them at reconcile time using the `sops-age` Secret in `flux-system` (bootstrapped by Pulumi). See README "secrets (SOPS)" for the user-facing view/edit commands.

When creating a new secret:

1. **Always use `stringData:` for Opaque Secrets**, never `data:`. The lint check `secret-stringdata` in `scripts/lint-k8s.py` enforces this. Plaintext values are far easier to review and edit; Kubernetes base64-encodes them at apply time so the in-cluster Secret is identical.

2. **Ask the user for secret values directly** — do not pull them from `.env` files, other hosts, or guess. If a placeholder is needed (e.g. for an OAuth client_secret before the provider has been set up), use a clearly-marked `FIXME` value and tell the user what to replace.

3. **Encrypt before committing**: `sops -e -i path/to/new-secret.sops.yaml`. The `.sops.yaml` config at the repo root applies the age recipient automatically to any path matching `*.sops.yaml`.

4. **Never `kubectl apply` a Secret directly** — commit and let Flux reconcile (general project rule, not secret-specific).

### Sharing secrets across namespaces (substituteFrom pattern)

When a secret value needs to be referenced from a ConfigMap or other manifest (e.g. embedded in an app config file as a placeholder), use Flux `postBuild.substituteFrom` rather than mounting the Secret directly. This keeps the bulk of the config file diffable as plaintext while keeping the actual values encrypted.

Pattern (see `servarr` and `authentik` for live examples):

- Put `${SECRET_FOO}` placeholders inline in the ConfigMap manifest.
- Create a SOPS-encrypted Secret in `flux-system` namespace (e.g. `<app>-vars`) containing the actual `SECRET_FOO: <value>` pairs.
- Apply it via a dedicated `<app>-vars` Flux Kustomization with `decryption.provider: sops`, separate from the consuming Kustomization.
- On the consuming Kustomization: add `dependsOn: [<app>-vars]` and `postBuild.substituteFrom: [{ kind: Secret, name: <app>-vars }]`.

The `flux-system` namespace placement is required: `postBuild.substituteFrom` only resolves sources in the Kustomization's own namespace (`flux-system`), not the app's namespace. The chicken-and-egg between secret creation and consumer reconciliation is solved by the separate `*-vars` Kustomization plus `dependsOn`.

### When SealedSecret-style ownership matters

Pruning a Kustomization that previously contained a `SealedSecret` (or any owner CR) will cascade-delete the owned Secret via Kubernetes garbage collection, even if Flux applies a replacement Secret in the same reconcile. The Secret comes back on the next reconcile (1m later), but `wait: true` with a 5–10m timeout will fail health checks during the gap. This is one-time pain during migration; not a steady-state concern.

## Cluster Access

kubectl is configured locally and can be used directly — no need to SSH into nodes for cluster operations. If the cluster has been redeployed (e.g. after `pulumi destroy && pulumi up`), refresh the local kubeconfig first:

```bash
./scripts/get-kubeconfig.sh
```

## Remote Debugging

### Proxmox host (192.168.20.89)

```bash
# SSH into Proxmox
ssh -i ~/.ssh/lxc_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 root@192.168.20.89

# List VMs
ssh -i ~/.ssh/lxc_ed25519 root@192.168.20.89 "qm list"

# View cloud-init output log for a running VM (e.g. vmId 100)
ssh -i ~/.ssh/lxc_ed25519 root@192.168.20.89 "cat /var/log/qemu-server/100.log"
```

### K3s nodes

```bash
# SSH into server node
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 k3s@192.168.20.100

# Run a remote command on the server
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 k3s@192.168.20.100 "<command>"

# Agent nodes follow the same pattern (IPs: 192.168.20.101, .102, ...)
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 k3s@192.168.20.101

# Tail cloud-init log on a node (snapshot only — avoid -f due to lag)
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.20.100 "sudo cat /var/log/cloud-init-output.log"

# Check K3s service status
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.20.100 "sudo systemctl status k3s --no-pager"

# Re-run cloud-init after updating a snippet (e.g. after pulumi up)
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.20.100 "sudo cloud-init clean && sudo reboot"
```

### Clean redeployment

It is fine to do a full `pulumi destroy && pulumi up` to wipe and redeploy all VMs when testing infrastructure changes. This is the preferred approach over trying to patch a broken VM in place.

## Key Dependencies

- `@muhlba91/pulumi-proxmoxve` — Pulumi provider for Proxmox VE API
- `@pulumi/pulumi` — Pulumi core SDK
- `@pulumi/command` — Remote/local commands (used to fetch and write kubeconfig)
- `@pulumi/kubernetes` — Kubernetes provider (also used for Flux Operator Helm release and FluxInstance CRD)

TypeScript is compiled by Pulumi at deploy time (`bin/` is gitignored). Use pnpm as the package manager.
