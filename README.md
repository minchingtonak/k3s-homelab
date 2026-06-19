# k3s-homelab

homelab k3s cluster running on [Proxmox VE](https://www.proxmox.com) virtual machines. infrastructure provisioned with [Pulumi](https://github.com/pulumi/pulumi), cluster workloads managed by [FluxCD](https://github.com/fluxcd/flux2) GitOps

## clusters (`k8s/clusters`)

flux `Kustomization` entrypoints for the homelab cluster. defines the reconciliation order and dependencies between infrastructure and app workloads. this is the top-level entry point flux watches

## cluster infrastructure (`k8s/infrastructure`)

- [Traefik](https://github.com/traefik/traefik) for ingress & SSL
- [cert-manager](https://github.com/cert-manager/cert-manager) for automatic SSL certificate generation (using DNS-01 challenge)
- [external-dns](https://github.com/kubernetes-sigs/external-dns) for automatic DNS record updates
  - [konnektr-io/external-dns-porkbun-webhook](https://github.com/konnektr-io/external-dns-porkbun-webhook) for Porkbun integration
- [MetalLB](https://github.com/metallb/metallb) for load balancing
- [CoreDNS](https://github.com/coredns/coredns) for in-cluster DNS
- [Authentik](https://github.com/goauthentik/authentik) as the cluster identity & SSO provider
- [Gatus](https://github.com/TwiN/gatus) for endpoint health monitoring and status page
- [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) for cluster monitoring
  - [Prometheus](https://github.com/prometheus/prometheus) for metric collection
  - [Alertmanager](https://github.com/prometheus/alertmanager) for alert routing and notification delivery
  - [Grafana](https://github.com/grafana/grafana) for metric & log display
- [Loki](https://github.com/grafana/loki) for log storage & querying
- [Grafana Alloy](https://github.com/grafana/alloy) for log collection and forwarding
- [FluxCD](https://github.com/fluxcd/flux2) for GitOps continuous delivery with [flux-operator](https://github.com/controlplaneio-fluxcd/flux-operator)
- [SOPS](https://github.com/getsops/sops) (with age) for encrypted secret management in Git, decrypted by Flux at reconcile time
- [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller) for automated k3s node upgrades via GitOps
- [reloader](https://github.com/stakater/Reloader) to restart pods when config changes
- [descheduler](https://github.com/kubernetes-sigs/descheduler) to dynamically schedule pods based on node metrics
- [nfs-subdir-external-provisioner](https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner) for simple distributed storage
- [Longhorn](https://github.com/longhorn/longhorn) for distributed block storage

## apps (`k8s/apps`)

user-facing applications deployed to the cluster. see manifests for details

## development

after cloning, configure git hooks:

```bash
make setup
```

## secrets (SOPS)

secrets are stored as `*.sops.yaml` files alongside other manifests, encrypted with age. the private key lives in `~/.config/sops/age/keys.txt`.

view a decrypted secret:

```bash
sops -d k8s/infrastructure/gatus/pushover-secret.sops.yaml
```

edit a secret interactively. opens `$EDITOR` with plaintext, re-encrypts on save:

```bash
sops k8s/infrastructure/gatus/pushover-secret.sops.yaml
```

## credits

- https://github.com/ahgraber/homelab-gitops-k3s
- https://github.com/ehlesp/smallab-k8s-pve-guide
- https://github.com/chr1sd/home-ops
