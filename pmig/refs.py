"""Device references that survive a change of disk controller and partition table.

fstab and the kernel command line may name the root disk by a controller-specific path
(/dev/sda1, /dev/xvda1) or by an MBR partition id. Those become filesystem UUIDs. A by-id or
by-path link names the source's hardware and becomes a UUID only for the root filesystem.
"""
from __future__ import annotations

import re

CRITICAL_MOUNTS = ("/", "/usr", "/var", "/etc", "/boot", "/boot/efi")
NOFAIL_OPTIONS = "nofail,x-systemd.device-timeout=30s"

_KERNEL_NAME = re.compile(r"^/dev/(?:(?:sd|hd|vd|xvd)([a-z]+)|nvme(\d+)n(\d+)p|mmcblk(\d+)p)(\d+)$")
_MBR_PARTUUID = re.compile(r"^([0-9a-fA-F]{8})-([0-9a-fA-F]{2})$")
_BY_HARDWARE = re.compile(r"^/dev/disk/by-(?:id|path)/")
_NOT_A_DISK = re.compile(r"^/dev/(?:sr|cdrom|dvd|fd|loop|zram|ram|shm|pts)")
_SPEC_IN_ARGS = re.compile(r"""\b(root|resume|RESUME)=([^\s"'\\]+)""")


class Devices:
    """What the image holds: partitions of its one disk, and every filesystem and volume in it.

    mbr_signature is the disk identifier MBR PARTUUIDs start with; root_spec is a stable reference
    to the filesystem the guest's root is on.
    """

    def __init__(self, partitions=(), uuids=(), labels=(), paths=(), partlabels=(), mbr_signature=None, root_spec=None):
        self.partitions = {part["num"]: part for part in partitions}
        self.uuids = {uuid.lower() for uuid in uuids if uuid}
        self.labels = {label for label in labels if label}
        self.paths = set(paths)
        self.partuuids = {part["guid"].lower() for part in partitions if part.get("guid")}
        self.partlabels = {name for name in partlabels if name}
        self.mbr_signature = (mbr_signature or "").lower()
        self.root_spec = root_spec

    def partition_uuid(self, num: int):
        """(status, spec) for a partition of the image's disk named by its number."""
        part = self.partitions.get(num)
        if part is None:
            return "missing", None
        if part.get("uuid"):
            return "rewrite", f"UUID={part['uuid']}"
        return "present", None

    def resolve(self, spec: str):
        """(status, replacement): status is opaque, present, rewrite, missing or ambiguous.

        ambiguous: a name the source's hardware gave a disk. Nothing ties it to this image's disk
        rather than to another disk of the source machine.
        """
        if spec.startswith("UUID="):
            return ("present" if spec[5:].strip('"').lower() in self.uuids else "missing"), None
        if spec.startswith("LABEL="):
            return ("present" if spec[6:].strip('"') in self.labels else "missing"), None
        if spec.startswith("PARTLABEL="):
            return ("present" if spec[10:].strip('"') in self.partlabels else "missing"), None
        if spec.startswith("PARTUUID=") or spec.startswith("/dev/disk/by-partuuid/"):
            value = spec.split("=", 1)[1].strip('"') if spec.startswith("PARTUUID=") else spec.rsplit("/", 1)[1]
            match = _MBR_PARTUUID.match(value)
            if match:
                if match.group(1).lower() != self.mbr_signature:
                    return "missing", None
                return self.partition_uuid(int(match.group(2), 16))
            return ("present" if value.lower() in self.partuuids else "missing"), None
        if not spec.startswith("/dev/") or _NOT_A_DISK.match(spec):
            return "opaque", None
        if spec.startswith("/dev/disk/by-uuid/"):
            return ("present" if spec[18:].lower() in self.uuids else "missing"), None
        if spec.startswith("/dev/disk/by-label/"):
            return ("present" if spec[19:] in self.labels else "missing"), None
        if _BY_HARDWARE.match(spec):
            return "ambiguous", None
        match = _KERNEL_NAME.match(spec)
        if match:
            letters, controller, namespace, mmc, num = match.groups()
            if letters is not None:
                first_disk = letters == "a"
            elif controller is not None:
                first_disk = controller == "0" and namespace == "1"
            else:
                first_disk = mmc == "0"
            return self.partition_uuid(int(num)) if first_disk else ("missing", None)
        if spec in self.paths:
            return "present", None
        return "missing", None


