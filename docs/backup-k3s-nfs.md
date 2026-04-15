# K3s NFS PVC Backup & Restore

## Architecture

The ZFS dataset `fast/k3s-nfs` on the Proxmox host (`192.168.8.89`) is the NFS backend
for all Kubernetes PVCs using the `nfs-zfs` StorageClass. A Dagu DAG (`k3s-nfs-backup`)
runs daily at 02:00 on the backup LXC and backs up the full dataset to both local and
cloud Proxmox Backup Server.

Flow per run:

1. **ZFS snapshot** — `fast/k3s-nfs@k3s-nfs-backup-<run-id>` is created for crash
   consistency (Prometheus, Grafana, and Seerr continue running against the live dataset
   while the backup reads from the frozen snapshot)
2. **Local PBS backup** — each PVC subdirectory under the snapshot is uploaded to the
   `k3s-nfs` datastore on the local PBS (`192.168.8.189`) using `proxmox-backup-client`
   with client-side AES-256 encryption
3. **Cloud PBS backup** — same subdirectories uploaded to the cloud PBS, identified by
   `--backup-id k3s-nfs` (separate from the existing combined appdata/personal-files backup)
4. **Snapshot destroyed** — exit handler always destroys the ZFS snapshot regardless of
   backup outcome

On local backup failure, the cloud backup still runs (`continue_on: failure: true`). Both
failures send Pushover and Slack notifications via `pbs-backup-common.sh`.

## Backup source

All subdirectories of `/fast/k3s-nfs/` are dynamically enumerated — new PVCs using
`nfs-zfs` are automatically included without any script changes. Current directories:

| Directory                                                       | Contents            |
| --------------------------------------------------------------- | ------------------- |
| `monitoring-prometheus-kube-prometheus-stack-prometheus-db-...` | Prometheus TSDB     |
| `monitoring-kube-prometheus-stack-grafana`                      | Grafana data        |
| `seerr-seerr-config`                                            | Seerr configuration |

Archived PVCs (renamed to `archived-<namespace>-<pvc-name>` on deletion) are also
captured automatically.

## Implementation

DAG and backup scripts live in the `hac` repo:

```
hosts/stacks/dagu/dags/
  k3s-nfs-backup.hbs.yaml                      # Dagu DAG
  scripts/
    k3s-nfs-local-pbs-backup.hbs.sh            # Local PBS backup script
    k3s-nfs-cloud-pbs-backup.hbs.sh            # Cloud PBS backup script
    keys/k3s-nfs.hbs.key                        # Encryption key (rendered from Pulumi secret)
```

## First-time setup

### 1. Create the PBS datastore (local PBS web UI or CLI)

```bash
# On the local PBS host
proxmox-backup-manager datastore create k3s-nfs /mnt/datastore/k3s-nfs
proxmox-backup-manager datastore update k3s-nfs \
  --keep-last 30 \
  --keep-monthly 6 \
  --keep-yearly 1
```

### 2. Generate the encryption key

Run this on any machine with `proxmox-backup-client` installed (your desktop, the backup
LXC, the PVE host, etc.):

```bash
# Interactive — you will be prompted to set a password
proxmox-backup-client key create ./k3s-nfs.key

# Copy the full JSON output — this is SECRET_K3S_NFS_PBS_CLIENT_KEY
cat ./k3s-nfs.key
```

Keep the key file and password safe — they are required for any restore. The key is
stored as a Pulumi secret and SCP'd to the PVE host at backup runtime; it does not need
to live on the PVE host permanently.

### 3. Add Pulumi secrets (from the hac repo)

