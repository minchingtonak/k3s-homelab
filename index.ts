import * as pulumi from '@pulumi/pulumi';
import * as proxmox from '@muhlba91/pulumi-proxmoxve';

const config = new pulumi.Config();
// const sshPublicKey = config.requireSecret('sshPublicKey');
const k3sToken = config.requireSecret('k3sToken');

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
// 1. DOWNLOAD CLOUD IMAGE
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
    // potentially try https://cloud.debian.org/images/cloud/trixie/20260402-2435/debian-13-generic-arm64-20260402-2435.qcow2
    overwrite: false,
  },
  { provider },
);

// =============================================================================
// 2. UPLOAD CLOUD-INIT SNIPPETS
// =============================================================================

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
manage_etc_hosts: true

packages:
  - qemu-guest-agent
  - curl
  - nfs-common

write_files:
  - path: /etc/rancher/k3s/config.yaml
    permissions: '0644'
    content: |
      write-kubeconfig-mode: "0644"
      cluster-init: true
      token: "${k3sToken}"
      tls-san:
        - "k3s-server-01"
        - "10.0.0.100"
      disable:
        - traefik

runcmd:
  - systemctl enable --now qemu-guest-agent
  - curl -sfL https://get.k3s.io | sh -s - server
  - mkdir -p /home/k3s/.kube
  - cp /etc/rancher/k3s/k3s.yaml /home/k3s/.kube/config
  - chown -R k3s:k3s /home/k3s/.kube
`,
    },
  },
  { provider },
);

// K3s Agent cloud-init
const k3sAgentCloudInit = new proxmox.storage.File(
  'k3s-agent-cloud-init',
  {
    nodeName: nodeName,
    datastoreId: snippetsDatastore,
    contentType: 'snippets',
    sourceRaw: {
      fileName: 'k3s-agent-init.yaml',
      data: pulumi.interpolate`#cloud-config
packages:
  - qemu-guest-agent
  - curl
  - nfs-common

write_files:
  - path: /etc/rancher/k3s/config.yaml
    permissions: '0600'
    content: |
      server: "https://10.0.0.100:6443"
      token: "${k3sToken}"

runcmd:
  - systemctl enable --now qemu-guest-agent
  - |
    until curl -sk https://10.0.0.100:6443/healthz 2>/dev/null; do
      echo "Waiting for K3s server..."
      sleep 10
    done
  - curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC='agent' sh -s -
`,
    },
  },
  { provider },
);

// =============================================================================
// 3. CREATE BASE TEMPLATE (Optional - if you want Pulumi to manage the template)
// =============================================================================
// const k3sTemplate = new proxmox.vm.VirtualMachine(
//   'k3s-template',
//   {
//     nodeName: nodeName,
//     vmId: 9000,
//     name: 'k3s-node-template',
//     template: true, // This makes it a template!

//     cpu: {
//       cores: 2,
//       sockets: 1,
//       type: 'x86-64-v2-AES',
//     },
//     memory: {
//       dedicated: 4096,
//     },

//     disks: [
//       {
//         interface: 'scsi0',
//         datastoreId: datastoreId,
//         fileId: debianCloudImage.id, // Import from downloaded image
//         size: 50,
//         discard: 'on',
//         ssd: true,
//       },
//     ],

//     networkDevices: [
//       {
//         bridge: 'vmbr0',
//         model: 'virtio',
//       },
//     ],

//     serialDevices: [{ device: 'socket' }],

//     scsiHardware: 'virtio-scsi-pci',
//     bootOrders: ['scsi0'],

//     agent: {
//       enabled: true,
//       trim: true,
//     },

//     initialization: {
//       datastoreId: datastoreId,
//       ipConfigs: [
//         {
//           ipv4: { address: 'dhcp' },
//         },
//       ],
//       userAccount: {
//         username: 'k3s',
//         keys: [sshPublicKey],
//       },
//     },

//     started: false, // Templates shouldn't be started
//   },
//   { provider },
// );

// // =============================================================================
// // 4. DEPLOY K3S SERVER NODE
// // =============================================================================
// const k3sServer = new proxmox.vm.VirtualMachine(
//   'k3s-server-01',
//   {
//     nodeName: nodeName,
//     vmId: 101,
//     name: 'k3s-server-01',

//     clone: {
//       vmId: 9000, // Clone from template
//       full: true,
//       nodeName: nodeName,
//     },

//     cpu: {
//       cores: 2,
//       sockets: 1,
//     },
//     memory: {
//       dedicated: 4096,
//     },

//     networkDevices: [
//       {
//         bridge: 'vmbr0',
//         model: 'virtio',
//       },
//     ],

//     agent: {
//       enabled: true,
//       trim: true,
//     },

//     initialization: {
//       datastoreId: datastoreId,
//       userDataFileId: k3sServerCloudInit.id, // Custom cloud-init!
//       ipConfigs: [
//         {
//           ipv4: {
//             address: '10.0.0.100/24',
//             gateway: '10.0.0.1',
//           },
//         },
//       ],
//       dns: {
//         servers: ['10.0.0.1', '8.8.8.8'],
//       },
//     },

//     started: true,
//     onBoot: true,
//     tags: ['k3s', 'server', 'kubernetes'],
//   },
//   { dependsOn: [k3sTemplate], provider },
// );

// // =============================================================================
// // 5. DEPLOY K3S AGENT NODES
// // =============================================================================
// const agentCount = 2;
// const k3sAgents: proxmox.vm.VirtualMachine[] = [];

// for (let i = 0; i < agentCount; i++) {
//   const agentNum = i + 1;
//   const vmId = 101 + agentNum;
//   const ipAddress = `10.0.0.${100 + agentNum}/24`;

//   const agent = new proxmox.vm.VirtualMachine(
//     `k3s-agent-${agentNum.toString().padStart(2, '0')}`,
//     {
//       nodeName: nodeName,
//       vmId: vmId,
//       name: `k3s-agent-${agentNum.toString().padStart(2, '0')}`,

//       clone: {
//         vmId: 9000,
//         full: true,
//         nodeName: nodeName,
//         retries: 3, // Helps with concurrent clones
//       },

//       cpu: {
//         cores: 4,
//         sockets: 1,
//       },
//       memory: {
//         dedicated: 8192,
//       },

//       networkDevices: [
//         {
//           bridge: 'vmbr0',
//           model: 'virtio',
//         },
//       ],

//       agent: {
//         enabled: true,
//         trim: true,
//       },

//       initialization: {
//         datastoreId: datastoreId,
//         userDataFileId: k3sAgentCloudInit.id,
//         ipConfigs: [
//           {
//             ipv4: {
//               address: ipAddress,
//               gateway: '10.0.0.1',
//             },
//           },
//         ],
//         dns: {
//           servers: ['10.0.0.1', '8.8.8.8'],
//         },
//       },

//       started: true,
//       onBoot: true,
//       tags: ['k3s', 'agent', 'kubernetes'],
//     },
//     { dependsOn: [k3sServer], provider },
//   ); // Wait for server to be created first

//   k3sAgents.push(agent);
// }

// // =============================================================================
// // 6. EXPORTS
// // =============================================================================
// export const serverIp = k3sServer.ipv4Addresses;
// export const agentIps = k3sAgents.map((a) => a.ipv4Addresses);
// export const templateId = k3sTemplate.vmId;
