# NFS Storage Setup

Kubernetes persistent storage is backed by a ZFS dataset on the Proxmox host, exported via NFS. The `nfs-zfs` StorageClass provisions subdirectories under this export for each PVC.

## Proxmox host setup (one-time)

SSH into the Proxmox host:

```bash
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89
```

### Create the ZFS dataset

```bash
zfs create -o compression=lz4 \
           -o atime=off \
           -o xattr=sa \
           -o acltype=posixacl \
           fast/k3s-nfs
```

Mounts at `/fast/k3s-nfs` by default (inherits the pool mountpoint).

### Install and configure the NFS server

```bash
apt update && apt install -y nfs-kernel-server
echo '/fast/k3s-nfs 192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash)' >> /etc/exports
exportfs -ra
systemctl enable --now nfs-kernel-server
```

`no_root_squash` is required — many container init processes run as root to set up
file permissions, and NFS clients are trusted (K3s VMs only).

### Verify

```bash
showmount -e localhost
# Expected: /fast/k3s-nfs  192.168.8.0/24
```

Test mount from a K3s node:

```bash
ssh -i ~/.ssh/k3s_ed25519 k3s@192.168.8.100 \
  "sudo mount -t nfs 192.168.8.89:/fast/k3s-nfs /mnt && ls /mnt && sudo umount /mnt"
```

## Kubernetes side

The `nfs-subdir-external-provisioner` Helm chart is deployed via Flux at
`k8s/infrastructure/nfs-provisioner/`. It creates the `nfs-zfs` StorageClass, which:

- Provisions each PVC as a subdirectory: `/fast/k3s-nfs/<namespace>-<pvc-name>/`
- Uses `reclaimPolicy: Retain` — deleted PVCs are archived as `archived-<namespace>-<pvc-name>/` rather than destroyed
- Does **not** enforce storage quotas (the 50Gi Prometheus request is advisory; Prometheus self-limits via `retentionSize`)

To use it, set `storageClassName: nfs-zfs` on a PVC. To make it the cluster default,
set `storageClass.defaultClass: true` in the HelmRelease values and annotate
`local-path` with `storageclass.kubernetes.io/is-default-class: "false"`.

## Checking usage

```bash
# PVC subdirectories on the Proxmox host
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89 "du -sh /fast/k3s-nfs/*"

# ZFS dataset usage
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89 "zfs list fast/k3s-nfs"

# Set a dataset quota if needed
ssh -i ~/.ssh/lxc_ed25519 root@192.168.8.89 "zfs set quota=500G fast/k3s-nfs"
```