def rewrite_fstab(text: str, devices: Devices, missing: str = "refuse"):
    """New fstab text and one finding per entry whose device is rewritten or not on the image's disk.

    A finding is {mount, spec, action}; action is rewritten, nofail, refused or ignored. A system
    mount whose device is absent is refused whatever its options and the policy say.
    """
    findings = []
    lines = []
    for line in text.splitlines(keepends=True):
        fields = line.split()
        if len(fields) < 3 or fields[0].startswith("#"):
            lines.append(line)
            continue
        spec, mount, options = fields[0], fields[1], fields[3] if len(fields) > 3 else "defaults"
        status, replacement = devices.resolve(spec)
        if mount == "/" and status in ("missing", "ambiguous") and devices.root_spec:
            # The root filesystem is the one this image was found by.
            status, replacement = "rewrite", devices.root_spec
        if status == "rewrite" and replacement:
            line = line.replace(spec, replacement, 1)
            findings.append({"mount": mount, "spec": spec, "action": "rewritten", "to": replacement})
        elif status in ("missing", "ambiguous"):
            if mount in CRITICAL_MOUNTS:
                findings.append({"mount": mount, "spec": spec, "action": "refused"})
            elif {"nofail", "noauto"} & set(options.split(",")):
                findings.append({"mount": mount, "spec": spec, "action": "ignored"})
            elif missing == "refuse":
                findings.append({"mount": mount, "spec": spec, "action": "refused"})
            else:
                if len(fields) > 3:
                    line = re.sub(r"^(\s*\S+\s+\S+\s+\S+\s+)(\S+)", rf"\g<1>\g<2>,{NOFAIL_OPTIONS}", line, count=1)
                else:
                    line = line.rstrip("\n") + f" {NOFAIL_OPTIONS} 0 0\n"
                findings.append({"mount": mount, "spec": spec, "action": "nofail"})
        lines.append(line)
    return "".join(lines), findings


def rewrite_args(text: str, devices: Devices) -> str:
    """root= and resume= device specs in a kernel command line or a file that holds one."""
    def replace(match):
        status, replacement = devices.resolve(match.group(2))
        if match.group(1) == "root" and status in ("missing", "ambiguous") and devices.root_spec:
            status, replacement = "rewrite", devices.root_spec
        return f"{match.group(1)}={replacement}" if status == "rewrite" else match.group(0)

    return _SPEC_IN_ARGS.sub(replace, text)


def edit_grubenv(data: bytes, transform) -> bytes:
    """Apply transform(text) to a GRUB environment block, keeping its fixed size."""
    size = len(data)
    text = data.decode("utf-8", errors="surrogateescape").rstrip("#")
    new = transform(text)
    if new == text:
        return data
    encoded = new.encode("utf-8", errors="surrogateescape")
    if len(encoded) > size:
        raise ValueError("the GRUB environment block has no room for the change")
    return encoded + b"#" * (size - len(encoded))


# Files that carry a kernel command line or a device spec for the initramfs.
ARG_FILES = (
    "/etc/default/grub",
    "/etc/default/grub.d/*.cfg",
    "/etc/kernel/cmdline",
    "/boot/loader/entries/*.conf",
    "/boot/grub2/grub.cfg",
    "/boot/grub/grub.cfg",
    "/etc/initramfs-tools/conf.d/resume",
)
GRUBENV_FILES = ("/boot/grub2/grubenv", "/boot/efi/EFI/*/grubenv", "/boot/grub/grubenv")


def devices_of(guest, disk: dict) -> Devices:
    """The device table of a mounted guest."""
    g = guest.g
    uuids, labels, paths = [], [], []
    for device in g.list_filesystems():
        paths.append(device)
        for store, getter in ((uuids, g.vfs_uuid), (labels, g.vfs_label)):
            try:
                store.append(getter(device))
            except RuntimeError:
                pass
    for lv in g.lvs():
        paths.append(lv)
        vg, name = lv.split("/")[2:4]
        paths.append("/dev/mapper/" + vg.replace("-", "--") + "-" + name.replace("-", "--"))
    names = [part.get("name", "") for part in disk["partitions"]]
    root_spec = None
    if guest.root:
        try:
            root_spec = f"UUID={g.vfs_uuid(guest.root)}" if guest.root.startswith(guest.disk) else guest.root
        except RuntimeError:
            root_spec = None
    return Devices(disk["partitions"], uuids, labels, paths, names, disk.get("mbr_signature"), root_spec)


def apply(guest, disk: dict, missing: str) -> list:
    """Rewrite the references of a mounted guest. Returns the fstab findings."""
    devices = devices_of(guest, disk)
    fstab = guest.read("/etc/fstab", "") or ""
    new_fstab, findings = rewrite_fstab(fstab, devices, missing)
    if new_fstab != fstab:
        guest.backup_copy("/etc/fstab")
        guest.write("/etc/fstab", new_fstab)
    for path in guest.paths(ARG_FILES):
        if guest.edit(path, lambda text: rewrite_args(text, devices)):
            guest.note("INFO", "refs", f"{path}: device names replaced by filesystem UUIDs")
    for path in guest.paths(GRUBENV_FILES):
        guest.edit_grubenv(path, lambda text: rewrite_args(text, devices))
    for finding in findings:
        if finding["action"] == "rewritten":
            guest.note("INFO", "refs", f"fstab {finding['mount']}: {finding['spec']} -> {finding['to']}")
        elif finding["action"] == "nofail":
            guest.note("WARN", "missing-mount",
                       f"fstab {finding['mount']} names {finding['spec']}, which is not in this image; marked nofail")
    # LVM's devices file pins physical volumes to the identifiers the source hypervisor gave them.
    if guest.is_file("/etc/lvm/devices/system.devices"):
        guest.backup_move("/etc/lvm/devices/system.devices")
        guest.note("INFO", "refs", "moved /etc/lvm/devices/system.devices aside: it names the source's disks")
    lvm_conf = guest.read("/etc/lvm/lvm.conf", "") or ""
    if re.search(r"^\s*(global_)?filter\s*=", lvm_conf, re.M):
        guest.note("WARN", "lvm-filter", "/etc/lvm/lvm.conf sets a device filter; it must accept /dev/vd*")
    return findings
