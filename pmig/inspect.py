"""Read-only inspection of a source disk: the facts a conversion needs and the reasons to refuse one."""
from __future__ import annotations

import re

from . import refs
from .guest import Guest
from .util import ARCHES, MigrateError, host_arch, qemu_img_info

ESP_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
BIOS_BOOT_GUID = "21686148-6449-6E6F-744E-656564454649"
LVM_GUID = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
SWAP_GUID = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
RAID_GUID = "A19D880F-05FC-4D3B-A006-743F0F84911E"

# MBR partition id -> GPT type GUID.
MBR_TO_GPT = {0x82: SWAP_GUID, 0x8E: LVM_GUID, 0xFD: RAID_GUID, 0xEF: ESP_GUID}
MBR_EXTENDED = (0x05, 0x0F, 0x85)

SOURCE_FORMATS = ("qcow2", "raw")
EL_IDS = ("rhel", "centos", "rocky", "almalinux", "ol", "fedora")
DEB_IDS = ("ubuntu", "debian")
# Packages whose versions the conversion reads.
WATCHED_PACKAGES = (
    "cloud-init", "qemu-guest-agent", "dracut", "dracut-core", "initramfs-tools-core", "systemd",
    "lvm2", "netplan.io", "NetworkManager", "network-manager", "grub2-common", "grub-common",
)


def parse_os_release(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        match = re.match(r'^([A-Z0-9_]+)=(.*)$', line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def os_family(os_id: str, id_like: str) -> str | None:
    names = [os_id] + id_like.split()
    if any(name in EL_IDS for name in names):
        return "el"
    if any(name in DEB_IDS for name in names):
        return "deb"
    return None


def version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:3])


def _partitions(guest: Guest) -> dict:
    g, dev = guest.g, guest.disk
    disk = {
        "size": g.blockdev_getsize64(dev),
        "sector_size": g.blockdev_getss(dev),
        "parttype": None,
        "disk_guid": None,
        "partitions": [],
    }
    try:
        disk["parttype"] = g.part_get_parttype(dev)
    except RuntimeError:
        return disk
    if disk["parttype"] not in ("msdos", "gpt"):
        return disk
    filesystems = g.list_filesystems()
    if disk["parttype"] == "gpt":
        disk["disk_guid"] = g.part_get_disk_guid(dev)
    else:
        disk["mbr_signature"] = "%08x" % int.from_bytes(g.pread_device(dev, 4, 440), "little")
    for part in g.part_list(dev):
        num = part["part_num"]
        device = f"{dev}{num}"
        entry = {
            "num": num,
            "start": part["part_start"],
            "end": part["part_end"],
            "size": part["part_size"],
            "vfs": filesystems.get(device, ""),
            "uuid": "",
            "label": "",
            "bootable": False,
        }
        if disk["parttype"] == "gpt":
            entry["type"] = g.part_get_gpt_type(dev, num).upper()
            entry["guid"] = g.part_get_gpt_guid(dev, num).upper()
            entry["name"] = g.part_get_name(dev, num)
            entry["attributes"] = g.part_get_gpt_attributes(dev, num)
            entry["kind"] = {ESP_GUID: "esp", BIOS_BOOT_GUID: "bios_boot"}.get(entry["type"], "data")
        else:
            mbr_id = g.part_get_mbr_id(dev, num)
            entry["mbr_id"] = mbr_id
            entry["mbr_kind"] = g.part_get_mbr_part_type(dev, num)
            entry["bootable"] = bool(g.part_get_bootable(dev, num))
            entry["type"] = MBR_TO_GPT.get(mbr_id, LINUX_GUID)
            if mbr_id in MBR_EXTENDED or entry["mbr_kind"] == "extended":
                entry["kind"] = "extended"
            elif mbr_id == 0xEF:
                entry["kind"] = "esp"
            else:
                entry["kind"] = "data"
        if entry["kind"] != "extended" and entry["vfs"] not in ("", "unknown"):
            for key, getter in (("uuid", g.vfs_uuid), ("label", g.vfs_label)):
                try:
                    entry[key] = getter(device)
                except RuntimeError:
                    pass
        disk["partitions"].append(entry)
    return disk


