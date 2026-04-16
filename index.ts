import * as pulumi from '@pulumi/pulumi';
import * as proxmox from '@muhlba91/pulumi-proxmoxve';
import * as command from '@pulumi/command';
import * as k8s from '@pulumi/kubernetes';

const config = new pulumi.Config();
const sshPublicKey = config.requireSecret('sshPublicKey');
const k3sToken = config.requireSecret('k3sToken');
const forgejoRepo = config.require('forgejoRepo');
const forgejoSshKey = config.requireSecret('forgejoSshKey');
// pulumi config set k3s-homelab:forgejoKnownHosts "$(ssh-keyscan -T 10 -p 222 forgejo.backup.homelab.akmin.dev 2>/dev/null | grep -v '^#')"
const forgejoKnownHosts = config.require('forgejoKnownHosts');
const sshPrivateKey = config.requireSecret('sshPrivateKey');
// Export from cluster: kubectl get secret -n flux-system -l sealedsecrets.bitnami.com/sealed-secrets-key -o jsonpath='{.data.tls\.key}' | base64 -d
const sealedSecretsKey = config.requireSecret('sealedSecretsKey');
const sealedSecretsCert = config.require('sealedSecretsCert');

const k3sVersion = 'v1.32.3+k3s1';

const nodeName = 'homelab';
const datastoreId = 'fast';
const snippetsDatastore = 'local'; // Must have snippets enabled

const provider = new proxmox.Provider('proxmox-provider', {
  username: 'root@pam',
  password: config.requireSecret('proxmoxPassword'),
  endpoint: config.require('proxmoxEndpoint'),
  insecure: true,
});

// =============================================================================
// 1. NETWORK INFRASTRUCTURE
// =============================================================================
// G017: Create an isolated Linux bridge (vmbr1) for internal K3s cluster
// communication. No IP, no ports — purely L2 isolation between cluster nodes.
const vmbr1 = new proxmox.network.NetworkBridge(
  'vmbr1',
  {
    nodeName: nodeName,
    name: 'vmbr1',
    autostart: true,
    vlanAware: false,
    comment: 'K3s cluster inter-node networking',
    // No address, gateway, or ports — intentionally isolated
  },
  { provider },
);

// =============================================================================
// 2. DOWNLOAD CLOUD IMAGE
// =============================================================================
const debianCloudImage = new proxmox.download.File(
  'debian-cloud-image',
  {
    nodeName: nodeName,
    datastoreId: 'local',
    contentType: 'iso',
    fileName: 'debian-13-genericcloud-amd64.img',
    // info: https://cloud.debian.org/images/cloud/
    url: 'https://cloud.debian.org/images/cloud/trixie/20260402-2435/debian-13-genericcloud-amd64-20260402-2435.qcow2',
    overwrite: false,
  },
  { provider },
);

// =============================================================================
// 2. SHARED CLOUD-INIT SNIPPETS
// =============================================================================

// Sysctl: TCP/IP stack hardening (G021 - 80_tcp_hardening.conf)
const sysctlTcpHardening = `
# Disable IPv6
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1

# Disable source routing
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0

# Disable ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0

# Enable reverse path filtering
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# SYN flood protection
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_syn_retries = 2
net.ipv4.tcp_synack_retries = 2

# TCP timeout hardening
net.ipv4.tcp_fin_timeout = 10
net.ipv4.tcp_max_orphans = 65536

# Ignore bogus ICMP errors
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
`.trimStart();

// Sysctl: Network optimizations (G021 - 85_network_optimizations.conf)
const sysctlNetworkOptimizations = `
# TCP Fast Open (client + server)
net.ipv4.tcp_fastopen = 3

# TCP keepalive tuning
net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_intvl = 10
net.ipv4.tcp_keepalive_probes = 6

# MTU probing
net.ipv4.tcp_mtu_probing = 1

# Connection queues
net.core.somaxconn = 256000
net.ipv4.tcp_max_syn_backlog = 40000

# BBR congestion control
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Ephemeral port range
net.ipv4.ip_local_port_range = 30000 65535

# UDP memory
net.ipv4.udp_mem = 65536 131072 262144

# Buffer tuning
net.core.rmem_default = 1048576
net.core.rmem_max = 16777216
net.core.wmem_default = 1048576
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 1048576 2097152
net.ipv4.tcp_wmem = 4096 65536 16777216
`.trimStart();

