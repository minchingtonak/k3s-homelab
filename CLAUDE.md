# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TypeScript + Pulumi Infrastructure-as-Code project that provisions a K3s (lightweight Kubernetes) cluster on Proxmox VE. All infrastructure is defined in `index.ts`.

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
pnpm exec tsc --noEmit
```

## Architecture

All infrastructure is defined in a single `index.ts` with these logical sections (some currently commented out):

1. **Network bridge** — Creates `vmbr1`, a Linux bridge for inter-node cluster traffic (layer 2, no IP). Nodes use `vmbr0` (host) + `vmbr1` (cluster-internal).

2. **Cloud image** — Downloads Debian 13 genericcloud QCOW2 from the official Debian repository into Proxmox local ISO storage.

3. **Cloud-init snippets** — Shared sysctl configs applied to every node:
   - TCP hardening (disables IPv6, enables syncookies, RFC 3704 filtering)
   - Network optimizations (BBR, TCP Fast Open, 16MB buffers)
   - Memory optimizations (swappiness=2, overcommit for K3s)
   - Kernel optimizations (high inotify limits for Kubernetes watch API)
   - SSH hardening (key-only auth, no root login)
   - fail2ban jail (3 retries, 24h ban)

4. **K3s server cloud-init** — Control plane (`k3s-server-01`, IP `10.0.0.100`). Installs K3s in cluster-init mode with Traefik and ServiceLB disabled.

5. **K3s agent cloud-init** — Worker nodes. Includes a health-check loop that waits for the server at `10.0.0.100:6443` before joining.

6. **VM provisioning** (commented out) — Template VM (vmId 9000) cloned into server (vmId 101) and agents (vmId 102+). Agents: 4 CPU / 8GB RAM / 50GB disk. Server: 2 CPU / 4GB RAM / 20GB disk.

## Stack Configuration

Secrets in `Pulumi.dev.yaml` are Pulumi-encrypted:
- `k3s-homelab:proxmoxEndpoint` — Proxmox API URL (`https://192.168.8.89:8006`)
- `k3s-homelab:proxmoxPassword` — Proxmox API password
- `k3s-homelab:k3sToken` — Shared K3s cluster token

To set/update secrets: `pulumi config set --secret k3s-homelab:<key> <value>`

## Remote Debugging

> **Network warning:** SSH connections to VMs from the desktop are intermittently laggy and can freeze for over a minute before recovering. This is a known infrastructure issue. When running remote commands, use `ConnectTimeout` and `ServerAliveInterval` flags, and treat timeouts as transient rather than fatal. Avoid commands that stream output indefinitely (e.g. `tail -f`) unless in an interactive session.

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

### Known issues

- **Agent cannot reach server node** — `curl https://192.168.8.100:6443` from the agent VM fails. This is currently blocking agent nodes from joining the cluster. The agent cloud-init health-check loop (`until curl -sk https://${k3sServerIp}:6443/healthz`) will spin indefinitely until this is resolved. Both VMs are on `vmbr0` in the same `/24` so routing is not the issue. Likely culprits to investigate:
  1. **K3s iptables rules** — K3s in `cluster-init` mode can add rules that drop traffic not originating from `127.0.0.1`. Check with `sudo iptables -L -n -v` on the server.
  2. **Proxmox firewall** — May be enabled on the vmbr0 bridge or the VM's network device. Check in the Proxmox web UI under Datacenter/Node/VM → Firewall.
  3. **`rp_filter = 1` sysctl** — Enabled in our cloud-init hardening. If the VM's routing table is asymmetric it silently drops packets. Check with `sudo sysctl net.ipv4.conf.all.rp_filter`.
  - To narrow it down, run `sudo tcpdump -i eth0 host 192.168.8.101` on the server while the agent attempts to connect — if packets arrive but get no response, it's iptables; if packets never arrive, it's Proxmox firewall or a lower-level issue.

### Clean redeployment

It is fine to do a full `pulumi destroy && pulumi up` to wipe and redeploy all VMs when testing infrastructure changes. This is the preferred approach over trying to patch a broken VM in place.

## Key Dependencies

- `@muhlba91/pulumi-proxmoxve` — Pulumi provider for Proxmox VE API
- `@pulumi/pulumi` — Pulumi core SDK

TypeScript is compiled by Pulumi at deploy time (`bin/` is gitignored). Use pnpm as the package manager.