```bash
# Key file contents (the JSON from cat /root/k3s-nfs.key)
pulumi config set --secret "lxc#backup#dagu:SECRET_K3S_NFS_PBS_CLIENT_KEY" '<key-json>'

# Password used when generating the key
pulumi config set --secret "lxc#backup#dagu:SECRET_K3S_NFS_ENCRYPTION_PASSWORD" '<password>'

# Create two healthcheck monitors and paste their ping URLs
pulumi config set --secret "lxc#backup#dagu:SECRET_K3S_NFS_HEALTHCHECK_URL" '<url>'
pulumi config set --secret "lxc#backup#dagu:SECRET_K3S_NFS_CLOUD_HEALTHCHECK_URL" '<url>'
```

### 4. Deploy

```bash
cd /home/akmin/workspace/hac
pulumi up
```

Dagu picks up the new DAG automatically from its schedule.

## Restore procedure

### Prerequisites

- SSH access to the Proxmox host (`192.168.8.89`)
- The encryption key file (`/root/k3s-nfs.key` on the PVE host, or export it again
  from the Pulumi secret `SECRET_K3S_NFS_PBS_CLIENT_KEY`)
- The encryption password (`SECRET_K3S_NFS_ENCRYPTION_PASSWORD` from Pulumi)
- PBS credentials (`SECRET_EDSAC_PBS_PASSWORD` for local PBS)

### List available backups

```bash
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89

export PBS_PASSWORD='<pbs-password>'
export PBS_ENCRYPTION_PASSWORD='<encryption-password>'

proxmox-backup-client snapshot list \
  --repository root@pam@192.168.8.189:k3s-nfs \
  --keyfile /root/k3s-nfs.key
```

The `backup-id` is `k3s-nfs`. Each snapshot contains one `.pxar` archive per PVC.

### Restore a single PVC directory

Scale down the affected workload first so nothing is writing to the directory:

```bash
# Example: restore Seerr config from latest backup
kubectl scale deployment seerr -n seerr --replicas=0

# On the Proxmox host — replace <snapshot-id> with e.g. host/k3s-nfs/2025-01-15T02:05:00Z
proxmox-backup-client restore <snapshot-id> seerr-seerr-config.pxar \
  /fast/k3s-nfs/seerr-seerr-config/ \
  --repository root@pam@192.168.8.189:k3s-nfs \
  --keyfile /root/k3s-nfs.key

# Scale the workload back up
kubectl scale deployment seerr -n seerr --replicas=1
```

### Restore all PVC data (full restore)

```bash
# Scale down all workloads that use nfs-zfs PVCs
kubectl scale deployment --all -n monitoring --replicas=0
kubectl scale deployment --all -n seerr --replicas=0

# Restore each archive — list archive names from the snapshot list output
# and repeat for each .pxar archive in the snapshot
proxmox-backup-client restore <snapshot-id> monitoring-kube-prometheus-stack-grafana.pxar \
  /fast/k3s-nfs/monitoring-kube-prometheus-stack-grafana/ \
  --repository root@pam@192.168.8.189:k3s-nfs \
  --keyfile /root/k3s-nfs.key

# ... repeat for each archive ...

# Scale workloads back up
kubectl scale deployment --all -n monitoring --replicas=1
kubectl scale deployment --all -n seerr --replicas=1
```

### Restore from cloud PBS

Same procedure, substitute the cloud PBS repository:

```bash
export PBS_PASSWORD='<cloud-api-token>'  # SECRET_CLOUD_PBS_API_TOKEN_SECRET

proxmox-backup-client restore <snapshot-id> <archive-name>.pxar \
  /fast/k3s-nfs/<archive-name>/ \
  --repository '78cce27bc683469fb4cd@pbs!homelab-pve@sh19-112.prod.cloud-pbs.com:78cce27bc683469fb4cd' \
  --keyfile /root/k3s-nfs.key
```

## Monitoring

- Dagu web UI shows DAG run history, step logs, and failure status
- Pushover and Slack notifications fire on any script error
- Healthcheck monitors alert if backups stop completing
- PBS web UI (`https://192.168.8.189:8007`) shows backup history and storage usage for
  the `k3s-nfs` datastore

See also: [NFS Storage Setup](./nfs-storage-setup.md)