// Sysctl: Memory optimizations (G021 - 85_memory_optimizations.conf)
const sysctlMemoryOptimizations = `
vm.swappiness = 2
vm.overcommit_memory = 1
vm.vfs_cache_pressure = 500
vm.dirty_background_ratio = 5
vm.dirty_ratio = 10
vm.dirty_writeback_centisecs = 3000
vm.dirty_expire_centisecs = 18000
vm.max_map_count = 262144
`.trimStart();

// Sysctl: Kernel optimizations (G021 - 85_kernel_optimizations.conf)
const sysctlKernelOptimizations = `
kernel.unprivileged_bpf_disabled = 1
kernel.sched_autogroup_enabled = 0
kernel.keys.maxkeys = 2000
fs.inotify.max_queued_events = 8388608
fs.inotify.max_user_instances = 65536
fs.inotify.max_user_watches = 4194304
`.trimStart();

// Hardened sshd_config (G021 - key-only auth, no root login)
const sshdConfig = `
Port 22
LoginGraceTime 45
PermitRootLogin no
StrictModes yes
MaxAuthTries 3
MaxSessions 10

PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys

PasswordAuthentication no
KbdInteractiveAuthentication no

UsePAM yes
X11Forwarding no
PrintMotd no

AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
`.trimStart();

// Fail2ban SSH jail (G021 - maxretry=3)
const fail2banSshdJail = `
[sshd]
enabled = true
port = ssh
maxretry = 3
findtime = 3600
bantime = 86400
`.trimStart();

// =============================================================================
// 3. UPLOAD CLOUD-INIT SNIPPETS
// =============================================================================

const k3sServerIp = '192.168.8.100';
const gatewayIp = '192.168.8.1';

// K3s Server cloud-init
const k3sServerCloudInit = new proxmox.storage.File(
  'k3s-server-cloud-init',
  {
    nodeName: nodeName,
    datastoreId: snippetsDatastore,
    contentType: 'snippets',
    sourceRaw: {
      fileName: 'k3s-server-init.yaml',
      data: pulumi.interpolate`#cloud-config
hostname: k3s-server-01
fqdn: k3s-server-01.homelab.cloud
manage_etc_hosts: true

users:
  - name: k3s
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ${sshPublicKey}

timezone: UTC

packages:
  - qemu-guest-agent
  - curl
  - locales
  - nfs-common
  - ethtool
  - fail2ban
  - gdisk
  - htop
  - net-tools
  - sudo
  - tree
  - vim

write_files:
  - path: /etc/rancher/k3s/config.yaml
    permissions: '0644'
    content: |
      write-kubeconfig-mode: "0644"
      cluster-init: true
      token: "${k3sToken}"
      tls-san:
        - "k3s-server-01"
        - "${k3sServerIp}"
      disable:
        - traefik
        - servicelb

  - path: /etc/sysctl.d/80_tcp_hardening.conf
    permissions: '0644'
    content: |
      ${sysctlTcpHardening.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_network_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlNetworkOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_memory_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlMemoryOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_kernel_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlKernelOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/ssh/sshd_config
    permissions: '0644'
    content: |
      ${sshdConfig.replace(/\n/g, '\n      ')}

  - path: /etc/fail2ban/jail.d/01_sshd.conf
    permissions: '0644'
    content: |
      ${fail2banSshdJail.replace(/\n/g, '\n      ')}

runcmd:
  - systemctl enable --now qemu-guest-agent
  # Generate and apply locale (locale module runs before packages, so do it here)
  - locale-gen en_US.UTF-8
  - update-locale LANG=en_US.UTF-8
  # Apply sysctl settings
  - sysctl --system
  # Disable root account (G021)
  - usermod -s /usr/sbin/nologin root
  - passwd -l root
  # Disable transparent hugepages (G021)
  - sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\\(.*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 transparent_hugepage=never"/' /etc/default/grub
  - update-grub
  # Purge microcode packages (not needed in VMs, G021)
  - apt purge -y intel-microcode amd-microcode 2>/dev/null || true
  # Restart hardened services
  - systemctl restart fail2ban
  - systemctl restart ssh
  # Install K3s server
  - curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${k3sVersion} sh -s - server
  - until command -v kubectl &>/dev/null; do sleep 2; done
  - mkdir -p /home/k3s/.kube
  - cp /etc/rancher/k3s/k3s.yaml /home/k3s/.kube/config
  - chown -R k3s:k3s /home/k3s/.kube
`,
    },
  },
  { provider },
);

// K3s Agent cloud-init — created per-agent inside the deploy loop so each gets its own hostname.

