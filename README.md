# Petasus Image Builder

Converts the disk of a Linux virtual machine that runs somewhere else (another KVM or OpenStack
cloud, a VMware export converted to QCOW2, a hand-installed machine) into an image the Petasus AI
Cloud platform boots, and publishes it as a [KubeVirt containerDisk](https://kubevirt.io/user-guide/storage/disks_and_volumes/#containerdisk).

The source disk is never booted and never written. The conversion works on a copy, offline, through
[libguestfs](https://libguestfs.org/); it needs no login to the guest, no working cloud-init in it and
no particular Python version. The result is then booted on a throwaway overlay, on the device model
the platform's virtual machines have, and only a disk that passes is packaged.

## What the platform expects of a guest

| The platform | so the guest must |
|---|---|
| boots every VM with UEFI firmware, Secure Boot off, and keeps no firmware boot entries | carry an EFI system partition whose loader sits at the [removable-media path](https://uefi.org/specs/UEFI/2.10/03_Boot_Manager.html#removable-media-boot-behavior) `EFI/BOOT/BOOTX64.EFI` (`BOOTAA64.EFI` on arm64) |
| attaches every disk as virtio-blk | boot from `/dev/vda` on an initramfs that does not depend on the source's disk controller, with no `/dev/sdX` names in fstab or on the kernel command line |
| attaches the pod network NIC at PCI `0000:01:00.0` and configures it by the name `enp1s0` | use [predictable interface names](https://systemd.io/PREDICTABLE_INTERFACE_NAMES/) and give that NIC that name |
| delivers users, keys, host name and network configuration on a [cloud-init NoCloud](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html) disk at every start | run cloud-init with the NoCloud datasource, with no setting that pins another datasource or turns its network configuration off |
| reads the guest's OS, addresses and filesystems through the [QEMU guest agent](https://kubevirt.io/user-guide/user_workloads/guest_agent_information/) | run `qemu-guest-agent` |
| scrapes guest memory and InfiniBand counters from [node_exporter](https://prometheus.io/docs/guides/node-exporter/) on 9100/tcp | run it and let 9100/tcp through the guest firewall |
| builds a local scratch volume on a VM's passthrough NVMe drives at boot | have `lvm2` and `xfsprogs` installed |

## Host requirements

A Rocky Linux 9 or 10 host of the guest's CPU architecture with KVM (`/dev/kvm`), and room under
`output/` for about three times the data the source disk holds.

```bash
make deps
```

installs the packages that are missing: `guestfs-tools`, `libguestfs-xfs`, `python3-libguestfs`,
`python3-pyyaml`, the UEFI firmware (`edk2-ovmf`, or `edk2-aarch64` on arm64), `qemu-kvm`, `qemu-img`,
`podman`, `xorriso`, `dosfstools`, `gdisk`, `openssh-clients`. It installs nothing when that would upgrade or remove a package the host
already has, and it never updates the host.

A conversion of a guest that runs no node exporter downloads the official `node_exporter` release
once, checks it against the release's checksum list and keeps it under `.cache/`. `NODE_EXPORTER_TARBALL` names a release tarball that is
already on the host instead.

## Converting a disk

```bash
make inspect SRC=/data/customer/web01.qcow2                 # what the disk holds; changes nothing
make migrate SRC=/data/customer/web01.qcow2 NAME=web01      # convert and verify
make package NAME=web01 TAG=v1                              # containerDisk image
make push    NAME=web01 TAG=v1 REGISTRY=registry.example.com REPOSITORY=migrated/web01
```

`NAME` is the image's name on the platform: lowercase letters, digits, `.` and `-`. Everything a
conversion produces is under `output/NAME/`:

| File | Content |
|---|---|
| `disk.qcow2` | the converted disk, compressed; its virtual size is the source's, or larger by the EFI system partition it gained |
| `manifest.json` | checksums of the source and the result, the options, the guest's distribution, every change made (`notes`) and the platform features the conversion found the guest cannot use (`restrictions`) |
| `inspect.json` | the inspection of the source |
| `verify.json`, `serial-*.log` | the checks of the verification boots, with the platform features they found unusable (`restrictions`), and the serial console of each |
| `image.json`, `push.json` | written by `make push`: the request that registers the image, and the pushed digest |

One conversion runs per `NAME` at a time. Every run starts from the source again; a run that fails
or is interrupted leaves the previous `disk.qcow2`, if any, untouched. A disk that fails its
verification is not kept; its `manifest.json`, `verify.json` and serial consoles go to
`output/NAME/failed/`.

### Options

| Variable | Values | Meaning |
|---|---|---|
| `SRC`, `SRC_FORMAT` | path; `qcow2` or `raw` | the source disk and its format (detected when unset) |
| `GENERALIZE` | `preserve` (default), `template` | what happens to the source machine's state: see [What is kept](#what-is-kept) |
| `NETWORK` | `reset` (default), `keep` | whether the source NICs' configuration is set aside: see [What changes in the guest](#what-changes-in-the-guest) |
| `UEFI_CONVERT` | `auto` (default), `never` | whether a disk without an EFI system partition is converted or refused |
| `MISSING_MOUNTS` | `refuse` (default), `nofail` | what to do when fstab mounts a filesystem that is not on this disk |
| `BUILD_REPO_FILE` | path | a `.repo` file (RHEL family) or an APT source file (`.list` or `.sources`) that is the only package source during the conversion |
| `EL_REPO_ISO` | path | a RHEL-family installation DVD image that is the only package source during the conversion |
| `GUEST_STEP_TIMEOUT` | seconds (3600) | how long one step inside the guest (packages, services, boot loader) may run |
| `SKIP_VERIFY` | `1` | convert without the verification boots; `make verify` runs them later |
| `VERIFY_TIMEOUT` | seconds (900) | how long a verification boot may take to bring the guest agent up |
| `VERIFY_EXTRA` | path | a shell script run as root in the verification VM; a non-zero exit fails the verification |

A guest whose own repositories are reachable from the host needs neither repository option. A RHEL
guest without a subscription, or a release whose repositories are gone, takes its packages from
`EL_REPO_ISO` or `BUILD_REPO_FILE`; neither is left in the image. Packages are checked against the
signing keys the guest already trusts. Repository metadata and downloaded packages stay in the
conversion's memory, not on the guest's disk; the guest's root needs 100 MiB free for what is
installed.

## What changes in the guest

Only what the platform needs is touched, and each change is listed in `manifest.json`.

| Area | Change |
|---|---|
| Device references | `/dev/sdX`, `/dev/xvdX`, `/dev/nvmeXnYpZ` and MBR `PARTUUID` references in fstab, the kernel command line and the resume configuration become filesystem UUIDs. A `/dev/disk/by-id` or `by-path` link names the source's hardware, not a filesystem: for the root filesystem it becomes the root's UUID, anywhere else it counts as a filesystem that is not on this disk (see [Refusals](#refusals)). `/etc/lvm/devices/system.devices`, which pins physical volumes to the source's disk identifiers, is moved aside. |
| Partition table | A disk with no EFI system partition becomes a GPT disk with one: see [BIOS disks](#bios-disks). |
| Packages | Installed when missing: `cloud-init`, the partition grower (`cloud-utils-growpart`, with `gdisk` before RHEL-family 10, or `cloud-guest-utils`), `qemu-guest-agent`, `lvm2`, `xfsprogs`, `openssh-server`, `sudo`, `dosfstools`; `NetworkManager` on RHEL-family 8 and later, `netplan.io` on Ubuntu 18.04 and later; the UEFI GRUB packages when the disk had no UEFI loader. Nothing else is upgraded, except what a new package depends on and a `cloud-init` older than 22.3, which is upgraded when the repositories carry a newer one. |
| cloud-init | A drop-in that sorts last sets `datasource_list: [ NoCloud, None ]`. `network:` and `manual_cache_clean` settings in the system configuration, `cloud-init.disabled`, `ds-identify.cfg`, unit masks and the previous instance's state under `/var/lib/cloud` are removed. |
| Network | With `NETWORK=reset`, the profiles of the source's physical NICs, and of the bonds, bridges and VLANs built on them, move to `/var/backups/petasus-migrate/` (NetworkManager keyfiles, `ifcfg-*` with their route and rule files, netplan, systemd-networkd, `/etc/network/interfaces`). Loopback, dummy devices, bridges without a physical port and tunnels stay. Rules that pin interface names to the source's MAC addresses move too, DHCP leases are deleted, and a systemd `.link` file names the NIC at PCI `0000:01:00.0` `enp1s0`. |
| Kernel arguments | `net.ifnames`, `biosdevname` and the arguments that disable cloud-init or pin its datasource are removed. A serial console (`console=ttyS0`, `ttyAMA0` on arm64) and `modprobe.blacklist=nouveau,amdgpu nouveau.modeset=0` are added; an existing module blacklist is merged, not replaced. |
| Guest agent | Enabled, with the RPC policy of every platform image: the RPCs that run commands, read or write files, or change passwords and SSH keys from the host side are closed. On an SELinux guest the boolean that lets the agent freeze filesystems is set. An agent older than QEMU 5.1, which reports the host name it read when it started, starts after cloud-init. |
| node_exporter | Installed as `/usr/local/bin/node_exporter` with the unit `node_exporter.service` and the account `nodeusr`, serving the memory and InfiniBand collectors. A node exporter unit the guest already enables (`node_exporter`, `prometheus-node-exporter`, `node-exporter`) is left as it is. |
| Guest firewall | An enabled firewalld or ufw gains a rule for 9100/tcp. Other rule sets are reported, not edited. |
| Initramfs | Rebuilt for every installed kernel without fitting it to the build hardware (`hostonly="no"` for dracut, `MODULES=most` for initramfs-tools), with the virtio disk drivers. |
| Boot loader | A UEFI GRUB is installed when the disk had none, and the removable-media loader is made sure of. |
| SELinux | The mode is kept. Files are relabeled by the guest's own policy, keeping customizable types. |

### BIOS disks

A disk that boots only through BIOS has no EFI system partition. With `UEFI_CONVERT=auto` the
conversion writes a new GPT disk: a 256 MiB EFI system partition at the front, then every partition
of the source copied byte for byte and compared afterwards. Filesystem UUIDs, LVM volumes and
partition numbers are unchanged (logical partitions keep their numbers as GPT partitions), GPT
partition GUIDs and names are carried over, and the last partition can still grow into a larger
disk. A FAT partition the guest already mounts at `/boot/efi` on an MBR disk becomes the EFI system
partition as it is. The virtual size grows by the new partition.

### What is kept

| | `GENERALIZE=preserve` | `GENERALIZE=template` |
|---|---|---|
| Accounts, passwords, `authorized_keys` | kept | passwords locked, every `~/.ssh` deleted |
| Logs, shell history, temporary files | kept | deleted |
| Application data and service configuration | kept | kept |
| SSH host keys | kept; the platform regenerates them for each VM | deleted |
| `/etc/machine-id` | replaced by a new value; the platform regenerates it for each VM | emptied |

`preserve` moves one machine. `template` makes an image many VMs start from, and does what the
platform does to a root disk it captures as an image.

## Refusals

`make inspect` and `make migrate` name the reason and stop when the disk

- holds no Linux system, or more than one;
- is of another CPU architecture than the host;
- has no partition table, holds LUKS volumes or software RAID members, or is hibernated;
- has a volume group with physical volumes on other disks, or a filesystem named in fstab that does not mount;
- runs no systemd, or a RHEL-family release before 7, or a kernel without virtio 1.0 drivers (before 4.0);
- has neither dracut nor initramfs-tools;
- mounts a filesystem that is not on this disk (`MISSING_MOUNTS=nofail` converts anyway and marks the
  entry `nofail`; `/`, `/usr`, `/var`, `/etc`, `/boot` and `/boot/efi` are always refused);
- has its EFI system partition mounted elsewhere than `/boot/efi`;
- has no EFI system partition and `UEFI_CONVERT=never`.

## Verification

`make migrate` ends with it; `make verify NAME=...` runs it again. The converted disk is started
twice from cold, each time on a new overlay 2 GiB larger than the disk, with a fresh UEFI variable
store, non-transitional virtio disks, the NIC at PCI `0000:01:00.0`, and a NoCloud disk with the
user-data and network-config the platform writes. The first start is also rebooted once.

Required — a failure stops the pipeline:

- the guest agent answers, every RPC KubeVirt needs is enabled, every closed RPC is disabled, and it
  reports the host name cloud-init set;
- the pod NIC is `enp1s0` at PCI `0000:01:00.0`, has its DHCP address and the only default route;
- the user from the NoCloud disk logs in over SSH with its key;
- cloud-init finished on the NoCloud datasource, set the host name and ran the user script;
- the system booted through UEFI from `/dev/vda`;
- node_exporter answers on 9100/tcp from outside the guest;
- the SELinux mode is the one the disk is configured with;
- all of it again after the reboot and on the second start, which also gets its own machine-id.

Restricted — recorded in `verify.json` and in the summary, and the disk still passes:

| Check | Meaning when restricted |
|---|---|
| `password-user` | The password users the platform sets do not log in: cloud-init before 22.3 does not read `chpasswd.users`. SSH keys work. |
| `root-growth` | The root filesystem does not grow by itself into a larger root disk (an LVM root, or a partition behind it). Grow it by hand after resizing. |
| `local-scratch` | The guest's `lvm2` lacks options the platform's scratch volume uses; give such a VM only flavors with the `raw-nvme` extra spec. |

The verification host's QEMU machine type may be older than the platform's; the first VM created
from the image is the final check.

## Registering the image

`make push` pushes `localhost/petasus-migrate/NAME:TAG` to `REGISTRY/REPOSITORY:TAG` and writes
`output/NAME/image.json`, the body of the platform's image registration:

| Variable | Meaning |
|---|---|
| `REGISTRY`, `REPOSITORY`, `TAG` | where the image is pushed |
| `REGISTRY_AUTHFILE`, `REGISTRY_CA` | a containers auth file and a CA certificate for the registry, when the host's defaults do not cover it |
| `SOURCE_REF` | the reference the platform pulls, when it reaches the registry under another name than the push did |
| `DISTRO_TYPE` | the platform's distro type. The default is `ubuntu` for Debian-family guests and `rocky` for RHEL-family guests of any vendor; the guest's real distribution is in `description`. |

```bash
curl -X POST "$EDGETRON_API/edgetron/resources/kubevirt/images" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d @output/web01/image.json
```

`size` is the disk's virtual size in GiB, rounded up; a VM's flavor needs a root disk at least that
large. The import is finished when the image's `phase` is `Succeeded`.

## Guests

Converted and verified with this tool on x86_64; the sources marked *platform VM* were also
registered and started as a platform VM. `tests/e2e/fixtures.sh` builds every source.

| Source | Platform VM | Restricted |
|---|---|---|
| Rocky Linux 8.10 GenericCloud | yes | |
| Rocky Linux 9 GenericCloud without cloud-init and guest agent, `net.ifnames=0`, a static `ifcfg-eth0`, `/dev/sda` names in fstab and on the kernel command line, an initramfs without virtio drivers, firewalld admitting SSH only, no reachable repository (packages from the Rocky DVD) | yes | |
| Rocky Linux 9 GenericCloud LVM | | `root-growth` |
| Rocky Linux 10 GenericCloud | | |
| CentOS 7 GenericCloud 2211: BIOS, MBR, packages from the CentOS vault | yes | `password-user`, `local-scratch` |
| CentOS 7.8 installed by virt-builder: BIOS, GPT with a BIOS boot partition, no cloud-init | | `password-user`, `local-scratch` |
| Ubuntu 20.04 cloud image | | `local-scratch` |
| Ubuntu 20.04 installed by virt-builder: BIOS, MBR with a logical root partition and a FAT `/boot/efi` without a UEFI loader, no cloud-init | yes | `local-scratch` |
| Ubuntu 22.04 cloud image with cloud-init disabled and pinned to another datasource with networking off, a static netplan for the source's NIC, a MAC-pinned interface name, `net.ifnames=0`, ufw admitting SSH only | yes | `local-scratch` |
| Ubuntu 24.04 cloud image | | |
| Ubuntu 26.04 cloud image | yes | |

Other RHEL-family releases from 7 and Ubuntu releases from 18.04 take the same code paths. Ubuntu
16.04 converts without netplan, its network rendered by cloud-init through ifupdown, and is not
verified. Not verified: RHEL itself (the RHEL-family paths were exercised with Rocky Linux and
CentOS, including installation without reachable repositories from a Rocky DVD), Debian, and arm64
hosts and guests.

## Limits

- One disk per conversion: the boot disk. Data disks are separate volumes on the platform.
- Sources are QCOW2 or raw files; convert other formats with `qemu-img convert` first.
- Btrfs root filesystems are refused where the host's libguestfs cannot open them (RHEL-family hosts).
- A guest's applications that are bound to the source's addresses, host name or NIC names need their
  own reconfiguration; the conversion reports what it set aside and does not edit applications.

## Development

```bash
make test                                              # unit tests, script syntax
tests/e2e/fixtures.sh ~/fixtures                       # public images and hostile variants of them
make migrate SRC=~/fixtures/rocky-9-hostile.qcow2 NAME=t1 \
  EL_REPO_ISO=~/fixtures/rocky-9-dvd.iso VERIFY_EXTRA=$PWD/tests/e2e/preserved.sh
```

| Path | Content |
|---|---|
| `bin/petasus-migrate`, `pmig/` | the command line and the host side: inspection, text edits of guest files through libguestfs, relocation, verification, packaging |
| `guest/` | POSIX shell scripts that run inside the guest's root filesystem: packages, services, initramfs and boot loader |
| `files/` | files installed into the guest |
| `containerdisk/Containerfile` | the containerDisk image |
| `tests/` | unit tests; `tests/e2e/` prepares source disks and checks that a workload survived |
