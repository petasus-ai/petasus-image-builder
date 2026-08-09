# Petasus AI Cloud - Virtual Machine Image Builder

The **Petasus VM Image Builder** provides an automated, declarative framework for constructing secure, optimized, and reproducible Golden VM Images tailored for the **Petasus AI Cloud** infrastructure. Leveraging HashiCorp Packer, this toolchain ensures consistency across environments, providing a standardized foundation for high-performance AI workloads and GPU virtualization instances.

> **Branch note (`master-cilium`):** this branch prepares **customer-supplied (migrated) QCOW2 images** for the Cilium-based Petasus AI Cloud / EdgeStack platform. It installs and configures exactly what the platform's VM lifecycle needs — [`qemu-guest-agent`](https://kubevirt.io/user-guide/virtual_machines/guest_agent_information/) (VM state, IP and guest-OS reporting), [Prometheus `node_exporter`](https://prometheus.io/docs/guides/node-exporter/) v1.11.1 on TCP 9100 (monitoring), cloud-init [NoCloud](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html) readiness (per-VM users, SSH keys, and network configuration) — and scrubs build-time identity and network state so every VM boots fresh. [Predictable network interface naming](https://www.freedesktop.org/software/systemd/man/latest/systemd.net-naming-scheme.html) is preserved (**never** set `net.ifnames=0`): the platform addresses the default NIC as `enp1s0`. Security hardening (CIS etc.) is intentionally **out of scope** for migrated images. The supported guest matrix is Rocky Linux, RHEL, and Ubuntu; Windows guests are not supported by this toolchain (ansible-over-ssh into a Linux guest).

## 📋 Supported Operating Systems

### Host Environments

The build environment must be running one of the following host operating systems:

* **Rocky Linux:** 9, 10

### Guest Environments

The builder currently supports preparing images for the following guest distributions across multiple architectures:

| Distribution | Version(s) | Architecture Support |
| --- | --- | --- |
| **Rocky Linux** | 8, 9, 10 | x86_64, ARM64 |
| **RHEL** | 8, 9, 10 | x86_64, ARM64 |
| **Ubuntu** | 20.04, 22.04, 24.04, 26.04 | x86_64, ARM64 |