// =============================================================================
// 4. CREATE BASE TEMPLATE (Optional - if you want Pulumi to manage the template)
// =============================================================================
const k3sTemplate = new proxmox.vm.VirtualMachine(
  'k3s-template',
  {
    nodeName: nodeName,
    vmId: 1000,
    name: 'k3s-node-template',
    template: true,

    cpu: {
      cores: 2,
      sockets: 1,
      type: 'host', // G020: use host CPU type for best performance
    },
    memory: {
      dedicated: 2048, // G020: 2048 MiB max
      floating: 1024, // G020: 1024 MiB minimum (balloon)
    },

    disks: [
      {
        interface: 'scsi0',
        datastoreId: datastoreId,
        fileId: debianCloudImage.id,
        size: 10, // G020: 10 GiB for template disk
        discard: 'on',
        ssd: true, // G020: SSD emulation enabled
      },
    ],

    networkDevices: [
      {
        bridge: 'vmbr0', // Primary LAN interface
        model: 'virtio',
      },
      {
        bridge: 'vmbr1', // G020: Secondary interface for inter-node communication
        model: 'virtio',
        firewall: false,
      },
    ],

    serialDevices: [{ device: 'socket' }],

    scsiHardware: 'virtio-scsi-pci',
    bootOrders: ['scsi0'],

    agent: {
      enabled: true, // G020: QEMU guest agent enabled
      trim: true,
    },

    initialization: {
      datastoreId: datastoreId,
      ipConfigs: [
        {
          ipv4: { address: 'dhcp' },
        },
        {
          ipv4: { address: 'dhcp' }, // vmbr1 - configure static post-deploy
        },
      ],
    },

    started: false,
  },
  // ignoreChanges: provider bug muhlba91/pulumi-proxmoxve#664 — speed is always
  // returned by the provider with zero values regardless of inputs, causing
  // a perpetual diff. Ignore until the upstream fix lands.
  { provider, ignoreChanges: ['disks[0].speed'] },
);

// =============================================================================
// 5. DEPLOY K3S SERVER NODE
// =============================================================================
const k3sServer = new proxmox.vm.VirtualMachine(
  'k3s-server-01',
  {
    nodeName: nodeName,
    vmId: 100,
    name: 'k3s-server-01',

    clone: {
      vmId: k3sTemplate.vmId,
      full: true,
      nodeName: nodeName,
    },

    cpu: {
      cores: 2,
      sockets: 1,
      type: 'host',
    },
    memory: {
      dedicated: 6144,
      floating: 6144,
    },

    // Cannot reference k3sTemplate.disks — the template disk carries fileId pointing
    // to the cloud image, which must not be set on a cloned disk.
    disks: [
      {
        interface: 'scsi0',
        datastoreId: datastoreId,
        size: 10,
        discard: 'on',
        ssd: true,
      },
    ],

    networkDevices: [
      { bridge: 'vmbr0', model: 'virtio' },
      { bridge: 'vmbr1', model: 'virtio', firewall: false },
    ],

    agent: k3sTemplate.agent.apply((v) => v!),

    initialization: {
      datastoreId: datastoreId,
      userDataFileId: k3sServerCloudInit.id,
      ipConfigs: [
        {
          ipv4: {
            address: `${k3sServerIp}/24`,
            gateway: gatewayIp,
          },
        },
        {
          ipv4: { address: 'dhcp' }, // vmbr1
        },
      ],
      dns: {
        servers: ['1.0.0.1', '8.8.8.8'],
        domain: 'homelab.cloud',
      },
    },

    started: true,
    onBoot: true,
    tags: ['k3s', 'server', 'kubernetes'],
  },
  { dependsOn: [k3sTemplate], provider, ignoreChanges: ['disks[0].speed'] },
);

// =============================================================================
// 6. DEPLOY K3S AGENT NODES
// =============================================================================
const agentCount = 1;
const k3sAgents: proxmox.vm.VirtualMachine[] = [];

