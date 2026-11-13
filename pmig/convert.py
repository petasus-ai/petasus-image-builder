"""The conversion: a working copy of the source, changed offline, becomes output/<name>/disk.qcow2."""
from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
import time
import uuid

from . import cloudinit, kargs, network, refs
from .assets import node_exporter
from .guest import BACKUP_DIR, GUEST_DIR, Guest
from .inspect import _partitions, esp_partition, gather, inspect_disk
from .platform import ALLOWED_RPCS, EXPORTER_UNITS, FORBIDDEN_RPCS, POD_NIC_LINK
from .relocate import relocate
from .verify import failure, summary, verify_disk
from .util import (MigrateError, Refused, arch_info, free_bytes, gib_ceil, locked, log, need, qemu_img_info, run,
                   sha256_file, write_json)

NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
# The operations the platform's own image cleaning runs on a captured root disk.
SYSPREP_OPERATIONS = ("machine-id,ssh-hostkeys,ssh-userdir,passwd-backups,net-hwaddr,net-nmconn,udev-persistent-net,"
                      "dhcp-client-state,bash-history,logfiles,tmp-files,customize")
ESP_FSTAB_OPTIONS = "umask=0077,shortname=winnt"


def check_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise MigrateError(f"NAME {name!r} must be lowercase letters, digits, '.' or '-' (at most 63 characters)")


def mount_esp(guest: Guest, disk: dict) -> None:
    """Have the EFI system partition mounted at /boot/efi and named in fstab."""
    g = guest.g
    mounted = g.mountpoints()
    esp = esp_partition({"disk": disk})
    if esp is None:
        raise MigrateError("the disk has no EFI system partition")
    device = f"{guest.disk}{esp['num']}"
    if mounted.get(device) == "/boot/efi":
        return
    if device in mounted:
        raise Refused([f"the EFI system partition is mounted at {mounted[device]}; only /boot/efi is supported"])
    fstab = guest.read("/etc/fstab", "") or ""
    if not re.search(r"^\s*\S+\s+/boot/efi\s", fstab, re.M):
        if fstab and not fstab.endswith("\n"):
            fstab += "\n"
        guest.write("/etc/fstab", fstab + f"UUID={g.vfs_uuid(device)} /boot/efi vfat {ESP_FSTAB_OPTIONS} 0 2\n")
        guest.note("INFO", "efi", "added the EFI system partition to fstab at /boot/efi")
    g.mkdir_p("/boot/efi")
    g.mount(device, "/boot/efi")


def enabled_exporter(guest: Guest) -> str | None:
    """The node exporter unit the guest already starts at boot, if any: wanted or required, not masked."""
    for unit in EXPORTER_UNITS:
        masked = guest.g.is_symlink(f"/etc/systemd/system/{unit}") and \
            guest.g.readlink(f"/etc/systemd/system/{unit}") == "/dev/null"
        if not masked and guest.paths([f"/etc/systemd/system/*.wants/{unit}", f"/etc/systemd/system/*.requires/{unit}"]):
            return unit
    return None


def new_machine_id(guest: Guest) -> None:
    """A populated machine-id that is not the source machine's.

    An empty one would make systemd treat the first boot as a first boot and apply unit presets,
    enabling services the source had turned off.
    """
    value = uuid.uuid4().hex + "\n"
    guest.write("/etc/machine-id", value, mode=0o444)
    dbus = "/var/lib/dbus/machine-id"
    if guest.g.is_file(dbus, followsymlinks=False):
        guest.write(dbus, value)


def lock_passwords(guest: Guest) -> None:
    def lock(text: str) -> str:
        lines = []
        for line in text.splitlines():
            fields = line.split(":")
            if len(fields) > 1 and not re.match(r"^[!*]+$", fields[1]):
                fields[1] = "!"
            lines.append(":".join(fields))
        return "\n".join(lines) + "\n"

    guest.edit("/etc/shadow", lock)


def relabel(guest: Guest, mode: str) -> None:
    """Label what the conversion wrote, by the guest's own SELinux policy."""
    if mode == "disabled":
        return
    match = re.search(r"^SELINUXTYPE=(\S+)", guest.read("/etc/selinux/config", "") or "", re.M)
    spec = f"/etc/selinux/{match.group(1) if match else 'targeted'}/contexts/files/file_contexts"
    g = guest.g
    if not guest.is_file(spec):
        guest.note("WARN", "selinux", f"{spec} is missing; the guest relabels itself at its first boot")
        g.touch("/.autorelabel")
        return
    try:
        for device, mountpoint in sorted(g.mountpoints().items(), key=lambda item: len(item[1])):
            if g.vfs_type(device) not in ("vfat", "swap"):
                g.selinux_relabel(spec, mountpoint, force=False)
    except RuntimeError as err:
        guest.note("WARN", "selinux", f"offline relabel failed ({err}); the guest relabels itself at its first boot")
        g.touch("/.autorelabel")


