# k3s-homelab

homelab k3s cluster running on baremetal nodes. nodes provisioned with [Ansible](https://github.com/ansible/ansible) (see `ansible/`), cluster workloads managed by [FluxCD](https://github.com/fluxcd/flux2) GitOps

## clusters (`k8s/clusters`)

flux `Kustomization` entrypoints for the homelab cluster. defines the reconciliation order and dependencies between infrastructure and app workloads. this is the top-level entry point flux watches

## cluster infrastructure (`k8s/infrastructure`)

- [Traefik](https://github.com/traefik/traefik) for ingress & SSL
- [cert-manager](https://github.com/cert-manager/cert-manager) for automatic SSL certificate generation (using DNS-01 challenge)
- [external-dns](https://github.com/kubernetes-sigs/external-dns) for automatic DNS record updates
  - [konnektr-io/external-dns-porkbun-webhook](https://github.com/konnektr-io/external-dns-porkbun-webhook) for Porkbun integration
  - [home-operations/external-dns-unifi-webhook](https://github.com/home-operations/external-dns-unifi-webhook) for UniFi integration (LAN split DNS)
- [MetalLB](https://github.com/metallb/metallb) for load balancing
- [CoreDNS](https://github.com/coredns/coredns) for in-cluster DNS
- [Authentik](https://github.com/goauthentik/authentik) as the cluster identity & SSO provider
- [Gatus](https://github.com/TwiN/gatus) for endpoint health monitoring and status page
- [metrics-server](https://github.com/kubernetes-sigs/metrics-server) for internal cluster resource metrics
- [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) for cluster monitoring
  - [Prometheus](https://github.com/prometheus/prometheus) for metric collection
  - [Alertmanager](https://github.com/prometheus/alertmanager) for alert routing and notification delivery
  - [Grafana](https://github.com/grafana/grafana) for metric & log display
- [Loki](https://github.com/grafana/loki) for log storage & querying
- [Grafana Alloy](https://github.com/grafana/alloy) for log collection and forwarding
- [FluxCD](https://github.com/fluxcd/flux2) for GitOps continuous delivery with [flux-operator](https://github.com/controlplaneio-fluxcd/flux-operator)
- [Helm](https://github.com/helm/helm) for packaging upstream charts, reconciled by flux's helm-controller
- [SOPS](https://github.com/getsops/sops) (with age) for encrypted secret management in Git, decrypted by flux at reconcile time
- [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller) for automated k3s node upgrades via GitOps
- [reloader](https://github.com/stakater/Reloader) to restart pods when config changes
- [descheduler](https://github.com/kubernetes-sigs/descheduler) to dynamically schedule pods based on node metrics
- [KEDA](https://github.com/kedacore/keda) for event-driven autoscaling
  - [http-add-on](https://github.com/kedacore/http-add-on) to scale idle apps to zero and wake them on the first HTTP request
- [Longhorn](https://github.com/longhorn/longhorn) for distributed block storage

### bundled with k3s

components that ship with [k3s](https://github.com/k3s-io/k3s) itself. not managed by flux.

- [Flannel](https://github.com/flannel-io/flannel) for pod networking (default CNI, VXLAN backend)
- [kube-proxy](https://github.com/kubernetes/kubernetes) for service routing via node iptables rules
- [kube-router](https://github.com/cloudnativelabs/kube-router) for `NetworkPolicy` enforcement
- [containerd](https://github.com/containerd/containerd) as the container runtime
- [helm-controller](https://github.com/k3s-io/helm-controller) for reconciling `HelmChart` resources
- [etcd](https://github.com/etcd-io/etcd) as the control plane datastore (embedded, HA)

#### disabled k3s defaults

set via `k3s_disable` in `group_vars/all`

- `traefik` - replaced by the flux-managed Traefik deployment
- `servicelb` - replaced by MetalLB
- `local-storage` - replaced by Longhorn
- `metrics-server` - replaced by the flux-managed deployment
- `coredns` - disabled shortly after cluster bootstrapping and replaced by the flux-managed CoreDNS deployment

## apps (`k8s/apps`)

user-facing applications deployed to the cluster. see manifests for details

#### noteworthy apps

- [Headlamp](https://github.com/kubernetes-sigs/headlamp) is an easy-to-use cluster management web UI
- [homepage](https://github.com/gethomepage/homepage) is a declaratively configured dashboard with k8s support and automatic service discovery
- [Immich](https://github.com/immich-app/immich) is a high-performance photo and video management solution with a solid mobile and web experience
- [Navidrome](https://github.com/navidrome/navidrome) is a web-based music collection server and streamer with support for many mobile, web, and desktop clients
- [Jellyfin](https://github.com/jellyfin/jellyfin) is a free software media system designed to manage and stream local digital media
- [Forgejo](https://codeberg.org/forgejo/forgejo) is a lightweight software forge with GitHub-Actions-like CI that can mirror/backup remote repositories
- [Apprise](https://github.com/caronc/apprise) is a lightweight notification server that allows sending notifications to almost all popular notification services available today

## development

after cloning:

1. [install aqua](https://aquaproj.github.io/docs/install)
2. `make setup`
3. `make help`

## dependency management

CI, hooks under `.githooks/`, and local development all resolve the same versions from these files

- [aqua](https://github.com/aquaproj/aqua) for declarative CLI tool version management (see `aqua.yaml`)
  - [dprint](https://github.com/dprint/dprint) for formatting
  - [gitleaks](https://github.com/gitleaks/gitleaks) for pre-commit secret scanning
  - [uv](https://github.com/astral-sh/uv) for python dependency management, declaring packages using [inline script metadata](https://peps.python.org/pep-0723/#example)
  - [SOPS](https://github.com/getsops/sops) for viewing and editing encrypted secrets
  - [kubectl](https://github.com/kubernetes/kubernetes) for cluster access
  - [flux](https://github.com/fluxcd/flux2) for inspecting reconciliation state

## secrets

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