for (let i = 0; i < agentCount; i++) {
  const agentNum = i + 1;
  const vmId = 100 + agentNum;
  const agentName = `k3s-agent-${agentNum.toString().padStart(2, '0')}`;
  const ipAddress = `192.168.8.${vmId}/24`;

  const agentCloudInit = new proxmox.storage.File(
    `k3s-agent-cloud-init-${agentNum.toString().padStart(2, '0')}`,
    {
      nodeName: nodeName,
      datastoreId: snippetsDatastore,
      contentType: 'snippets',
      sourceRaw: {
        fileName: `k3s-agent-init-${agentNum.toString().padStart(2, '0')}.yaml`,
        data: pulumi.interpolate`#cloud-config
hostname: ${agentName}
fqdn: ${agentName}.homelab.cloud
manage_etc_hosts: true

users:
  - name: k3s
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ${sshPublicKey}

timezone: UTC

packages:
  - qemu-guest-agent
  - curl
  - locales
  - nfs-common
  - ethtool
  - fail2ban
  - gdisk
  - htop
  - net-tools
  - sudo
  - tree
  - vim

write_files:
  - path: /etc/rancher/k3s/config.yaml
    permissions: '0600'
    content: |
      server: "https://${k3sServerIp}:6443"
      token: "${k3sToken}"

  - path: /etc/sysctl.d/80_tcp_hardening.conf
    permissions: '0644'
    content: |
      ${sysctlTcpHardening.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_network_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlNetworkOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_memory_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlMemoryOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/sysctl.d/85_kernel_optimizations.conf
    permissions: '0644'
    content: |
      ${sysctlKernelOptimizations.replace(/\n/g, '\n      ')}

  - path: /etc/ssh/sshd_config
    permissions: '0644'
    content: |
      ${sshdConfig.replace(/\n/g, '\n      ')}

  - path: /etc/fail2ban/jail.d/01_sshd.conf
    permissions: '0644'
    content: |
      ${fail2banSshdJail.replace(/\n/g, '\n      ')}

runcmd:
  - systemctl enable --now qemu-guest-agent
  # Generate and apply locale (locale module runs before packages, so do it here)
  - locale-gen en_US.UTF-8
  - update-locale LANG=en_US.UTF-8
  # Apply sysctl settings
  - sysctl --system
  # Disable root account (G021)
  - usermod -s /usr/sbin/nologin root
  - passwd -l root
  # Disable transparent hugepages (G021)
  - sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\\(.*\\)"/GRUB_CMDLINE_LINUX_DEFAULT="\\1 transparent_hugepage=never"/' /etc/default/grub
  - update-grub
  # Purge microcode packages (not needed in VMs, G021)
  - apt purge -y intel-microcode amd-microcode 2>/dev/null || true
  # Restart hardened services
  - systemctl restart fail2ban
  - systemctl restart ssh
  # Wait for K3s server to be ready, then join
  - |
    until curl -sk https://${k3sServerIp}:6443/healthz 2>/dev/null; do
      echo "Waiting for K3s server..."
      sleep 10
    done
  - curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${k3sVersion} INSTALL_K3S_EXEC='agent' sh -s -
`,
      },
    },
    { provider },
  );

  const agent = new proxmox.vm.VirtualMachine(
    agentName,
    {
      nodeName: nodeName,
      vmId: vmId,
      name: agentName,

      clone: {
        vmId: k3sTemplate.vmId,
        full: true,
        nodeName: nodeName,
        retries: 3,
      },

      cpu: {
        cores: 4,
        sockets: 1,
        type: 'host',
      },
      memory: {
        dedicated: 8192,
        floating: 8192,
      },

      disks: [
        {
          interface: 'scsi0',
          datastoreId: datastoreId,
          size: 50,
          discard: 'on',
          ssd: true,
        },
      ],

      networkDevices: [
        { bridge: 'vmbr0', model: 'virtio' },
        { bridge: 'vmbr1', model: 'virtio', firewall: false },
      ],

      agent: k3sTemplate.agent.apply((v) => v!),

      initialization: {
        datastoreId: datastoreId,
        userDataFileId: agentCloudInit.id,
        ipConfigs: [
          {
            ipv4: {
              address: ipAddress,
              gateway: gatewayIp,
            },
          },
          {
            ipv4: { address: 'dhcp' }, // vmbr1
          },
        ],
        dns: {
          servers: ['1.0.0.1', '8.8.8.8'],
          domain: 'homelab.cloud',
        },
      },

      started: true,
      onBoot: true,
      tags: ['k3s', 'agent', 'kubernetes'],
    },
    { dependsOn: [k3sServer, agentCloudInit], provider, ignoreChanges: ['disks[0].speed'] },
  );

  k3sAgents.push(agent);
}

// =============================================================================
// 7. BOOTSTRAP FLUX
// =============================================================================

const kubeconfigPath = `${process.env.HOME}/.kube/k3s-homelab`;

