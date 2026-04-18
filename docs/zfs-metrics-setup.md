# ZFS Metrics Setup

Prometheus scrapes ZFS pool metrics from the Proxmox host (192.168.8.89) via a static node-exporter target. The K3s VMs themselves don't have ZFS — the pool lives on the Proxmox host, mounted to the cluster via NFS.

## Install node-exporter on the Proxmox host

SSH into the Proxmox host and run:

```bash
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89
```

Install and enable node-exporter with the ZFS collector:

```bash
apt-get install -y prometheus-node-exporter

# Enable the ZFS collector (disabled by default on Linux)
mkdir -p /etc/default
cat > /etc/default/prometheus-node-exporter <<'EOF'
ARGS="--collector.zfs"
EOF

systemctl enable prometheus-node-exporter
systemctl restart prometheus-node-exporter
```

Verify it's up and exposing ZFS metrics:

```bash
curl -s http://localhost:9100/metrics | grep ^node_zfs
```

You should see metrics like:
- `node_zfs_arc_size` — ARC (adaptive replacement cache) size
- `node_zfs_pool_state` — pool health (0 = online)
- `node_zfs_pool_io_*` — pool read/write throughput

## Prometheus scrape target

The scrape config is already wired in the HelmRelease at `prometheus.prometheusSpec.additionalScrapeConfigs`. Once node-exporter is running on the Proxmox host, Prometheus will pick up the target automatically on next reconciliation.

Verify the target is up in Prometheus:

```
https://prometheus.item.fyi/targets
```

Look for `proxmox-host` under the `proxmox-host` job.

## Grafana dashboard

Dashboard 7845 (ZFS pool metrics) is provisioned automatically via the HelmRelease. It appears in Grafana under **Community → ZFS Pool** once Prometheus has data from the Proxmox host.

## Firewall note

If the Proxmox host has a firewall (Proxmox Datacenter → Firewall), ensure port 9100 is accessible from the K3s cluster network (192.168.8.0/24). Node-exporter binds to all interfaces by default.
