# Debian host OS updates

Every Debian host in the cluster fleet patches itself daily through
`unattended-upgrades`; reboots are always manual. Nothing reboots a node on
its own - when a reboot is needed, an alert fires and a human triggers it
through kured, which drains, reboots and uncordons the node safely.

## Layer 1 - automatic patching (unattended-upgrades)

Configured by `ansible/roles/unattended_upgrades` on every node:

- Origins: the stock Debian security + point-release set from
  `50unattended-upgrades`, plus `${distro_codename}-updates` via
  `/etc/apt/apt.conf.d/52unattended-upgrades-local`.
- `apt-daily.timer` refreshes lists and downloads packages; the
  `apt-daily-upgrade.timer` slot (daily ~6:00 + jitter) installs them.
- `Automatic-Reboot` is off everywhere, on purpose (see below).
- `reboot-notifier` is installed so libc/OpenSSL-level updates also set
  `/var/run/reboot-required`, not just kernel updates.

What was updated, when, is logged on each host:

- `/var/log/unattended-upgrades/unattended-upgrades.log` - per-run summary
- `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log` - full dpkg output
- `/var/log/dpkg.log` - every package transaction

## Layer 2 - metrics and alerts (Prometheus)

The role installs `prometheus-node-exporter-collectors` and a systemd timer
(`apt-info-prom.timer`) that publishes apt metrics every 30 minutes to
`/var/lib/node_exporter/textfile_collector`, which the kube-prometheus-stack
node-exporter DaemonSet mounts (see the `prometheus-node-exporter` values in
`k8s/infrastructure/kube-prometheus-stack/helmrelease.yaml`).

Alerts (`k8s/infrastructure/kube-prometheus-stack-config/os-updates-alerts.yaml`)
all arrive through the existing Alertmanager Pushover receiver:

| Alert                  | Fires when                         | Meaning                           |
| ---------------------- | ---------------------------------- | --------------------------------- |
| `DebianUpdatesStalled` | pending updates > 0 for 24h        | daily upgrade failed              |
| `DebianAptCacheStale`  | no apt update for 48h              | apt-daily.timer broken            |
| `DebianRebootRequired` | `/var/run/reboot-required` present | a reboot is due - trigger kured   |
| `HostRebooted`         | boot time < 10m                    | after-the-fact record of a reboot |

## Layer 3 - manual reboots via kured

kured (`k8s/infrastructure/kured`) watches a custom sentinel,
`/var/run/kured-reboot` - deliberately NOT the stock
`/var/run/reboot-required`, so a pending reboot never reboots anything by
itself. To reboot a node:

```bash
ssh root@<node> touch /var/run/kured-reboot
```

Within 5 minutes kured acquires the cluster-wide reboot lock, cordons and
drains the node (Longhorn evicts/rebuilds replicas; the drain has no timeout
so it waits for rebuilds), reboots, waits for the node to return, and
uncordons. Each stage notifies Pushover (drain / reboot / uncordoned).

With 3 nodes and 3 replicas per volume, `concurrency: 1` guarantees two
healthy replicas stay available throughout. Remove the sentinel file after
the reboot if you want a clean slate (`rm /var/run/kured-reboot` after
uncordon; kured deletes it itself as part of the reboot flow).

## Rolling out to other hosts

The Ansible role is host-agnostic: add the role to a host's playbook and, if
the host is not a k3s node, also scrape its node-exporter with a static
target (see the `proxmox-host` job in
`k8s/infrastructure/kube-prometheus-stack/helmrelease.yaml` and
`docs/zfs-metrics-setup.md` for the pattern). Unattended-upgrades config and
the apt metrics timer work identically on any Debian 12/13 host.

## Verification

After applying the Ansible role on a node:

```bash
systemctl status apt-info-prom.timer
cat /var/lib/node_exporter/textfile_collector/apt_info.prom
```

After kured is deployed: `kubectl -n kured get ds,kustomization` - and the
`kured` DaemonSet should show 3/3 ready.