> **RHEL note:** RHEL KVM guest images must be downloaded from the [Red Hat Customer Portal](https://access.redhat.com/downloads) with an active account and placed under `images/` (see the `iso_url`/`iso_checksum` placeholders in `packer/qemu/qemu-rhel-*.json`). The build installs packages with `dnf`, so the base image needs a package source: an attached subscription, Satellite, a local mirror — **or the RHEL installation DVD ISO served as a local repository** (a [Red Hat-documented practice](https://access.redhat.com/solutions/1355683); the DVD carries the `BaseOS/` and `AppStream/` repo trees). To use the ISO path, mount and serve it on the build host, then pass `el_repo_url` to the build — inside the build VM the host is reachable at `10.0.2.2` ([QEMU user-mode networking](https://www.qemu.org/docs/master/system/devices/net.html#using-the-user-mode-network-stack)):
>
> ```bash
> sudo mount -o loop,ro rhel-9.x-aarch64-dvd.iso /mnt/rhel-dvd
> python3 -m http.server 8099 --directory /mnt/rhel-dvd &
> PACKER_FLAGS='-var ansible_extra_vars=el_repo_url=http://10.0.2.2:8099' \
>   make build-qemu-rhel-9-uefi-aarch64
> ```
>
> The build writes `/etc/yum.repos.d/petasus-build-media.repo` from `el_repo_url` and sysprep removes it, so the shipped image carries no build-host reference.

## 🚀 Getting Started

The project utilizes a `Makefile` to streamline the build process, reducing the complexity of interacting directly with Packer CLI.

### Step 1: Clone the Repository

Ensure that Git, GNU Make, are installed. Clone the repository to your designated build server.

```bash
sudo dnf install -y git make
git clone https://github.com/petasus-ai/petasus-image-builder.git
cd petasus-image-builder

```

### Step 2: Install Dependencies

Ensure that Packer are installed along with the necessary QEMU dependencies.

```bash
make deps-qemu

```

### Step 3: Prepare the Base Image

Place your pre-built, cloud-ready QCOW2 base image into the `images/` directory.

> **⚠️ Important Note on Base Images:**
> Because Petasus AI Cloud relies on cloud-native provisioning workflows, this builder strictly supports **cloud-ready QCOW2 images** as the base. Standard ISO installers are not supported. Ensure your base QCOW2 image comes pre-installed with `cloud-init` and other essential cloud initialization utilities.

```bash
# Example
cp /path/to/your/base-image.qcow2 images/

```

### Step 4: Verify Image Integrity and Update Configuration

To ensure build integrity, calculate the SHA-256 checksum of your target base image.

```bash
sha256sum images/<your-image-name>

```

Navigate to the corresponding JSON configuration file for your target Linux distribution. Update the `iso_url` and `iso_checksum` parameters with your base image path and the value generated in the previous step.

*Example configuration snippet (`qemu-ubuntu-2404.json`):*

```json
{
  "build_name": "ubuntu-2404",
  "distro_name": "ubuntu",
  "ssh_username": "ubuntu",
  "os_display_name": "Ubuntu 24.04",
  "guest_os_type": "ubuntu-64",
  "iso_url": "images/ubuntu-2404.qcow2",
  "iso_checksum": "50c38d3f7307fe770c15a69b316d0001ac28e484239218d23e1ca8c8e7ec9a10",
  "iso_checksum_type": "sha256",
  "shutdown_command": "shutdown -P now",
  "ansible_python_interpreter": "/usr/bin/python3"
}

```

Alternatively, override the base image on the command line without editing the file:

```bash
PACKER_FLAGS='-var iso_url=images/customer.qcow2 -var iso_checksum=<sha256>' \
  make build-qemu-ubuntu-2404

```

### Step 5: Build the Image

Execute the build process for your desired OS using the `make` command.

```bash
# Example: Building Ubuntu 24.04 (x86_64) or ARM64
make build-qemu-ubuntu-2404
make build-qemu-ubuntu-2404-aarch64

```

*Note: Upon successful completion, the finalized image artifact will be generated and located in the `output/` directory.*

What the build applies inside the guest:

* **`qemu-guest-agent`** installed and enabled — the platform reads VM state, interface addresses, and guest OS info through it.
* **`node_exporter` v1.11.1** ([official release binary](https://prometheus.io/download/#node_exporter)) as a systemd service on TCP 9100 — scraped by the platform's per-project monitoring service.
* **Network normalization** — predictable NIC naming preserved; on Ubuntu the legacy `ifupdown` stack is purged in favor of netplan; build-time cloud-init network profiles (`/etc/netplan/50-cloud-init.yaml`, NetworkManager `cloud-init-*.nmconnection` keyfiles) are removed so per-VM cloud-init configures the real NIC (`enp1s0`, DHCP) cleanly.
* **Identity scrub (sysprep)** — machine-id (including the D-Bus symlink), SSH host keys, cloud-init instance state, logs, shell history, and the build user's SSH access are all reset; each VM regenerates them on first boot.

### Step 6: Import the Image into the Platform

Two ingestion paths are available.

**Option A — Upload the QCOW2 directly (recommended for migrated images).**
Create a platform volume with the `UploadImage` import source, push the bytes with [`virtctl image-upload`](https://github.com/kubevirt/containerized-data-importer/blob/main/doc/upload.md), then create the VM from that boot volume:

```bash
# 1. Create an upload volume (metadata must match the image: distro, arch, boot type)
curl -X POST "$EDGETRON_API/edgetron/resources/kubevirt/volumes" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "migrated-boot", "project": "<project>", "capacity": 16,
       "import_source": "UploadImage", "os_type": "linux", "os_distro": "ubuntu",
       "cpu_arch": "aarch64", "boot_type": "uefi"}'

# 2. Upload the processed QCOW2 into the waiting DataVolume
virtctl image-upload dv migrated-boot -n <project> --no-create \
  --image-path output/ubuntu-2404.qcow2 \
  --uploadproxy-url https://<cdi-uploadproxy> --insecure

# 3. Create the VM from the boot volume
curl -X POST "$EDGETRON_API/edgetron/resources/kubevirt/vms" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "migrated-vm", "project": "<project>", "boot_dv": "migrated-boot",
       "flavor": "<flavor>", "username": "<login-user>", "keypair": "<keypair>"}'

```

**Option B — Containerize for the shared image catalogue.**
To publish the image as a reusable golden image, wrap it as a [containerDisk](https://kubevirt.io/user-guide/storage/disks_and_volumes/#containerdisk) OCI image and register it through the platform's image API (`POST /edgetron/resources/kubevirt/images` with the `docker://` source URL):

```dockerfile
# Dockerfile
FROM scratch
ADD --chown=107:107 output/ubuntu-2404.qcow2 /disk/

```

```bash
export REGISTRY="registry.petasus.ai/images"
export IMAGE_TAG="ubuntu-2404:latest"
docker build -t $REGISTRY/$IMAGE_TAG -f Dockerfile .
docker push $REGISTRY/$IMAGE_TAG

```

## 🧹 Maintenance & Cleanup

To maintain a clean working environment and free up disk space, you can use the following utility commands:

**Remove generated build artifacts:**

```bash
make clean-qemu

```

**Clear Packer cache (temporary build files):**

```bash
make clean-packer-cache

```