def convert_guest(guest: Guest, args, root: str, media_dev: str | None) -> dict:
    """Everything done inside the mounted working copy. Returns the facts of the converted guest."""
    guest.mount_os()
    disk = _partitions(guest)
    mount_esp(guest, disk)
    facts = gather(guest)
    os_info, arch = facts["os"], arch_info(facts["os"]["arch"])
    efi_install = not facts["boot"]["efi"]["fallback"]
    if efi_install:
        guest.note("INFO", "efi", "the EFI system partition has no fallback loader; installing one")
    exporter = enabled_exporter(guest)

    guest.install_scripts(os.path.join(root, "guest"), {
        "FAMILY": os_info["family"], "OS_ID": os_info["id"], "OS_MAJOR": os_info["major"],
        "EFI_ARCH": arch["efi"], "EFI_BOOT": arch["boot_efi"], "DEB_ARCH": arch["deb"], "GRUB_TARGET": arch["grub_target"],
        "POD_LINK": POD_NIC_LINK, "ALLOW_RPCS": ",".join(ALLOWED_RPCS), "BLOCK_RPCS": ",".join(FORBIDDEN_RPCS),
        "NODE_EXPORTER": exporter or "",
        "PM_EFI_INSTALL": int(efi_install),
        "PM_INITRAMFS": facts["boot"]["initramfs_generator"],
        "PM_SELINUX": facts["selinux"],
        "PM_MEDIA_DEV": media_dev or "",
    })
    if exporter is None:
        guest.upload(node_exporter(os.path.join(root, ".cache"), os_info["arch"]), f"{GUEST_DIR}/node_exporter", 0o755)
        guest.upload(os.path.join(root, "files", "node_exporter.service"), f"{GUEST_DIR}/node_exporter.service")
    if args.build_repo_file:
        suffix = ".repo" if os_info["family"] == "el" else os.path.splitext(args.build_repo_file)[1] or ".list"
        guest.upload(args.build_repo_file, f"{GUEST_DIR}/repos/build{suffix}")

    guest.run_script("packages.sh")
    log("  editing cloud-init, network and kernel argument configuration")
    cloudinit.apply(guest)
    if args.network == "reset":
        network.reset(guest)
    network.clear_leases(guest)
    with open(os.path.join(root, "files", "70-petasus-pod-nic.link"), encoding="utf-8") as handle:
        guest.write(POD_NIC_LINK, handle.read(), mode=0o644)
    guest.run_script("services.sh")
    kargs.apply(guest, arch["console"])
    guest.run_script("boot.sh")

    if args.generalize == "template":
        lock_passwords(guest)
    else:
        new_machine_id(guest)
    guest.remove_scripts()
    if guest.is_dir(BACKUP_DIR) and not guest.g.ls(BACKUP_DIR):
        guest.g.rmdir(BACKUP_DIR)
    facts = gather(guest)
    root_stat = guest.g.statvfs("/")
    facts["root_fs_bytes"] = root_stat["blocks"] * root_stat["frsize"]
    facts["homes"] = [line.split(":")[5] for line in (guest.read("/etc/passwd", "") or "").splitlines()
                      if len(line.split(":")) > 5 and line.split(":")[5].startswith("/")]
    relabel(guest, facts["selinux"])
    return facts


def sysprep(disk: str, fmt: str, homes: list) -> None:
    """Template mode: what the platform's image cleaning does to a captured root disk."""
    cmd = ["virt-sysprep", "--format", fmt, "-a", disk, "--operations", SYSPREP_OPERATIONS]
    for home in sorted(set(homes)):
        cmd += ["--delete", f"{home}/.ssh"]
    cmd += ["--delete", "/var/lib/cloud/*", "--delete", "/var/log/cloud-init*",
            "--delete", "/etc/netplan/50-cloud-init.yaml", "--delete", "/etc/ssh/sshd_config.d/50-cloud-init.conf"]
    run(cmd, capture=True)


