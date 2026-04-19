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

TypeScript + Pulumi Infrastructure-as-Code project that provisions a K3s (lightweight Kubernetes) cluster on Proxmox VE, bootstrapped with Flux for GitOps. All infrastructure is defined in `index.ts`.

## Commands

```bash
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

All infrastructure is defined in a single `index.ts` with these sections:

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

5. **K3s server node** (vmId 100) — Control plane (`k3s-server-01`, IP `192.168.8.100`). 2 CPU / 4GB RAM / 10GB disk. Installs K3s in cluster-init mode with Traefik and ServiceLB disabled.

6. **K3s agent nodes** (vmId 101+) — Worker nodes. 4 CPU / 8GB RAM / 50GB disk. Health-check loop waits for server at `192.168.8.100:6443` before joining. Currently `agentCount = 1`.

7. **Flux bootstrap** — Fetches kubeconfig from the server node via SSH (replacing `127.0.0.1` with the external IP), writes it to `~/.kube/k3s-homelab`, installs the Flux Operator via Helm, then applies a `FluxInstance` CRD that bootstraps Flux against the git repository. Flux manages `clusters/homelab` path.

## GitOps Manifest Layout

Flux watches `clusters/homelab` in this repo. The layout uses two layers:

- **`clusters/homelab/`** — Flux `Kustomization` resources (entry points). Each file here points to a path in `infrastructure/` and controls ordering via `dependsOn`.
- **`infrastructure/<component>/`** — Actual Kubernetes manifests (HelmRepository, HelmRelease, Namespace, CRDs, config resources).

The two-phase pattern is used for operators that install CRDs (e.g. MetalLB): one `Kustomization` installs the operator and waits (`wait: true`), a second `Kustomization` with `dependsOn` applies CRD-backed config resources only after the CRDs exist.

`scratch/` holds WIP manifests not yet wired into Flux. `scripts/get-kubeconfig.sh` is a helper to pull kubeconfig from the server node.

## Stack Configuration

Config and secrets in `Pulumi.dev.yaml` are Pulumi-encrypted:
- `k3s-homelab:proxmoxEndpoint` — Proxmox API URL (`https://192.168.8.89:8006`)
- `k3s-homelab:proxmoxPassword` — Proxmox API password
- `k3s-homelab:k3sToken` — Shared K3s cluster token
- `k3s-homelab:sshPublicKey` — SSH public key injected into all VMs via cloud-init
- `k3s-homelab:sshPrivateKey` — SSH private key used to fetch kubeconfig from the server node
- `k3s-homelab:gitRepo` — Git repository HTTPS URL for Flux GitOps

To set/update secrets: `pulumi config set --secret k3s-homelab:<key> <value>`

## Cluster Access

kubectl is configured locally and can be used directly — no need to SSH into nodes for cluster operations. If the cluster has been redeployed (e.g. after `pulumi destroy && pulumi up`), refresh the local kubeconfig first:

```bash
./scripts/get-kubeconfig.sh
```

## Remote Debugging

### Proxmox host (192.168.8.89)

```bash
# SSH into Proxmox
ssh -i ~/.ssh/lxc_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 root@192.168.8.89

# List VMs
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89 "qm list"

# View cloud-init output log for a running VM (e.g. vmId 100)
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89 "cat /var/log/qemu-server/100.log"
```

### K3s nodes

```bash
# SSH into server node
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=6 k3s@192.168.8.100

# Run a remote command on the server
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 k3s@192.168.8.100 "<command>"

# Agent nodes follow the same pattern (IPs: 192.168.8.101, .102, ...)
ssh -i ~/.ssh/k3s_ed25519 -o ConnectTimeout=30 k3s@192.168.8.101

# Tail cloud-init log on a node (snapshot only — avoid -f due to lag)
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.8.100 "sudo cat /var/log/cloud-init-output.log"

# Check K3s service status
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.8.100 "sudo systemctl status k3s --no-pager"

# Re-run cloud-init after updating a snippet (e.g. after pulumi up)
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.8.100 "sudo cloud-init clean && sudo reboot"
```

### Clean redeployment

It is fine to do a full `pulumi destroy && pulumi up` to wipe and redeploy all VMs when testing infrastructure changes. This is the preferred approach over trying to patch a broken VM in place.

## Key Dependencies

- `@muhlba91/pulumi-proxmoxve` — Pulumi provider for Proxmox VE API
- `@pulumi/pulumi` — Pulumi core SDK
- `@pulumi/command` — Remote/local commands (used to fetch and write kubeconfig)
- `@pulumi/kubernetes` — Kubernetes provider (also used for Flux Operator Helm release and FluxInstance CRD)

TypeScript is compiled by Pulumi at deploy time (`bin/` is gitignored). Use pnpm as the package manager.