def _kernels(guest: Guest) -> list:
    """Installed kernels that carry a module tree, with whether virtio disk and network drivers exist."""
    kernels = []
    for moddir in guest.glob("/lib/modules/*"):
        version = moddir.rsplit("/", 1)[1]
        if not guest.is_dir(f"{moddir}/kernel"):
            continue
        image = next((path for path in (f"/boot/vmlinuz-{version}", f"{moddir}/vmlinuz") if guest.is_file(path)), None)
        builtin = guest.read(f"{moddir}/modules.builtin", "") or ""
        drivers = {}
        for name, subdir in (("virtio_blk", "block"), ("virtio_net", "net"), ("virtio_pci", "virtio")):
            present = f"/{name}.ko" in builtin or bool(guest.glob(f"{moddir}/kernel/drivers/{subdir}/{name}.ko*"))
            drivers[name] = present
        kernels.append({"version": version, "image": image, "virtio": all(drivers.values())})
    return kernels


def _efi(guest: Guest, arch: str) -> dict:
    """What the mounted ESP holds: whether the fallback loader for arch is there, and vendor directories."""
    arch_boot = ARCHES.get(arch, {}).get("boot_efi")
    efi = {"fallback": False, "vendors": []}
    for path in guest.glob("/boot/efi/EFI/*"):
        name = path.rsplit("/", 1)[1]
        if name.upper() == "BOOT":
            efi["fallback"] = any(entry.upper() == arch_boot for entry in guest.g.ls(path))
        elif guest.is_dir(path):
            efi["vendors"].append(name)
    return efi


def gather(guest: Guest) -> dict:
    """Facts about a mounted guest. The caller has run mount_os()."""
    g, root = guest.g, guest.root
    release = parse_os_release(guest.read("/etc/os-release", "") or guest.read("/usr/lib/os-release", "") or "")
    os_id = release.get("ID", g.inspect_get_distro(root))
    os_info = {
        "id": os_id,
        "id_like": release.get("ID_LIKE", ""),
        "version_id": release.get("VERSION_ID", ""),
        "pretty": release.get("PRETTY_NAME", g.inspect_get_product_name(root)),
        "major": g.inspect_get_major_version(root),
        "minor": g.inspect_get_minor_version(root),
        "arch": g.inspect_get_arch(root),
    }
    os_info["family"] = os_family(os_id, os_info["id_like"])

    packages = {}
    try:
        for app in g.inspect_list_applications2(root):
            if app["app2_name"] in WATCHED_PACKAGES:
                packages[app["app2_name"]] = app["app2_version"]
    except RuntimeError as err:
        guest.note("WARN", "packages", f"the package database could not be read: {err}")

    mountpoints = g.inspect_get_mountpoints(root)
    selinux = "disabled"
    match = re.search(r"^SELINUX=(\w+)", guest.read("/etc/selinux/config", "") or "", re.M)
    if match:
        selinux = match.group(1)

    if os_info["family"] == "deb" and "initramfs-tools-core" in packages:
        generator = "initramfs-tools"
    elif "dracut" in packages or "dracut-core" in packages or guest.is_file("/usr/bin/dracut"):
        generator = "dracut"
    elif guest.is_file("/usr/sbin/update-initramfs"):
        generator = "initramfs-tools"
    else:
        generator = None

    volume_groups = []
    for vg in g.vgs_full():
        volume_groups.append({"name": vg["vg_name"], "partial": "p" in vg["vg_attr"]})

    return {
        "os": os_info,
        "packages": packages,
        "root": {"device": root, "vfs": g.vfs_type(root)},
        "mountpoints": mountpoints,
        "boot": {
            "kernels": _kernels(guest),
            "initramfs_generator": generator,
            "efi": _efi(guest, os_info["arch"]),
        },
        "systemd": guest.is_file("/usr/lib/systemd/systemd") or guest.is_file("/lib/systemd/systemd"),
        "selinux": selinux,
        "cloud_init": {"installed": "cloud-init" in packages or guest.is_file("/usr/bin/cloud-init"),
                       "version": packages.get("cloud-init", "")},
        "volume_groups": volume_groups,
    }


def inspect_disk(path: str, fmt: str | None = None, missing_mounts: str = "refuse", uefi_convert: str = "auto") -> dict:
    """Everything known about a source image without changing it, and every reason to refuse it."""
    image = qemu_img_info(path, fmt)
    fmt = fmt or image["format"]
    info = {
        "source": {
            "path": path,
            "format": fmt,
            "virtual_size": image["virtual-size"],
            "actual_size": image.get("actual-size", 0),
            "backing_file": image.get("backing-filename"),
        },
        "refusals": [],
        "notes": [],
    }
    if fmt not in SOURCE_FORMATS:
        info["refusals"].append(f"the source format is {fmt}; qcow2 and raw are supported")
        return info
    with Guest(path, fmt=fmt, readonly=True) as guest:
        g = guest.g
        info["disk"] = _partitions(guest)
        info["filesystems"] = g.list_filesystems()
        info["md_devices"] = g.list_md_devices()
        try:
            guest.find_root()
            if g.inspect_get_type(guest.root) != "linux":
                raise MigrateError(f"the guest is {g.inspect_get_type(guest.root)}; only Linux guests are supported")
            info["mount_failures"] = guest.mount_os()
        except MigrateError as err:
            info["refusals"].append(str(err))
            return info
        info.update(gather(guest))
        info["fstab"] = guest.read("/etc/fstab", "") or ""
        info["notes"] = guest.notes
        info["needs_esp"] = needs_esp(info)
        info["esp_candidate"] = esp_candidate(info, guest.disk) if info["needs_esp"] else None
        info["refusals"] = refusals(info) or option_refusals(info, refs.devices_of(guest, info["disk"]),
                                                              missing_mounts, uefi_convert)
    return info