def migrate(args, root: str) -> None:
    check_name(args.name)
    need("qemu-img", "sgdisk", "mkfs.vfat", "virt-sparsify", "virt-sysprep")
    for path in (args.src, args.build_repo_file, args.el_repo_iso):
        if path and not os.path.isfile(path):
            raise MigrateError(f"{path} does not exist")
    out = os.path.join(args.output, args.name)
    if os.path.commonpath([os.path.realpath(args.src), os.path.realpath(out)]) == os.path.realpath(out):
        raise MigrateError(f"{args.src} is inside {out}, which the conversion replaces; copy the source elsewhere first")
    with locked(out):
        for stale in glob.glob(os.path.join(out, ".work.*")):
            shutil.rmtree(stale, ignore_errors=True)

        log(f"inspecting {args.src}")
        info = inspect_disk(args.src, args.src_format, args.missing_mounts, args.uefi_convert)
        if info["refusals"]:
            raise Refused(info["refusals"])
        source = info["source"]
        needed = 3 * max(source["actual_size"], 1 << 30) + (2 << 30)
        if free_bytes(out) < needed:
            raise MigrateError(f"{out} has {free_bytes(out) >> 30} GiB free; this conversion needs about {needed >> 30} GiB")

        work = tempfile.mkdtemp(prefix=".work.", dir=out)
        try:
            started = time.time()
            disk, fmt = os.path.join(work, "work.qcow2"), "qcow2"
            log("copying the source to a working image")
            run(["qemu-img", "convert", "-f", source["format"], "-O", "qcow2", args.src, disk])
            source_sha = sha256_file(args.src)

            notes = []
            log("making device references independent of the disk controller")
            with Guest(disk, fmt=fmt) as guest:
                guest.mount_os()
                refs.apply(guest, info["disk"], args.missing_mounts)
                notes += guest.notes

            layout = None
            if info["needs_esp"]:
                log("no EFI system partition: relocating the partitions onto a GPT disk with one")
                raw = os.path.join(work, "relocated.raw")
                layout = relocate(disk, fmt, raw, info["disk"], info["esp_candidate"])
                os.unlink(disk)
                disk, fmt = raw, "raw"

            log("installing and configuring what the platform needs")
            extra = [{"path": args.el_repo_iso, "format": "raw", "readonly": True}] if args.el_repo_iso else []
            with Guest(disk, fmt=fmt, network=True, extra=extra) as guest:
                try:
                    facts = convert_guest(guest, args, root, "/dev/sdb" if extra else None)
                finally:
                    notes += guest.notes

            if args.generalize == "template":
                log("template mode: removing the source's identity and credentials")
                sysprep(disk, fmt, facts["homes"])

            log("reclaiming free space and compressing")
            sparsify = run(["virt-sparsify", "--in-place", "--format", fmt, disk], check=False, capture=True)
            if sparsify.returncode != 0:
                notes.append({"level": "WARN", "key": "sparsify", "message": "free space was not reclaimed: "
                              + (sparsify.stderr.strip().splitlines() or ["unknown error"])[-1]})
            final = os.path.join(work, "disk.qcow2")
            run(["qemu-img", "convert", "-c", "-f", fmt, "-O", "qcow2", disk, final])
            run(["qemu-img", "check", final], capture=True)
            image = qemu_img_info(final, "qcow2")

            manifest = {
                "name": args.name,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_seconds": int(time.time() - started),
                "source": {"path": os.path.abspath(args.src), "sha256": source_sha, "format": source["format"],
                           "virtual_size": source["virtual_size"]},
                "options": {"generalize": args.generalize, "network": args.network, "uefi_convert": args.uefi_convert,
                            "missing_mounts": args.missing_mounts, "build_repo_file": args.build_repo_file,
                            "el_repo_iso": args.el_repo_iso},
                "os": facts["os"],
                "selinux": facts["selinux"],
                "cloud_init": facts["cloud_init"],
                "packages": facts["packages"],
                "disk": {"sha256": sha256_file(final), "virtual_size": image["virtual-size"],
                         "size_gib": gib_ceil(image["virtual-size"]), "file_size": os.path.getsize(final),
                         "relocated": layout is not None},
                "layout": layout,
                "root_fs_bytes": facts["root_fs_bytes"],
                "notes": notes,
                "restrictions": [note for note in notes if note["level"] == "RESTRICT"],
            }
            write_json(os.path.join(work, "manifest.json"), manifest)
            info.pop("fstab", None)
            write_json(os.path.join(work, "inspect.json"), info)
            log(f"converted {facts['os']['pretty']} ({manifest['disk']['size_gib']} GiB virtual, "
                f"{manifest['disk']['file_size'] >> 20} MiB file)")

            produced = ["manifest.json", "inspect.json"]
            result = None
            if not args.skip_verify:
                result = verify_disk(final, manifest, work)
                write_json(os.path.join(work, "verify.json"), result)
                produced += ["verify.json"] + [name for name in ("serial-1.log", "serial-2.log")
                                               if os.path.exists(os.path.join(work, name))]
                if not result["passed"]:
                    raise MigrateError(f"{failure(result)}; the checks and serial consoles are in "
                                       + keep_failed(work, out, produced))
            publish(work, out, produced)
            if result is not None:
                log(summary(result))
        finally:
            shutil.rmtree(work, ignore_errors=True)


def keep_failed(work: str, out: str, records: list) -> str:
    """Move a failed run's records to out/failed; the published output stays as it is."""
    failed = os.path.join(out, "failed")
    shutil.rmtree(failed, ignore_errors=True)
    os.makedirs(failed)
    for name in records:
        shutil.move(os.path.join(work, name), os.path.join(failed, name))
    return failed


def publish(work: str, out: str, records: list) -> None:
    """Replace the output with this run's. The disk goes last: an interrupted publication leaves the
    previous disk with records that do not match it, which package refuses."""
    for stale in ("verify.json", "image.json", "push.json", "serial-1.log", "serial-2.log"):
        if os.path.exists(os.path.join(out, stale)):
            os.unlink(os.path.join(out, stale))
    shutil.rmtree(os.path.join(out, "failed"), ignore_errors=True)
    for name in records + ["disk.qcow2"]:
        os.replace(os.path.join(work, name), os.path.join(out, name))
    log(f"wrote {os.path.join(out, 'disk.qcow2')}")
