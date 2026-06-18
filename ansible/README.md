# Ansible — baremetal k3s node provisioning

Provisions **baremetal** Debian 13 (trixie) nodes, replicating the node init that
`pulumi/index.ts` does for Proxmox VMs via cloud-init — minus the VM-only bits
(`qemu-guest-agent`, microcode purge).

Two use cases, one set of roles:

- **Add nodes to the existing cluster** (`inventory/proxmox`) — baremetal nodes join
  the Pulumi-managed control plane at `192.168.20.100` (HA servers and/or agents).
- **Bootstrap a brand-new cluster** (`inventory/minicluster`) — one node
  (`192.168.20.110`) runs `cluster-init`, agents (`.111`, `.112`) join it.

Pulumi remains the source of truth for the Proxmox VMs themselves.

## Layout

```
ansible/
├── ansible.cfg              # no default inventory — always pass -i
├── requirements.yml
├── site.yml                 # base roles on all, then k3s servers, then agents
├── group_vars/all/          # SHARED across every cluster (version, packages, ssh key, disable, labels)
├── inventory/
│   └── minicluster/         # new cluster — .110 cluster-init + .111/.112 agents
└── roles/
    ├── common               # k3s user, packages, locale, timezone
    ├── ssh_hardening        # sshd hardening (key-only, root login key-only)
    ├── sysctl_tuning        # the four /etc/sysctl.d drop-ins
    ├── kernel_tuning        # transparent hugepages off via grub
    └── k3s                  # server (cluster-init or HA-join) / agent, by k3s_role + k3s_cluster_init
```

Per-cluster vars live under `inventory/<cluster>/group_vars/all/`:
`k3s_server_url` (where joiners register) and `k3s_token` (SOPS-encrypted).
Shared, non-secret config lives in the playbook-level `group_vars/all/`.

## One-time setup

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml

# Public key for the k3s user (shared across clusters):
$EDITOR group_vars/all/main.yml          # k3s_user_ssh_key

# Tokens (one per cluster), then encrypt each in place:
$EDITOR inventory/minicluster/group_vars/all/secrets.sops.yml  # NEW token: openssl rand -hex 32
sops -e -i inventory/minicluster/group_vars/all/secrets.sops.yml
```

## Bootstrap the new cluster

Fill in the hosts in `inventory/minicluster/hosts.yml` (already scaffolded with
`.110`/`.111`/`.112`), then:

```bash
cd ansible
ansible-playbook -i inventory/minicluster site.yml --check --diff   # dry run
ansible-playbook -i inventory/minicluster site.yml                  # apply
```

`minicluster-server-01` has `k3s_cluster_init: true` → it bootstraps the cluster.
The agents poll `https://192.168.20.110:6443/healthz` and only join once it's ready,
so a single run brings up the whole cluster.

### Point the cluster at this repo (Flux GitOps)

After `site.yml`, bootstrap Flux (one-time) — mirrors the Pulumi bootstrap in
`pulumi/index.ts`:

```bash
cd ansible
ansible-playbook -i inventory/minicluster flux.yml
```

This runs only on the cluster-init server and:

1. installs **flux-operator** via k3s's built-in `HelmChart` CRD (OCI chart, no helm
   binary / no `curl | bash`),
2. creates the **`sops-age`** secret in `flux-system` from your local age key
   (`flux_sops_age_key_file`, default `~/.config/sops/age/keys.txt`) so
   kustomize-controller can decrypt `*.sops.yaml`,
3. applies a bootstrap **`FluxInstance`** pointing at this repo, path
   `flux_cluster_path` (set per cluster, e.g. `k8s/clusters/minicluster`).

Once Flux syncs, the in-repo `<cluster_path>/flux-system/flux-instance.yaml` becomes
the source of truth. The `FluxInstance` is applied only if absent, so re-running
`flux.yml` won't fight Flux's ownership of its spec.

## Add nodes to the existing cluster

Uncomment/add hosts under `k3s_servers` (HA) and/or `k3s_agents` in
`inventory/proxmox/hosts.yml`, then:

```bash
cd ansible
ansible-playbook -i inventory/proxmox site.yml --check --diff
ansible-playbook -i inventory/proxmox site.yml
```

All nodes join `192.168.20.100`. HA servers inherit the same `disable:` list
(`k3s_disable`) as the existing control plane, as k3s requires.

## Verify

```bash
ansible-inventory -i inventory/minicluster --list   # confirms SOPS decrypt + groups
ansible -i inventory/minicluster all -m ping
# after a run, from a machine with cluster access:
kubectl get nodes -o wide
```

On a node: `cat /sys/kernel/mm/transparent_hugepage/enabled` (shows `[never]` after
reboot), `sysctl net.ipv4.tcp_congestion_control` (=bbr), `systemctl status k3s`
(server) / `k3s-agent` (agent).

## Notes

- SOPS vars are auto-decrypted by the `community.sops.sops` vars plugin
  (`vars_plugins_enabled` in `ansible.cfg`); needs the age key at
  `~/.config/sops/age/keys.txt`. The repo `.sops.yaml` has an `ansible/**` rule that
  encrypts these vars files whole (they have arbitrary keys, not `data/stringData`).
- The new cluster needs its OWN token — do not reuse the proxmox cluster's.
- Ansible connects as **root** over SSH key (`ansible_user: root` in each inventory;
  key at `ansible_ssh_private_key_file`). A fresh baremetal node only has root, and
  the hardened sshd keeps root reachable via key (`PermitRootLogin prohibit-password`,
  `PasswordAuthentication no`), so there's no bootstrap user to juggle. The `common`
  role still creates the unprivileged `k3s` user (with the same key) for parity with
  the VM nodes. No `sudo`/`become` is used — switch `ansible_user` to a sudo user and
  re-enable `become` in `ansible.cfg` if you ever move off root.