// Fetch kubeconfig with loopback replaced by external IP so it works remotely.
const fetchKubeconfig = new command.remote.Command(
  'fetch-kubeconfig',
  {
    connection: {
      host: k3sServerIp,
      user: 'k3s',
      privateKey: sshPrivateKey,
      dialErrorLimit: 50, // buffer for cloud-init wait time
    },
    create: `until sudo test -f /etc/rancher/k3s/k3s.yaml; do sleep 5; done && sudo cat /etc/rancher/k3s/k3s.yaml | sed 's/127\\.0\\.0\\.1/${k3sServerIp}/g'`,
    triggers: [k3sServer.id],
  },
  { dependsOn: [k3sServer] },
);

// Write kubeconfig to a local file via stdin to avoid shell quoting issues with cert data.
const writeKubeconfig = new command.local.Command(
  'write-kubeconfig',
  {
    create: `mkdir -p "$(dirname "${kubeconfigPath}")" && cat > "${kubeconfigPath}" && chmod 600 "${kubeconfigPath}"`,
    delete: `rm -f "${kubeconfigPath}"`,
    stdin: fetchKubeconfig.stdout,
  },
  { dependsOn: [fetchKubeconfig] },
);

// Wait for the K3s API server to be fully ready before applying any k8s resources.
const waitForK8s = new command.local.Command(
  'wait-for-k8s',
  {
    create: `until kubectl --kubeconfig "${kubeconfigPath}" get nodes &>/dev/null; do sleep 5; done`,
    triggers: [writeKubeconfig.id],
  },
  { dependsOn: [writeKubeconfig] },
);

const k8sProvider = new k8s.Provider(
  'k8s-provider',
  { kubeconfig: kubeconfigPath },
  { dependsOn: [waitForK8s] },
);

const fluxOperator = new k8s.helm.v3.Release(
  'flux-operator',
  {
    name: 'flux-operator',
    chart: 'oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator',
    version: '0.46.0',
    namespace: 'flux-system',
    createNamespace: true,
  },
  { provider: k8sProvider, dependsOn: [k3sServer, ...k3sAgents] },
);

// Pre-create the sealing key so the controller uses a stable key across cluster rebuilds.
// If this Secret exists with the active label when the controller starts, it adopts it
// instead of generating a new one — keeping existing SealedSecrets decryptable.
new k8s.core.v1.Secret(
  'sealed-secrets-key',
  {
    metadata: {
      name: 'sealed-secrets-key',
      namespace: 'flux-system',
      labels: { 'sealedsecrets.bitnami.com/sealed-secrets-key': 'active' },
    },
    type: 'kubernetes.io/tls',
    stringData: {
      'tls.key': sealedSecretsKey,
      'tls.crt': sealedSecretsCert,
    },
  },
  { provider: k8sProvider, dependsOn: [fluxOperator] },
);

const fluxGitSecret = new k8s.core.v1.Secret(
  'flux-git-credentials',
  {
    metadata: { name: 'flux-git-credentials', namespace: 'flux-system' },
    stringData: {
      identity: forgejoSshKey,
      known_hosts: forgejoKnownHosts,
    },
  },
  { provider: k8sProvider, dependsOn: [fluxOperator] },
);

// Bootstrap-only FluxInstance: minimal config to get Flux running so it can sync
// k8s/clusters/homelab from git. Once Flux is up, it reconciles the full config from
// k8s/clusters/homelab/flux-system/flux-instance.yaml — that file is the source of truth.
new k8s.apiextensions.CustomResource(
  'flux-instance',
  {
    apiVersion: 'fluxcd.controlplane.io/v1',
    kind: 'FluxInstance',
    metadata: { name: 'flux', namespace: 'flux-system' },
    spec: {
      distribution: { version: '2.8.5', registry: 'ghcr.io/fluxcd' },
      sync: {
        kind: 'GitRepository',
        url: forgejoRepo,
        ref: 'refs/heads/main',
        // If this path changes, update k8s/clusters/homelab/flux-system/flux-instance.yaml too.
        path: 'k8s/clusters/homelab',
        pullSecret: 'flux-git-credentials',
      },
    },
  },
  {
    provider: k8sProvider,
    dependsOn: [fluxGitSecret],
    // Flux's kustomize-controller takes ownership of .spec after the initial apply,
    // reconciling it from k8s/clusters/homelab/flux-system/flux-instance.yaml.
    // ignoreChanges prevents SSA field conflicts between Pulumi and kustomize-controller.
    ignoreChanges: ['spec'],
  },
);

// =============================================================================
// 9. EXPORTS
// =============================================================================
export const serverIp = k3sServer.ipv4Addresses;
export const agentIps = k3sAgents.map((a) => a.ipv4Addresses);
export const templateId = k3sTemplate.vmId;