def option_refusals(info: dict, devices, missing_mounts: str, uefi_convert: str) -> list:
    """Refusals that depend on the conversion's options."""
    reasons = []
    _, findings = refs.rewrite_fstab(info["fstab"], devices, missing_mounts)
    for finding in findings:
        if finding["action"] == "refused":
            reasons.append(f"fstab mounts {finding['mount']} from {finding['spec']}, which is not in this image"
                           + ("" if finding["mount"] in refs.CRITICAL_MOUNTS
                              else " (MISSING_MOUNTS=nofail converts anyway and marks the entry nofail)"))
    if info["needs_esp"] and uefi_convert == "never":
        reasons.append("the disk has no EFI system partition and UEFI_CONVERT=never")
    return reasons


def esp_partition(info: dict) -> dict | None:
    for part in info["disk"]["partitions"]:
        if part["kind"] == "esp" and part["vfs"] == "vfat":
            return part
    return None


def needs_esp(info: dict) -> bool:
    return "disk" in info and esp_partition(info) is None


def esp_candidate(info: dict, disk: str) -> int | None:
    """A FAT partition the guest mounts at /boot/efi although the partition table does not type it as one."""
    device = info.get("mountpoints", {}).get("/boot/efi", "")
    for part in info["disk"]["partitions"]:
        if device == f"{disk}{part['num']}" and part["vfs"] == "vfat" and part["kind"] == "data" and part["size"] >= 64 << 20:
            return part["num"]
    return None


def refusals(info: dict) -> list:
    """Why this disk cannot be converted; empty when it can."""
    reasons = []
    os_info, disk = info["os"], info["disk"]
    if os_info["arch"] != host_arch():
        reasons.append(f"the guest is {os_info['arch']} and this host is {host_arch()}; convert on a host of the guest's architecture")
    if os_info["family"] is None:
        reasons.append(f"the distribution {os_info['id']!r} is neither RHEL-family nor Debian-family")
    if disk["parttype"] not in ("msdos", "gpt"):
        reasons.append("the disk has no partition table (a filesystem or LVM volume on the whole disk)")
    if any(vfs == "crypto_LUKS" for vfs in info["filesystems"].values()):
        reasons.append("the disk holds LUKS-encrypted volumes")
    if info["md_devices"]:
        reasons.append("the disk holds software RAID (md) members")
    if any(vfs == "swsuspend" for vfs in info["filesystems"].values()):
        reasons.append("the guest is hibernated; boot it and shut it down cleanly first")
    for vg in info["volume_groups"]:
        if vg["partial"]:
            reasons.append(f"volume group {vg['name']} has physical volumes on disks that are not in this image")
    if info.get("mount_failures"):
        failed = ", ".join(f"{mp} ({err})" for mp, err in info["mount_failures"].items())
        reasons.append(f"filesystems named in fstab do not mount: {failed}")
    if not info["systemd"]:
        reasons.append("the guest does not run systemd")
    kernels = info["boot"]["kernels"]
    if not any(kernel["image"] for kernel in kernels):
        reasons.append("no installed kernel was found")
    elif not any(kernel["image"] and kernel["virtio"] for kernel in kernels):
        reasons.append("no installed kernel carries the virtio disk and network drivers")
    if os_info["family"] == "el" and os_info["major"] and os_info["major"] < 7:
        reasons.append(f"{os_info['pretty']} predates the supported RHEL-family releases (7 and later)")
    newest = max((version_tuple(kernel["version"]) for kernel in kernels), default=())
    if os_info["family"] == "deb" and newest and newest < (4, 0):
        reasons.append(f"kernel {'.'.join(map(str, newest))} has no virtio 1.0 drivers (4.0 and later do)")
    if info["boot"]["initramfs_generator"] is None:
        reasons.append("neither dracut nor initramfs-tools is installed")
    return reasons
