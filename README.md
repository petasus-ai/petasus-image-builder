# Petasus AI Cloud - Virtual Machine Image Builder

The **Petasus VM Image Builder** provides an automated, declarative framework for constructing secure, optimized, and reproducible Golden VM Images tailored for the **Petasus AI Cloud** infrastructure. Leveraging HashiCorp Packer, this toolchain ensures consistency across environments, providing a standardized foundation for high-performance AI workloads and GPU virtualization instances.

## 📋 Supported Operating Systems

### Host Environments

The build environment must be running one of the following host operating systems:

* **Rocky Linux:** 9, 10

### Guest Environments

The builder currently supports generating Golden Images for the following guest distributions across multiple architectures:

| Distribution | Version(s) | Architecture Support |
| --- | --- | --- |
| **Rocky Linux** | 8, 9, 10 | x86_64, ARM64 |
| **AlmaLinux** | 8, 9, 10 | x86_64, ARM64 |
| **Ubuntu** | 20.04, 22.04, 24.04 | x86_64, ARM64 |
| **Debian** | 11, 12 | x86_64, ARM64 |
| **OpenSUSE** | 15 | x86_64 |

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

Navigate to the corresponding JSON configuration file for your target Linux distribution. Update the `iso_checksum` parameter with the value generated in the previous step.

*Example configuration snippet (`ubuntu-2404.json`):*

```json
{
  "build_name": "ubuntu-2404",
  "distro_name": "ubuntu",
  "ssh_username": "ubuntu",
  "os_display_name": "Ubuntu 24.04",
  "guest_os_type": "ubuntu-64",
  "qemu_binary": "/usr/libexec/qemu-kvm",
  "iso_url": "images/ubuntu-2404.qcow2",
  "iso_checksum": "50c38d3f7307fe770c15a69b316d0001ac28e484239218d23e1ca8c8e7ec9a10",
  "iso_checksum_type": "sha256",
  "shutdown_command": "shutdown -P now",
  "ansible_python_interpreter": "/usr/bin/python3"
}

```

### Step 5: Build the Golden Image

Execute the build process for your desired OS using the `make` command.

```bash
# Example: Building Ubuntu 24.04
make build-qemu-ubuntu-2404

```

*Note: Upon successful completion, the finalized Golden Image artifact will be generated and located in the `output/` directory.*

### Step 6: Containerize the Image for KubeVirt CDI

To seamlessly provision Virtual Machines in Petasus AI Cloud via KubeVirt, the generated `qcow2` image must be containerized. KubeVirt's Containerized Data Importer (CDI) can pull this OCI container image directly from your registry.

**1. Install Container Tools**

If your build server does not have Docker installed, install it using the following commands (Rocky/AlmaLinux example):

```bash
sudo dnf config-manager --add-repo=https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# Log out and log back in to apply the group membership

```

*(Note: Podman is also fully supported and uses the same command syntax).*

**2. Create a Dockerfile**

KubeVirt CDI expects the image to be placed in the `/disk/` directory of a scratch container.

Create a file named `Dockerfile` in the root of the project:

```dockerfile
# Dockerfile
FROM alpine
ADD output/ubuntu-2404.qcow2 /disk/

```

**3. Build and Push the Container Disk**

Build the container image and push it to your organization's container registry.

```bash
# Set your registry target
export REGISTRY="registry.petasus.ai/images"
export IMAGE_TAG="ubuntu-2404:latest"

# Build the OCI container image
docker build -t $REGISTRY/$IMAGE_TAG -f Dockerfile .

# Push to the remote registry
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
